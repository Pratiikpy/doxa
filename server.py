"""The HTTP surface — service listing, x402 payment, and the paid endpoints.

Three details here are not obvious and were each learned from a rejected listing:

**The validator probes with no body.** It sends a bare request to the resource URL and expects a 402
with a challenge. A handler that validates input first and returns 400 fails review, because the
validator never gets to see a challenge at all. So payment is resolved before the body is even parsed.

**The challenge travels in the `PAYMENT-REQUIRED` header**, base64-encoded, not in the JSON body. It is
mirrored into the body for human readability, but the header is what is checked.

**Payment settles before the handler runs.** That makes every failure after settlement a failure the
customer has already paid for, so the handler must not produce a confident wrong answer — which is why
the nodes refuse to audit a challenge page rather than describing it, and why a model outage is
reported as an outage rather than as a finding.
"""
from __future__ import annotations

import hmac
import json
import os
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response

from config import get_settings
from contract import ArtifactRequest
from nodes import build_registry
from runtime import VOLATILE_RESULT_KEYS, Runtime
from x402 import (PAYMENT_HEADER, PAYMENT_REQUIRED_HEADER, PAYMENT_RESPONSE_HEADER,
                  build_challenge, malformed_payment, verify_payment)

log = logging.getLogger("doxa")

SETTINGS = get_settings()
# Set only in this host's environment. Empty means the internal door does not exist at all.
_INTERNAL_SECRET = os.environ.get("DOXA_INTERNAL_SECRET", "").strip()
REGISTRY = build_registry()
RUNTIME = Runtime(REGISTRY)

