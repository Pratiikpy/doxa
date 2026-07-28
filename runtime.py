"""Doxa Verification Runtime — the engine behind every node.

Produces the Universal Artifact Contract: runs a node, validates it, attempts
reproduction (L3) and independent/differential verification (L4), builds a
Replay Capsule, and signs a tamper-evident receipt (Ed25519).
"""
from __future__ import annotations

import base64
import hashlib
import json
import platform
import logging
import re
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from config import Settings, get_settings
from contract import (
    ArtifactEnvelope,
    ArtifactError,
    ArtifactRef,
    ArtifactRequest,
    ErrorCode,
    Lineage,
    Metrics,
    Provenance,
    Receipt,
    ReplayCapsule,
    ToolInfo,
    Validation,
    ValidationCheck,
    VerificationLevel,
)

# ----------------------------------------------------------------------------- hashing
def canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def _warn_unread_input(ctx, node, supplied) -> None:
    """Say so when a field in the request is not one this node reads.

    An unrecognised field is dropped and the node's declared default used instead, silently. Measured
    on a sibling ASP, where a caller asking for two venues via a misspelled key received five computed
    from defaults, and on another where a 120-character cap became 8,079 characters. Nothing in the
    response said the field had been discarded, so the caller had no way to tell they had been
    answered a different question from the one they asked and paid for.

    A warning rather than a refusal: rejecting an unexpected field would break any client sending one
    today, which is too high a price for catching a typo. Warnings travel in the envelope and are
    covered by the signature, so the notice cannot be separated from the answer it qualifies.

    Nodes that declare no fields are left alone — inventing an accepted set we cannot derive would
    flag correct calls, and a checker that cries wolf gets ignored.
    """
    import difflib

    accepted = set(getattr(node, "requires", ()) or ()) | set(getattr(node, "optional", ()) or ())
    if not accepted or not isinstance(supplied, dict):
        return
    unknown = sorted(k for k in supplied if k not in accepted)
    if not unknown:
        return
    hints = []
    for key in unknown:
        near = difflib.get_close_matches(key, sorted(accepted), n=1, cutoff=0.7)
        hints.append(f"{key!r}" + (f" (did you mean {near[0]!r}?)" if near else ""))
    ctx.warn(
        "Ignored, because this service does not read "
        + ("them: " if len(unknown) > 1 else "it: ") + ", ".join(hints)
        + ". The default was used instead, so this answer may not be the one you intended. "
          "Accepted fields: " + ", ".join(sorted(accepted)) + ".")


def sha256_hex(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    elif not isinstance(data, (bytes, bytearray)):
        data = canonical_json(data)
    return "sha256:" + hashlib.sha256(data).hexdigest()


# Measurements OF the measurement. They describe how long this particular fetch took, not what was
# found, and they differ on every run by construction.
#
# `output_hash` covered them, and the consequence was not cosmetic. The runtime re-runs any node
# declaring `deterministic = True` and promotes it to L3_REPRODUCED when the two digests match — so
# every page node did a second full fetch and audit of the customer's site on every single call, to
# compare a hash that could never agree, and was capped at L2 forever.
#
# Measured live before the change: page.audit, page.readability, page.chunk and schema.validate all
# carried `ttfb_ms` in their result and all returned L2_VALIDATED; robots.check carries no timing and
# returned L3_REPRODUCED. Perfect correlation across the set, which is what turned a suspicion into
# a cause.
#
# So the digest covers the content and not the stopwatch. The fields stay in the response — a buyer
# who wants to know the page was slow should still be told — they are simply not part of what the
# hash attests to. `output_hash_excludes` is published in the envelope so the digest remains
# something the buyer can recompute for themselves offline, which is the entire point of it.
VOLATILE_RESULT_KEYS = frozenset({"ttfb_ms", "total_ms", "duration_ms", "elapsed_ms",
                                  "elapsed_seconds", "fetched_at", "measured_at"})


def stable_view(value):
    """`value` with per-run timing removed, at any depth — what the output digest attests to."""
    if isinstance(value, dict):
        return {k: stable_view(v) for k, v in value.items() if k not in VOLATILE_RESULT_KEYS}
    if isinstance(value, list):
        return [stable_view(v) for v in value]
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------------- signing
class Signer:
    """Ed25519 signer for Doxa's receipts.

    Key precedence: an explicitly supplied hex key, then the key file, then a freshly generated one.

    The order matters. Generating a key into the container filesystem — which was the only behaviour
    — means a restart, a redeploy or a second replica each mint a DIFFERENT identity. Receipts issued
    before the restart then verify against a key the service no longer holds, and two replicas sign
    the same work under two different identities. For a service whose entire proposition is a
    tamper-evident receipt, an identity that changes on every deploy is not a stable anchor, so the
    key is supplied as a secret in production and `key_source` reports which path was taken.
    """

    def __init__(self, key_path: str, key_hex: str | None = None):
        if key_hex:
            self._sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(key_hex.strip()))
            self.key_source = "configured_secret"
        else:
            p = Path(key_path)
            if p.exists():
                self._sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(p.read_text().strip()))
                self.key_source = "key_file"
            else:
                self._sk = Ed25519PrivateKey.generate()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(self._sk.private_bytes_raw().hex())
                self.key_source = "generated_ephemeral"
        self._pk = self._sk.public_key()
        self.public_hex = self._pk.public_bytes_raw().hex()

    def sign(self, manifest_sha256: str) -> Receipt:
        sig = self._sk.sign(manifest_sha256.encode("utf-8")).hex()
        return Receipt(
            manifest_sha256=manifest_sha256,
            algo="ed25519",
            signature=sig,
            public_key=self.public_hex,
            signed_at=_now_iso(),
        )

    @staticmethod
    def verify(manifest_sha256: str, signature_hex: str, public_key_hex: str) -> bool:
        try:
            pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
            pk.verify(bytes.fromhex(signature_hex), manifest_sha256.encode("utf-8"))
            return True
        except Exception:
            return False


