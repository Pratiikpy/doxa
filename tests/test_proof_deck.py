"""The proof deck is a served page, so it gets the same treatment as the services.

Its whole claim is that it was generated from a recorded run rather than written by hand. That claim
is only worth anything if the page cannot render numbers the recording does not contain, cannot swallow
a failed purchase, and cannot be made to emit markup by whatever a deliverable happens to hold.
"""
from __future__ import annotations

import proof


def _data(**over):
    base = {
        "generated_at": "27 July 2026",
        "tests": 266,
        "audit": {"passed": 49, "total": 49, "examples": ["a sample assertion"]},
        "a2a": {"lede": "the flow", "note": "the caveat",
                "steps": [["Payment settles", "tx 0x" + "ab" * 32]]},
        "services": [
            {"endpoint": "page.asai", "price": 0.02, "seconds": 7.2, "checks": "2/2",
             "bytes": 898, "tx": "0x" + "cd" * 32, "problems": []},
        ],
        "deliverables": {},
    }
    base.update(over)
    return base


def test_a_failed_purchase_is_never_rendered_as_delivered():
    """A page that reports 36/36 while a row failed is worse than no page."""
    data = _data(services=[{"endpoint": "page.audit", "price": 0.01, "seconds": 2.8,
                            "checks": "3/4", "bytes": 700, "tx": "0x" + "ef" * 32,
                            "problems": ["validation failed"]}])

    html = proof.render(data)

    assert "FAILED" in html
    assert "0/1" in html or "delivered</td>" not in html


def test_the_headline_count_comes_from_the_rows_not_a_constant():
    data = _data(services=[
        {"endpoint": "a", "price": 0.01, "seconds": 1, "checks": "1/1", "bytes": 1,
         "tx": "0x" + "11" * 32, "problems": []},
        {"endpoint": "b", "price": 0.01, "seconds": 1, "checks": "1/1", "bytes": 1,
         "tx": "0x" + "22" * 32, "problems": ["broke"]},
    ])

    html = proof.render(data)

    assert "1/2" in html, "one of two delivered; the page must say so"


def test_every_settlement_hash_is_a_link_to_the_explorer():
    html = proof.render(_data())

    assert f'{proof.EXPLORER}0x{"cd" * 32}' in html


def test_a_hash_inside_an_agent_to_agent_step_is_also_clickable():
    """A transaction hash a reader cannot open is decoration."""
    html = proof.render(_data())

    assert f'{proof.EXPLORER}0x{"ab" * 32}' in html


def test_deliverable_content_cannot_inject_markup():
    data = _data(deliverables={"robots.check": {
        "verdicts": [{"agent": "<script>alert(1)</script>", "allowed": True, "rule": "n/a"}]}})

    html = proof.render(data)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_a_showcase_entry_with_no_recorded_deliverable_is_skipped_not_faked():
    data = _data(deliverables={})

    html = proof.render(data)

    assert "What an AI crawler actually sees" not in html


def test_the_page_states_the_payer_and_payee_are_the_same_wallet():
    """Omitting this would let a reviewer read self-funded test traffic as demand."""
    html = proof.render(_data())

    assert "same wallet" in html


def test_no_recorded_run_yields_an_honest_placeholder_not_a_crash(monkeypatch):
    monkeypatch.setattr(proof, "load", lambda: None)

    html = proof.page()

    assert "No recorded run" in html
    assert "36/36" not in html


def test_the_spend_is_summed_from_the_rows():
    data = _data(services=[
        {"endpoint": "a", "price": 0.25, "seconds": 1, "checks": "1/1", "bytes": 1,
         "tx": "0x" + "11" * 32, "problems": []},
        {"endpoint": "b", "price": 0.005, "seconds": 1, "checks": "1/1", "bytes": 1,
         "tx": "0x" + "22" * 32, "problems": []},
    ])

    assert "0.255" in proof.render(data)


def test_no_excerpt_line_forces_the_reader_to_scroll_sideways():
    """A finding message left unwrapped made one block 1,890px wide against a 984px column.

    The evidence was present and effectively unreadable, which for a page whose entire purpose is
    showing evidence is the same as not having it.
    """
    data = _data(deliverables={
        "page.audit": {"findings": [
            {"severity": "high", "code": "links.http_on_https",
             "message": "x " * 300}]},
        "ai.visibility": {
            "overall": {"answers_measured": 1, "answers_mentioning_brand": 1, "mention_rate": 1.0},
            "brand": "Notion",
            "by_prompt": [{"prompt": "q " * 120, "mention_rate": 1.0, "best_rank": 2,
                           "runs": [{"evidence": {"context": "e " * 200}}]}]},
        "content.charts": {"figures": [], "figures_rejected": 0,
                           "statistics_detected_in_text": 18, "chartable_values_found": 0,
                           "numeric_values_by_kind": {"year": 18}, "note": "n " * 200},
    })

    for endpoint in ("page.audit", "ai.visibility", "content.charts"):
        body = proof._excerpt(endpoint, data["deliverables"][endpoint])
        longest = max((len(line) for line in body.splitlines()), default=0)
        assert longest <= proof.WIDTH, f"{endpoint} has a {longest}-column line"


