"""Sitemaps — and, more usefully, the difference between the sitemap and the site.

A sitemap that parses is not the finding. The finding is the disagreement: URLs listed but not
reachable by any link (orphans the site is trying to force-feed), and URLs reachable by link but never
listed (pages the site forgot to declare). Neither is visible from the file alone.

Index sitemaps are followed one level, which is all the spec allows anyway, and both plain XML and
gzip are handled because a large site will always ship `.xml.gz`.
"""
from __future__ import annotations

import gzip
import re
import urllib.parse
from typing import Any

from bs4 import BeautifulSoup

from checks.base import Finding, Severity
from crawler import CrawlResult, normalise
from fetch import fetch

MAX_SITEMAP_URLS = 50_000        # the spec's own per-file limit
MAX_SITEMAP_BYTES = 50 * 1024 * 1024


def _text_of(page) -> str:
    """Sitemaps are frequently served gzipped, sometimes without a matching content-type."""
    raw = (page.html or "")
    if raw.startswith("\x1f\x8b") or page.url.endswith(".gz"):
        try:
            return gzip.decompress(raw.encode("latin-1", "ignore")).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return raw
    return raw


def parse_sitemap(url: str, *, depth: int = 0, timeout: int = 20) -> dict[str, Any]:
    """Return the URLs a sitemap declares, following an index one level."""
    out: dict[str, Any] = {"url": url, "urls": [], "children": [], "errors": [], "kind": "unknown"}
    try:
        page = fetch(url, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"could not be fetched: {type(e).__name__}")
        return out
    if not page.ok:
        out["errors"].append(f"returned HTTP {page.status}")
        return out

    body = _text_of(page)
    if len(body) > MAX_SITEMAP_BYTES:
        out["errors"].append("larger than the 50 MB limit the sitemap spec sets")
        return out

    soup = BeautifulSoup(body, "xml") if "<" in body[:200] else None
    if soup is None or not soup.find():
        # A plain-text sitemap is permitted: one URL per line.
        lines = [l.strip() for l in body.splitlines() if l.strip().startswith("http")]
        if lines:
            out["kind"] = "text"
            out["urls"] = [normalise(u) for u in lines[:MAX_SITEMAP_URLS]]
            return out
        out["errors"].append("is neither valid XML nor a plain list of URLs")
        return out

    if soup.find("sitemapindex"):
        out["kind"] = "index"
        for loc in soup.find_all("loc")[:1000]:
            child = (loc.get_text() or "").strip()
            if not child:
                continue
            if depth == 0:
                out["children"].append(parse_sitemap(child, depth=1, timeout=timeout))
            else:
                out["errors"].append(f"nests more than one level deep at {child}")
        for c in out["children"]:
            out["urls"].extend(c["urls"])
            out["errors"].extend(f"{c['url']}: {e}" for e in c["errors"])
        return out

    out["kind"] = "urlset"
    seen: list[str] = []
    for u in soup.find_all("url")[:MAX_SITEMAP_URLS]:
        loc = u.find("loc")
        if loc is None or not (loc.get_text() or "").strip():
            out["errors"].append("a <url> entry has no <loc>")
            continue
        raw = loc.get_text().strip()
        if not raw.startswith(("http://", "https://")):
            out["errors"].append(f"relative URL in <loc>: {raw[:80]}")
            continue
        seen.append(normalise(raw))
    out["urls"] = seen
    return out


