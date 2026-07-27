"""Links — the ones on the page, and what is actually at the other end.

Two halves, deliberately separated.

The registry checks below are offline: they read the markup and judge shape. Ported from SEONaut
`internal/issues/page/links.go`, thresholds verbatim — over 100 links on a page, internal `nofollow`,
external links without `nofollow`, `http://` links on an `https://` page, a page with no links at all,
and links pointing at localhost (a staging leak that ships to production more often than anyone admits).

Resolving every link over the network is the other half, and it is a paid service rather than a check,
because it costs real time and real requests. It lives in `resolve_links` and every hop goes through
the same SSRF guard as the page fetch: a link on an untrusted page is attacker-controlled input, and
following it blindly is exactly the request-forgery this service must not make.
"""
from __future__ import annotations

import concurrent.futures
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

from checks.base import Finding, Severity, registry
from checks.page_html import soup
from fetch import Page, SsrfError, guard_url, head

# Anchors that never point at a document.
SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "sms:", "data:", "callto:", "whatsapp:")


@dataclass
class Link:
    href: str                 # exactly as written in the markup
    url: str                  # resolved against the page URL
    text: str
    rel: str = ""
    internal: bool = False
    nofollow: bool = False
    # filled in only by resolve_links()
    status: int = 0
    error: str = ""
    final_url: str = ""

    def as_dict(self) -> dict[str, Any]:
        d = {"href": self.href, "url": self.url, "text": self.text[:120],
             "internal": self.internal, "nofollow": self.nofollow}
        if self.status or self.error:
            d |= {"status": self.status, "error": self.error, "final_url": self.final_url}
        return d


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _registrable(hostname: str) -> str:
    """Enough of the host to decide 'same site'.

    `www.example.com` and `example.com` are the same site to a human and to a search engine, so a
    naive host equality test would report every canonical link as external. This takes the last two
    labels, which is right for the common case and wrong only for multi-label public suffixes
    (`co.uk`); being wrong there means calling a same-site link internal, which is the harmless
    direction.
    """
    parts = hostname.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else hostname


def extract_links(page: Page, *, rendered: bool = False) -> list[Link]:
    """Every anchor with an href, resolved against the page's own URL.

    Cached on the page: link extraction feeds several checks and the paid resolver, and parsing the
    same anchors five times is waste the caller pays for.
    """
    attr = "_links_rendered" if rendered else "_links"
    cached = getattr(page, attr, None)
    if cached is not None:
        return cached

    s = soup(page, rendered=rendered)
    base_url = page.url or page.requested_url
    # A <base href> silently re-points every relative link on the page. Ignoring it makes every
    # resolved URL wrong on the sites that use one.
    base_tag = s.find("base", href=True)
    if base_tag:
        base_url = urllib.parse.urljoin(base_url, base_tag["href"].strip())
    page_host = _registrable(_host(base_url))

    out: list[Link] = []
    for a in s.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if not href or href.startswith("#"):
            continue
        if href.lower().startswith(SKIP_SCHEMES):
            continue
        try:
            resolved = urllib.parse.urljoin(base_url, href)
        except ValueError:
            continue
        scheme = urllib.parse.urlsplit(resolved).scheme.lower()
        if scheme not in ("http", "https"):
            continue
        rel = " ".join(a.get("rel") or []).lower() if isinstance(a.get("rel"), list) else \
            (a.get("rel") or "").lower()
        out.append(Link(href=href, url=resolved, text=(a.get_text() or "").strip(), rel=rel,
                        internal=_registrable(_host(resolved)) == page_host,
                        nofollow="nofollow" in rel))
    setattr(page, attr, out)
    return out


def _page_is_nofollow(page: Page) -> bool:
    """A page-level `<meta name=robots content=nofollow>` makes every link on it nofollow."""
    s = soup(page)
    for m in s.find_all("meta"):
        if (m.get("name") or "").lower() in ("robots", "googlebot"):
            if "nofollow" in (m.get("content") or "").lower():
                return True
    return "nofollow" in (page.headers.get("x-robots-tag", "") or "").lower()


