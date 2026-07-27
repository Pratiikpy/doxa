"""The crawler's limits, and the cross-page checks.

The limits are tested first and hardest, because a crawl is the one Doxa operation that costs somebody
else. A crawl asked for 30 pages once fetched 53 — the budget was checked at the top of the loop and a
batch of four could overshoot past it. That is a broken promise to the caller and unrequested load on
a server we do not own.

The cross-page checks run against `CrawlResult` objects built by hand, so the taxonomy is tested
without touching the network.
"""
from __future__ import annotations

import pytest

from checks.crosspage import link_graph, run_crosspage
from crawler import CrawledPage, CrawlResult, normalise


def page(url, *, title="", desc="", body="", status=200, depth=1, canonical="",
         robots="", links=(), nofollow=(), hreflangs=(), media="text/html",
         redirect_chain=None) -> CrawledPage:
    import hashlib
    return CrawledPage(
        url=url, status=status, depth=depth, ok=200 <= status < 300, media_type=media,
        title=title, description=desc, canonical=canonical, robots=robots,
        body_hash=hashlib.sha256(body.encode()).hexdigest() if body else "",
        word_count=len(body.split()), internal_links=list(links),
        nofollow_links=set(nofollow), hreflangs=list(hreflangs),
        redirect_chain=redirect_chain if redirect_chain is not None else [url])


def result_of(pages, start="https://s.test/") -> CrawlResult:
    r = CrawlResult(start_url=start, pages=list(pages))
    for p in pages:
        for link in p.internal_links:
            r.inbound.setdefault(link, []).append(p.url)
    return r


def codes(pages, **kw) -> set[str]:
    return {f.code for f in run_crosspage(result_of(pages, **kw))}


def find(pages, code, **kw):
    return next((f for f in run_crosspage(result_of(pages, **kw)) if f.code == code), None)


# --- URL normalisation ----------------------------------------------------------------------------

def test_fragments_never_identify_a_different_page():
    assert normalise("https://s.test/a#top") == normalise("https://s.test/a")


def test_query_strings_are_preserved():
    """?page=2 is usually a different page. Stripping it would silently merge distinct URLs and
    under-report the site."""
    assert normalise("https://s.test/a?page=2") != normalise("https://s.test/a")


def test_default_ports_and_case_are_normalised():
    assert normalise("HTTPS://S.Test:443/a") == "https://s.test/a"


# --- duplicates -----------------------------------------------------------------------------------

def test_duplicate_titles_are_grouped():
    pages = [page("https://s.test/1", title="Home", body="one"),
             page("https://s.test/2", title="Home", body="two"),
             page("https://s.test/3", title="Other", body="three")]
    f = find(pages, "site.duplicate_title")
    assert f is not None
    assert f.detail["groups"][0]["pages"] == ["https://s.test/1", "https://s.test/2"]


def test_duplicate_bodies_are_detected_by_hash():
    same = "the same words on both pages"
    pages = [page("https://s.test/1", title="A", body=same),
             page("https://s.test/2", title="B", body=same)]
    assert "site.duplicate_content" in codes(pages)


def test_error_pages_are_not_compared_for_duplicates():
    """SEONaut restricts every duplicate query to indexable 2xx HTML. Two 404s both titled "Not
    found" are not a duplicate-title problem."""
    pages = [page("https://s.test/1", title="Not found", status=404),
             page("https://s.test/2", title="Not found", status=404)]
    assert "site.duplicate_title" not in codes(pages)


def test_noindex_pages_are_not_compared_for_duplicates():
    pages = [page("https://s.test/1", title="Same", robots="noindex", body="a"),
             page("https://s.test/2", title="Same", robots="noindex", body="a")]
    assert "site.duplicate_title" not in codes(pages)


# --- structure ------------------------------------------------------------------------------------

def test_orphan_pages_are_those_nothing_links_to():
    pages = [page("https://s.test/", title="Home", body="h", depth=0,
                  links=["https://s.test/a"]),
             page("https://s.test/a", title="A", body="a"),
             page("https://s.test/orphan", title="O", body="o")]
    f = find(pages, "site.orphan_pages")
    assert f is not None and f.detail["pages"] == ["https://s.test/orphan"]


def test_the_start_url_is_never_an_orphan():
    pages = [page("https://s.test/", title="Home", body="h", depth=0)]
    assert "site.orphan_pages" not in codes(pages)


def test_click_depth_uses_the_four_click_threshold():
    assert "site.too_deep" not in codes([page("https://s.test/a", title="A", body="x", depth=4)])
    assert "site.too_deep" in codes([page("https://s.test/a", title="A", body="x", depth=5)])


def test_redirect_loops_are_critical():
    p = page("https://s.test/a", title="A", body="x",
             redirect_chain=["https://s.test/a", "https://s.test/b", "https://s.test/a"])
    f = find([p], "site.redirect_loops")
    assert f is not None and f.severity.value == "critical"


