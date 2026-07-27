"""hreflang — the tag almost nobody gets right.

Ported from SEONaut `internal/issues/page/hreflangs.go`. Their four single-page failures are the ones
provable from one document:

  * no `x-default`
  * no self-reference — the page must list itself among the alternates
  * the declared `hreflang` for the self-reference disagrees with `<html lang>`
  * a relative URL, which search engines will not follow across locales

Reciprocity — B must point back at A — is genuinely cross-page and belongs to the site crawl, not here.
Reporting it from a single page would mean guessing, so this file does not: it says which pages would
have to be fetched to prove it.

Codes are also read from the `Link:` HTTP header, which is the correct place to declare hreflang for a
PDF and is where sites that do it properly put it. SEONaut parses only the markup.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

from checks.base import Finding, Severity, registry
from checks.page_html import soup
from fetch import Page

# BCP-47: a language subtag, optional script, optional region. Deliberately not a full validator —
# it rejects the mistakes people actually make (`en_US`, `english`, `en-USA`) and allows valid tags.
#
# Matched case-insensitively, because BCP-47 says so (RFC 5646 §2.1.1: "All language tags are to be
# treated as case-insensitive"). `zh-Hans` is the recommended casing, not a requirement — an earlier
# case-sensitive version of this pattern reported react.dev's perfectly valid `zh-hans` as malformed,
# which is exactly the kind of confidently-wrong finding a signed audit cannot afford.
BCP47 = re.compile(r"^[a-z]{2,3}(-[a-z]{4})?(-([a-z]{2}|\d{3}))?$", re.I)
LINK_HEADER = re.compile(r'<([^>]+)>\s*;\s*rel\s*=\s*"?alternate"?[^,]*?hreflang\s*=\s*"?([^";,]+)"?',
                         re.I)


def collect_hreflangs(page: Page) -> list[dict[str, Any]]:
    s = soup(page)
    base = page.url or page.requested_url
    out: list[dict[str, Any]] = []
    for link in s.find_all("link"):
        rels = link.get("rel") or []
        rels = [r.lower() for r in (rels if isinstance(rels, list) else [rels])]
        if "alternate" not in rels:
            continue
        lang = (link.get("hreflang") or "").strip()
        if not lang:
            continue
        href = (link.get("href") or "").strip()
        out.append({"lang": lang, "href": href,
                    "url": urllib.parse.urljoin(base, href) if href else "",
                    "absolute": bool(urllib.parse.urlsplit(href).scheme),
                    "source": "html"})
    for m in LINK_HEADER.finditer(page.headers.get("link", "") or ""):
        href, lang = m.group(1).strip(), m.group(2).strip()
        out.append({"lang": lang, "href": href,
                    "url": urllib.parse.urljoin(base, href),
                    "absolute": bool(urllib.parse.urlsplit(href).scheme),
                    "source": "link-header"})
    return out


def _same(a: str, b: str) -> bool:
    """URL equality for this purpose: scheme and host case-insensitive, trailing slash ignored."""
    def norm(u: str) -> str:
        p = urllib.parse.urlsplit(u)
        return f"{p.scheme.lower()}://{(p.hostname or '').lower()}{(p.path or '/').rstrip('/') or '/'}" \
               f"{('?' + p.query) if p.query else ''}"
    return norm(a) == norm(b)


@registry.register("hreflang", "International targeting")
def check_hreflang(page: Page) -> list[Finding]:
    tags = collect_hreflangs(page)
    if not tags:
        # No hreflang is correct for a single-language site. Saying nothing is right; claiming a
        # fault would be noise on the great majority of pages.
        return []

    s = soup(page)
    html_lang = ((s.html.get("lang") if s.html else "") or "").strip()
    page_url = page.url or page.requested_url
    out: list[Finding] = []

    langs = [t["lang"] for t in tags]
    if not any(l.lower() == "x-default" for l in langs):
        out.append(Finding("hreflang.no_xdefault", Severity.LOW,
                           "There is no x-default alternate, so a visitor whose language matches none "
                           "of the listed ones has no defined page to land on.",
                           {"languages": langs}))

    self_refs = [t for t in tags if t["url"] and _same(t["url"], page_url)]
    if not self_refs:
        out.append(Finding("hreflang.no_self_reference", Severity.HIGH,
                           "The page lists alternates but does not list itself. Every page in an "
                           "hreflang set must include a self-reference or search engines discard the "
                           "whole set.",
                           {"page": page_url, "alternates": [t["url"] for t in tags[:10]]}))
    elif html_lang:
        mismatched = [t for t in self_refs
                      if t["lang"].lower() != "x-default"
                      and t["lang"].lower() != html_lang.lower()]
        if mismatched:
            out.append(Finding("hreflang.lang_mismatch", Severity.HIGH,
                               f"The page declares <html lang=\"{html_lang}\"> but its own hreflang "
                               f"entry says \"{mismatched[0]['lang']}\". One of the two is wrong.",
                               {"html_lang": html_lang,
                                "hreflang": [t["lang"] for t in mismatched]}))

    relative = [t for t in tags if t["href"] and not t["absolute"]]
    if relative:
        out.append(Finding("hreflang.relative_url", Severity.HIGH,
                           f"{len(relative)} hreflang URL(s) are relative. hreflang must use absolute "
                           f"URLs including the scheme and host, or they are ignored.",
                           {"hrefs": [t["href"] for t in relative[:10]]}))

    malformed = [t for t in tags
                 if t["lang"].lower() != "x-default" and not BCP47.match(t["lang"])]
    if malformed:
        out.append(Finding("hreflang.malformed_code", Severity.HIGH,
                           f"{len(malformed)} hreflang value(s) are not valid language codes: "
                           f"{', '.join(t['lang'] for t in malformed[:5])}. The usual mistakes are an "
                           f"underscore instead of a hyphen and a country name instead of its code.",
                           {"codes": [t["lang"] for t in malformed[:10]]}))

    seen: dict[str, int] = {}
    for t in tags:
        seen[t["lang"].lower()] = seen.get(t["lang"].lower(), 0) + 1
    dupes = {k: v for k, v in seen.items() if v > 1}
    if dupes:
        out.append(Finding("hreflang.duplicate_code", Severity.HIGH,
                           f"{len(dupes)} language code(s) are declared more than once, pointing at "
                           f"different URLs. Search engines cannot tell which is correct.",
                           {"duplicates": dupes}))

    missing_href = [t for t in tags if not t["href"]]
    if missing_href:
        out.append(Finding("hreflang.missing_href", Severity.HIGH,
                           f"{len(missing_href)} alternate(s) declare a language but no href.",
                           {"languages": [t["lang"] for t in missing_href[:10]]}))

    if not out:
        out.append(Finding("hreflang.ok", Severity.INFO,
                           f"{len(tags)} hreflang alternate(s) declared and internally consistent. "
                           f"Whether each target links back is a cross-page question — a site audit "
                           f"proves that.",
                           {"languages": langs, "reciprocity_requires": [t["url"] for t in tags[:10]]}))
    return out
