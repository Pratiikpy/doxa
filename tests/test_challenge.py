"""Refusing to audit the wrong document.

A bot-mitigation product answers with an HTML page, sometimes with HTTP 200. An audit tool that does
not recognise it proceeds to describe the interstitial: no meta description, one link, thin content.
Every finding is true about the challenge page and worthless about the site.

This happened here. While these services were being tested, Vercel started challenging our fetcher on
react.dev, and `page.links` reported that react.dev has exactly one link — to
`vercel.link/security-checkpoint`. A signed, paid, confidently wrong answer about a document the
customer has never seen. These tests exist so that cannot come back.
"""
from __future__ import annotations

import pytest

from checks.challenge import detect, looks_auditable
from checks.machine import probe_ai_crawlers
from tests.test_checks_page import doc, page


def test_vercel_challenge_is_detected_by_its_header():
    p = page("<html><head><title>Vercel Security Checkpoint</title></head><body>x</body></html>",
             status=403, headers={"x-vercel-mitigated": "challenge"})
    ch = detect(p)
    assert ch is not None and ch.vendor == "Vercel"
    assert "x-vercel-mitigated" in ch.evidence


def test_cloudflare_interstitial_served_with_200_is_still_a_challenge():
    """The dangerous case: a 200 that is not the page. Status alone would let this through."""
    p = page("<html><head><title>Just a moment...</title></head>"
             "<body>Checking your browser before accessing the site.</body></html>", status=200)
    ch = detect(p)
    assert ch is not None and ch.vendor == "Cloudflare"


@pytest.mark.parametrize("html,headers,vendor", [
    ("<html><body>Incapsula incident ID: 1234-5678</body></html>", {}, "Imperva Incapsula"),
    ("<html><body><div id='px-captcha'></div></body></html>", {}, "PerimeterX / HUMAN"),
    ("<html><body>Sucuri WebSite Firewall - Access Denied</body></html>", {}, "Sucuri"),
    ("<html><body>x</body></html>", {"x-datadome": "protected"}, "DataDome"),
    ("<html><body>x</body></html>", {"x-amzn-waf-action": "captcha"}, "AWS WAF"),
])
def test_each_vendor_signature(html, headers, vendor):
    assert detect(page(html, status=403, headers=headers)).vendor == vendor


def test_a_real_article_about_cloudflare_is_not_a_challenge():
    """Cloudflare's own blog must not be reported as blocking itself. Length and status separate a
    long editorial page that mentions these products from a small interstitial that is one."""
    body = "<h1>How we built it</h1><p>" + ("We use Cloudflare and its challenge platform. " * 900) + "</p>"
    p = page(doc(body=body), status=200)
    assert detect(p) is None


def test_a_normal_page_is_auditable():
    ok, why = looks_auditable(page(doc(), status=200))
    assert ok and why == ""


def test_a_404_is_not_auditable_and_says_why():
    ok, why = looks_auditable(page(doc(), status=404))
    assert not ok and "404" in why


def test_a_pdf_is_not_page_auditable():
    ok, why = looks_auditable(page("%PDF-1.4", status=200, media="application/pdf"))
    assert not ok and "HTML" in why


def test_a_challenged_page_is_not_auditable():
    p = page("<html><body>x</body></html>", status=403,
             headers={"x-vercel-mitigated": "challenge"})
    ok, why = looks_auditable(p)
    assert not ok and "Vercel" in why


def test_crawler_probe_refuses_to_report_a_clean_result_without_a_baseline():
    """The exact false clean this caught: when the browser fetch is itself challenged, every
    crawler comparison falls through to 'all treated the same', which reads as a pass."""
    challenged = page("<html><body>verifying your browser</body></html>", status=403,
                      headers={"x-vercel-mitigated": "challenge"})
    findings = probe_ai_crawlers("https://site.test/p", challenged, crawlers=["GPTBot"])
    codes = {f.code for f in findings}
    assert codes == {"aicrawler.baseline_unavailable"}
    assert "aicrawler.allowed" not in codes
    assert findings[0].severity.value == "critical"
    # And it must not have gone out to the network to reach that conclusion.
    assert findings[0].detail["crawlers_not_tested"] == ["GPTBot"]