@registry.register("links", "Links on the page")
def check_links(page: Page) -> list[Finding]:
    links = extract_links(page)
    internal = [l for l in links if l.internal]
    external = [l for l in links if not l.internal]
    page_nofollow = _page_is_nofollow(page)
    out: list[Finding] = []

    if not links:
        out.append(Finding("links.deadend", Severity.HIGH,
                           "The page has no links at all, so a crawler that arrives here has nowhere "
                           "to go and the page passes no value on.", {}))
    if len(links) > 100:
        out.append(Finding("links.too_many", Severity.LOW,
                           f"There are {len(links)} links on the page. Over 100 dilutes the value each "
                           f"one carries (SEONaut's threshold).",
                           {"count": len(links), "internal": len(internal),
                            "external": len(external)}))

    nofollowed_internal = [l for l in internal if l.nofollow]
    if page_nofollow and internal:
        out.append(Finding("links.internal_nofollow", Severity.HIGH,
                           f"The page is marked nofollow, so all {len(internal)} internal links are "
                           f"ignored and the pages they point to may never be found.",
                           {"source": "meta robots", "internal": len(internal)}))
    elif nofollowed_internal:
        out.append(Finding("links.internal_nofollow", Severity.LOW,
                           f"{len(nofollowed_internal)} internal link(s) are nofollow, so they pass no "
                           f"value to your own pages.",
                           {"links": [l.as_dict() for l in nofollowed_internal[:10]],
                            "count": len(nofollowed_internal)}))

    followed_external = [l for l in external if not l.nofollow]
    if followed_external and not page_nofollow:
        out.append(Finding("links.external_dofollow", Severity.INFO,
                           f"{len(followed_external)} external link(s) are followed, passing value off "
                           f"the site. That is normal for editorial links and a problem for "
                           f"user-generated ones.",
                           {"count": len(followed_external),
                            "sample": [l.as_dict() for l in followed_external[:10]]}))

    if (page.url or "").lower().startswith("https://"):
        insecure = [l for l in links if l.url.lower().startswith("http://")]
        if insecure:
            out.append(Finding("links.http_on_https", Severity.HIGH,
                               f"{len(insecure)} link(s) point at plain http:// from an https:// page. "
                               f"Each one is an extra redirect at best and a downgrade at worst.",
                               {"links": [l.as_dict() for l in insecure[:10]],
                                "count": len(insecure)}))

    page_host = _host(page.url or page.requested_url)
    if page_host not in ("localhost", "127.0.0.1"):
        local = [l for l in links if _host(l.url) in ("localhost", "127.0.0.1", "0.0.0.0", "::1")]
        if local:
            out.append(Finding("links.localhost", Severity.HIGH,
                               f"{len(local)} link(s) point at localhost. These work on the developer's "
                               f"machine and are broken for every visitor.",
                               {"links": [l.as_dict() for l in local[:10]]}))

    empty_anchor = [l for l in links if not l.text and not _has_image_child(page, l)]
    if empty_anchor:
        out.append(Finding("links.empty_anchor", Severity.LOW,
                           f"{len(empty_anchor)} link(s) have no text and no image, so neither a "
                           f"screen reader nor a crawler can tell what they point to.",
                           {"count": len(empty_anchor),
                            "sample": [l.as_dict() for l in empty_anchor[:10]]}))
    return out


def _has_image_child(page: Page, link: Link) -> bool:
    """An empty anchor wrapping an image is normal markup, not a fault — check before reporting."""
    s = soup(page)
    for a in s.find_all("a", href=True):
        if (a["href"] or "").strip() == link.href:
            return bool(a.find(["img", "svg", "picture", "video"]))
    return False


# --- the paid half: actually go and look --------------------------------------------------------