# Two sentences: what it does, and what you get back. The listing validator rejects a one-part
# description, and a customer deciding whether to spend money deserves both halves anyway.
DESCRIPTIONS: dict[str, str] = {
    "page.audit": "Runs the full technical audit of one page — title, description, canonical, "
                  "headings, indexability, language, viewport, links, images, structured data and "
                  "security headers. Returns every fault with a stable code, a plain explanation and "
                  "the evidence behind it.",
    "page.links": "Requests the page's links, up to the limit you set (150 by default), and reports "
                  "what actually came back. Returns the broken links, the ones that redirect and the "
                  "ones that could not be checked, each with its status, and says how many were left "
                  "unchecked.",
    "page.images": "Inspects every image for missing alt text, missing dimensions, broken sources and "
                   "oversized files. Returns the offending images with their sizes so the heavy ones "
                   "can be fixed first.",
    "schema.validate": "Parses the page's JSON-LD, microdata and RDFa and validates each type against "
                       "the properties Google documents as required. Returns what is declared, what is "
                       "missing, and any block that fails to parse.",
    "page.hreflang": "Checks international targeting for the failures that silently void an hreflang "
                     "set — no self-reference, no x-default, relative URLs, malformed codes. Returns "
                     "each alternate and the URLs needed to prove reciprocity.",
    "page.asai": "Fetches the page twice, once as raw HTML and once with JavaScript executed, and "
                 "diffs the readable text. Returns how much of your content exists only after "
                 "JavaScript runs, and is therefore invisible to crawlers that do not execute it.",
    "page.blocked": "Presents six AI crawlers' real user-agents by default, or the ones you name, and "
                    "compares the response with what a browser receives. Returns which crawlers your "
                    "CDN refuses or serves a smaller page to — something robots.txt cannot tell you.",
    "llms.check": "Checks whether the site publishes a valid /llms.txt and how complete it is. Returns "
                  "its structure, size and link count, or a note that it is absent.",
    "robots.check": "Parses robots.txt with the longest-match rule real crawlers use and evaluates it "
                    "for every named AI agent. Returns an allow or deny verdict per agent with the "
                    "exact rule that decided it.",
    "page.aeo": "Assesses whether a model can answer *from* this page — answer-first structure, "
                "concrete evidence, chunkability, readability, freshness and authorship. Returns each "
                "weakness with the specific change that fixes it.",
    "page.chunk": "Splits the page the way a retrieval system would, keeping each passage with the "
                  "heading it sits under. Returns the citable spans with character offsets so any "
                  "quote can be traced back to its source.",
    "page.readability": "Measures Flesch reading ease, sentence length and the filler and hedging that "
                        "displace specifics. Returns the metrics with the passages responsible.",
    "geo.score": "Scores the page and its site files from 0 to 100 for AI readiness across eight "
                 "categories. Returns the full breakdown plus a fix list ordered by points gained per "
                 "unit of effort.",
    "ai.visibility": "Asks language models the questions your buyers actually ask, several times each, "
                     "and measures how often you are recommended. Returns the mention rate, your rank "
                     "where the answer was ranked, and the sentence that mentioned you.",
    "ai.brand": "Asks models to describe your brand and reports what they say. Returns each "
                "description alongside your own site copy, up to 1,500 characters each, so you can "
                "see where they are stale, vague or wrong.",
    "ai.citations": "Finds who models recommend in your market instead of you. Returns every product "
                    "and vendor named alongside or instead of you, with how often and for which "
                    "questions.",
    "ai.prompts": "Derives the questions a buyer would ask before they know you exist, from what your "
                  "site says it does. Returns a prompt set you can use as a visibility baseline.",
    "site.audit": "Crawls the site within a page and time budget you set, then runs the eight checks "
                  "that only a whole crawl can perform — duplicate titles, descriptions and bodies, "
                  "orphan pages, click depth, redirect chains and canonical conflicts. Returns every "
                  "fault with the pages involved, and states exactly where the crawl stopped.",
    "site.graph": "Maps the internal link structure and computes a PageRank-style flow across it. "
                  "Returns which pages the structure strands, which are dead ends, and which "
                  "concentrate the most internal value.",
    "site.sitemap": "Parses the sitemap, follows an index one level, and diffs it against a real "
                    "crawl. Returns the URLs it lists that do not load or are noindex, and the "
                    "indexable pages it forgot to declare.",
    "site.aeo": "Scores AI readiness across the whole site rather than one page, fetching the "
                "site-level files once and applying them to every page. Returns the mean, the "
                "spread, and the specific pages dragging the score down.",
    "kw.discover": "Expands a seed keyword through live autocomplete — alphabetically, by question "
                   "word and by preposition — across two engines. Returns the long tail people "
                   "actually type, each phrase labelled with its intent and which engines saw it.",
    "kw.questions": "Collects the questions real people ask about a topic, from autocomplete and "
                    "from Stack Exchange and Hacker News threads. Returns each question with its "
                    "score and a URL you can open.",
    "kw.demand": "Composes a comparative demand index from four measurable signals: long-tail depth, "
                 "engine agreement, community discussion and Wikipedia pageview trend. Returns the "
                 "index with every input and weight itemised — explicitly not a search volume.",
    "kw.cluster": "Groups keywords by shared meaning and by intent, so that each cluster maps onto a "
                  "page rather than onto vocabulary. Returns labelled clusters with every input "
                  "phrase placed exactly once.",
    "corpus.presence": "Checks how much of your domain Common Crawl has actually captured, across "
                       "several recent crawls. Returns coverage per crawl and an uncapped scale "
                       "figure — the answer to whether AI systems trained on the open corpus have "
                       "ever seen your content.",
    "links.inbound": "Finds citations of your domain in the sources models demonstrably read — "
                     "Wikipedia, Hacker News and GitHub — plus your Wikidata entity if one exists. "
                     "Returns every citation as a URL you can open, with no estimated totals.",
    "links.compare": "Measures your citations and corpus presence against a named competitor using "
                     "identical sources and limits for both. Returns each side's figures and the gap, "
                     "so the comparison is fair even though neither number is a complete backlink "
                     "count.",
    "compete.compare": "Fetches your page and a named rival's at the same moment and runs both "
                       "through identical checks. Returns each side's score and faults, plus the "
                       "faults only one of you carries — the difference is real rather than an "
                       "artefact of measuring them differently.",
    "audit.diff": "Compares two signed Doxa audits of the same page taken at different times. "
                  "Returns what was fixed, what was introduced, what still stands, and the receipts "
                  "for both so a third party can verify the change rather than take your word.",
    "badge": "Measures the page's AI readiness and renders it as an embeddable SVG badge. Returns "
             "the badge and its embed snippet, coloured by the band it actually measured — it turns "
             "red when the score does.",
    "report.pdf": "Runs the full audit and typesets it as a PDF a person can hand to a client or a "
                  "board. Returns the document with every finding and its evidence, signed by the "
                  "same receipt as the response.",
    "content.audit": "Reads the page, determines the question it is trying to answer, and reports "
                     "whether it actually answers it. Returns the exact sentence that does — checked "
                     "against the page first — or the specific gaps that remain.",
    "content.brief": "Turns a topic into a page specification grounded in questions people really "
                     "ask. Returns the answer-first opening, the sections and their lengths, the "
                     "entities to name and the schema to emit — a spec, not copy.",
    "content.charts": "Extracts the figures already stated in a page and structures them as a table "
                      "and schema.org Dataset a model can quote exactly. Returns only figures that "
                      "appear verbatim in the source; anything unverifiable is discarded.",
    "seo.engagement": "Assesses the site and turns what it finds into a costed, sequenced programme "
                      "— worst problems first, each phase citing the findings that justify it and "
                      "naming the endpoints that execute it. Returns a quote built from real prices "
                      "and a real assessment, ending in a re-measurement that proves the change.",
}

