"""Single-page HTML checks — title, description, headings, canonical, indexability, lang, viewport.

Ported from SEONaut (MIT) `internal/issues/page/`. Their thresholds are used verbatim; where this file
does something SEONaut does not, the comment says why.

Parsing is done with lxml through BeautifulSoup rather than regex. An earlier instinct to regex the
`<title>` out is wrong for a service that signs its answers: a commented-out title, a title inside an
SVG, or a `<title>` in the body rather than the head all produce a confidently wrong measurement.
"""
from __future__ import annotations

import re
import unicodedata
import urllib.parse
from typing import Any

from bs4 import BeautifulSoup

from checks.base import Finding, Severity, is_html, is_html_200, registry
from fetch import Page

def soup(page: Page, rendered: bool = False) -> BeautifulSoup:
    """Parse once per page. Checks run in a batch and would otherwise reparse twenty times.

    The cache lives ON the page object, not in a module dict keyed by ``id(page)``. CPython reuses an
    id as soon as the object is collected, so a module-level id cache will hand one page another
    page's parsed HTML — producing a confidently wrong, signed answer about the wrong document. The
    tests caught it; the bug would have been invisible in production until someone compared two
    audits.
    """
    attr = "_soup_rendered" if rendered else "_soup_raw"
    cached = getattr(page, attr, None)
    if cached is None:
        html = page.rendered_html if (rendered and page.rendered_html) else page.html
        cached = BeautifulSoup(html or "", "lxml")
        setattr(page, attr, cached)
    return cached


def _text(el) -> str:
    return (el.get_text() or "").strip() if el is not None else ""


def _abs(base: str, href: str) -> str:
    try:
        return urllib.parse.urljoin(base, (href or "").strip())
    except Exception:  # noqa: BLE001
        return href or ""


# --- status and transport -------------------------------------------------------------------------

@registry.register("status", "HTTP status and redirects", applies=lambda p: True)
def check_status(page: Page) -> list[Finding]:
    out: list[Finding] = []
    if page.blocked_reason:
        return [Finding("fetch.blocked", Severity.CRITICAL,
                        f"The URL could not be fetched: {page.blocked_reason}",
                        {"reason": page.blocked_reason})]
    if page.error and not page.status:
        return [Finding("fetch.failed", Severity.CRITICAL,
                        f"The page could not be fetched: {page.error}", {"error": page.error})]

    s = page.status
    if 300 <= s < 400:
        out.append(Finding("status.3xx", Severity.HIGH,
                           f"The URL returns {s} and redirects instead of serving content.",
                           {"status": s, "chain": [h.url for h in page.hops]}))
    elif 400 <= s < 500:
        out.append(Finding("status.4xx", Severity.CRITICAL,
                           f"The URL returns {s}. Visitors and crawlers get nothing.", {"status": s}))
    elif 500 <= s:
        out.append(Finding("status.5xx", Severity.CRITICAL,
                           f"The URL returns {s}. The server is failing on this page.", {"status": s}))

    # A redirect CHAIN is separate from a single redirect: each hop loses a little equity and adds
    # latency, and a loop is fatal. SEONaut reports these as distinct codes.
    if len(page.hops) > 2:
        out.append(Finding("redirect.chain", Severity.HIGH,
                           f"There are {len(page.hops) - 1} redirects before the final page. "
                           f"Each hop costs time and a little ranking signal.",
                           {"hops": [{"url": h.url, "status": h.status} for h in page.hops]}))
    seen = [h.url for h in page.hops]
    if len(seen) != len(set(seen)):
        out.append(Finding("redirect.loop", Severity.CRITICAL,
                           "The redirects loop back on themselves, so the page never loads.",
                           {"hops": seen}))
    if page.ttfb_ms > 800:
        out.append(Finding("ttfb.slow", Severity.LOW,
                           f"The server took {page.ttfb_ms} ms to send the first byte "
                           f"(SEONaut's threshold is 800 ms).", {"ttfb_ms": page.ttfb_ms}))
    if page.hops and page.hops[0].url.lower().startswith("http://"):
        out.append(Finding("scheme.http", Severity.HIGH,
                           "The URL was requested over plain HTTP rather than HTTPS.",
                           {"url": page.hops[0].url}))
    return out


