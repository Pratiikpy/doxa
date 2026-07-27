"""A page that was never fetched must not be described as a page that returned the wrong type.

`looks_auditable` classified failures in order: anti-bot challenge, then HTTP error status, then
"not HTML". A URL whose DNS never resolved has **no status at all**, so it fell past the error branch
and was reported as *"The URL returned an unknown content type rather than HTML"* — describing a
response that never arrived, and sending the customer to check a header when the real problem was
their hostname.

Measured as a customer against the live service, alongside the 404 and 500 cases, which were both
described accurately. Only the never-reached case was wrong.
"""
from __future__ import annotations

import pytest

from checks.challenge import looks_auditable
from fetch import Page


def _page(**kw) -> Page:
    return Page(url=kw.pop("url", "https://example.org/"),
                requested_url=kw.pop("requested_url", "https://example.org/"), **kw)


def test_a_url_that_never_resolved_says_so():
    ok, why = looks_auditable(_page(status=0, error="DNS resolution failed for nope.example"))
    assert ok is False
    assert "could not be fetched" in why
    assert "DNS resolution failed" in why
    assert "content type" not in why, "a response that never happened has no content type"


def test_a_blocked_url_reports_the_block_not_a_content_type():
    ok, why = looks_auditable(_page(status=0, blocked_reason="blocked port: 22"))
    assert ok is False
    assert "blocked port: 22" in why
    assert "content type" not in why


def test_no_reason_at_all_still_reads_sensibly():
    ok, why = looks_auditable(_page(status=0))
    assert ok is False
    assert "no response was received" in why


@pytest.mark.parametrize("status", [404, 410, 500, 503])
def test_an_http_error_is_still_reported_as_that_status(status):
    """The branch that was already correct must stay correct."""
    ok, why = looks_auditable(_page(status=status, ok=False, media_type="text/html"))
    assert ok is False
    assert f"HTTP {status}" in why
    assert "itself the finding" in why


def test_a_real_non_html_response_still_reports_its_type():
    ok, why = looks_auditable(_page(status=200, ok=True, media_type="application/pdf"))
    assert ok is False
    assert "application/pdf" in why


def test_a_normal_html_page_is_auditable():
    ok, why = looks_auditable(_page(status=200, ok=True, media_type="text/html"))
    assert ok is True and why == ""