# A service that ships without a description would be listed with a one-line stub, which the OKX
# listing validator rejects and which tells a prospective buyer nothing. Failing at import is the
# only way to guarantee the two never drift apart.
_undescribed = sorted({n["endpoint"] for n in REGISTRY.list()} - set(DESCRIPTIONS))
if _undescribed:
    raise RuntimeError(
        "every service must carry a two-part description before it can be served; missing: "
        + ", ".join(_undescribed))

app = FastAPI(title="Doxa", version="1.0",
              description="Technical SEO, answer-engine readiness and AI visibility, with every "
                          "answer signed.")


def _price(endpoint: str) -> str:
    node = REGISTRY.get(endpoint)
    return f"{node.price_usdt:.6f}".rstrip("0").rstrip(".") if node else "0"


def _describe(endpoint: str) -> str:
    return DESCRIPTIONS.get(endpoint, f"Doxa service {endpoint}.")


@app.get("/")
def root() -> dict:
    return {"name": "Doxa",
            "tagline": "Nothing is optimised until it is measured.",
            "services": len(REGISTRY.list()),
            "docs": "/services",
            "proof": "/proof",
            "verify": "/verify",
            "health": "/health"}


@app.get("/proof", response_class=Response)
def proof_deck() -> Response:
    """Every service, bought for real, with the answer it returned.

    Rendered from a recorded run rather than written by hand, so the page cannot claim a number that
    did not happen. Free and unauthenticated: evidence nobody can read is not evidence.
    """
    import proof
    return Response(content=proof.page(), media_type="text/html; charset=utf-8")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "services": len(REGISTRY.list()), "ts": int(time.time())}


@app.get("/services")
def services() -> dict:
    out = []
    for n in REGISTRY.list():
        out.append({**n, "description": _describe(n["endpoint"]),
                    "input": _contract(n["endpoint"]),
                    "url": f"{SETTINGS.public_base_url.rstrip('/')}/a2mcp/{n['endpoint']}"})
    return {"services": out, "count": len(out)}


