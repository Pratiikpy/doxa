"""Keywords, demand and citations.

The rule these all serve: **a source that failed is never reported as a zero.** "No citations found"
and "Wikipedia was unreachable" look identical in a results table and mean opposite things — one is a
reason to act, the other is our outage. Every node here has to keep them apart.

Pure functions are tested directly. The live sources are exercised behind `@pytest.mark.network`, so
the suite runs offline.
"""
from __future__ import annotations

import pytest

from providers.keywords import classify_intent, cluster, demand_index


# --- intent -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("phrase,intent", [
    ("buy crm software", "transactional"),
    ("crm pricing", "transactional"),
    ("best crm for startups", "commercial"),
    ("hubspot vs salesforce", "commercial"),
    ("salesforce login", "navigational"),
    ("what is a crm", "informational"),
    ("how does a crm work", "informational"),
])
def test_intent_classification(phrase, intent):
    assert classify_intent(phrase) == intent


def test_commercial_beats_informational_when_a_phrase_is_both():
    """"best crm software" is a question and a shortlist. The shortlist is what it is worth."""
    assert classify_intent("what is the best crm software") == "commercial"


def test_transactional_outranks_commercial():
    assert classify_intent("best crm pricing") == "transactional"


# --- clustering ---------------------------------------------------------------------------------------

def test_intent_is_a_hard_cluster_boundary():
    """"buy crm" and "what is crm" share every content word and are completely different pages."""
    clusters = cluster(["buy crm software", "crm software price",
                        "what is crm software", "how does crm software work"])
    intents = {c["intent"] for c in clusters}
    assert "transactional" in intents and "informational" in intents
    for c in clusters:
        assert len({classify_intent(p) for p in c["phrases"]}) == 1


def test_every_phrase_is_placed_exactly_once():
    phrases = ["best crm for startups", "best crm for small business", "what is a crm",
               "crm pricing", "how to choose a crm", "top crm tools"]
    clusters = cluster(phrases)
    placed = [p for c in clusters for p in c["phrases"]]
    assert sorted(placed) == sorted(phrases)
    assert len(placed) == len(set(placed))


def test_clusters_are_ordered_largest_first():
    clusters = cluster(["a crm tool", "another crm tool", "crm tool guide", "buy widgets"])
    assert all(clusters[i]["size"] >= clusters[i + 1]["size"] for i in range(len(clusters) - 1))


def test_clustering_an_empty_list_is_not_an_error():
    assert cluster([]) == []


# --- demand index -------------------------------------------------------------------------------------

def test_the_index_is_recomputable_from_its_stated_components(monkeypatch):
    """The number has to be arguable. If the components cannot reproduce it, it is a black box."""
    import providers.keywords as K
    monkeypatch.setattr(K, "expand", lambda *a, **k: ([], []))
    monkeypatch.setattr(K, "stackexchange_questions", lambda *a, **k: [{"score": 5}] * 10)
    monkeypatch.setattr(K, "hackernews_questions", lambda *a, **k: [{"score": 3}] * 10)
    monkeypatch.setattr(K, "wikipedia_interest",
                        lambda *a, **k: {"article": "X", "monthly_average": 25_000,
                                         "trend": "rising"})
    r = demand_index("anything")
    usable = {k: v for k, v in r["components"].items() if v.get("score") is not None}
    expected = round(100 * sum(v["score"] * v["weight"] for v in usable.values())
                     / sum(v["weight"] for v in usable.values()), 1)
    assert abs(r["demand_index"] - expected) < 0.15


def test_an_unreachable_source_lowers_confidence_rather_than_the_score(monkeypatch):
    """Counting a dead source as zero would report a busy topic as having no demand."""
    import providers.keywords as K
    monkeypatch.setattr(K, "expand", lambda *a, **k: ([K.Suggestion("x", {"google"}, 1)] * 0, []))

    def boom(*a, **k):
        raise K.SourceUnavailable("network down")
    monkeypatch.setattr(K, "stackexchange_questions", boom)
    monkeypatch.setattr(K, "hackernews_questions", boom)
    monkeypatch.setattr(K, "wikipedia_interest", boom)

    r = demand_index("anything")
    assert r["confidence"] < 1.0
    assert r["components"]["community"]["unavailable"] is True
    assert r["components"]["community"]["score"] is None
    assert r["sources_unreachable"]


def test_the_index_always_disclaims_search_volume():
    """Every competitor sells a modelled monthly volume as if it were measured. This one says what
    it is."""
    import providers.keywords as K
    r = K.demand_index.__doc__
    assert "NOT a search volume" in r


def test_caveat_travels_with_the_number(monkeypatch):
    import providers.keywords as K
    monkeypatch.setattr(K, "expand", lambda *a, **k: ([], []))
    monkeypatch.setattr(K, "stackexchange_questions", lambda *a, **k: [])
    monkeypatch.setattr(K, "hackernews_questions", lambda *a, **k: [])
    monkeypatch.setattr(K, "wikipedia_interest", lambda *a, **k: None)
    assert "not a monthly search volume" in demand_index("x")["caveat"]


# --- live sources -------------------------------------------------------------------------------------

@pytest.mark.network
def test_autocomplete_returns_a_real_long_tail():
    from providers.keywords import expand
    suggestions, failures = expand("headless cms", prepositions=False)
    assert len(suggestions) > 50, f"only {len(suggestions)} suggestions; failures={failures}"
    assert all("headless" in s.phrase for s in suggestions)


@pytest.mark.network
def test_common_crawl_scale_distinguishes_a_big_site_from_a_small_one():
    """Sampled capture counts both hit the cap on any large domain, so without the block count a
    400-page site and a 400,000-page site report identical coverage."""
    from providers.corpus import common_crawl_presence
    big = common_crawl_presence("stripe.com", indexes=1).as_dict()
    small = common_crawl_presence("example.com", indexes=1).as_dict()
    assert big["index_blocks_latest"] > small["index_blocks_latest"]


@pytest.mark.network
def test_a_capped_sample_says_so():
    from providers.corpus import common_crawl_presence
    p = common_crawl_presence("stripe.com", indexes=1, page_limit=20)
    row = p.per_crawl[0]
    assert row["sample_capped"] is True
    assert "at least 20" in row["captures_note"]


@pytest.mark.network
def test_wikipedia_citations_are_articles_not_talk_pages():
    from providers.corpus import wikipedia_citations
    cites, _ = wikipedia_citations("stripe.com", limit=10)
    assert cites
    assert not any(c.title.startswith(("User talk:", "Talk:", "Wikipedia:")) for c in cites)
    assert all(c.url.startswith("https://en.wikipedia.org/wiki/") for c in cites)
