"""The published verification instructions must actually verify a real receipt.

"Verifiable offline" is the whole proposition. If the instructions on /verify describe the wrong bytes,
an honest third party follows them, fails, and concludes a valid receipt was forged — which is worse
than publishing nothing. So this test follows the published steps literally, with no help from the
internals, and checks a real envelope.
"""
from __future__ import annotations

import hashlib
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi.testclient import TestClient

import server
from contract import ArtifactRequest

client = TestClient(server.app)


def a_real_envelope() -> dict:
    env = server.RUNTIME.execute(
        ArtifactRequest(endpoint="audit.diff", input={
            "before": {"result": {"findings": [
                {"code": "title.empty", "severity": "critical", "message": "x", "detail": {}}]}},
            "after": {"result": {"findings": []}}}))
    return json.loads(env.model_dump_json())


def test_following_the_published_steps_verifies_a_real_receipt():
    published = client.get("/verify").json()
    env = a_real_envelope()
    receipt = env["receipt"]

    # Step 1 and 2: rebuild the manifest from exactly the fields /verify names.
    fields = published["manifest"]["fields"]
    assert fields == ["endpoint", "input_hashes", "output_hash", "tool", "level", "job_id"]
    manifest = {"endpoint": env["endpoint"], "input_hashes": env["input_hashes"],
                "output_hash": env["output_hash"], "tool": env["tool"],
                "level": env["validation"]["level"], "job_id": env["job_id"]}
    blob = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
    assert digest == receipt["manifest_sha256"], "the manifest recipe does not reproduce the digest"

    # Step 3: the signature is over those bytes, with the key /verify publishes.
    assert receipt["public_key"] == published["public_key_ed25519"]
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(receipt["public_key"])).verify(
        bytes.fromhex(receipt["signature"]), digest.encode("utf-8"))


def test_the_published_python_snippet_runs_and_passes():
    """The snippet is copy-paste guidance. If it does not execute, it is decoration."""
    published = client.get("/verify").json()
    env = a_real_envelope()
    exec(published["python"], {"env": env})     # noqa: S102 - executing our own published snippet


def test_the_instructions_do_not_claim_the_receipt_body_is_signed():
    published = client.get("/verify").json()
    assert "manifest_sha256" in published["signed_bytes"]
    assert "receipt body" not in json.dumps(published)


def test_a_tampered_output_hash_fails_verification():
    """Verification has to actually detect tampering, or it is theatre."""
    env = a_real_envelope()
    env["output_hash"] = "sha256:" + "0" * 64
    manifest = {"endpoint": env["endpoint"], "input_hashes": env["input_hashes"],
                "output_hash": env["output_hash"], "tool": env["tool"],
                "level": env["validation"]["level"], "job_id": env["job_id"]}
    blob = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
    assert digest != env["receipt"]["manifest_sha256"]