@app.get("/.well-known/x402")
def well_known_x402() -> dict:
    """The route table, keyed without a method.

    The key is `"* /a2mcp/<endpoint>"`. A method-qualified key (`"POST /a2mcp/..."`) causes the OKX
    validator's probe — which does not use POST — to miss the route entirely and see no challenge.
    """
    routes: dict[str, Any] = {}
    for n in REGISTRY.list():
        routes[f"* /a2mcp/{n['endpoint']}"] = {
            "scheme": SETTINGS.x402_scheme,
            "network": SETTINGS.x402_network,
            "asset": SETTINGS.x402_asset,
            "amount": str(int(round(float(n["price_usdt"]) * 10 ** SETTINGS.x402_asset_decimals))),
            "payTo": SETTINGS.pay_to,
            "maxTimeoutSeconds": SETTINGS.x402_max_timeout_seconds,
            "extra": {"name": SETTINGS.x402_asset_name,
                      "version": SETTINGS.x402_asset_version,
                      "decimals": SETTINGS.x402_asset_decimals},
            "description": _describe(n["endpoint"]),
        }
    return {"x402Version": SETTINGS.x402_version, "routes": routes}


def headers_for(payment) -> dict:
    """The settlement receipt header, when the facilitator returned one.

    `payment` is None on the internal path, where the A2A daemon has already been paid through
    escrow and nothing settles here — so there is no receipt to echo.
    """
    if payment is None or not payment.response_header:
        return {}
    return {PAYMENT_RESPONSE_HEADER: payment.response_header}


def _contract(endpoint: str) -> dict | None:
    node = REGISTRY.get(endpoint)
    return node.input_contract() if node else None


def _challenge_response(endpoint: str) -> Response:
    header_val, challenge = build_challenge(endpoint, _price(endpoint), SETTINGS,
                                            _describe(endpoint), _contract(endpoint))
    return JSONResponse(
        status_code=402,
        content={"error": "payment required", "endpoint": endpoint,
                 "price_usdt": _price(endpoint), "challenge": challenge},
        headers={PAYMENT_REQUIRED_HEADER: header_val})


