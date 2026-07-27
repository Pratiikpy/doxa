"""AI visibility — the parts that must never be wrong.

These services tell a customer whether models recommend them. Two failure modes would be worse than
returning nothing:

  * reporting a model outage as "you were not mentioned", which the customer would act on;
  * reporting a rank the answer does not support.

Both happened during development. An answer whose literal words were "🏆 Top pick: Notion" was reported
as rank 6, because the parser counted the feature bullets underneath each recommendation as separate
ranked items. These tests pin the corrected behaviour.

No network: the model layer is exercised through parsing functions and a stub, because a test that
needs a live model is a test that cannot run in CI.
"""
from __future__ import annotations

import pytest

from nodes.visibility_nodes import _mentions, _prominence, _rank_of, ranked_items
from providers.llm import _parse_json, strip_reasoning

# A real minimax-m3 answer, kept verbatim: this is the shape the parser must handle.
REAL_ANSWER = """For an all-in-one team workspace combining **notes, tasks, and a wiki**, here are \
the top recommendations:

### 🏆 Top pick: **Notion**
- Best all-rounder for notes, tasks, databases, and wikis
- Highly customizable pages and templates
- Free for small teams; affordable paid plans

### Other strong options:

| Tool | Best for | Notes |
|------|----------|-------|
| **Confluence + Jira** | Larger / engineering teams | Industry standard wiki |
| **ClickUp** | Teams wanting tasks-first | Strong project management |
| **Coda** | Spreadsheet lovers | Combines docs and tables |

### Quick recommendation based on need:
- **Small/medium team, want flexibility** → Notion
- **Dev team already using Jira** → Confluence
"""


def test_the_top_pick_is_rank_one():
    """The regression that motivated the rewrite: this answer says "Top pick: Notion" and an earlier
    parser called it rank 6 by counting the feature bullets beneath each recommendation."""
    assert _rank_of(REAL_ANSWER, {"matched": "Notion"}) == 1


def test_products_rank_in_the_order_the_model_listed_them():
    assert _rank_of(REAL_ANSWER, {"matched": "Confluence"}) == 2
    assert _rank_of(REAL_ANSWER, {"matched": "ClickUp"}) == 3
    assert _rank_of(REAL_ANSWER, {"matched": "Coda"}) == 4


def test_feature_bullets_are_not_ranked_items():
    items = ranked_items(REAL_ANSWER)
    assert not any("Best all-rounder" in i for i in items)
    assert not any("Highly customizable" in i for i in items)


def test_section_labels_are_not_recommendations():
    """"Other strong options:" is a heading for a section, not something being recommended. Counting
    it pushes every product below it down a place."""
    items = ranked_items(REAL_ANSWER)
    assert not any(i.rstrip().endswith(":") for i in items)
    assert "Tool" not in items          # the table's header row


def test_prose_with_no_list_yields_no_rank():
    """A rank invented from unstructured prose would be a number the answer does not support."""
    prose = "You could use Notion for this. It works well for small teams."
    assert _rank_of(prose, {"matched": "Notion"}) is None


def test_prominence_is_available_even_without_a_rank():
    prose = "There are many options. " * 20 + "Notion is one of them."
    ev = _mentions(prose, ["Notion"], "notion.com")
    assert _rank_of(prose, ev) is None
    assert _prominence(prose, ev) > 0.8        # named right at the end


# --- mention matching -----------------------------------------------------------------------------

def test_a_mention_returns_the_sentence_as_evidence():
    ev = _mentions("For teams I would suggest Notion or Coda. Both are good.",
                   ["Notion"], "notion.com")
    assert ev["matched"] == "Notion"
    assert "suggest Notion or Coda" in ev["context"]


def test_matching_is_on_word_boundaries():
    """Reach must not match "reached" — a substring match would manufacture mentions."""
    assert _mentions("The team reached a decision.", ["Reach"], "reach.test") is None
    assert _mentions("Try Reach for this.", ["Reach"], "reach.test") is not None


def test_the_domain_counts_as_a_mention():
    ev = _mentions("See notion.com for details.", ["Notion Labs Inc"], "notion.com")
    assert ev is not None and ev["matched"] == "notion.com"


def test_absence_is_absence():
    assert _mentions("I recommend Coda and ClickUp.", ["Notion"], "notion.com") is None


# --- the model client -----------------------------------------------------------------------------

def test_reasoning_blocks_are_stripped():
    assert strip_reasoning("<think>weighing options</think>Paris") == "Paris"


def test_a_truncated_reasoning_block_leaves_nothing():
    """minimax-m3 opens with <think>. When the token budget runs out inside it the response is a 200
    carrying only the scratchpad — publishing that as the customer's answer would be a defect."""
    assert strip_reasoning("<think>the user is asking about the capital of") == ""


@pytest.mark.parametrize("raw", [
    '{"a": 1}',
    '```json\n{"a": 1}\n```',
    'Here is the JSON you asked for:\n{"a": 1}\nHope that helps!',
])
def test_json_survives_the_wrappers_models_add(raw):
    assert _parse_json(raw) == {"a": 1}


def test_unparseable_json_raises_rather_than_returning_empty():
    with pytest.raises(ValueError):
        _parse_json("I am afraid I cannot help with that.")


# --- truncated JSON, which is what actually breaks these services in production -------------------

