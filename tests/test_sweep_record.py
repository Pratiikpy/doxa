"""A filtered purchase run must not discard the rest of the record.

`paid_sweep.py --only robots.check` replaced thirty-six recorded rows with one. The proof deck is
generated from that file, so the next build would have rendered "1/1 services delivered" from a run
that had in fact bought all thirty-six — destroying the evidence and misreporting it in one step.
"""
from __future__ import annotations

import json


def _merge(previous: list, fresh: list, order: list[str]) -> list:
    """The merge `paid_sweep.main` performs for a filtered run, isolated for testing."""
    bought = {r["endpoint"] for r in fresh}
    rows = [r for r in previous if r["endpoint"] not in bought] + fresh
    rank = {name: i for i, name in enumerate(order)}
    rows.sort(key=lambda r: rank.get(r["endpoint"], len(rank)))
    return rows


ORDER = ["page.audit", "robots.check", "ai.visibility"]
PREVIOUS = [{"endpoint": "page.audit", "tx": "0xold1"},
            {"endpoint": "robots.check", "tx": "0xold2"},
            {"endpoint": "ai.visibility", "tx": "0xold3"}]


def test_endpoints_not_bought_this_run_are_kept():
    rows = _merge(PREVIOUS, [{"endpoint": "robots.check", "tx": "0xnew"}], ORDER)

    assert len(rows) == 3
    assert {r["endpoint"] for r in rows} == {"page.audit", "robots.check", "ai.visibility"}


def test_the_endpoint_bought_this_run_is_replaced_not_duplicated():
    rows = _merge(PREVIOUS, [{"endpoint": "robots.check", "tx": "0xnew"}], ORDER)

    matching = [r for r in rows if r["endpoint"] == "robots.check"]
    assert len(matching) == 1
    assert matching[0]["tx"] == "0xnew", "the newer purchase is the one on record"


def test_the_record_stays_in_service_order():
    rows = _merge(PREVIOUS, [{"endpoint": "page.audit", "tx": "0xnew"}], ORDER)

    assert [r["endpoint"] for r in rows] == ORDER


def test_a_full_run_needs_no_previous_record():
    fresh = [{"endpoint": e, "tx": "0xf"} for e in ORDER]

    assert _merge([], fresh, ORDER) == fresh


def test_the_recorded_run_still_holds_every_service():
    """Guards the artefact itself, not only the function — this is what actually got destroyed."""
    from pathlib import Path

    record = Path(__file__).resolve().parent.parent / ".paid-sweep.json"
    if not record.exists():
        return                                  # nothing recorded on this machine yet
    rows = json.loads(record.read_text(encoding="utf-8"))

    assert len({r["endpoint"] for r in rows}) == len(rows), "an endpoint appears twice"
    assert len(rows) >= 36, f"the record has shrunk to {len(rows)} rows"
