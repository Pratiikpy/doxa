"""What only a whole crawl can see.

Nine classes of fault are invisible from a single page, because they are relationships between pages.
A duplicate title is not a property of either page; an orphan page is defined by the absence of links
from everywhere else. Ported from SEONaut `internal/issues/multipage/`, whose reporters are SQL over
the crawl table — the same conditions, expressed over the crawl in memory:

    duplicated title / description / body      identical values across indexable HTML pages
    orphan pages                               no incoming internal link anywhere in the crawl
    click depth                                more than 4 clicks from the start
    redirect chains and loops                  more than one hop, or a repeat
    canonicalised to error/redirect/           a canonical pointing somewhere that cannot receive it
      non-canonical/non-indexable
    internal nofollow to an indexable page     the page is crawlable but the site refuses to vouch
    mixed follow and nofollow inbound          the same page linked both ways
    hreflang without a return link             the reciprocity that cannot be proven from one page

Duplicate comparison is restricted to indexable HTML pages that returned 2xx, exactly as SEONaut's
queries are. Counting a 404 and a 500 as "duplicate titles" because both say "Error" would be noise.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from checks.base import Finding, Severity
from crawler import CrawlResult, CrawledPage

MAX_CLICK_DEPTH = 4          # SEONaut: depth > 4
SAMPLE = 12                  # how many example URLs to attach to a finding


def _sample(urls: list[str]) -> list[str]:
    return sorted(urls)[:SAMPLE]


def _indexable_html(pages: list[CrawledPage]) -> list[CrawledPage]:
    return [p for p in pages if p.media_type == "text/html" and p.ok and p.indexable]


def _groups(pages: list[CrawledPage], key) -> dict[str, list[str]]:
    """Group indexable pages by a field, keeping only the values that repeat."""
    out: dict[str, list[str]] = defaultdict(list)
    for p in pages:
        v = (key(p) or "").strip()
        if v:
            out[v].append(p.url)
    return {v: urls for v, urls in out.items() if len(urls) > 1}


def run_crosspage(result: CrawlResult) -> list[Finding]:
    pages = result.pages
    indexable = _indexable_html(pages)
    by_url = result.by_url()
    out: list[Finding] = []

    # --- duplicates ------------------------------------------------------------------------------
    for field, label, code, severity in (
            ("title", "title", "site.duplicate_title", Severity.HIGH),
            ("description", "meta description", "site.duplicate_description", Severity.LOW)):
        dupes = _groups(indexable, lambda p, f=field: getattr(p, f))
        if dupes:
            worst = sorted(dupes.items(), key=lambda kv: -len(kv[1]))[:5]
            out.append(Finding(
                code, severity,
                f"{len(dupes)} {label}(s) are used on more than one page — "
                f"{sum(len(v) for v in dupes.values())} pages in total. Search engines cannot tell "
                f"these pages apart, and neither can a model choosing which one to cite.",
                {"groups": [{"value": v[:120], "pages": _sample(u)} for v, u in worst],
                 "distinct_duplicated_values": len(dupes)}))

    body_dupes = _groups(indexable, lambda p: p.body_hash)
    if body_dupes:
        worst = sorted(body_dupes.items(), key=lambda kv: -len(kv[1]))[:5]
        out.append(Finding(
            "site.duplicate_content", Severity.HIGH,
            f"{sum(len(v) for v in body_dupes.values())} pages share their body text with another "
            f"page. Duplicate content splits ranking signals between the copies and wastes crawl "
            f"budget on pages that add nothing.",
            {"groups": [{"pages": _sample(u), "words": by_url[u[0]].word_count} for _, u in worst],
             "duplicate_sets": len(body_dupes)}))

    # --- orphans ---------------------------------------------------------------------------------
    # Only meaningful for pages reached some other way than a link — here, from a sitemap or the
    # start URL. Within a link-following crawl every page except the start has an inbound link by
    # construction, so this reports what the crawl *proves*: pages nothing links to.
    linked = set(result.inbound)
    orphans = [p.url for p in indexable if p.url not in linked and p.url != result.start_url]
    if orphans:
        out.append(Finding(
            "site.orphan_pages", Severity.HIGH,
            f"{len(orphans)} page(s) have no incoming internal link anywhere in the crawl. Nothing "
            f"points at them, so a crawler arriving at your home page will never reach them.",
            {"pages": _sample(orphans), "count": len(orphans)}))

    # --- click depth -----------------------------------------------------------------------------
    deep = [p.url for p in indexable if p.depth > MAX_CLICK_DEPTH]
    if deep:
        out.append(Finding(
            "site.too_deep", Severity.LOW,
            f"{len(deep)} page(s) are more than {MAX_CLICK_DEPTH} clicks from the start. Pages that "
            f"far down are crawled less often and treated as less important.",
            {"pages": _sample(deep), "max_depth": max(p.depth for p in indexable)}))

    # --- redirects -------------------------------------------------------------------------------
    chains = [p for p in pages if len(p.redirect_chain) > 2]
    if chains:
        out.append(Finding(
            "site.redirect_chains", Severity.HIGH,
            f"{len(chains)} URL(s) redirect more than once before serving a page. Each extra hop "
            f"costs time and loses a little of the signal being passed.",
            {"chains": [{"url": p.url, "hops": p.redirect_chain[:6]} for p in chains[:SAMPLE]]}))
    loops = [p for p in pages if len(p.redirect_chain) != len(set(p.redirect_chain))]
    if loops:
        out.append(Finding(
            "site.redirect_loops", Severity.CRITICAL,
            f"{len(loops)} URL(s) redirect in a loop and never resolve to a page.",
            {"urls": _sample([p.url for p in loops])}))

    # --- canonical, the four cross-page failures -------------------------------------------------
    to_error, to_redirect, to_noncanonical, to_nonindexable = [], [], [], []
    for p in pages:
        if not p.canonical or p.canonical == p.url:
            continue
        target = by_url.get(p.canonical)
        if target is None:
            continue                      # outside the crawl; not provable, so not claimed
        row = {"page": p.url, "canonical": p.canonical, "target_status": target.status}
        if not target.ok:
            to_error.append(row)
        elif len(target.redirect_chain) > 1:
            to_redirect.append(row)
        elif target.canonical and target.canonical != target.url:
            to_noncanonical.append(row | {"target_canonical": target.canonical})
        elif not target.indexable:
            to_nonindexable.append(row)

    for rows, code, severity, message in (
            (to_error, "site.canonical_to_error", Severity.CRITICAL,
             "point at a URL that does not load. The canonical names the version to index, so the "
             "page is nominating a broken URL and may drop out entirely."),
            (to_redirect, "site.canonical_to_redirect", Severity.HIGH,
             "point at a URL that redirects. A canonical should name the final URL directly."),
            (to_noncanonical, "site.canonical_to_noncanonical", Severity.HIGH,
             "point at a page that itself canonicalises somewhere else. A chain of canonicals is "
             "resolved unpredictably."),
            (to_nonindexable, "site.canonical_to_nonindexable", Severity.CRITICAL,
             "point at a page marked noindex. The page nominates a version that must not be "
             "indexed, so neither gets indexed.")):
        if rows:
            out.append(Finding(code, severity,
                               f"{len(rows)} canonical tag(s) {message}",
                               {"pages": rows[:SAMPLE], "count": len(rows)}))

    # --- internal nofollow and mixed signals -----------------------------------------------------
    nofollowed_to: dict[str, set[str]] = defaultdict(set)
    followed_to: dict[str, set[str]] = defaultdict(set)
    for p in pages:
        for link in set(p.internal_links):
            (nofollowed_to if link in p.nofollow_links else followed_to)[link].add(p.url)

    nofollow_indexable = [u for u in nofollowed_to
                          if u in by_url and by_url[u].indexable and u not in followed_to]
    if nofollow_indexable:
        out.append(Finding(
            "site.nofollow_to_indexable", Severity.HIGH,
            f"{len(nofollow_indexable)} indexable page(s) are only ever linked with nofollow. The "
            f"pages are meant to be indexed, but the site declines to pass any value to them.",
            {"pages": _sample(nofollow_indexable)}))

    mixed = [u for u in followed_to if u in nofollowed_to]
    if mixed:
        out.append(Finding(
            "site.mixed_follow_signals", Severity.LOW,
            f"{len(mixed)} page(s) are linked with nofollow from some pages and without it from "
            f"others. The inconsistency is usually a template bug rather than a decision.",
            {"pages": _sample(mixed)}))

    # --- hreflang reciprocity, the check that needs the whole crawl ------------------------------
    missing_return, hreflang_broken, hreflang_noindex = [], [], []
    for p in pages:
        for alt in p.hreflangs:
            target = by_url.get(alt["url"])
            if target is None:
                continue
            if not target.ok:
                hreflang_broken.append({"page": p.url, "alternate": alt["url"],
                                        "status": target.status})
                continue
            if not target.indexable:
                hreflang_noindex.append({"page": p.url, "alternate": alt["url"]})
            if not any(back["url"] == p.url for back in target.hreflangs):
                missing_return.append({"page": p.url, "alternate": alt["url"],
                                       "lang": alt["lang"]})
    if missing_return:
        out.append(Finding(
            "site.hreflang_no_return_link", Severity.HIGH,
            f"{len(missing_return)} hreflang alternate(s) do not link back. hreflang must be "
            f"reciprocal — if B does not name A, search engines discard the whole set.",
            {"pairs": missing_return[:SAMPLE]}))
    if hreflang_broken:
        out.append(Finding("site.hreflang_to_error", Severity.HIGH,
                           f"{len(hreflang_broken)} hreflang alternate(s) point at a URL that does "
                           f"not load.", {"pairs": hreflang_broken[:SAMPLE]}))
    if hreflang_noindex:
        out.append(Finding("site.hreflang_to_nonindexable", Severity.HIGH,
                           f"{len(hreflang_noindex)} hreflang alternate(s) point at a noindex page.",
                           {"pairs": hreflang_noindex[:SAMPLE]}))

    # --- broken internal links -------------------------------------------------------------------
    broken: dict[str, list[str]] = defaultdict(list)
    for p in pages:
        if p.ok or p.media_type == "" and not p.status:
            continue
        if p.status >= 400 or p.status == 0:
            broken[p.url] = sorted(set(result.inbound.get(p.url, [])))[:6]
    if broken:
        out.append(Finding(
            "site.broken_internal_links", Severity.CRITICAL,
            f"{len(broken)} internal URL(s) do not load. Every link pointing at them is a visitor "
            f"hitting a dead end.",
            {"broken": [{"url": u, "status": by_url[u].status, "linked_from": src}
                        for u, src in list(broken.items())[:SAMPLE]]}))

    # --- an explicit note when nothing is wrong ---------------------------------------------------
    if not out:
        out.append(Finding("site.crosspage_clean", Severity.INFO,
                           f"No cross-page faults found across {len(pages)} crawled page(s): no "
                           f"duplicate titles, descriptions or bodies, no orphans, no redirect "
                           f"chains and no canonical conflicts.",
                           {"pages_crawled": len(pages)}))
    return out


def link_graph(result: CrawlResult) -> dict[str, Any]:
    """The internal link structure, and the pages the structure fails.

    PageRank-style flow is computed here rather than approximated by counting links: a page linked
    once from the home page usually matters more than one linked ten times from the footer, and only
    an iterative computation reflects that.
    """
    pages = [p for p in result.pages if p.media_type == "text/html" and p.ok]
    urls = [p.url for p in pages]
    index = {u: i for i, u in enumerate(urls)}
    n = len(urls)
    if n == 0:
        return {"pages": 0}

    outbound: list[list[int]] = [[] for _ in range(n)]
    for p in pages:
        for link in set(p.internal_links):
            j = index.get(link)
            if j is not None and j != index[p.url]:
                outbound[index[p.url]].append(j)

    # Damped iterative PageRank. 30 passes is well past convergence at these sizes.
    damping, rank = 0.85, [1.0 / n] * n
    for _ in range(30):
        nxt = [(1 - damping) / n] * n
        for i, targets in enumerate(outbound):
            if not targets:
                # A dead end would leak its rank out of the system; spread it instead.
                share = damping * rank[i] / n
                nxt = [v + share for v in nxt]
                continue
            share = damping * rank[i] / len(targets)
            for j in targets:
                nxt[j] += share
        rank = nxt

    inbound_counts = {u: len(set(result.inbound.get(u, []))) for u in urls}
    ranked = sorted(({"url": u, "score": round(rank[i], 5),
                      "inbound_links": inbound_counts[u],
                      "outbound_links": len(set(outbound[i])),
                      "depth": pages[i].depth} for i, u in enumerate(urls)),
                    key=lambda r: -r["score"])
    return {"pages": n,
            "most_linked": ranked[:15],
            "least_linked": [r for r in ranked if r["inbound_links"] == 0][:15],
            "dead_ends": [r["url"] for r in ranked if r["outbound_links"] == 0][:15],
            "method": "damped PageRank over internal links, 30 iterations, d=0.85"}
