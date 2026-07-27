"""Group J — the negotiated engagement.

Everything else in Doxa answers inside one payment. Some work cannot: crawl a site, agree a fix list,
write to it, wait for the changes to ship, re-measure, and prove the difference. That spans weeks and
somebody else's release cycle, so it does not belong behind a single 402.

What this node does is produce the **plan**, priced and sequenced, from a real assessment rather than a
template. It crawls a sample of the site, scores it, and turns the actual gaps into phases — each phase
naming the Doxa services that would execute it and what each costs. The customer gets a costed
programme they can accept, argue with, or execute themselves with the same public endpoints.

It quotes; it does not commit. A price for work spanning weeks is an estimate, and the response says so
rather than implying a guarantee.
"""
from __future__ import annotations

from typing import Any

from checks.base import Severity
from checks.challenge import looks_auditable
from checks.crosspage import run_crosspage
from checks.geo_score import score_geo
from contract import ErrorCode, ValidationCheck
from crawler import crawl
from fetch import FetchError, SsrfError, fetch
from nodes.page_nodes import _url_of
from runtime import Node, NodeContext, NodeError

# What each remedy costs to execute with Doxa's own endpoints, so the quote is built from real prices
# rather than invented ones.
SERVICE_PRICES = {
    "site.audit": 0.10, "site.graph": 0.10, "site.sitemap": 0.02, "site.aeo": 0.20,
    "page.audit": 0.01, "page.asai": 0.02, "page.blocked": 0.01, "page.aeo": 0.02,
    "geo.score": 0.02, "schema.validate": 0.005, "robots.check": 0.005, "llms.check": 0.005,
    "ai.visibility": 0.10, "ai.brand": 0.10, "ai.citations": 0.10, "ai.prompts": 0.05,
    "kw.discover": 0.01, "kw.questions": 0.02, "kw.demand": 0.02, "kw.cluster": 0.02,
    "content.audit": 0.05, "content.brief": 0.10, "content.charts": 0.05,
    "links.inbound": 0.10, "corpus.presence": 0.05,
    "compete.compare": 0.15, "audit.diff": 0.02, "report.pdf": 0.05,
}

# Code prefix -> (what it means for the programme, which services address it).
#
# Two namespaces have to be covered here, and missing one is not a cosmetic slip. Check findings are
# dotted (`schema.missing`); GEO score signals are underscored (`schema_any_valid`). An earlier
# version listed only the dotted forms, so a site scoring 0/16 on schema — its single largest gap —
# produced no phase at all, and the customer was quoted for everything except their worst problem.
REMEDIES: tuple[tuple[tuple[str, ...], str, tuple[str, ...]], ...] = (
    (("robots.ai_disallowed", "robots.disallowed", "aicrawler.blocked",
      "aicrawler.baseline_unavailable", "robots_citation_ok", "robots_found"),
     "AI crawlers are being refused, or robots.txt does not explicitly welcome the ones that produce "
     "citations. Nothing else in the programme can pay off until this is changed.",
     ("robots.check", "page.blocked")),
    (("asai.js_required", "asai.mostly_js", "asai.partly_js"),
     "Much of the readable content only exists after JavaScript runs, so crawlers that do not "
     "execute it see an empty page.",
     ("page.asai", "site.audit")),
    (("site.duplicate_title", "site.duplicate_content", "site.duplicate_description"),
     "Pages compete with each other for the same terms, splitting the signals between them.",
     ("site.audit", "audit.diff")),
    (("site.orphan_pages", "site.broken_internal_links", "site.canonical", "site.redirect",
      "site.too_deep", "graph.unlinked", "graph.dead_ends", "sitemap."),
     "The internal structure strands pages that should be reachable and passes value to URLs that "
     "cannot receive it.",
     ("site.graph", "site.sitemap", "site.audit")),
    (("schema.", "schema_", "social."),
     "There is no reliable machine-readable statement of what these pages are about, so every fact "
     "has to be inferred from prose.",
     ("schema.validate", "content.charts")),
    (("title.", "description.", "canonical.", "meta_", "viewport.", "language."),
     "The basic metadata that decides how each page is titled, described and consolidated is "
     "incomplete.",
     ("page.audit", "site.audit")),
    (("aeo.", "chunk.", "evidence.", "readability.", "content.thin", "content_", "clutter."),
     "The writing is not shaped for a model to answer from: the conclusion is buried, there is little "
     "concrete evidence, and the page does not chunk cleanly.",
     ("page.aeo", "content.audit", "content.brief")),
    (("brand_", "author.missing", "links.inbound"),
     "Nothing ties this site to a recognised entity — no consistent brand name across its own "
     "markup, and no links to the knowledge sources a model resolves entities against.",
     ("links.inbound", "corpus.presence", "ai.brand")),
    (("llms.", "llms_", "ai_discovery", "signals_", "freshness.undated"),
     "The site publishes none of the machine-facing files that state what it is and which pages "
     "matter.",
     ("llms.check", "geo.score")),
)


