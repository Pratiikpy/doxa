"""Every advertised example must be a call that succeeds.

`audit.diff` shipped an `example_input` of two empty `findings` lists, which its own comparison then
rejected as having nothing to compare. The example is what the x402 challenge publishes and what a
buying agent copies, so every caller who followed the documentation paid and received INVALID_INPUT.
Found by buying all 36 services as a customer, not by reading the code.

The sweep below covers the deterministic, offline nodes. It cannot cover the ones that fetch a URL
or call a model without turning the test suite into a network and spend dependency, so those are
listed explicitly rather than silently skipped — an exclusion nobody can see is how this defect
survived in the first place.
"""
from __future__ import annotations

import pytest

from contract import ArtifactRequest
from nodes import build_registry
from runtime import Runtime

REGISTRY = build_registry()
RUNTIME = Runtime(REGISTRY)

# Nodes whose example necessarily reaches the network or a model. Named, not skipped by accident.
NEEDS_NETWORK_OR_MODEL = {
    "page.audit", "page.links", "page.images", "schema.validate", "page.hreflang", "page.asai",
    "page.blocked", "llms.check", "robots.check", "page.aeo", "page.chunk", "page.readability",
    "geo.score", "ai.visibility", "ai.brand", "ai.citations", "ai.prompts", "site.audit",
    "site.graph", "site.sitemap", "site.aeo", "kw.discover", "kw.questions", "kw.demand",
    "kw.cluster", "corpus.presence", "cite.verify", "cite.compare", "compete.compare",
    "content.brief", "content.charts", "page.answers", "report.pdf", "badge.svg",
}

ALL_ENDPOINTS = [info["endpoint"] for info in REGISTRY.list()]
OFFLINE = [n for n in ALL_ENDPOINTS if n not in NEEDS_NETWORK_OR_MODEL]


def test_the_offline_set_is_not_empty():
    """If the exclusion list ever swallows everything, this file would pass while testing nothing."""
    assert OFFLINE, "no offline nodes left to check — the exclusion list has grown too far"


@pytest.mark.parametrize("endpoint", OFFLINE)
def test_the_documented_example_succeeds(endpoint):
    node = REGISTRY.get(endpoint)
    example = getattr(node, "example_input", None)
    assert example, f"{endpoint} advertises no example; a buyer has nothing to copy"

    env = RUNTIME.execute(ArtifactRequest(endpoint=endpoint, input=example)).model_dump()
    assert env["ok"] is True, (
        f"{endpoint}: the advertised example fails — "
        f"{(env.get('error') or {}).get('message', env.get('status'))}")
    assert env["status"] != "failed"


@pytest.mark.network
@pytest.mark.parametrize("endpoint", sorted(NEEDS_NETWORK_OR_MODEL & set(ALL_ENDPOINTS)))
def test_the_documented_example_succeeds_online(endpoint):
    """The same guarantee for the nodes that fetch or call a model.

    Marked `network` so the default run stays offline and free, but it exists because this is where
    the defects actually were: three of the four broken examples found by buying the catalogue were
    nodes excluded from the offline sweep — two pointing at `example.com`, one at `rival.com`.
    Placeholder domains look harmless in a diff and fail every time in production.
    """
    node = REGISTRY.get(endpoint)
    example = getattr(node, "example_input", None)
    assert example, f"{endpoint} advertises no example; a buyer has nothing to copy"

    env = RUNTIME.execute(ArtifactRequest(endpoint=endpoint, input=example)).model_dump()
    assert env["ok"] is True, (
        f"{endpoint}: the advertised example fails — "
        f"{(env.get('error') or {}).get('message', env.get('status'))}")


def test_no_example_points_at_a_domain_that_serves_nothing():
    """Offline and total: catches the whole class without fetching anything.

    Deliberately **not** a ban on `example.com`. Buying the catalogue showed 25 services auditing it
    successfully — it is a real, stable, fetchable page, and a blanket rule would have forced 27
    working examples to change for nothing. The domains below are different in kind: they resolve to
    nothing auditable, so any example built on one is a paid failure by construction. `rival.com` was
    exactly that, on the $0.15 service.
    """
    placeholders = ("rival.com", "competitor.com", "yoursite.com", "mysite.com",
                    "yourdomain.com", "yoursite.example", "acme.com", "foo.com")
    offenders = []
    for info in REGISTRY.list():
        node = REGISTRY.get(info["endpoint"])
        blob = str(getattr(node, "example_input", "") or "").lower()
        hits = [p for p in placeholders if p in blob]
        if hits:
            offenders.append(f"{info['endpoint']} -> {hits}")
    assert not offenders, "examples pointing at placeholder domains: " + "; ".join(offenders)


def test_diffing_two_clean_audits_is_a_result_not_an_error():
    """Someone confirming a regression-free deploy is asking a real question, and paying for it."""
    env = RUNTIME.execute(ArtifactRequest(
        endpoint="audit.diff",
        input={"before": {"result": {"findings": []}},
               "after": {"result": {"findings": []}}})).model_dump()
    assert env["ok"] is True
    assert env["result"]["fixed"] == []
    assert env["result"]["introduced"] == []


def test_input_that_is_not_an_audit_envelope_is_still_refused():
    """The permissive change above must not swallow genuinely unusable input."""
    env = RUNTIME.execute(ArtifactRequest(
        endpoint="audit.diff",
        input={"before": {"nothing": 1}, "after": {"nothing": 2}})).model_dump()
    assert env["ok"] is False
    assert env["error"]["code"] == "INVALID_INPUT"
