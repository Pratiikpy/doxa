"""An unreachable source is not evidence of absence.

`corpus.presence` for stripe.com got HTTP 503 from both Common Crawl indexes and published the
verdict "Present in 0 of 2 crawl(s) checked." Every structured field was correct — `sources_unreachable`
listed both crawls — but the one sentence a human reads said Stripe is not in Common Crawl.

The project already enforces this rule for language models ("a failed call is never a negative
answer"). These pin it for the corpus source too.
"""
from __future__ import annotations

import types

from nodes.market_nodes import CorpusPresence


class _Presence:
    """The shape `corpus.common_crawl_presence` returns, with only what the verdict reads."""

    def __init__(self, checked: int, present: int, unreachable: list[str]):
        self.checked_indexes = checked
        self.crawls_present_in = present
        self.unreachable = unreachable

    def as_dict(self) -> dict:
        return {"indexes_checked": self.checked_indexes,
                "crawls_present_in": self.crawls_present_in,
                "sources_unreachable": list(self.unreachable),
                "per_crawl": []}


def _verdict(checked: int, present: int, unreachable: list[str]) -> str:
    """Run the node's verdict logic against a stubbed presence result."""
    node = CorpusPresence()
    captured: dict = {}

    class _Ctx:
        options: dict = {"indexes": checked}
        input: dict = {"domain": "example.com"}

        def warn(self, msg: str) -> None:
            captured["warned"] = msg

    stub = _Presence(checked, present, unreachable)
    import nodes.market_nodes as m
    real = m.corpus.common_crawl_presence
    m.corpus.common_crawl_presence = lambda *a, **k: stub  # type: ignore[assignment]
    try:
        return node.run(_Ctx())["verdict"]
    finally:
        m.corpus.common_crawl_presence = real  # type: ignore[assignment]


def test_every_index_unreachable_never_reads_as_absence():
    verdict = _verdict(checked=2, present=0, unreachable=["CC-MAIN-2026-25", "CC-MAIN-2026-21"])

    assert "could not be measured" in verdict
    assert "Present in 0" not in verdict
    assert "Not present" not in verdict


def test_a_partial_outage_states_what_was_actually_reachable():
    verdict = _verdict(checked=3, present=1, unreachable=["CC-MAIN-2026-21"])

    assert "1 of 2" in verdict, "the denominator must be the crawls that answered"
    assert "unknown, not zero" in verdict


def test_a_genuine_absence_is_still_stated_plainly():
    verdict = _verdict(checked=3, present=0, unreachable=[])

    assert "Not present in any crawl index checked" in verdict


def test_a_genuine_presence_is_unchanged():
    verdict = _verdict(checked=3, present=2, unreachable=[])

    assert "Present in 2 of 3" in verdict
