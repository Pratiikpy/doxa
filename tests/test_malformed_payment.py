"""A bad payment header must get an answer, not a dropped socket.

Reported by the OKX reviewer against #9626: every malformed `PAYMENT-SIGNATURE` produced "empty reply
from server" — no HTTP response at all — while the unpaid 402 path stayed healthy.

The cause was not an unhandled throw in the decode (that was already caught). The rejected-payment
branch rebuilt its body by adding a `payment_error` field, then reused the *original* challenge
response's headers — including its `content-length`. uvicorn wrote the declared number of bytes,
found more, and killed the connection. It only ever affected this one path, which is exactly the path
where a caller most needs to be told what they got wrong.
"""
from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

import server
from x402 import decode_challenge, make_dev_payment

client = TestClient(server.app)
ENDPOINT = "/a2mcp/robots.check"
BODY = {"input": {"url": "https://example.com"}}


@pytest.mark.parametrize("header,why", [
    ("!!!not-base64!!!", "not base64 at all"),
    ("YWJj", "valid base64 that is not JSON"),
    ("eyJmb28iOiJiYXIifQ==", "valid base64 JSON that is not a payment payload"),
    ("", "empty header"),
    ("W10=", "valid base64 JSON that is an array, not an object"),
])
def test_a_malformed_header_gets_a_response_not_a_dropped_socket(header, why):
    r = client.post(ENDPOINT, headers={"PAYMENT-SIGNATURE": header}, json=BODY)
    assert r.status_code in (400, 402), f"{why}: got {r.status_code}"
    assert r.content, f"{why}: empty body"
    assert r.headers.get("content-length") is None or \
        int(r.headers["content-length"]) == len(r.content), \
        f"{why}: content-length disagrees with the body — this is what killed the connection"


@pytest.mark.parametrize("header", ["!!!not-base64!!!", "YWJj", "W10="])
def test_an_undecodable_header_is_a_typed_400_naming_the_field(header):
    """Paying again cannot fix a header the caller built wrongly, so it is a 400, not a 402."""
    r = client.post(ENDPOINT, headers={"PAYMENT-SIGNATURE": header}, json=BODY)
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "malformed_payment_signature"
    assert body["field"] == "PAYMENT-SIGNATURE"
    assert body["error"] and body["expected"]


def test_a_wellformed_but_rejected_payment_still_gets_a_fresh_challenge():
    """A spent nonce is a payment problem: the caller should pay again, so give them a live one."""
    challenge = decode_challenge(client.post(ENDPOINT).headers["PAYMENT-REQUIRED"])
    payment = make_dev_payment(challenge)
    assert client.post(ENDPOINT, headers={"X-PAYMENT": payment}, json=BODY).status_code == 200
    replay = client.post(ENDPOINT, headers={"X-PAYMENT": payment}, json=BODY)
    assert replay.status_code == 402
    assert replay.json()["payment_error"]
    assert decode_challenge(replay.headers["PAYMENT-REQUIRED"])["nonce"]


def test_the_rejected_response_declares_its_own_length():
    """The regression itself: a stale content-length copied onto a longer body."""
    r = client.post(ENDPOINT, headers={"X-PAYMENT": base64.b64encode(
        json.dumps({"nonce": "does-not-exist"}).encode()).decode()}, json=BODY)
    assert r.status_code == 402
    assert int(r.headers["content-length"]) == len(r.content)


def test_the_unpaid_path_is_unaffected():
    r = client.post(ENDPOINT, json=BODY)
    assert r.status_code == 402
    assert int(r.headers["content-length"]) == len(r.content)


def test_every_service_survives_a_malformed_header():
    """One handler, but assert it across the catalogue — a reviewer will not stop at one endpoint."""
    for svc in client.get("/services").json()["services"]:
        r = client.post(f"/a2mcp/{svc['endpoint']}",
                        headers={"PAYMENT-SIGNATURE": "!!!bad!!!"}, json={})
        assert r.status_code == 400, f"{svc['endpoint']} returned {r.status_code}"
        assert r.json()["code"] == "malformed_payment_signature"


def test_an_empty_paid_call_returns_the_contract_not_an_error():
    """The first real agent-to-agent purchase of this service paid 0.005 USDT and got INVALID_INPUT:
    the buying agent's pre-check found no declared inputs, so it sent an empty body. Payment settles
    before this code runs, so answering "you forgot the url" and keeping the money is charging for
    nothing."""
    challenge = decode_challenge(client.post(ENDPOINT).headers["PAYMENT-REQUIRED"])
    r = client.post(ENDPOINT, headers={"X-PAYMENT": make_dev_payment(challenge)}, json={})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "input_required"
    assert body["required"] == ["url"]
    assert body["example_request"]["input"]["url"]
    assert body["what_this_does"]


def test_the_challenge_tells_a_client_what_to_send():
    """Without this an x402 client has no way to know an input is required."""
    for endpoint in ("robots.check", "kw.discover", "links.compare"):
        challenge = decode_challenge(
            client.post(f"/a2mcp/{endpoint}").headers["PAYMENT-REQUIRED"])
        schema = challenge["resource"].get("inputSchema")
        assert schema, f"{endpoint} advertises no input schema"
        assert schema["required"], f"{endpoint} declares no required field"
        assert schema["example"], f"{endpoint} offers no worked example"


def test_every_service_declares_a_usable_contract():
    for svc in client.get("/services").json()["services"]:
        contract = svc["input"]
        assert contract["required"], f"{svc['endpoint']} declares no required input"
        assert contract["example"], f"{svc['endpoint']} has no worked example"
        for field in contract["required"]:
            assert field in contract["example"], \
                f"{svc['endpoint']}'s example omits required field {field!r}"
