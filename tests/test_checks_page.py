"""Page-check correctness.

These assert the *thresholds ported from SEONaut* and the guards around them. Every number here was
read out of `internal/issues/page/*.go`, so if one of these fails it means we have drifted from the
taxonomy we claim to implement.

Network is not touched: a Page is constructed directly. A check that needs the internet to be tested
is a check that cannot be tested in CI.
"""
from __future__ import annotations

import pytest

from checks.base import Severity, registry
import checks.page_html  # noqa: F401  (registers the checks)
import checks.machine    # noqa: F401
from fetch import Page


def page(html: str = "", *, status: int = 200, url: str = "https://site.test/p",
         media: str = "text/html", headers: dict | None = None, ttfb: int = 100,
         rendered: str = "") -> Page:
    return Page(url=url, requested_url=url, status=status, ok=200 <= status < 300,
                media_type=media, html=html, rendered_html=rendered, rendered=bool(rendered),
                headers=headers or {}, ttfb_ms=ttfb,
                hops=[__import__("fetch").Hop(url=url, status=status)])


def codes(p: Page) -> set[str]:
    return {f.code for f in registry.run(p)}


def find(p: Page, code: str):
    return next((f for f in registry.run(p) if f.code == code), None)


DOC = "<html lang='en'><head><title>{t}</title>{extra}</head><body>{body}</body></html>"


def doc(title="A perfectly reasonable page title here", extra="", body="<h1>H</h1>"):
    return DOC.format(t=title, extra=extra, body=body)


# --- title: SEONaut thresholds are short < 20, long > 60 ------------------------------------------

def test_title_missing_is_critical():
    f = find(page("<html><head></head><body>x</body></html>"), "title.empty")
    assert f is not None and f.severity is Severity.CRITICAL


def test_title_short_boundary_is_exclusive_at_20():
    assert "title.short" in codes(page(doc(title="x" * 19)))
    assert "title.short" not in codes(page(doc(title="x" * 20)))


def test_title_long_boundary_is_exclusive_at_60():
    assert "title.long" not in codes(page(doc(title="x" * 60)))
    assert "title.long" in codes(page(doc(title="x" * 61)))


def test_multiple_titles_reported():
    html = "<html><head><title>One</title><title>Two</title></head><body>b</body></html>"
    f = find(page(html), "title.multiple")
    assert f is not None and f.detail["count"] == 2


# --- description: short < 80, long > 160 ----------------------------------------------------------

def test_description_boundaries():
    short = f"<meta name='description' content='{'x' * 79}'>"
    exact = f"<meta name='description' content='{'x' * 80}'>"
    long_ = f"<meta name='description' content='{'x' * 161}'>"
    assert "description.short" in codes(page(doc(extra=short)))
    assert "description.short" not in codes(page(doc(extra=exact)))
    assert "description.long" in codes(page(doc(extra=long_)))


# --- the guard that stops noise on error pages ------------------------------------------------------

def test_a_404_reports_the_404_and_not_every_content_fault():
    """SEONaut guards content reporters on a 2xx. Without that guard a 404 page reports a missing
    title, missing H1 and thin content — three findings that bury the one that matters."""
    c = codes(page("<html><head></head><body></body></html>", status=404))
    assert "status.4xx" in c
    assert "title.empty" not in c
    assert "h1.missing" not in c
    assert "content.thin" not in c


def test_non_html_is_not_content_checked():
    c = codes(page("binary", media="application/pdf"))
    assert "title.empty" not in c


# --- indexability is the finding that outranks everything -------------------------------------------

def test_noindex_is_critical():
    f = find(page(doc(extra="<meta name='robots' content='noindex,follow'>")), "robots.noindex")
    assert f is not None and f.severity is Severity.CRITICAL


def test_noindex_via_x_robots_tag_header():
    f = find(page(doc(), headers={"x-robots-tag": "noindex"}), "robots.noindex")
    assert f is not None and f.detail["source"] == "x-robots-tag"


def test_nosnippet_is_flagged_because_it_blocks_citation():
    assert "robots.nosnippet" in codes(page(doc(extra="<meta name='robots' content='nosnippet'>")))


# --- canonical: the five failure modes ---------------------------------------------------------------

def test_relative_canonical_is_high():
    f = find(page(doc(extra="<link rel='canonical' href='/other'>")), "canonical.relative")
    assert f is not None and f.severity is Severity.HIGH


def test_multiple_canonicals():
    html = doc(extra="<link rel='canonical' href='https://a.test/'>"
                     "<link rel='canonical' href='https://b.test/'>")
    assert "canonical.multiple" in codes(page(html))


def test_canonical_html_header_mismatch():
    html = doc(extra="<link rel='canonical' href='https://site.test/a'>")
    f = find(page(html, headers={"link": '<https://site.test/b>; rel="canonical"'}),
             "canonical.mismatch")
    assert f is not None


def test_self_canonical_is_not_a_finding():
    html = doc(extra="<link rel='canonical' href='https://site.test/p'>")
    assert "canonical.other" not in codes(page(html))


# --- headings ---------------------------------------------------------------------------------------

def test_missing_h1():
    assert "h1.missing" in codes(page(doc(body="<p>text</p>")))