# --- title ----------------------------------------------------------------------------------------

@registry.register("title", "Page title")
def check_title(page: Page) -> list[Finding]:
    s = soup(page)
    head = s.head or s
    titles = head.find_all("title") if head else []
    out: list[Finding] = []
    if not titles:
        return [Finding("title.empty", Severity.CRITICAL,
                        "The page has no title tag. This is the single strongest on-page signal and "
                        "the text a search result shows.", {})]
    title = _text(titles[0])
    n = len(title)
    if not title:
        out.append(Finding("title.empty", Severity.CRITICAL,
                           "The title tag is present but empty.", {}))
    else:
        if n < 20:
            out.append(Finding("title.short", Severity.LOW,
                               f"The title is {n} characters. Under 20 leaves room unused.",
                               {"title": title, "length": n}))
        if n > 60:
            out.append(Finding("title.long", Severity.LOW,
                               f"The title is {n} characters and will be cut off in results "
                               f"(over 60).", {"title": title, "length": n}))
    if len(titles) > 1:
        out.append(Finding("title.multiple", Severity.HIGH,
                           f"There are {len(titles)} title tags. Only the first is used and the rest "
                           f"suggest a template bug.",
                           {"count": len(titles), "titles": [_text(t) for t in titles[:5]]}))
    return out


# --- meta description -----------------------------------------------------------------------------

@registry.register("description", "Meta description")
def check_description(page: Page) -> list[Finding]:
    s = soup(page)
    metas = [m for m in s.find_all("meta")
             if (m.get("name") or "").lower() == "description"]
    out: list[Finding] = []
    if not metas:
        return [Finding("description.empty", Severity.HIGH,
                        "There is no meta description, so the search engine will invent the snippet.",
                        {})]
    desc = (metas[0].get("content") or "").strip()
    n = len(desc)
    if not desc:
        out.append(Finding("description.empty", Severity.HIGH,
                           "The meta description tag is present but empty.", {}))
    else:
        if n < 80:
            out.append(Finding("description.short", Severity.LOW,
                               f"The description is {n} characters. Under 80 wastes the snippet.",
                               {"description": desc, "length": n}))
        if n > 160:
            out.append(Finding("description.long", Severity.LOW,
                               f"The description is {n} characters and will be truncated (over 160).",
                               {"description": desc, "length": n}))
    if len(metas) > 1:
        out.append(Finding("description.multiple", Severity.HIGH,
                           f"There are {len(metas)} meta description tags.", {"count": len(metas)}))
    return out


# --- headings --------------------------------------------------------------------------------------

@registry.register("headings", "Heading structure")
def check_headings(page: Page) -> list[Finding]:
    s = soup(page)
    out: list[Finding] = []
    h1s = s.find_all("h1")
    if not h1s:
        out.append(Finding("h1.missing", Severity.HIGH,
                           "The page has no H1. Both readers and models use it to decide what the "
                           "page is about.", {}))
    elif len([h for h in h1s if _text(h)]) == 0:
        out.append(Finding("h1.missing", Severity.HIGH, "The H1 is present but empty.", {}))
    elif len(h1s) > 1:
        out.append(Finding("h1.multiple", Severity.LOW,
                           f"There are {len(h1s)} H1 tags. One main heading is clearer.",
                           {"count": len(h1s), "headings": [_text(h)[:80] for h in h1s[:5]]}))

    # Order: a jump from H2 straight to H4 breaks the outline a model uses to navigate the page.
    levels = [int(t.name[1]) for t in s.find_all(re.compile(r"^h[1-6]$"))]
    bad: list[dict[str, int]] = []
    prev = 0
    for lv in levels:
        if prev and lv > prev + 1:
            bad.append({"from": prev, "to": lv})
        prev = lv
    if bad:
        out.append(Finding("headings.order", Severity.LOW,
                           f"The heading levels skip a step {len(bad)} time(s), for example H{bad[0]['from']} "
                           f"straight to H{bad[0]['to']}. The outline is harder to follow.",
                           {"skips": bad[:10], "sequence": levels[:40]}))
    return out