def check_sitemap(site_url: str, crawl: CrawlResult | None = None,
                  declared: list[str] | None = None) -> tuple[dict[str, Any], list[Finding]]:
    """Find the sitemaps, parse them, and diff them against the crawl when one is available."""
    p = urllib.parse.urlsplit(site_url)
    root = f"{p.scheme}://{p.netloc}"
    candidates = list(declared or [])
    if not candidates:
        candidates = [f"{root}/sitemap.xml", f"{root}/sitemap_index.xml"]

    parsed = [parse_sitemap(u) for u in candidates[:10]]
    found = [s for s in parsed if s["urls"] or s["kind"] != "unknown"]
    findings: list[Finding] = []

    if not found:
        return ({"sitemaps": [], "urls": 0},
                [Finding("sitemap.missing", Severity.HIGH,
                         "No sitemap was found at the usual locations and none is declared in "
                         "robots.txt. A sitemap is how a crawler discovers pages that are not "
                         "well linked.",
                         {"tried": candidates})])

    all_urls: list[str] = []
    for s in found:
        all_urls.extend(s["urls"])
    unique = sorted(set(all_urls))
    report: dict[str, Any] = {
        "sitemaps": [{"url": s["url"], "kind": s["kind"], "urls": len(s["urls"]),
                      "errors": s["errors"][:10]} for s in found],
        "urls": len(unique),
        "duplicate_entries": len(all_urls) - len(unique),
    }

    errors = [e for s in found for e in s["errors"]]
    if errors:
        findings.append(Finding("sitemap.malformed", Severity.HIGH,
                                f"The sitemap has {len(errors)} structural problem(s). Entries a "
                                f"parser rejects are simply not crawled.",
                                {"errors": errors[:15]}))
    if report["duplicate_entries"]:
        findings.append(Finding("sitemap.duplicates", Severity.LOW,
                                f"{report['duplicate_entries']} URL(s) are listed more than once.",
                                {"duplicates": report["duplicate_entries"]}))

    if crawl is not None:
        crawled = {p.url for p in crawl.pages if p.media_type == "text/html"}
        indexable = {p.url for p in crawl.pages if p.indexable}
        listed = set(unique)

        not_reachable = sorted(listed - crawled)
        not_listed = sorted(indexable - listed)
        non_indexable_listed = sorted(
            u for u in listed & crawled if not crawl.by_url()[u].indexable)
        broken_listed = sorted(
            u for u in listed & crawled if not crawl.by_url()[u].ok)

        report |= {"listed_and_crawled": len(listed & crawled),
                   "listed_not_reached_by_link": len(not_reachable),
                   "crawled_not_listed": len(not_listed)}

        if broken_listed:
            findings.append(Finding(
                "sitemap.lists_broken_urls", Severity.HIGH,
                f"The sitemap lists {len(broken_listed)} URL(s) that do not load. A sitemap is a "
                f"declaration that these pages are worth crawling.",
                {"urls": broken_listed[:12]}))
        if non_indexable_listed:
            findings.append(Finding(
                "sitemap.lists_noindex", Severity.HIGH,
                f"The sitemap lists {len(non_indexable_listed)} page(s) marked noindex — the site "
                f"is asking for them to be crawled and refusing to let them be indexed.",
                {"urls": non_indexable_listed[:12]}))
        if not_listed:
            findings.append(Finding(
                "sitemap.incomplete", Severity.LOW,
                f"{len(not_listed)} indexable page(s) found by crawling are not in the sitemap.",
                {"urls": not_listed[:12], "count": len(not_listed)}))
        if not_reachable and not crawl.truncated:
            # Only claimed when the crawl finished: a truncated crawl has not proven these
            # unreachable, only that it did not get to them.
            findings.append(Finding(
                "sitemap.orphans", Severity.LOW,
                f"{len(not_reachable)} URL(s) in the sitemap were never reached by following links. "
                f"They are only discoverable through the sitemap itself.",
                {"urls": not_reachable[:12], "count": len(not_reachable)}))
        elif not_reachable:
            findings.append(Finding(
                "sitemap.unverified", Severity.INFO,
                f"{len(not_reachable)} sitemap URL(s) were not visited because the crawl stopped "
                f"early ({crawl.stopped_because}), so whether links reach them is unproven.",
                {"count": len(not_reachable)}))

    if not findings:
        findings.append(Finding("sitemap.ok", Severity.INFO,
                                f"The sitemap parses cleanly and lists {len(unique)} URL(s).",
                                {"urls": len(unique)}))
    return report, findings
