"""Nothing a customer reads should look like a crash report.

Every service in Doxa calls somebody else's API, so upstream failures are routine. The raw text of a
requests/urllib3 exception is a nested repr containing object addresses — handing that back to a
paying customer looks broken, whatever the underlying cause. These tests pin the presentation.
"""
from __future__ import annotations

import pytest
import requests

from runtime import humanise_error


@pytest.mark.parametrize("exc", [
    requests.exceptions.ConnectTimeout(
        "HTTPSConnectionPool(host='index.commoncrawl.org', port=443): Max retries exceeded with "
        "url: /collinfo.json (Caused by ConnectTimeoutError(<urllib3.connection.HTTPSConnection "
        "object at 0x0000021D817C3390>, 'Connection timed out. (connect timeout=45)'))"),
    requests.exceptions.ReadTimeout(
        "HTTPSConnectionPool(host='api.github.com', port=443): Read timed out. (read timeout=30)"),
    requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='x.test', port=443): Max retries exceeded (Caused by "
        "NewConnectionError(<urllib3.connection.HTTPSConnection object at 0x00007FFF>, 'failed'))"),
])
def test_no_object_reprs_or_addresses_ever_reach_the_customer(exc):
    msg = humanise_error(exc)
    assert "0x" not in msg, msg
    assert "object at" not in msg, msg
    assert "urllib3" not in msg, msg
    assert "Caused by" not in msg, msg


def test_the_message_still_says_what_went_wrong_and_where():
    msg = humanise_error(requests.exceptions.ConnectTimeout(
        "HTTPSConnectionPool(host='index.commoncrawl.org', port=443): Max retries exceeded"))
    assert "did not accept a connection in time" in msg
    assert "index.commoncrawl.org" in msg
    assert "[ConnectTimeout]" in msg      # the class name is kept, for diagnosis


def test_an_ordinary_message_is_passed_through():
    assert humanise_error(ValueError("the model did not return usable JSON")).startswith(
        "the model did not return usable JSON")


def test_a_very_long_message_is_capped():
    msg = humanise_error(RuntimeError("x" * 5000))
    assert len(msg) < 400 and msg.endswith("[RuntimeError]")


def test_an_empty_exception_still_reads_as_a_sentence():
    assert humanise_error(RuntimeError()) == "the service failed unexpectedly. [RuntimeError]"


def test_every_service_error_message_is_presentable():
    """Whatever a node raises, the envelope's message must read as prose — no stack frames, no
    file paths, no reprs."""
    import server
    from contract import ArtifactRequest

    bad = server.RUNTIME.execute(ArtifactRequest(endpoint="page.audit", input={"url": ""}))
    env = bad.model_dump()
    msg = env["error"]["message"]
    assert msg and msg[0].isupper(), msg
    for forbidden in ("Traceback", "0x", "object at", "File \"", "line "):
        assert forbidden not in msg, f"{forbidden!r} leaked into: {msg}"