# ----------------------------------------------------------------------------- nodes
# Object reprs leak into requests/urllib3 exception text: "<urllib3.connection.HTTPSConnection object
# at 0x000001A2...>". A customer must never be shown that.
_OBJECT_REPR = re.compile(r"<[\w.]+ object at 0x[0-9a-fA-F]+>")
_NESTED = re.compile(r"\s*\(Caused by .*?\)\s*$", re.S)

# Plain sentences for the failures that actually happen, keyed by exception class name.
_FRIENDLY = {
    "ConnectTimeout": "an upstream service did not accept a connection in time",
    "ReadTimeout": "an upstream service accepted the connection but did not answer in time",
    "ConnectionError": "an upstream service could not be reached",
    "SSLError": "an upstream service presented a TLS certificate that could not be verified",
    "TooManyRedirects": "the target URL redirected too many times",
    "JSONDecodeError": "an upstream service returned something that was not valid JSON",
    "SourceUnavailable": "a data source was unavailable",
    "LLMUnavailable": "no language model could be reached",
}


def humanise_error(e: Exception, limit: int = 300) -> str:
    """Turn an exception into something a customer can read, without inventing what went wrong.

    The class name is kept, because it is the one part that helps diagnosis, and a plain description
    is added where the failure is one we recognise. What is removed is only noise: object addresses,
    nested `Caused by` chains and pool internals.
    """
    name = type(e).__name__
    text = _NESTED.sub("", str(e) or "")
    text = _OBJECT_REPR.sub("a connection", text)
    text = " ".join(text.split())
    friendly = _FRIENDLY.get(name)
    if friendly:
        host = ""
        m = re.search(r"host='([^']+)'", text) or re.search(r"https?://([^/\s]+)", text)
        if m:
            host = f" ({m.group(1)})"
        return f"{friendly}{host}. [{name}]"
    if not text:
        return f"the service failed unexpectedly. [{name}]"
    return (text[:limit] + "…" if len(text) > limit else text) + f" [{name}]"


log = logging.getLogger("doxa.runtime")


class EngineUnavailable(RuntimeError):
    """Raised when a required engine/library is not installed."""