# --- canonical ---------------------------------------------------------------------------------------

@registry.register("canonical", "Canonical URL")
def check_canonical(page: Page) -> list[Finding]:
    s = soup(page)
    out: list[Finding] = []
    links = [l for l in s.find_all("link")
             if "canonical" in [r.lower() for r in (l.get("rel") or [])]]
    header_canonical = ""
    lh = page.headers.get("link", "")
    m = re.search(r'<([^>]+)>\s*;\s*rel\s*=\s*"?canonical"?', lh, re.I)
    if m:
        header_canonical = m.group(1).strip()

    if len(links) > 1:
        out.append(Finding("canonical.multiple", Severity.HIGH,
                           f"There are {len(links)} canonical tags. Search engines will ignore all of "
                           f"them and choose for themselves.",
                           {"count": len(links),
                            "values": [(l.get("href") or "") for l in links[:5]]}))
    if not links and not header_canonical:
        return out + [Finding("canonical.missing", Severity.LOW,
                              "There is no canonical tag, so duplicate versions of this page cannot "
                              "be consolidated.", {})]

    href = (links[0].get("href") or "").strip() if links else header_canonical
    if href and not re.match(r"^https?://", href):
        out.append(Finding("canonical.relative", Severity.HIGH,
                           f"The canonical URL {href!r} is relative. It must be absolute to be "
                           f"reliable.", {"canonical": href}))
    absolute = _abs(page.url, href)

    if header_canonical and links:
        html_abs = _abs(page.url, (links[0].get("href") or ""))
        hdr_abs = _abs(page.url, header_canonical)
        if html_abs.rstrip("/") != hdr_abs.rstrip("/"):
            out.append(Finding("canonical.mismatch", Severity.HIGH,
                               "The canonical in the HTML and the one in the HTTP header disagree.",
                               {"html": html_abs, "header": hdr_abs}))

    if absolute and absolute.rstrip("/") != page.url.rstrip("/"):
        out.append(Finding("canonical.other", Severity.INFO,
                           f"This page points its canonical at a different URL, so it is asking not "
                           f"to be indexed in its own right.",
                           {"canonical": absolute, "page": page.url}))
    return out


# --- indexability -------------------------------------------------------------------------------------

@registry.register("indexability", "Indexability")
def check_indexability(page: Page) -> list[Finding]:
    s = soup(page)
    out: list[Finding] = []
    directives: list[str] = []
    for m in s.find_all("meta"):
        name = (m.get("name") or "").lower()
        if name in ("robots", "googlebot"):
            directives += [d.strip().lower() for d in (m.get("content") or "").split(",")]
    xr = page.headers.get("x-robots-tag", "")
    if xr:
        directives += [d.strip().lower() for d in xr.split(",")]

    if "noindex" in directives:
        out.append(Finding("robots.noindex", Severity.CRITICAL,
                           "This page tells search engines not to index it. If that is unintended, "
                           "nothing else on this report matters.",
                           {"directives": sorted(set(directives)),
                            "source": "x-robots-tag" if "noindex" in xr.lower() else "meta"}))
    if "nofollow" in directives:
        out.append(Finding("robots.nofollow", Severity.HIGH,
                           "This page tells search engines not to follow any of its links.",
                           {"directives": sorted(set(directives))}))
    if "nosnippet" in directives:
        out.append(Finding("robots.nosnippet", Severity.LOW,
                           "The nosnippet directive stops search engines and AI answers from quoting "
                           "this page — which also stops it being cited.",
                           {"directives": sorted(set(directives))}))
    if "noimageindex" in directives:
        out.append(Finding("robots.noimageindex", Severity.LOW,
                           "Images on this page are excluded from image search.", {}))
    return out


# --- language ------------------------------------------------------------------------------------------

@registry.register("language", "Language declaration")
def check_language(page: Page) -> list[Finding]:
    s = soup(page)
    html_el = s.find("html")
    lang = (html_el.get("lang") or "").strip() if html_el else ""
    if not lang:
        return [Finding("lang.missing", Severity.LOW,
                        "The html tag has no lang attribute, so the page language is left to be "
                        "guessed.", {})]
    if not re.match(r"^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$", lang):
        return [Finding("lang.invalid", Severity.LOW,
                        f"The declared language {lang!r} is not a valid language tag.",
                        {"lang": lang})]
    return []