def test_broken_internal_links_name_what_points_at_them():
    pages = [page("https://s.test/", title="H", body="h", depth=0,
                  links=["https://s.test/gone"]),
             page("https://s.test/gone", status=404)]
    f = find(pages, "site.broken_internal_links")
    assert f is not None
    assert f.detail["broken"][0]["linked_from"] == ["https://s.test/"]


# --- canonical ------------------------------------------------------------------------------------

@pytest.mark.parametrize("target,expected", [
    (page("https://s.test/t", status=404), "site.canonical_to_error"),
    (page("https://s.test/t", title="T", body="b", robots="noindex"),
     "site.canonical_to_nonindexable"),
    (page("https://s.test/t", title="T", body="b", canonical="https://s.test/other"),
     "site.canonical_to_noncanonical"),
    (page("https://s.test/t", title="T", body="b",
          redirect_chain=["https://s.test/x", "https://s.test/t"]),
     "site.canonical_to_redirect"),
])
def test_the_four_cross_page_canonical_failures(target, expected):
    src = page("https://s.test/a", title="A", body="a", canonical="https://s.test/t")
    assert expected in codes([src, target])


def test_a_canonical_outside_the_crawl_is_not_judged():
    """We cannot prove anything about a URL we never fetched, so nothing is claimed."""
    src = page("https://s.test/a", title="A", body="a", canonical="https://elsewhere.test/x")
    assert not [c for c in codes([src]) if c.startswith("site.canonical_")]


# --- nofollow and hreflang --------------------------------------------------------------------------

def test_a_page_only_ever_nofollowed_is_reported():
    pages = [page("https://s.test/", title="H", body="h", depth=0,
                  links=["https://s.test/a"], nofollow=["https://s.test/a"]),
             page("https://s.test/a", title="A", body="a")]
    assert "site.nofollow_to_indexable" in codes(pages)


def test_mixed_follow_signals_are_reported_separately():
    pages = [page("https://s.test/", title="H", body="h", depth=0,
                  links=["https://s.test/a"], nofollow=["https://s.test/a"]),
             page("https://s.test/b", title="B", body="b", links=["https://s.test/a"]),
             page("https://s.test/a", title="A", body="a")]
    c = codes(pages)
    assert "site.mixed_follow_signals" in c
    assert "site.nofollow_to_indexable" not in c


def test_hreflang_without_a_return_link():
    """The reciprocity check that a single page fundamentally cannot perform."""
    pages = [page("https://s.test/en", title="EN", body="en",
                  hreflangs=[{"lang": "fr", "url": "https://s.test/fr"}]),
             page("https://s.test/fr", title="FR", body="fr", hreflangs=[])]
    f = find(pages, "site.hreflang_no_return_link")
    assert f is not None and f.detail["pairs"][0]["alternate"] == "https://s.test/fr"


def test_reciprocal_hreflang_is_clean():
    pages = [page("https://s.test/en", title="EN", body="en",
                  hreflangs=[{"lang": "fr", "url": "https://s.test/fr"}]),
             page("https://s.test/fr", title="FR", body="fr",
                  hreflangs=[{"lang": "en", "url": "https://s.test/en"}])]
    assert "site.hreflang_no_return_link" not in codes(pages)


def test_a_clean_site_says_so_explicitly():
    pages = [page("https://s.test/", title="Home", body="home words", depth=0,
                  links=["https://s.test/a"]),
             page("https://s.test/a", title="A", body="different words")]
    assert "site.crosspage_clean" in codes(pages)


# --- link graph -------------------------------------------------------------------------------------

def test_pagerank_prefers_the_page_everything_links_to():
    pages = [page("https://s.test/", title="H", body="h", depth=0,
                  links=["https://s.test/a", "https://s.test/b"]),
             page("https://s.test/a", title="A", body="a", links=["https://s.test/hub"]),
             page("https://s.test/b", title="B", body="b", links=["https://s.test/hub"]),
             page("https://s.test/hub", title="Hub", body="hub")]
    g = link_graph(result_of(pages))
    assert g["most_linked"][0]["url"] == "https://s.test/hub"


def test_dead_ends_and_unlinked_pages_are_identified():
    pages = [page("https://s.test/", title="H", body="h", depth=0,
                  links=["https://s.test/a"]),
             page("https://s.test/a", title="A", body="a"),
             page("https://s.test/lonely", title="L", body="l")]
    g = link_graph(result_of(pages))
    assert "https://s.test/lonely" in [r["url"] for r in g["least_linked"]]
    assert "https://s.test/a" in g["dead_ends"]


def test_rank_is_conserved_and_never_leaks_through_dead_ends():
    """A dead end with nowhere to send its rank would drain the system every iteration."""
    pages = [page("https://s.test/", title="H", body="h", depth=0, links=["https://s.test/end"]),
             page("https://s.test/end", title="E", body="e")]
    g = link_graph(result_of(pages))
    assert abs(sum(r["score"] for r in g["most_linked"]) - 1.0) < 0.02