class NodeError(RuntimeError):
    def __init__(self, code: ErrorCode, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class NodeContext:
    def __init__(self, request: ArtifactRequest, settings: Settings, job_id: str):
        self.request = request
        self.settings = settings
        self.job_id = job_id
        self.input = request.input
        self.options = request.options
        self.warnings: list[str] = []
        self.artifacts: list[ArtifactRef] = []

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    # Inline anything up to this size directly in the JSON response. Above it, the caller uses
    # download_url — base64 costs 4/3 the bytes and a multi-megabyte PDF would bloat the receipt
    # (which is hashed and signed) far past anything an agent wants to hold in a context window.
    INLINE_LIMIT_BYTES = 256 * 1024

    def add_artifact(self, name: str, data: bytes, mime_type: str) -> ArtifactRef:
        settings = self.settings
        adir = Path(settings.artifact_dir)
        adir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(data).hexdigest()
        fpath = adir / digest  # content-addressed
        if not fpath.exists():
            fpath.write_bytes(data)
            # The name/mime are not recoverable from a content-addressed blob, so record them
            # beside it; the download route needs them to serve a correct Content-Type.
            (adir / f"{digest}.meta").write_text(
                json.dumps({"name": name, "mime_type": mime_type}), encoding="utf-8"
            )
        inline = len(data) <= self.INLINE_LIMIT_BYTES
        ref = ArtifactRef(
            name=name,
            mime_type=mime_type,
            bytes=len(data),
            sha256="sha256:" + digest,
            uri=f"artifact://{digest}",
            expires_at=(datetime.now(timezone.utc) + timedelta(seconds=settings.artifact_ttl_seconds)).isoformat(),
            download_url=f"{settings.public_base_url.rstrip('/')}/artifact/{digest}",
            content_b64=base64.b64encode(data).decode("ascii") if inline else None,
            content_inlined=inline,
        )
        self.artifacts.append(ref)
        return ref


# Real names for the libraries a node actually depends on, so a Replay Capsule can report the version
# that RAN instead of a placeholder. Keyed by the node's declared `engine`.
_ENGINE_DISTRIBUTIONS = {
    "pillow": "pillow", "pil": "pillow", "doxa-image": "pillow",
    "pypdf": "pypdf", "doxa-pdf": "pypdf",
    "dnspython": "dnspython", "doxa-dns": "dnspython",
    "beautifulsoup4": "beautifulsoup4", "bs4": "beautifulsoup4", "doxa-html": "beautifulsoup4",
    "openai": "openai",
}

_VERSION_CACHE: dict[str, str] = {}


def _dist_version(name: str) -> str | None:
    """Installed version of a distribution, or None. Cached — this runs on every artifact."""
    if name in _VERSION_CACHE:
        return _VERSION_CACHE[name] or None
    try:
        from importlib.metadata import version as _v
        got = _v(name)
    except Exception:  # noqa: BLE001
        got = ""
    _VERSION_CACHE[name] = got
    return got or None


def _environment() -> str:
    """The actual interpreter, not a hardcoded guess.

    `environment` said "python-3.11" regardless of what was running. A replay capsule whose stated
    environment is a constant cannot be used to reproduce anything.
    """
    return f"python-{platform.python_version()}"


def _tool_versions(node) -> dict:
    """What actually produced this artifact.

    This used to be `{node.engine: node.engine_version}`, and `engine_version` defaults to "1.0" on the
    base class. Nodes that never overrode it therefore attested a version of "1.0" — and for nodes whose
    engine names a real third-party library that became `{"pillow": "1.0"}`, a version of Pillow that
    has never existed. The version is inside the signed manifest, so this was a signed false statement
    about how the result was produced, which is precisely what the receipt is supposed to rule out.

    Now: third-party engines report their INSTALLED version; our own engines report the interpreter
    they ran on, since there is no separate package to version.
    """
    out: dict[str, str] = {"python": platform.python_version()}
    dist = _ENGINE_DISTRIBUTIONS.get((node.engine or "").lower())
    if dist:
        v = _dist_version(dist)
        out[dist] = v or "unknown"
    else:
        # An in-house engine. Report the declared version only when a node actually set one, so the
        # base-class placeholder never reaches a receipt.
        declared = getattr(node, "engine_version", None)
        if declared and declared != Node.engine_version:
            out[node.engine] = str(declared)
        else:
            out[node.engine] = f"doxa@python-{platform.python_version()}"
    return out


class Node(ABC):
    name: str = "abstract"
    price_usdt: float = 0.0
    deterministic: bool = False
    asp_type: str = "A2MCP"  # or A2A for heavy/bespoke
    engine: str = "python-stdlib"
    engine_version: str = "1.0"

    # What this endpoint needs, declared rather than discovered by trial and error.
    #
    # A buying agent reads the 402 challenge to work out how to call the service. If the challenge
    # only states a price, the agent has no way to know an input is required — it sends an empty body,
    # pays, and receives an error. That happened on the first real A2A purchase of this service: the
    # client's pre-check "reported no required inputs", so no url was sent and 0.005 USDT bought an
    # INVALID_INPUT. Declaring the contract here puts it in the challenge, in `/services`, and in the
    # reply to an empty paid call.
    requires: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    example_input: dict | None = None

    def input_contract(self) -> dict:
        return {"required": list(self.requires),
                "optional": list(self.optional),
                "example": self.example_input or {}}

    def engine_available(self) -> bool:  # override for optional deps
        return True

    @abstractmethod
    def run(self, ctx: NodeContext) -> dict: ...

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        """Deterministic quality checks. Override per node. Default: non-empty result."""
        return [ValidationCheck(name="non_empty_result", passed=bool(result))]

    def tool_info(self) -> ToolInfo:
        return ToolInfo(name=self.engine, version=self.engine_version)


class NodeRegistry:
    def __init__(self):
        self._nodes: dict[str, Node] = {}
        self._verifiers: dict[str, str] = {}  # node_name -> independent verifier node_name

    def register(self, node: Node, verifier: str | None = None) -> None:
        self._nodes[node.name] = node
        if verifier:
            self._verifiers[node.name] = verifier

    def get(self, name: str) -> Node | None:
        return self._nodes.get(name)

    def verifier_for(self, name: str) -> Node | None:
        vn = self._verifiers.get(name)
        return self._nodes.get(vn) if vn else None

    def list(self) -> list[dict]:
        return [
            {"endpoint": n.name, "price_usdt": n.price_usdt, "asp_type": n.asp_type,
             "engine": n.engine, "deterministic": n.deterministic}
            for n in self._nodes.values()
        ]


# ----------------------------------------------------------------------------- runtime
class Runtime:
    def __init__(self, registry: NodeRegistry, settings: Settings | None = None):
        self.registry = registry
        self.settings = settings or get_settings()
        self.signer = Signer(self.settings.signing_key_path,
                             getattr(self.settings, "_signing_key_hex", None))

    def execute(self, request: ArtifactRequest) -> ArtifactEnvelope:
        started = time.perf_counter()
        job_id = "job_" + uuid.uuid4().hex[:16]
        request_id = "req_" + uuid.uuid4().hex[:16]
        node = self.registry.get(request.endpoint)

        if node is None:
            return self._fail(request_id, job_id, request.endpoint,
                              ErrorCode.INVALID_INPUT, f"unknown endpoint '{request.endpoint}'", started)

        ctx = NodeContext(request, self.settings, job_id)
        _warn_unread_input(ctx, node, request.input)
        input_hashes = [sha256_hex(request.input)]
        params_hash = sha256_hex(request.options)

        # engine availability
        try:
            if not node.engine_available():
                return self._fail(request_id, job_id, node.name,
                                  ErrorCode.ENGINE_UNAVAILABLE,
                                  f"engine '{node.engine}' not installed", started,
                                  input_hashes=input_hashes)
        except Exception as e:  # noqa
            return self._fail(request_id, job_id, node.name, ErrorCode.ENGINE_UNAVAILABLE,
                              str(e), started, input_hashes=input_hashes)

        # run node (L1)
        try:
            result = node.run(ctx)
        except NodeError as e:
            return self._fail(request_id, job_id, node.name, e.code, e.message, started,
                              input_hashes=input_hashes, warnings=ctx.warnings)
        except EngineUnavailable as e:
            return self._fail(request_id, job_id, node.name, ErrorCode.ENGINE_UNAVAILABLE,
                              str(e), started, input_hashes=input_hashes, warnings=ctx.warnings)
        except Exception as e:  # noqa
            # The raw text of a requests/urllib3 exception is a nested repr with object addresses in
            # it. Handing that to a paying customer looks like a crash report, so it is translated
            # into something a person can read and act on. The full detail is still logged.
            log.exception("unhandled error in %s", node.name)
            return self._fail(request_id, job_id, node.name, ErrorCode.ENGINE_FAILED,
                              humanise_error(e), started,
                              input_hashes=input_hashes, warnings=ctx.warnings)

        output_hash = sha256_hex(stable_view(result))

        # L2: validation
        checks = node.validate(result, ctx)
        level = VerificationLevel.L1_PRODUCED
        status = "validated"
        if checks and all(c.passed for c in checks):
            level = VerificationLevel.L2_VALIDATED
        elif checks and not all(c.passed for c in checks):
            status = "quality_failed"

        # L3: reproduction for deterministic nodes
        if level == VerificationLevel.L2_VALIDATED and node.deterministic:
            try:
                ctx2 = NodeContext(request, self.settings, job_id)
                result2 = node.run(ctx2)
                if sha256_hex(stable_view(result2)) == output_hash:
                    level = VerificationLevel.L3_REPRODUCED
            except Exception:  # noqa
                ctx.warn("reproduction attempt failed; staying at L2")

        # L4: independent/differential verification
        differential_agreement = None
        independent_verifier = None
        verifier = self.registry.verifier_for(node.name)
        if level == VerificationLevel.L3_REPRODUCED and verifier is not None:
            try:
                if verifier.engine_available():
                    vctx = NodeContext(request, self.settings, job_id)
                    vresult = verifier.run(vctx)
                    agree, detail = _agree(result, vresult)
                    differential_agreement = agree
                    independent_verifier = verifier.name
                    checks.append(ValidationCheck(name="differential_verification",
                                                  passed=agree, detail=detail))
                    if agree:
                        level = VerificationLevel.L4_INDEPENDENT
                    else:
                        ctx.warn(f"independent verifier {verifier.name} disagreed: {detail}")
            except Exception as e:  # noqa
                ctx.warn(f"independent verification errored: {e}")

        validation = Validation(
            level=level,
            status=status,
            tests=checks,
            warnings=ctx.warnings,
            independent_verifier=independent_verifier,
            differential_agreement=differential_agreement,
        )

        replay = ReplayCapsule(
            environment=_environment(),
            command=f"doxa.run('{node.name}')",
            tool_versions=_tool_versions(node),
            parameters_sha256=params_hash,
            input_refs=input_hashes,
        )

        # provenance: in-toto style attestation (real, signed below). c2pa/sigstore/ots are hooks.
        attestation = {
            "_type": "https://in-toto.io/Statement/v1",
            "predicateType": "https://doxa.dev/artifact/v1",
            "subject": [{"name": node.name, "digest": {"sha256": output_hash.split(":")[-1]}}],
            "predicate": {"inputs": input_hashes, "level": level.value, "run": job_id},
        }
        provenance = Provenance(in_toto_attestation=attestation)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        metrics = Metrics(
            duration_ms=elapsed_ms,
            input_bytes=len(canonical_json(request.input)),
            output_bytes=len(canonical_json(result)),
            cost_usdt=node.price_usdt,
        )

        env = ArtifactEnvelope(
            ok=True,
            status="completed",
            request_id=request_id,
            job_id=job_id,
            endpoint=node.name,
            result=result,
            artifacts=ctx.artifacts,
            input_hashes=input_hashes,
            output_hash=output_hash,
            tool=node.tool_info(),
            parameters=request.options,
            validation=validation,
            lineage=Lineage(workflow_run=job_id),
            provenance=provenance,
            replay=replay,
            metrics=metrics,
            warnings=ctx.warnings,
        )
        env.receipt = self._sign(env)
        return env

    # ------------------------------------------------------------------ helpers
    def _manifest(self, env: ArtifactEnvelope) -> str:
        manifest = {
            "endpoint": env.endpoint,
            "input_hashes": env.input_hashes,
            "output_hash": env.output_hash,
            "tool": env.tool.model_dump() if env.tool else None,
            "level": env.validation.level.value,
            "job_id": env.job_id,
        }
        return sha256_hex(manifest)

    def _sign(self, env: ArtifactEnvelope) -> Receipt:
        return self.signer.sign(self._manifest(env))

    def _fail(self, request_id, job_id, endpoint, code, message, started,
              input_hashes=None, warnings=None) -> ArtifactEnvelope:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        env = ArtifactEnvelope(
            ok=False,
            status="failed",
            request_id=request_id,
            job_id=job_id,
            endpoint=endpoint,
            input_hashes=input_hashes or [],
            validation=Validation(level=VerificationLevel.L1_PRODUCED, status="unverified"),
            metrics=Metrics(duration_ms=elapsed_ms),
            warnings=warnings or [],
            error=ArtifactError(code=code, message=message),
        )
        env.receipt = self._sign(env)
        return env

    def verify_receipt(self, env: ArtifactEnvelope) -> bool:
        if env.receipt is None:
            return False
        manifest = self._manifest(env)
        if manifest != env.receipt.manifest_sha256:
            return False
        return Signer.verify(manifest, env.receipt.signature, env.receipt.public_key)


def _agree(a: dict, b: dict) -> tuple[bool, str]:
    """Differential agreement: compare on each node's declared 'verify_key' if present,
    else exact-equality of the two result dicts."""
    ka = a.get("verify_key")
    if ka is not None and "verify_key" in b:
        agree = a["verify_key"] == b["verify_key"]
        return agree, f"verify_key match={agree}"
    agree = sha256_hex(a) == sha256_hex(b)
    return agree, f"exact match={agree}"
