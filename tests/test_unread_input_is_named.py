"""A request field this service does not read must be named, not dropped in silence.

Measured on sibling ASPs: a caller asking for two venues through a misspelled key received five
computed from defaults, and a 120-character cap became 8,079 characters. Nothing in either response
said the field had been discarded, so the caller had no way to tell they had been answered a
different question from the one they paid for. Doxa dropped unrecognised fields the same way.

A warning rather than a refusal: rejecting an unexpected field would break any client sending one
today. Warnings ride in the envelope and are covered by the signature.
"""
from __future__ import annotations

import pytest

from contract import ArtifactRequest
from nodes import build_registry
from runtime import Runtime

RUNTIME = Runtime(build_registry())
DIFF_INPUT = {"before": {"result": {"findings": [{"code": "title.missing", "severity": "error"}]}},
              "after": {"result": {"findings": []}}}


def run(endpoint, **inp):
    return RUNTIME.execute(ArtifactRequest(endpoint=endpoint, input=inp)).model_dump()


def _warnings(env) -> str:
    return " ".join(env.get("warnings") or [])


def test_an_unread_field_is_named():
    w = _warnings(run("audit.diff", **DIFF_INPUT, depht=2))
    assert "depht" in w
    assert "may not be the one you intended" in w
    assert "Accepted fields:" in w


def test_a_near_miss_gets_a_suggestion():
    w = _warnings(run("audit.diff", **{**DIFF_INPUT, "beforre": {}}))
    assert "did you mean 'before'" in w


def test_a_correct_request_is_not_warned_about():
    assert _warnings(run("audit.diff", **DIFF_INPUT)) == ""


def test_the_warning_does_not_change_the_result():
    """A notice must annotate the answer, never replace or degrade it."""
    clean = run("audit.diff", **DIFF_INPUT)
    noisy = run("audit.diff", **DIFF_INPUT, nonsense=1)
    assert noisy["ok"] is True
    assert noisy["result"]["fixed"] == clean["result"]["fixed"]


def test_the_warning_is_inside_the_signed_envelope():
    env = run("audit.diff", **DIFF_INPUT, nonsense=1)
    assert env.get("warnings")
    assert env.get("receipt"), "and covered by the receipt the buyer verifies"


@pytest.mark.parametrize("endpoint", ["audit.diff"])
def test_ordinary_calls_stay_silent(endpoint):
    """If the check misfires it fires on everything; this is the canary for that."""
    assert _warnings(run(endpoint, **DIFF_INPUT)) == ""