def test_a_truncated_response_keeps_only_the_complete_elements():
    """Models emit a <think> block, then fenced JSON, and run out of budget partway through. The
    complete objects before the cut are still worth returning; the half-written one is not."""
    from providers.llm import _salvage_json
    out = _salvage_json('```json\n{"figures":[{"label":"a","value":"1"},'
                        '{"label":"b","value":"2"},{"label":"c')
    assert out == {"figures": [{"label": "a", "value": "1"}, {"label": "b", "value": "2"}]}


def test_salvage_handles_a_truncated_top_level_array():
    from providers.llm import _salvage_json
    assert _salvage_json('[{"a":1},{"b":2},{"c') == [{"a": 1}, {"b": 2}]


def test_salvage_handles_nesting():
    from providers.llm import _salvage_json
    assert _salvage_json('{"x":{"y":[{"a":1},{"b":2},{"bro') == {"x": {"y": [{"a": 1}, {"b": 2}]}}


def test_salvage_never_completes_a_half_written_value():
    """The invariant that matters: a value the model did not finish is dropped, never guessed.

    Recovery keeps the fields that were completed — that is what makes a truncated response usable
    instead of a wasted paid call — but a figure whose value was cut off comes back without one, and
    content.charts then discards it for having nothing to verify against the page.
    """
    from providers.llm import _salvage_json
    out = _salvage_json('{"figures":[{"label":"Revenue","value":"4')
    assert out["figures"][0]["label"] == "Revenue"
    assert "value" not in out["figures"][0], "a truncated value must be dropped, not completed"


def test_salvage_refuses_when_not_even_one_field_is_complete():
    from providers.llm import _salvage_json
    with pytest.raises(ValueError):
        _salvage_json('{"question": "What is')


def test_salvage_leaves_a_complete_document_alone():
    from providers.llm import _salvage_json
    assert _salvage_json('{"a":1,"b":[1,2,3]}') == {"a": 1, "b": [1, 2, 3]}


def test_a_string_containing_brackets_does_not_confuse_the_walker():
    from providers.llm import _salvage_json
    assert _salvage_json('{"figures":[{"label":"a {weird} [name]","value":"1"},{"lab') == \
        {"figures": [{"label": "a {weird} [name]", "value": "1"}]}


def test_an_escaped_quote_does_not_confuse_the_walker():
    from providers.llm import _salvage_json
    assert _salvage_json(r'{"figures":[{"label":"say \"hi\"","value":"1"},{"lab') == \
        {"figures": [{"label": 'say "hi"', "value": "1"}]}


def test_truncation_is_detected_from_the_api_not_guessed():
    """finish_reason is the API telling us it ran out of budget. Inferring it from the text would
    misread a genuinely malformed answer as a truncated one."""
    from providers.llm import Completion
    assert Completion(text="x", model="m", finish_reason="length").truncated is True
    assert Completion(text="x", model="m", finish_reason="stop").truncated is False


def test_a_bare_list_is_reshaped_to_the_expected_object():
    """Models return the bare list about as often as the wrapped object, and salvaging a truncated
    response can lose the wrapper entirely. Every caller then did .get() on a list and raised
    AttributeError — an engine failure on a call the customer had already paid for."""
    from providers.llm import as_object
    assert as_object([{"a": 1}], "figures") == {"figures": [{"a": 1}]}
    assert as_object({"figures": [{"a": 1}]}, "figures") == {"figures": [{"a": 1}]}
    assert as_object("not json at all", "figures") == {}
    assert as_object(None, "figures") == {}


# --- the time budget, which protects the payment window -------------------------------------------

def test_the_model_chain_stops_once_the_budget_is_spent(monkeypatch):
    """`timeout` bounds one request; the chain multiplies it. Five models at 70s each is 350s, and
    the x402 challenge only promises the caller 300 — so the whole call needs its own ceiling."""
    import providers.llm as L

    calls: list[str] = []

    class SlowResponse:
        status_code = 504
        text = "gateway timeout"

    def slow_post(url, **kw):
        calls.append(kw["json"]["model"])
        # Simulate each attempt consuming most of the budget.
        import time as _t
        _t.sleep(0.4)
        return SlowResponse()

    monkeypatch.setattr(L.requests, "post", slow_post)
    llm = L.LLM()
    monkeypatch.setattr(llm, "models", lambda: ["m1", "m2", "m3", "m4", "m5"])
    monkeypatch.setattr(type(llm), "available", property(lambda self: True))

    with pytest.raises(L.LLMUnavailable):
        llm.complete("x", deadline_s=1.0, timeout=60)

    assert len(calls) < 5, f"the chain ignored the budget and tried {len(calls)} models"


def test_without_a_budget_the_whole_chain_is_still_tried(monkeypatch):
    """The ceiling must be opt-in: a caller that does not set one keeps the full fallback."""
    import providers.llm as L

    calls: list[str] = []

    class Failed:
        status_code = 503
        text = "unavailable"

    monkeypatch.setattr(L.requests, "post",
                        lambda url, **kw: (calls.append(kw["json"]["model"]), Failed())[1])
    llm = L.LLM()
    monkeypatch.setattr(llm, "models", lambda: ["m1", "m2", "m3"])
    monkeypatch.setattr(type(llm), "available", property(lambda self: True))

    with pytest.raises(L.LLMUnavailable):
        llm.complete("x")
    assert calls == ["m1", "m2", "m3"]
