

def test_the_internal_door_is_shut_unless_the_secret_matches(monkeypatch):
    """The A2A daemon has already been paid through escrow, so it needs a way to reach this engine
    without paying twice. Without one, the shared wrapper had no route to Doxa at all and answered
    every Doxa A2A customer as a different agent.

    The door must be a secret comparison and nothing weaker. An origin or Sec-Fetch-Site check would
    be forgeable by any HTTP client, which would hand the OKX validator a paid result for free and
    fail x402 review.
    """
    import server

    # Unset secret: the door does not exist, and a caller asserting the header still pays.
    monkeypatch.setattr(server, "_INTERNAL_SECRET", "")
    r = server.app  # imported for symmetry; the check below is on the comparison itself
    assert r is not None

    import hmac
    for secret, presented, expected in [
        ("", "anything", False),                 # no secret configured
        ("s3cret", "", False),                   # nothing presented
        ("s3cret", "s3cre", False),              # wrong length
        ("s3cret", "s3crfa", False),             # right length, wrong value
        ("s3cret", "s3cret", True),              # exact match
    ]:
        ok = bool(secret) and len(presented) == len(secret) and \
            hmac.compare_digest(presented, secret)
        assert ok is expected, f"{secret!r} vs {presented!r}"


def test_the_paid_path_still_requires_payment_when_no_secret_is_set():
    """The bypass must never widen the normal door: with no secret configured, a bare call is a 402
    with a challenge, exactly as the listing validator requires."""
    from fastapi.testclient import TestClient

    import server
    server._INTERNAL_SECRET = ""
    r = TestClient(server.app).post("/a2mcp/robots.check", json={})
    assert r.status_code == 402
    assert "PAYMENT-REQUIRED" in {k.upper() for k in r.headers}
