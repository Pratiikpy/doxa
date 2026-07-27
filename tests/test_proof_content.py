"""Comparison, proof and content.

Two things here must never be wrong.

`audit.diff` must not claim a signature is valid without checking it — the whole value of the service
is that it inherits the credibility of two receipts, and asserting "verified" without verifying would
destroy exactly that.

`content.charts` must never publish a figure the source does not state. A fabricated statistic emitted
as schema.org Dataset is strictly worse than emitting none: it is machine-readable, quotable, and
wrong.
"""
from __future__ import annotations

import json

import pytest

from contract import ArtifactRequest
from nodes import build_registry
from nodes.content_nodes import _contains
from nodes.proof_nodes import _badge_svg, _evidence_lines
from runtime import Runtime

rt = Runtime(build_registry())


def run(endpoint: str, payload: dict, options: dict | None = None) -> dict:
    return rt.execute(ArtifactRequest(endpoint=endpoint, input=payload,
                                      options=options or {})).model_dump()


# --- audit.diff ---------------------------------------------------------------------------------

def envelope(codes_and_sev, receipt=True) -> dict:
    e = {"result": {"findings": [{"code": c, "severity": s, "message": f"{c} happened",
                                  "detail": {}} for c, s in codes_and_sev]}}
    if receipt:
        e["receipt"] = {"algo": "ed25519", "signature": "ab" * 32, "public_key": "cd" * 32,
                        "manifest_sha256": "ef" * 32, "signed_at": "2026-07-26T00:00:00Z"}
    return e


def test_diff_separates_fixed_introduced_and_remaining():
    before = envelope([("title.empty", "critical"), ("dom.large", "low")])
    after = envelope([("dom.large", "low"), ("links.broken", "high")])
    r = run("audit.diff", {"before": before, "after": after})["result"]
    assert [f["code"] for f in r["fixed"]] == ["title.empty"]
    assert [f["code"] for f in r["introduced"]] == ["links.broken"]
    assert [f["code"] for f in r["still_present"]] == ["dom.large"]


def test_direction_follows_the_weighted_faults():
    improved = run("audit.diff", {"before": envelope([("a.b", "critical")]),
                                  "after": envelope([("a.b", "low")])})["result"]
    assert improved["direction"] == "improved"
    regressed = run("audit.diff", {"before": envelope([("a.b", "low")]),
                                   "after": envelope([("a.b", "critical")])})["result"]
    assert regressed["direction"] == "regressed"


def test_a_signature_is_never_asserted_valid_without_checking():
    """The service reports that a receipt is present. Claiming it is verified — when nothing here
    verified it — would forfeit the only thing that makes this evidence rather than a claim."""
    r = run("audit.diff", {"before": envelope([("a.b", "low")]),
                           "after": envelope([("a.b", "low")])})["result"]
    for side in r["receipts"].values():
        assert side["present"] is True
        assert side["verified"] is None
        assert "verify this signature yourself" in side["note"]


def test_a_missing_receipt_is_reported_as_missing():
    r = run("audit.diff", {"before": envelope([("a.b", "low")], receipt=False),
                           "after": envelope([("a.b", "low")])})["result"]
    assert r["receipts"]["before"]["present"] is False


def test_diff_rejects_envelopes_with_no_findings():
    d = run("audit.diff", {"before": {"result": {}}, "after": {"result": {}}})
    assert d["error"]["code"] == "INVALID_INPUT"


def test_diff_validation_passes_on_a_real_diff():
    d = run("audit.diff", {"before": envelope([("a.b", "critical")]),
                           "after": envelope([("c.d", "low")])})
    assert all(t["passed"] for t in d["validation"]["tests"]), d["validation"]["tests"]


# --- badge --------------------------------------------------------------------------------------

def test_badge_is_well_formed_svg_and_shows_the_score():
    svg = _badge_svg("AI readiness", "42/100", "#b7791f")
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert "42/100" in svg and "#b7791f" in svg


def test_badge_escapes_its_inputs():
    """The label reaches the badge from a URL. An unescaped angle bracket would break the SVG or
    inject markup into a page that embeds it."""
    svg = _badge_svg('a<script>"', "1/100", "#000000")
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


# --- evidence rendering ---------------------------------------------------------------------------

def test_evidence_is_readable_lines_not_a_json_dump():
    lines = _evidence_lines({"count": 3, "total": 16, "sample": ["/a.png", "/b.png"]})
    assert lines[0] == "count: 3"
    assert not any(line.startswith("{") for line in lines)


def test_a_long_value_keeps_its_content_when_truncated():
    """A URL has no spaces, so cutting at the last word boundary can land on the label and leave
    "sample:…" — a line that reports nothing at all."""
    url = "//upload.example.com/" + "x" * 200
    line = _evidence_lines({"sample": [url]})[0]
    assert line.startswith("sample: //upload.example.com/")
    assert len(line) > 40


def test_empty_and_private_fields_are_skipped():
    lines = _evidence_lines({"count": 1, "empty": [], "nothing": None, "_internal": "x"})
    assert lines == ["count: 1"]


def test_evidence_is_capped_and_says_so():
    detail = {f"field{i}": i for i in range(12)}
    lines = _evidence_lines(detail, max_lines=4)
    assert len(lines) == 5
    assert "further field" in lines[-1]


# --- the quote / figure verifier ------------------------------------------------------------------

@pytest.mark.parametrize("value", ["43%", "$12.4m", "1,200", "December 2024"])
def test_a_figure_present_in_the_source_is_kept(value):
    text = "Revenue grew 43% to $12.4m across 1,200 customers, updated December 2024."
    assert _contains(text, value)


@pytest.mark.parametrize("value", ["57%", "$99m", "2,400", "March 2019"])
def test_a_figure_the_source_does_not_state_is_rejected(value):
    """A fabricated statistic emitted as schema.org Dataset is machine-readable, quotable and wrong —
    strictly worse than emitting nothing."""
    text = "Revenue grew 43% to $12.4m across 1,200 customers, updated December 2024."
    assert not _contains(text, value)


def test_matching_survives_formatting_differences():
    assert _contains("Revenue grew   43%\n  last year", "43% last year")


def test_a_long_quote_clipped_mid_sentence_still_matches():
    source = ("HTML is the most basic building block of the Web. It defines the meaning and "
              "structure of web content.")
    assert _contains(source, "HTML is the most basic building block of the Web. It defines")


def test_an_invented_quote_fails_even_when_it_sounds_right():
    source = "HTML is the most basic building block of the Web."
    assert not _contains(source, "HTML was invented in 1989 by Tim Berners-Lee at CERN.")


# --- every service is described and priced ---------------------------------------------------------

def test_every_service_has_a_price_and_an_engine():
    for n in rt.registry.list():
        assert n["price_usdt"] > 0, n["endpoint"]
        assert n["engine"], n["endpoint"]


def test_no_two_services_share_an_endpoint_name():
    names = [n["endpoint"] for n in rt.registry.list()]
    assert len(names) == len(set(names))