# --- viewport ------------------------------------------------------------------------------------------

@registry.register("viewport", "Mobile viewport")
def check_viewport(page: Page) -> list[Finding]:
    s = soup(page)
    for m in s.find_all("meta"):
        if (m.get("name") or "").lower() == "viewport" and (m.get("content") or "").strip():
            return []
    return [Finding("viewport.missing", Severity.HIGH,
                    "There is no viewport meta tag, so the page will not lay out correctly on a "
                    "phone.", {})]


# --- content -------------------------------------------------------------------------------------------

NON_CONTENT = ("script", "style", "noscript", "template", "svg")

# Tags that end a line of running text. Everything absent here — <b>, <em>, <span>, <a>, <time>,
# <code> — is inline and must not introduce a space, or words split at their own markup.
BLOCK_LEVEL = ("address", "article", "aside", "blockquote", "br", "dd", "details", "dialog", "div",
               "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
               "h4", "h5", "h6", "header", "hgroup", "hr", "li", "main", "nav", "ol", "p", "pre",
               "section", "summary", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul")


def body_text(page: Page) -> str:
    """The words a reader actually sees on the page.

    Two mistakes are deliberately avoided here, both of which this codebase made first.

    It reads ``<body>`` only. ``get_text()`` on the whole document folds in the ``<title>`` and any
    meta text, so a 199-word page measured 205 and escaped the thin-content threshold. A title is not
    body copy.

    And it parses its own document rather than decomposing tags out of the shared soup. Every check
    reads that same cached object; stripping ``<script>`` for a word count would delete those scripts
    from the document every later check sees.

    The separator is the third mistake, and the subtlest. ``get_text(" ")`` puts a space between
    *every* text node, inline ones included, so ``Py<b>thon</b>`` becomes "Py thon" — two words where
    a reader sees one — and python.org's ``<time>2026-<span>07-23</span></time>`` becomes
    "2026- 07-23", which a figure extractor then quotes with the space in it. ``get_text("")`` fixes
    that and breaks the other end, running "…07-23" straight into the next paragraph. Only block
    boundaries are word boundaries, so only block boundaries get a separator.
    """
    cached = getattr(page, "_body_text", None)
    if cached is None:
        s = BeautifulSoup(page.html or "", "lxml")
        root = s.body or s
        for tag in root(NON_CONTENT):
            tag.decompose()
        for tag in root(BLOCK_LEVEL):
            tag.insert_before("\n")
            tag.insert_after("\n")
        cached = re.sub(r"\s+", " ", root.get_text("")).strip()
        setattr(page, "_body_text", cached)
    return cached


def content_words(page: Page) -> int:
    """Words of body copy, counted the way the 200-word threshold was calibrated.

    Porting a threshold means porting the measurement under it, and this is where the two diverge.
    SEONaut's ``countWords`` (internal/services/html_parser.go) skips the entire subtree of every
    ``<a>`` element and replaces Unicode punctuation and symbols with spaces before splitting. So its
    200 counts *prose*, not navigation: a page carrying 100 words of link text and 120 of copy is
    thin by that measure and comfortably over the line by a naive one.

    The gap is not marginal — python.org measures 1,024 words split naively and 592 SEONaut's way.
    Applying a borrowed number to a different quantity is how a check quietly stops firing.

    One deliberate deviation: SEONaut skips only ``<script>`` text, so stylesheet and ``<noscript>``
    content counts toward its total. Doxa strips all of ``NON_CONTENT``, because counting CSS as
    prose inflates the figure in the direction that hides thin pages.
    """
    cached = getattr(page, "_content_words", None)
    if cached is None:
        s = BeautifulSoup(page.html or "", "lxml")
        root = s.body or s
        for tag in root(NON_CONTENT):
            tag.decompose()
        for tag in root("a"):
            tag.decompose()
        text = root.get_text(" ")
        text = "".join(" " if unicodedata.category(ch)[0] in "PS" else ch for ch in text)
        cached = len(text.split())
        setattr(page, "_content_words", cached)
    return cached


@registry.register("content", "Content volume")
def check_content(page: Page) -> list[Finding]:
    s = soup(page)
    words = content_words(page)
    out: list[Finding] = []
    if words < 200:
        out.append(Finding("content.thin", Severity.HIGH if words < 50 else Severity.LOW,
                           f"The page has about {words} words of body copy, excluding link text. "
                           f"SEONaut treats under 200 as thin.", {"words": words}))
    # DOM size is a real cost for both browsers and models: an enormous DOM is slow to render and
    # eats a model's context before the content does.
    nodes = len(s.find_all(True))
    if nodes > 1500:
        out.append(Finding("dom.large", Severity.LOW,
                           f"The page has {nodes} HTML elements, which is heavy to render and to read.",
                           {"elements": nodes}))
    return out


# --- meta placement -------------------------------------------------------------------------------------

@registry.register("metaplace", "Meta tags in the body")
def check_meta_in_body(page: Page) -> list[Finding]:
    s = soup(page)
    body = s.body
    if body is None:
        return []
    stray = [m for m in body.find_all("meta")
             if (m.get("name") or "").lower() in ("description", "robots", "keywords", "viewport")]
    if stray:
        return [Finding("meta.in_body", Severity.HIGH,
                        f"{len(stray)} meta tag(s) that belong in the head are in the body, where "
                        f"they are ignored.",
                        {"count": len(stray),
                         "names": [(m.get("name") or "") for m in stray[:5]]})]
    return []


# --- URL shape ------------------------------------------------------------------------------------------

@registry.register("url", "URL shape", applies=is_html)
def check_url(page: Page) -> list[Finding]:
    out: list[Finding] = []
    p = urllib.parse.urlsplit(page.url)
    path = p.path or "/"
    if "_" in path:
        out.append(Finding("url.underscore", Severity.LOW,
                           "The URL uses underscores. Hyphens are read as word separators, "
                           "underscores are not.", {"path": path}))
    if " " in path or "%20" in path:
        out.append(Finding("url.space", Severity.LOW,
                           "The URL contains spaces.", {"path": path}))
    if "//" in path:
        out.append(Finding("url.double_slash", Severity.LOW,
                           "The URL path contains a double slash, which usually means a template "
                           "joined two parts badly.", {"path": path}))
    if len(page.url) > 200:
        out.append(Finding("url.long", Severity.LOW,
                           f"The URL is {len(page.url)} characters long.", {"length": len(page.url)}))
    return out


# --- security headers -------------------------------------------------------------------------------------

@registry.register("security", "Security headers")
def check_security(page: Page) -> list[Finding]:
    out: list[Finding] = []
    h = page.headers
    if page.url.lower().startswith("https://") and "strict-transport-security" not in h:
        out.append(Finding("header.hsts_missing", Severity.LOW,
                           "There is no Strict-Transport-Security header, so a first visit can be "
                           "downgraded to HTTP.", {}))
    if "content-security-policy" not in h:
        out.append(Finding("header.csp_missing", Severity.LOW,
                           "There is no Content-Security-Policy header.", {}))
    if h.get("x-content-type-options", "").lower() != "nosniff":
        out.append(Finding("header.nosniff_missing", Severity.LOW,
                           "X-Content-Type-Options is not set to nosniff, so browsers may guess the "
                           "content type.", {}))
    return out


# --- forms ---------------------------------------------------------------------------------------------------

@registry.register("forms", "Form security")
def check_forms(page: Page) -> list[Finding]:
    s = soup(page)
    out: list[Finding] = []
    on_https = page.url.lower().startswith("https://")
    for f in s.find_all("form"):
        action = (f.get("action") or "").strip()
        target = _abs(page.url, action) if action else page.url
        if target.lower().startswith("http://"):
            out.append(Finding("form.insecure_action", Severity.CRITICAL,
                               "A form posts to a plain HTTP address, so whatever is typed into it "
                               "travels unencrypted.", {"action": target}))
        elif not on_https:
            out.append(Finding("form.on_http", Severity.CRITICAL,
                               "There is a form on a page served over plain HTTP.",
                               {"page": page.url}))
    return out
