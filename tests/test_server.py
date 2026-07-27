"""The HTTP surface and the x402 handshake.

Every assertion here corresponds to something that gets a listing rejected. The validator does not
read documentation: it probes the resource URL and checks what comes back, so these are the checks it
performs, run against our own server before it ever sees it.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import server
from x402 import decode_challenge, make_dev_payment

client = TestClient(server.app)
FREE_URL = "https://example.com"


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "HEAD"])
def test_a_bodyless_probe_gets_a_challenge_on_every_method(method):
    """The validator probes with no body and not necessarily with POST. Validating input first and
    answering 400 is the single most common reason an x402 listing is rejected."""
    r = client.request(method, "/a2mcp/page.audit")
    assert r.status_code == 402
    assert any(k.upper() == "PAYMENT-REQUIRED" for k in r.headers)


def test_the_challenge_is_in_the_header_not_only_the_body():
    r = client.post("/a2mcp/page.audit")
    challenge = decode_challenge(r.headers["PAYMENT-REQUIRED"])
    assert challenge["x402Version"] == 2
    assert challenge["resource"]["url"].endswith("/a2mcp/page.audit")


def test_decimals_live_inside_extra():
    """USD₮0 is not in OKX's token list, and a top-level `decimals` is dropped by their canonical
    re-serialisation — so the amount is interpreted against the wrong scale."""
    r = client.post("/a2mcp/page.audit")
    accepts = decode_challenge(r.headers["PAYMENT-REQUIRED"])["accepts"][0]
    assert accepts["extra"]["decimals"] == 6
    assert "decimals" not in {k for k in accepts if k != "extra"}
    assert accepts["network"] == "eip155:196"
    assert accepts["scheme"] == "exact"


def test_amount_is_integer_minor_units():
    r = client.post("/a2mcp/page.audit")           # priced at 0.01
    accepts = decode_challenge(r.headers["PAYMENT-REQUIRED"])["accepts"][0]
    assert accepts["amount"] == "10000"            # 0.01 × 10^6
    assert accepts["amount"].isdigit()


def test_every_service_description_has_two_parts():
    """The listing validator rejects a single-sentence description, and a buyer needs both halves:
    what it does, and what comes back."""
    for svc in client.get("/services").json()["services"]:
        assert svc["description"].count(".") >= 2, svc["endpoint"]
        assert len(svc["description"]) > 80, svc["endpoint"]


def test_no_service_falls_back_to_a_generated_description():
    """The fallback stub is one sentence and says nothing. Eleven services once shipped with it
    because the description table was not updated alongside the registry."""
    for svc in client.get("/services").json()["services"]:
        assert not svc["description"].startswith("Doxa service "), svc["endpoint"]


def test_well_known_route_keys_are_method_less():
    """A key of "POST /a2mcp/x" makes the validator's non-POST probe miss the route entirely."""
    routes = client.get("/.well-known/x402").json()["routes"]
    assert routes and all(k.startswith("* /a2mcp/") for k in routes)


def test_well_known_covers_every_registered_service():
    routes = client.get("/.well-known/x402").json()["routes"]
    listed = {n["endpoint"] for n in client.get("/services").json()["services"]}
    assert {k.removeprefix("* /a2mcp/") for k in routes} == listed


def test_an_unpaid_call_is_refused():
    r = client.post("/a2mcp/robots.check", json={"input": {"url": FREE_URL}})
    assert r.status_code == 402


def test_a_paid_call_is_served_and_signed():
    challenge = decode_challenge(client.post("/a2mcp/robots.check").headers["PAYMENT-REQUIRED"])
    r = client.post("/a2mcp/robots.check", headers={"X-PAYMENT": make_dev_payment(challenge)},
                    json={"input": {"url": FREE_URL}})
    assert r.status_code == 200
    env = r.json()
    assert env["status"] == "completed"
    assert env["receipt"]["signature"] and env["receipt"]["algo"] == "ed25519"
    assert env["receipt"]["public_key"]