@dataclass
class LinkReport:
    checked: int = 0
    broken: list[dict] = field(default_factory=list)
    redirecting: list[dict] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    ok: int = 0
    # Stated rather than inferred. A page can hold 200 anchors pointing at 150 distinct URLs, and
    # checking 150 of them is full coverage, not a truncated run — comparing `checked` against the
    # raw anchor count would report a complete audit as incomplete.
    unique_found: int = 0
    skipped: int = 0

    @property
    def truncated(self) -> bool:
        return self.skipped > 0

    def as_dict(self) -> dict[str, Any]:
        return {"checked": self.checked, "ok": self.ok, "unique_found": self.unique_found,
                "skipped": self.skipped, "truncated": self.truncated,
                "broken": self.broken, "redirecting": self.redirecting, "blocked": self.blocked}


def resolve_links(page: Page, *, limit: int = 150, workers: int = 8,
                  timeout: int = 12) -> tuple[LinkReport, list[Finding]]:
    """Request every link and report what came back.

    Concurrency is bounded and so is the link count: this runs inside a paid request with a deadline,
    and a page with 4,000 links must not be able to turn one payment into 4,000 outbound requests.
    When the cap truncates, the report says so — a silent cap would read as "all links fine".

    Every URL is guarded again here even though it came off a fetched page. The page may be hostile;
    a link to `http://169.254.169.254/` is the metadata-service attack, and "we already fetched the
    page it was on" is not a reason to follow it.
    """
    links = extract_links(page)
    seen: dict[str, Link] = {}
    for l in links:
        seen.setdefault(l.url.split("#")[0], l)
    targets = list(seen.values())
    unique_found = len(targets)
    truncated = max(0, unique_found - limit)
    targets = targets[:limit]

    report = LinkReport(unique_found=unique_found, skipped=truncated)

    def probe(link: Link) -> Link:
        try:
            guard_url(link.url)
        except SsrfError as e:
            link.error = f"refused: {e}"
            return link
        except Exception as e:  # noqa: BLE001
            link.error = str(e)
            return link
        try:
            r = head(link.url, timeout=timeout)
            link.status = int(r.get("status") or 0)
            link.final_url = r.get("url") or link.url
        except Exception as e:  # noqa: BLE001
            link.error = type(e).__name__
        return link

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for link in pool.map(probe, targets):
            report.checked += 1
            if link.error:
                report.blocked.append(link.as_dict())
            elif link.status >= 400 or link.status == 0:
                report.broken.append(link.as_dict())
            elif 300 <= link.status < 400 or (link.final_url and
                                              link.final_url.split("#")[0] != link.url.split("#")[0]):
                report.redirecting.append(link.as_dict())
            else:
                report.ok += 1

    findings: list[Finding] = []
    if report.broken:
        internal_broken = [b for b in report.broken if b["internal"]]
        findings.append(Finding(
            "links.broken", Severity.CRITICAL if internal_broken else Severity.HIGH,
            f"{len(report.broken)} link(s) are broken"
            + (f", {len(internal_broken)} of them to your own pages" if internal_broken else "")
            + ". Every one is a visitor who hits a dead end.",
            {"broken": report.broken[:25], "count": len(report.broken)}))
    if report.redirecting:
        findings.append(Finding("links.redirecting", Severity.LOW,
                                f"{len(report.redirecting)} link(s) redirect rather than pointing at "
                                f"the final URL. Pointing them straight there saves a round trip.",
                                {"redirecting": report.redirecting[:25],
                                 "count": len(report.redirecting)}))
    if truncated:
        findings.append(Finding("links.truncated", Severity.INFO,
                                f"Only the first {limit} unique links were checked; {truncated} were "
                                f"not. Ask for a site crawl to cover them all.",
                                {"checked": limit, "skipped": truncated}))
    if not report.broken and report.checked:
        findings.append(Finding("links.all_ok", Severity.INFO,
                                f"All {report.checked} link(s) checked resolve successfully.",
                                {"checked": report.checked}))
    return report, findings