@app.api_route("/a2mcp/{endpoint:path}", methods=["GET", "POST", "PUT", "HEAD", "OPTIONS"])
async def paid_endpoint(endpoint: str, request: Request,
                        x_payment: str | None = Header(default=None),
                        payment_signature: str | None = Header(default=None),
                        x_doxa_internal: str | None = Header(default=None)) -> Response:
    """One paid service call.

    Payment is resolved before the body is read. The listing validator probes with no body at all and
    must still receive a 402 with a challenge; parsing input first would answer it with a 400 and the
    listing would be rejected for not implementing x402.
    """
    node = REGISTRY.get(endpoint)
    if node is None:
        return JSONResponse(status_code=404,
                            content={"error": f"unknown service '{endpoint}'",
                                     "services": [n["endpoint"] for n in REGISTRY.list()]})

    # The OKX agentic wallet sends `PAYMENT-SIGNATURE`; `X-PAYMENT` is the older name and is still
    # accepted. Reading only one of them means a customer paying through the official wallet is
    # handed a 402 for a payment they have already signed.
    # The A2A daemon has already been paid for this job through the marketplace's escrow, and it
    # calls the engine on localhost to produce the deliverable. Without a way in it would have to pay
    # a second time for work the customer has bought once — which is why the shared wrapper answered
    # every Doxa A2A customer as a different agent instead: it had no route to this engine at all.
    #
    # Deliberately narrow. It is a constant-time comparison against a secret that exists only in this
    # host's environment, never a header a client can simply assert, and never an origin or
    # Sec-Fetch-Site check — any HTTP client can forge those, which would hand the OKX validator a
    # paid result for free and fail x402 review. Unset secret means the door does not exist.
    internal = (x_doxa_internal or "").strip()
    internal_ok = bool(_INTERNAL_SECRET) and len(internal) == len(_INTERNAL_SECRET) and \
        hmac.compare_digest(internal, _INTERNAL_SECRET)

    authorization = payment_signature or x_payment
    if not authorization and not internal_ok:
        return _challenge_response(endpoint)

    # An unreadable header is answered before anything else. A caller with both a broken header and
    # an empty body has two problems, and only the header error explains why paying again will not
    # help — so it must not be hidden behind the input check that follows.
    broken = None if internal_ok else malformed_payment(authorization)
    if broken is not None:
        return JSONResponse(
            status_code=400,
            content={"error": broken.detail, "code": broken.code, "field": broken.field,
                     "endpoint": endpoint,
                     "expected": "base64 of the x402 v2 payment payload from your wallet; "
                                 "call this endpoint with no payment header to get a challenge "
                                 "to sign."})

    # The input contract is checked *before* settlement. This block used to run after, which is how
    # the first real agent-to-agent purchase of this service paid and received INVALID_INPUT: the
    # money moved, then the server explained what should have been sent. Answering "you forgot the
    # url" while keeping the fee is charging for nothing.
    #
    # The 200 and the body are unchanged from the reviewed behaviour on purpose — the only difference
    # a client can observe is that they are no longer billed for it.
    try:
        _early = await request.json()
    except Exception:  # noqa: BLE001
        _early = {}
    if not isinstance(_early, dict):
        _early = {}
    _early_input = _early.get("input") if isinstance(_early.get("input"), dict) else _early
    _node = REGISTRY.get(endpoint)
    if _node is not None:
        _missing = [f for f in (_node.requires or ()) if not _early_input.get(f)]
        if _missing:
            contract = _node.input_contract()
            unbilled = ("Nothing was billed for this call — your authorization was not settled. "
                        "Send the example above with payment to get the real result.")
            if not _early_input:
                return JSONResponse(status_code=200, content={
                    "endpoint": endpoint,
                    "status": "input_required",
                    "what_this_does": _describe(endpoint),
                    "required": contract["required"],
                    "optional": contract["optional"],
                    "example_request": {"input": contract["example"]},
                    "not_charged": unbilled})
            return JSONResponse(status_code=422, content={
                "error": f"missing required field(s): {', '.join(_missing)}",
                "code": "missing_required_field", "endpoint": endpoint,
                "required": contract["required"],
                "example_request": {"input": contract["example"]},
                "not_charged": unbilled})

    payment = None if internal_ok else verify_payment(
        authorization, SETTINGS, endpoint=endpoint, fee_usdt=_price(endpoint))
    if payment is not None and not payment.ok:
        if payment.malformed:
            # The header could not be decoded at all. That is the caller's bug, not a payment
            # problem — paying again would change nothing — so it gets a typed 400 naming the field
            # rather than a challenge that invites a pointless retry.
            return JSONResponse(
                status_code=400,
                content={"error": payment.detail, "code": payment.code, "field": payment.field,
                         "endpoint": endpoint,
                         "expected": "base64 of the x402 v2 payment payload from your wallet; "
                                     "call this endpoint with no payment header to get a challenge "
                                     "to sign."})
        # A well-formed authorization we declined gets a fresh challenge, because the caller's next
        # step really is to pay again and it needs a live nonce to do it.
        #
        # The challenge is rebuilt rather than reusing the earlier response's headers. Copying those
        # carried their `content-length` onto a longer body, so uvicorn wrote the declared number of
        # bytes and killed the socket — the caller saw "empty reply from server" instead of a reason,
        # on the one path where a reason matters most.
        header_val, challenge = build_challenge(endpoint, _price(endpoint), SETTINGS,
                                                _describe(endpoint), _contract(endpoint))
        return JSONResponse(
            status_code=402,
            content={"error": "payment required", "code": payment.code, "endpoint": endpoint,
                     "price_usdt": _price(endpoint), "payment_error": payment.detail,
                     "challenge": challenge},
            headers={PAYMENT_REQUIRED_HEADER: header_val})

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    node_input = payload.get("input") if isinstance(payload.get("input"), dict) else payload
    node = REGISTRY.get(endpoint)

    # The input contract was checked before settlement (above), so by here the request is servable.
    req = ArtifactRequest(
        endpoint=endpoint,
        input=node_input,
        options=payload.get("options") if isinstance(payload.get("options"), dict) else {},
        idempotency_key=payload.get("idempotency_key"),
    )
    envelope = RUNTIME.execute(req)
    return JSONResponse(status_code=200, headers=headers_for(payment),
                        content=json.loads(envelope.model_dump_json()))