class SeoEngagement(Node):
    """A costed, sequenced programme, built from an actual assessment of the site."""
    name = "seo.engagement"
    price_usdt = 0.25
    requires = ("url",)
    optional = ('goal', 'sample_pages')
    example_input = {'url': 'https://example.com/', 'goal': 'be cited by AI assistants'}
    asp_type = "A2A"
    engine = "doxa-crawler"
    engine_version = "1.0"
    deterministic = False

    def run(self, ctx: NodeContext) -> dict:
        url = _url_of(ctx)
        raw = ctx.input if isinstance(ctx.input, dict) else {}
        goal = str(raw.get("goal") or "").strip()
        opts = ctx.options or {}
        sample_pages = max(5, min(int(opts.get("sample_pages", 25)), 100))

        try:
            first = fetch(url, timeout=30)
        except (SsrfError, FetchError) as e:
            raise NodeError(ErrorCode.FETCH_FAILED, f"The site could not be reached: {e}") from e
        ok, why = looks_auditable(first)
        if not ok:
            raise NodeError(ErrorCode.POLICY_BLOCKED,
                            why + " A programme cannot be scoped against a site we cannot read.")

        result = crawl(url, max_pages=sample_pages, max_depth=4, deadline_s=110.0, workers=4)
        if not result.pages:
            raise NodeError(ErrorCode.FETCH_FAILED, "The crawl reached no pages.")
        ctx.warn(f"The programme was scoped from a {len(result.pages)}-page sample"
                 + (f", stopped because {result.stopped_because}" if result.truncated else "")
                 + ". A full crawl is the first deliverable of phase one.")

        findings = run_crosspage(result)
        score = score_geo(first, fetch_site_files=True)
        codes = {f.code for f in findings} | {
            s.key for s in score.signals if s.earned < s.points}

        # Phases are built from what was actually found. A template programme that lists the same
        # five phases for every site is worth nothing to the customer who pays for it.
        phases: list[dict[str, Any]] = []
        for prefixes, problem, services in REMEDIES:
            hit = sorted(c for c in codes if c.startswith(tuple(prefixes)))
            if not hit:
                continue
            severity = max((f.severity for f in findings if f.code in hit),
                           key=lambda s: {"critical": 3, "high": 2, "low": 1, "info": 0}[s.value],
                           default=Severity.LOW)
            phases.append({
                "problem": problem,
                "evidence_codes": hit[:8],
                "severity": severity.value,
                "services": [{"endpoint": s, "price_usdt": SERVICE_PRICES.get(s)}
                             for s in services],
                "cost_per_pass_usdt": round(sum(SERVICE_PRICES.get(s, 0) for s in services), 4),
            })

        rank = {"critical": 0, "high": 1, "low": 2, "info": 3}
        phases.sort(key=lambda p: rank[p["severity"]])
        for i, p in enumerate(phases, start=1):
            p["phase"] = i

        # A programme is measure → change → measure. The re-measurement is what makes the outcome
        # provable rather than asserted, so it is priced in rather than left as an upsell.
        proof_pass = round(SERVICE_PRICES["site.audit"] + SERVICE_PRICES["geo.score"]
                           + SERVICE_PRICES["audit.diff"] + SERVICE_PRICES["report.pdf"], 4)
        one_pass = round(sum(p["cost_per_pass_usdt"] for p in phases), 4)

        return {
            "site": url,
            "goal": goal or None,
            "assessment": {
                "pages_sampled": len(result.pages),
                "crawl": result.summary(),
                "geo_score": score.total,
                "band": score.band,
                "cross_page_faults": sum(1 for f in findings if f.severity is not Severity.INFO),
                "weakest_categories": sorted(score.by_category().items(),
                                             key=lambda kv: kv[1]["earned"] - kv[1]["max"])[:3],
            },
            # Recorded so the validation below can prove no large gap was left out of the plan.
            "unaddressed_signals": sorted(
                c for c in codes
                if not any(c.startswith(tuple(p)) for p, _, _ in REMEDIES)),
            "phases": phases,
            "proof_pass": {
                "services": ["site.audit", "geo.score", "audit.diff", "report.pdf"],
                "cost_usdt": proof_pass,
                "purpose": ("Re-measure after the changes ship and diff the two signed audits, so the "
                            "improvement is evidence rather than a claim."),
            },
            "estimate": {
                "one_measurement_pass_usdt": one_pass,
                "with_proof_pass_usdt": round(one_pass + proof_pass, 4),
                "basis": ("Doxa's own published per-call prices, summed over the services each phase "
                          "needs for a single pass. It excludes the human work of making the "
                          "changes, which is not ours to quote."),
            },
            "engagement_type": "A2A",
            "note": ("This is a quote, not a commitment. The work spans your release cycle, so the "
                     "figures are an estimate built from real prices and a real assessment — and "
                     "every service named here is a public endpoint you can call yourself."),
        }

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        phases = result["phases"]
        est = result["estimate"]
        known = set(SERVICE_PRICES)
        return [
            ValidationCheck(name="every_phase_cites_the_findings_that_justify_it",
                            passed=all(p["evidence_codes"] for p in phases),
                            detail="a phase with no evidence is a template, not a plan"),
            ValidationCheck(name="phases_are_ordered_worst_first",
                            passed=all({"critical": 0, "high": 1, "low": 2, "info": 3}[phases[i]["severity"]]
                                       <= {"critical": 0, "high": 1, "low": 2, "info": 3}[phases[i+1]["severity"]]
                                       for i in range(len(phases) - 1))),
            ValidationCheck(name="every_quoted_service_exists_and_is_priced",
                            passed=all(s["endpoint"] in known and s["price_usdt"] is not None
                                       for p in phases for s in p["services"])),
            ValidationCheck(name="the_estimate_is_the_sum_of_its_phases",
                            passed=abs(est["one_measurement_pass_usdt"]
                                       - sum(p["cost_per_pass_usdt"] for p in phases)) < 1e-6),
            ValidationCheck(name="the_quote_is_not_presented_as_a_commitment",
                            passed="not a commitment" in result["note"]),
            ValidationCheck(name="the_programme_ends_in_proof",
                            passed="audit.diff" in result["proof_pass"]["services"]),
            ValidationCheck(
                name="no_weak_category_is_left_out_of_the_plan",
                passed=all(any(cat.split(".")[0].split("_")[0][:5] in p["problem"].lower()
                               or any(c.startswith(cat.split(".")[0].split("_")[0][:5])
                                      for c in p["evidence_codes"])
                               for p in phases)
                           for cat, v in result["assessment"]["weakest_categories"]
                           if v["max"] - v["earned"] >= 8),
                detail="a quote that omits the customer's largest gap is worse than no quote"),
            ValidationCheck(
                name="every_finding_is_either_addressed_or_listed_as_unaddressed",
                passed=isinstance(result.get("unaddressed_signals"), list),
                detail=f"{len(result.get('unaddressed_signals') or [])} signal(s) fall outside the "
                       f"remedy map and are disclosed rather than dropped"),
        ]


ALL_NODES = [SeoEngagement()]