def test_a_showcase_card_cites_its_own_purchase_not_the_sweep_row():
    """The visibility card is backed by a separate purchase at the service's default settings.

    Quoting the sweep's cheaper row beside that richer deliverable would put a receipt next to work
    it did not pay for.
    """
    sweep_tx, own_tx = "0x" + "aa" * 32, "0x" + "bb" * 32
    data = _data(
        services=[{"endpoint": "ai.visibility", "price": 0.1, "seconds": 22.1, "checks": "5/5",
                   "bytes": 2378, "tx": sweep_tx, "problems": []}],
        deliverables={"ai.visibility": {
            "brand": "Notion", "overall": {"answers_measured": 9,
                                           "answers_mentioning_brand": 9, "mention_rate": 1.0},
            "by_prompt": [{"prompt": "q", "mention_rate": 1.0, "best_rank": 2, "runs": []}]}},
        showcase={"ai.visibility": {"endpoint": "ai.visibility", "price": 0.1, "seconds": 156.0,
                                    "checks": "5/5", "bytes": 5000, "tx": own_tx, "problems": []}})

    html = proof.render(data)
    card = html.split('<h3>Do models recommend you?')[1]

    assert own_tx[:14] in card
    assert "156.0s" in card
    assert sweep_tx in html, "the settlement table still shows the sweep's own purchase"


def test_a_table_shaped_answer_is_not_quoted_as_evidence_when_prose_exists():
    """Some models answer with a markdown comparison table.

    Its context window reads "|------|---------| | **Confluence** | Atlassian/Jira users" — honest,
    and unreadable as a quote. Another run is chosen; the quote itself is never edited.
    """
    runs = [{"evidence": {"context": "| Tool | Best For |\n|------|---------|\n| Confluence | teams |"}},
            {"evidence": {"context": "Notion is the strongest all-rounder for a small team."}}]

    assert proof._best_quote(runs) == "Notion is the strongest all-rounder for a small team."


def test_a_table_is_still_quoted_when_it_is_the_only_evidence():
    """Selecting nothing would be worse than an awkward quote: the claim would lose its proof."""
    runs = [{"evidence": {"context": "| Tool | Best For |\n|------|---------|"}}]

    assert proof._best_quote(runs) == "| Tool | Best For |\n|------|---------|"


def test_no_evidence_yields_no_quote_rather_than_an_empty_pair_of_marks():
    assert proof._best_quote([{"rank": 2}]) == ""


def test_no_excerpt_leaks_a_python_repr_to_the_reader():
    """`{'informational': 282, 'commercial': 31}` on a customer-facing page is a bug that escaped,
    not a presentation choice — and so are a bare `True` and a bare `None`."""
    import re

    data = _data(deliverables={
        "kw.discover": {"seed": "headless cms", "total": 314,
                        "by_intent": {"informational": 282, "commercial": 31},
                        "keywords": [{"times_suggested": 31, "intent": "informational",
                                      "phrase": "headless cms examples"}]},
        "links.inbound": {"total": 13, "by_source": {"hackernews": 5, "github": 5, "wikipedia": 3},
                          "wikidata_entity": {"id": "Q7624104", "description": "a company"},
                          "citations": [{"source": "wikipedia", "title": "Stripe, Inc."}]},
        "site.audit": {"coverage": {"pages_crawled": 8, "complete": False,
                                    "stopped_because": "the 8-page budget was reached"},
                       "crawl": {"max_depth": 1}, "findings": []},
    })

    for endpoint in ("kw.discover", "links.inbound", "site.audit"):
        body = proof._excerpt(endpoint, data["deliverables"][endpoint])
        assert not re.search(r"\{'|\['", body), f"{endpoint} leaks a container repr"
        assert not re.search(r"\b(True|False|None)\b", body), f"{endpoint} leaks a Python literal"


def test_text_is_shortened_at_a_word_boundary_not_mid_word():
    """A hard slice put "splitting th" and "descr" on the page.

    That reads as a rendering fault rather than a deliberate summary, on a page whose entire job is
    to look trustworthy.
    """
    assert proof._clip("Pages compete with each other for the same terms, splitting the equity", 62) \
        == "Pages compete with each other for the same terms, splitting…"


def test_clipping_leaves_short_text_untouched():
    assert proof._clip("Stripe, Inc.", 58) == "Stripe, Inc."


def test_clipping_handles_a_missing_value():
    assert proof._clip(None, 40) == ""