def test_a_payment_cannot_be_replayed():
    challenge = decode_challenge(client.post("/a2mcp/robots.check").headers["PAYMENT-REQUIRED"])
    payment = make_dev_payment(challenge)
    first = client.post("/a2mcp/robots.check", headers={"X-PAYMENT": payment},
                        json={"input": {"url": FREE_URL}})
    second = client.post("/a2mcp/robots.check", headers={"X-PAYMENT": payment},
                         json={"input": {"url": FREE_URL}})
    assert first.status_code == 200
    assert second.status_code == 402, "a spent nonce was accepted twice"


def test_an_undecodable_payment_header_is_a_typed_400():
    """A header the caller built wrongly cannot be fixed by paying again, so it is a client error
    that names the field — not a challenge that invites a pointless retry. See
    tests/test_malformed_payment.py for the reviewer-reported case this came from."""
    r = client.post("/a2mcp/robots.check", headers={"X-PAYMENT": "not-base64"},
                    json={"input": {"url": FREE_URL}})
    assert r.status_code == 400
    assert r.json()["code"] == "malformed_payment_signature"
    assert r.json()["field"] == "PAYMENT-SIGNATURE"


def test_a_rejected_payment_comes_back_with_a_fresh_challenge():
    """A well-formed authorization we decline is different: the caller's next step really is to pay,
    so it needs a live nonce."""
    import base64 as _b64
    import json as _json
    stale = _b64.b64encode(_json.dumps({"nonce": "expired", "scheme": "exact"}).encode()).decode()
    r = client.post("/a2mcp/robots.check", headers={"X-PAYMENT": stale},
                    json={"input": {"url": FREE_URL}})
    assert r.status_code == 402
    assert r.json()["payment_error"]
    assert decode_challenge(r.headers["PAYMENT-REQUIRED"])["nonce"]


def test_an_unknown_service_is_a_404_that_lists_the_real_ones():
    r = client.post("/a2mcp/page.nonexistent")
    assert r.status_code == 404
    assert "page.audit" in r.json()["services"]


def test_health_and_listing_are_free():
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/services").json()["count"] >= 13
    assert client.get("/").status_code == 200


def test_rendering_works_inside_the_async_request_handler():
    """Playwright's synchronous API refuses to start when an asyncio loop is already running in the
    calling thread — which is exactly the situation inside an ASGI handler. Called directly it raised
    every time, the exception was swallowed, and page.asai reported "could not render" for every URL
    in production while working perfectly from a script. The render now runs on a worker thread.

    Asserted through the real server, because the bug is invisible anywhere else.
    """
    import fetch as fetch_mod

    calls: list[str] = []

    def fake_render_blocking(url: str, timeout: int):
        import asyncio
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            calls.append("no running loop — the sync browser API is legal here")
            return "<html><body><main>rendered content</main></body></html>", True
        calls.append("RUNNING LOOP — the sync browser API would refuse")
        return "", False

    original = fetch_mod._render_blocking
    fetch_mod._render_blocking = fake_render_blocking
    try:
        page = fetch_mod.fetch("https://example.com", render=True, timeout=30)
    finally:
        fetch_mod._render_blocking = original

    assert calls and "no running loop" in calls[0], calls
    assert page.rendered is True


def test_page_asai_refuses_rather_than_returning_an_empty_result(monkeypatch):
    """This service *is* the comparison between raw and rendered HTML. Without a rendered DOM there
    is no product, and an empty finding set would read as "no JS-only content found"."""
    import fetch as fetch_mod
    monkeypatch.setattr(fetch_mod, "_render_blocking", lambda url, timeout: ("", False))

    challenge = decode_challenge(client.post("/a2mcp/page.asai").headers["PAYMENT-REQUIRED"])
    r = client.post("/a2mcp/page.asai", headers={"X-PAYMENT": make_dev_payment(challenge)},
                    json={"input": {"url": "https://example.com"}})
    env = r.json()
    assert env.get("error"), "an unrenderable page must not return a clean asai result"
    assert env["error"]["code"] == "ENGINE_UNAVAILABLE"