@app.get("/artifact/{digest}")
def artifact(digest: str) -> Response:
    """Serve a stored artifact by its content hash."""
    if not digest.isalnum() or len(digest) != 64:
        return JSONResponse(status_code=400, content={"error": "malformed digest"})
    path = Path(SETTINGS.artifact_dir) / digest
    if not path.exists():
        return JSONResponse(status_code=404, content={"error": "artifact not found or expired"})
    meta_path = path.with_suffix(".meta")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    return Response(content=path.read_bytes(),
                    media_type=meta.get("mime_type", "application/octet-stream"),
                    headers={"Content-Disposition":
                             f'attachment; filename="{meta.get("name", digest)}"'})


@app.get("/verify")
def verify_instructions() -> dict:
    """Everything needed to check a Doxa receipt without trusting Doxa.

    These instructions have to be exactly right or the guarantee is worthless. The signature is over
    the ASCII of `receipt.manifest_sha256` — *not* over the receipt body — and the manifest is a
    canonical JSON object of the six fields below. An earlier version of this endpoint said "canonical
    JSON of the receipt body", which would lead an honest verifier to hash the wrong bytes and
    conclude a valid receipt was forged.
    """
    return {
        "public_key_ed25519": RUNTIME.signer.public_hex,
        "algorithm": "Ed25519",
        "signed_bytes": "the UTF-8 bytes of the receipt's manifest_sha256 string, verbatim",
        "manifest": {
            "how": "sha256 of canonical JSON — keys sorted, separators (',',':'), no whitespace, "
                   "ensure_ascii false. The digest carries a 'sha256:' prefix.",
            "fields": ["endpoint", "input_hashes", "output_hash", "tool", "level", "job_id"],
            "note": "All six are echoed in the envelope, so the manifest can be rebuilt from the "
                    "response alone and compared against receipt.manifest_sha256.",
        },
        "output_hash": {
            "how": "sha256 of the canonical JSON of `result`, with the per-run timing keys below "
                   "removed at every depth first.",
            "excludes": sorted(VOLATILE_RESULT_KEYS),
            "why": "Those fields measure how long this particular fetch took, not what was found, "
                   "and they differ on every run. Hashing them meant two identical audits of the "
                   "same page never produced the same digest — so the digest could not be used to "
                   "compare runs, and the runtime's own reproduction check could never pass. They "
                   "are still in the response; they are simply not what the hash attests to.",
            "note": "Remove these keys from `result`, canonicalise, sha256, and you must get "
                    "`output_hash` exactly. The digest stays verifiable offline from the response "
                    "alone.",
        },
        "steps": [
            "Rebuild the manifest from the envelope's endpoint, input_hashes, output_hash, tool, "
            "validation.level and job_id.",
            "Canonicalise it and take its sha256; it must equal receipt.manifest_sha256.",
            "Verify receipt.signature over the UTF-8 bytes of that string with the public key "
            "above, which must also equal receipt.public_key.",
        ],
        "python": (
            "import hashlib, json\n"
            "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey\n"
            "m = {'endpoint': env['endpoint'], 'input_hashes': env['input_hashes'],\n"
            "     'output_hash': env['output_hash'], 'tool': env['tool'],\n"
            "     'level': env['validation']['level'], 'job_id': env['job_id']}\n"
            "blob = json.dumps(m, sort_keys=True, separators=(',', ':'), ensure_ascii=False)\n"
            "digest = 'sha256:' + hashlib.sha256(blob.encode('utf-8')).hexdigest()\n"
            "assert digest == env['receipt']['manifest_sha256']\n"
            "Ed25519PublicKey.from_public_bytes(bytes.fromhex(env['receipt']['public_key'])).verify(\n"
            "    bytes.fromhex(env['receipt']['signature']), digest.encode('utf-8'))"
        ),
        "key_source": RUNTIME.signer.key_source,
    }


@app.get("/verify/{receipt_id}")
def verify_receipt(receipt_id: str) -> dict:
    return {"receipt_id": receipt_id, **verify_instructions()}
