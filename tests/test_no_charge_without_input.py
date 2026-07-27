"""Input is validated before the money moves, not after.

This server's own comments record the incident: the first real agent-to-agent purchase of a Doxa
service paid and received INVALID_INPUT, because the buying agent found no declared inputs and sent
an empty body. The reply was then improved to hand back the contract — but it still ran after
settlement, so the caller was charged for being told what they should have sent. Measured live at
$0.01 before this fix.

The status codes are asserted deliberately. 200-with-the-contract for an empty body is the behaviour
that passed OKX review, and a bare 402 on the unpaid probe is what the listing validator reads. The
fix is meant to be invisible to clients except in ceasing to bill them, so a later change that
"simplifies" these into one error code would be a listing risk, not a cleanup.
"""
from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

import server
import x402

PAID = "/a2mcp/page.audit"          # requires `url`


@pytest.fixture()
def client():
    return TestClient(server.app)


@pytest.fixture()
def never_settles(monkeypatch):
    def boom(*a, **k):                                               # noqa: ANN002, ANN003
        raise AssertionError("payment settled for a request that could not be served")
    monkeypatch.setattr(server, "verify_payment", boom)


def _plausible_header() -> str:
    """Parses as an x402 v2 payload, so the malformed check passes and the input check decides."""
    return base64.b64encode(json.dumps({
        "x402Version": 2,
        "accepted": {"network": "eip155:196"},
        "payload": {"authorization": {"value": "1", "nonce": "0x" + "11" * 32}, "signature": "0x00"},
    }).encode()).decode()


def test_empty_body_is_answered_before_settlement(client, never_settles):
    r = client.post(PAID, json={}, headers={"PAYMENT-SIGNATURE": _plausible_header()})
    assert r.status_code == 200                       # unchanged from the reviewed behaviour
    body = r.json()
    assert body["status"] == "input_required"
    assert "not_charged" in body
    assert body["example_request"]["input"], "a refusal must show a valid call"
    assert "result" not in body, "a refusal must not be shaped like a result"


def test_partial_input_is_answered_before_settlement(client, never_settles):
    r = client.post(PAID, json={"not_the_url": "x"},
                    headers={"PAYMENT-SIGNATURE": _plausible_header()})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "missing_required_field"
    assert "not_charged" in body


def test_the_unpaid_probe_still_gets_a_challenge(client):
    """The listing validator sends no payment header and no body and reads the status code."""
    r = client.post(PAID, json={})
    assert r.status_code == 402
    assert r.headers.get(x402.PAYMENT_REQUIRED_HEADER)


@pytest.mark.parametrize("header", ["!!!not-base64!!!", "aGVsbG8=", "WzEsMiwzXQ==", "eyJub3BlIjoxfQ=="])
def test_a_broken_header_is_a_typed_400_and_outranks_the_input_check(client, header):
    """Two bugs at once: only the header error explains why paying again will not help."""
    r = client.post(PAID, json={}, headers={"PAYMENT-SIGNATURE": header})
    assert r.status_code == 400
    assert r.json()["code"] == "malformed_payment_signature"