def test_heading_level_skip_is_detected():
    f = find(page(doc(body="<h1>a</h1><h2>b</h2><h4>c</h4>")), "headings.order")
    assert f is not None and f.detail["skips"][0] == {"from": 2, "to": 4}


def test_correct_heading_order_produces_nothing():
    assert "headings.order" not in codes(page(doc(body="<h1>a</h1><h2>b</h2><h3>c</h3><h2>d</h2>")))


# --- forms and transport --------------------------------------------------------------------------------

def test_form_posting_to_http_is_critical():
    f = find(page(doc(body="<form action='http://x.test/submit'></form>")), "form.insecure_action")
    assert f is not None and f.severity is Severity.CRITICAL


def test_slow_ttfb_uses_the_800ms_threshold():
    assert "ttfb.slow" not in codes(page(doc(), ttfb=800))
    assert "ttfb.slow" in codes(page(doc(), ttfb=801))


# --- content ---------------------------------------------------------------------------------------------

def test_thin_content_threshold_is_200_words():
    assert "content.thin" in codes(page(doc(body="<p>" + "w " * 199 + "</p>")))
    assert "content.thin" not in codes(page(doc(body="<p>" + "w " * 200 + "</p>")))


def test_script_text_does_not_count_as_content():
    """A page whose only 'words' are minified JS is thin, however many tokens the file holds."""
    body = "<script>" + "var x = 1; " * 400 + "</script><p>five words of real text</p>"
    assert "content.thin" in codes(page(doc(body=body)))


def test_meta_in_body_is_reported():
    assert "meta.in_body" in codes(page(doc(body="<meta name='description' content='x'>")))


def test_title_text_is_not_counted_as_body_content():
    """A <title> is not body copy. Counting it made a 199-word page measure 205 and slip past the
    thin-content threshold — the check quietly disagreeing with the number it reports."""
    p = page(doc(title="Six words in the page title", body="<p>" + "w " * 199 + "</p>"))
    assert find(p, "content.thin").detail["words"] == 199


def test_running_checks_does_not_mutate_the_shared_document():
    """The content check needs text without <script>. It must not get it by decomposing tags out of
    the soup every other check shares, which silently deletes them from the whole audit."""
    from checks.page_html import soup as parsed
    p = page(doc(body="<script>var x = 1</script><p>hello</p>"))
    registry.run(p)
    assert parsed(p).find_all("script"), "the shared document lost its <script> tags"


def test_parsed_html_is_never_shared_between_pages():
    """The parse cache was keyed on id(page). CPython reuses an id as soon as an object is collected,
    so one page could be served another page's parsed HTML — a signed answer about the wrong
    document. Churning pages through the cache forces the id reuse that exposes it."""
    import gc

    from checks.page_html import soup as parsed
    titles = set()
    for i in range(200):
        p = page(doc(title=f"Distinct page title number {i} goes here"))
        titles.add(parsed(p).title.get_text())
        del p
        gc.collect()
    assert len(titles) == 200, f"{200 - len(titles)} pages were served another page's HTML"


# --- the JS-visibility check, which is the product ------------------------------------------------------

BODY = " ".join(f"word{i} a real sentence about the subject matter" for i in range(60))


def test_client_side_rendered_shell_is_critical():
    p = page("<html><head><title>App</title></head><body><div id='root'></div></body></html>",
             rendered=f"<html><body><main><h1>Real</h1><p>{BODY}</p></main></body></html>")
    f = find(p, "asai.js_required")
    assert f is not None and f.severity is Severity.CRITICAL
    assert f.detail["js_only_share"] > 0.9


def test_server_rendered_page_is_info_not_a_fault():
    same = f"<html><body><main><h1>Real</h1><p>{BODY}</p></main></body></html>"
    f = find(page(same, rendered=same), "asai.server_rendered")
    assert f is not None and f.severity is Severity.INFO
    assert f.detail["js_only_share"] == 0.0


def test_partial_injection_is_measured_by_content_not_length():
    half = " ".join(BODY.split()[:180])
    p = page(f"<html><body><p>{half}</p></body></html>",
             rendered=f"<html><body><p>{BODY}</p></body></html>")
    f = next(f for f in registry.run(p) if f.code.startswith("asai."))
    assert f.code in ("asai.mostly_js", "asai.partly_js")
    assert 0.2 < f.detail["js_only_share"] < 0.9


def test_asai_does_not_run_without_a_rendered_copy():
    """No rendered DOM means no evidence. Reporting 'server rendered' would be a false clean."""
    assert not [f for f in registry.run(page(doc())) if f.code.startswith("asai.")]


# --- registry hygiene ---------------------------------------------------------------------------------------

def test_no_duplicate_check_prefixes():
    prefixes = [c.code_prefix for c in registry.all()]
    assert len(prefixes) == len(set(prefixes))


def test_findings_are_ordered_by_severity():
    p = page("<html><head></head><body><form action='http://x/'></form></body></html>", status=200)
    sev = [f.severity for f in registry.run(p)]
    order = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.LOW: 2, Severity.INFO: 3}
    assert [order[s] for s in sev] == sorted(order[s] for s in sev)


def test_every_finding_has_a_message_and_a_code():
    p = page(doc(body="<p>short</p>"))
    for f in registry.run(p):
        assert f.code and "." in f.code, f"code must be namespaced: {f.code}"
        assert f.message and f.message[0].isupper(), f"message must read as a sentence: {f.message}"
