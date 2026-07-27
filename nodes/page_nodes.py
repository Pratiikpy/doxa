"""The page-level services — groups A, B and C.

Each node is one paid endpoint. The pattern is the same throughout: fetch once, run the relevant
checks, return findings with their evidence, and declare validation checks the runtime can assert
before the caller is charged for something wrong.

Two rules run through all of them.

*Say what was not done.* A cap, a skipped render, a failed sub-fetch — each is stated in the response.
A truncated result that reads as complete is worse than an error, because the caller acts on it.

*Never invent a clean bill of health.* If the evidence for a check could not be gathered, the answer is
"unknown", never "fine". The whole product is a signed claim; a false clean is the one output that
would make the signature worthless.
"""
from __future__ import annotations

import urllib.parse
from typing import Any

from checks.base import Finding, Severity, registry
from checks.challenge import detect as detect_challenge, looks_auditable
from checks.geo_score import score_geo, webmcp_readiness
from checks.machine import (check_llms_txt, check_robots_for_ai, fetch_robots, probe_ai_crawlers,
                            visible_text)
from contract import ErrorCode, ValidationCheck
from fetch import AI_CRAWLERS, FetchError, Page, SsrfError, fetch
from runtime import Node, NodeContext, NodeError

# Import for registration side-effects: a check that is never imported is never registered, and the
# audit would silently return fewer findings than it claims to run.
import checks.page_html   # noqa: F401
import checks.machine     # noqa: F401
import checks.links       # noqa: F401
import checks.images      # noqa: F401
import checks.hreflang    # noqa: F401
import checks.structured  # noqa: F401
import checks.aeo         # noqa: F401
import checks.integrity   # noqa: F401

SEVERITY_ORDER = {"critical": 0, "high": 1, "low": 2, "info": 3}


def _url_of(ctx: NodeContext) -> str:
    raw = ctx.input
    url = (raw.get("url") if isinstance(raw, dict) else raw) or ""
    url = str(url).strip()
    if not url:
        raise NodeError(ErrorCode.INVALID_INPUT, "A 'url' is required.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _fetch(ctx: NodeContext, url: str, *, render: bool = False, timeout: int = 30,
           require_auditable: bool = True) -> Page:
    """Fetch, and refuse to hand back a document that is not the customer's page.

    `require_auditable=False` is for the services whose whole subject is the blocking itself.
    """
    try:
        page = fetch(url, render=render, timeout=timeout)
    except SsrfError as e:
        raise NodeError(ErrorCode.INVALID_INPUT, f"That URL cannot be fetched: {e}") from e
    except FetchError as e:
        raise NodeError(ErrorCode.FETCH_FAILED, f"The page could not be fetched: {e}") from e

    if require_auditable:
        ok, why = looks_auditable(page)
        if not ok:
            # Auditing an interstitial produces twenty findings that are all true about the challenge
            # page and all worthless about the site. Refusing is the only honest answer.
            ch = detect_challenge(page)
            raise NodeError(ErrorCode.POLICY_BLOCKED if ch else ErrorCode.FETCH_FAILED,
                            why + (" Buy page.blocked to see exactly which readers are refused."
                                   if ch else ""))
    if render and not page.rendered:
        # Stated, not swallowed: without a rendered DOM the JS-visibility answer is unavailable, and
        # the caller must know that rather than read a clean result as proof.
        ctx.warn("JavaScript rendering was unavailable, so JS-only content could not be measured. "
                 "Findings below describe the raw HTML only.")
    return page


def _summarise(findings: list[Finding]) -> dict[str, Any]:
    counts = {s: 0 for s in ("critical", "high", "low", "info")}
    for f in findings:
        counts[f.severity.value] += 1
    return {"critical": counts["critical"], "high": counts["high"],
            "low": counts["low"], "info": counts["info"],
            "faults": counts["critical"] + counts["high"] + counts["low"]}


def _page_meta(page: Page) -> dict[str, Any]:
    return {"url": page.url, "requested_url": page.requested_url, "status": page.status,
            "media_type": page.media_type, "ttfb_ms": page.ttfb_ms, "total_ms": page.total_ms,
            "bytes": page.size_bytes, "rendered": page.rendered,
            "redirects": [{"url": h.url, "status": h.status} for h in page.hops[1:]] or None}


class _PageNode(Node):
    """Shared plumbing: fetch, run a subset of check groups, return findings and evidence."""
    groups: tuple[str, ...] = ()
    render = False
    asp_type = "A2MCP"
    engine = "doxa-html"
    engine_version = "1.0"
    deterministic = True

    def run(self, ctx: NodeContext) -> dict:
        url = _url_of(ctx)
        page = _fetch(ctx, url, render=self.render)
        findings = registry.run(page, groups=self.groups or None)
        return {"page": _page_meta(page),
                "summary": _summarise(findings),
                "findings": [f.as_dict() for f in findings]}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        findings = result.get("findings", [])
        page = result.get("page", {})
        checks = [
            ValidationCheck(name="page_was_fetched",
                            passed=bool(page.get("status")),
                            detail=f"status {page.get('status')}"),
            ValidationCheck(name="findings_have_codes_and_messages",
                            passed=all(f.get("code") and "." in f["code"] and f.get("message")
                                       for f in findings),
                            detail=f"{len(findings)} findings"),
            ValidationCheck(name="findings_ordered_by_severity",
                            passed=[SEVERITY_ORDER[f["severity"]] for f in findings]
                            == sorted(SEVERITY_ORDER[f["severity"]] for f in findings)),
            ValidationCheck(name="every_fault_carries_evidence",
                            passed=all(f.get("detail") is not None for f in findings
                                       if f["severity"] in ("critical", "high")),
                            detail="a fault without evidence is an assertion, not a finding"),
        ]
        return checks


class PageAudit(_PageNode):
    """The full single-page technical pass — every registered check."""
    name = "page.audit"
    price_usdt = 0.01
    requires = ("url",)
    optional = ()
    example_input = {'url': 'https://example.com/'}
    render = False


class PageLinks(Node):
    name = "page.links"
    price_usdt = 0.01
    requires = ("url",)
    optional = ('limit',)
    example_input = {'url': 'https://example.com/'}
    asp_type = "A2MCP"
    engine = "doxa-html"
    engine_version = "1.0"
    deterministic = False        # the web moves; two runs can legitimately disagree

    def run(self, ctx: NodeContext) -> dict:
        from checks.links import extract_links, resolve_links
        url = _url_of(ctx)
        limit = int((ctx.options or {}).get("limit", 150))
        page = _fetch(ctx, url)
        report, findings = resolve_links(page, limit=max(1, min(limit, 400)))
        links = extract_links(page)
        if report.blocked:
            ctx.warn(f"{len(report.blocked)} link(s) could not be checked and are reported as "
                     f"unknown rather than as working.")
        return {"page": _page_meta(page),
                "totals": {"found": len(links),
                           "internal": sum(1 for l in links if l.internal),
                           "external": sum(1 for l in links if not l.internal),
                           "unique": report.unique_found,
                           "unique_checked": report.checked},
                "result": report.as_dict(),
                "findings": [f.as_dict() for f in findings]}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        r = result.get("result", {})
        counted = r.get("ok", 0) + len(r.get("broken", [])) + len(r.get("redirecting", [])) \
            + len(r.get("blocked", []))
        return [
            ValidationCheck(name="every_checked_link_is_accounted_for",
                            passed=counted == r.get("checked", 0),
                            detail=f"{counted} classified of {r.get('checked')} checked"),
            ValidationCheck(name="truncation_is_disclosed",
                            passed=bool(r.get("truncated")) <=
                            any(f["code"] == "links.truncated" for f in result["findings"]),
                            detail=f"{r.get('skipped', 0)} unique link(s) not checked; a silent cap "
                                   f"reads as full coverage"),
            ValidationCheck(name="unreachable_links_not_called_working",
                            passed=all(b not in r.get("broken", []) for b in r.get("blocked", []))),
        ]


class PageImages(Node):
    name = "page.images"
    price_usdt = 0.005
    requires = ("url",)
    optional = ('weigh',)
    example_input = {'url': 'https://example.com/'}
    asp_type = "A2MCP"
    engine = "doxa-html"
    engine_version = "1.0"
    deterministic = False

    def run(self, ctx: NodeContext) -> dict:
        from checks.images import collect_images, weigh_images
        url = _url_of(ctx)
        page = _fetch(ctx, url)
        markup_findings = registry.run(page, groups=("page",))
        markup_findings = [f for f in markup_findings if f.code.startswith("images.")]
        weight_findings = weigh_images(page) if (ctx.options or {}).get("weigh", True) else []
        imgs = collect_images(page)
        all_findings = sorted(markup_findings + weight_findings,
                              key=lambda f: (SEVERITY_ORDER[f.severity.value], f.code))
        return {"page": _page_meta(page),
                "totals": {"images": len(imgs),
                           "without_alt": sum(1 for i in imgs if not i["has_alt"]),
                           "without_dimensions": sum(1 for i in imgs
                                                     if not i["width"] or not i["height"])},
                "summary": _summarise(all_findings),
                "findings": [f.as_dict() for f in all_findings]}


class SchemaValidate(_PageNode):
    name = "schema.validate"
    price_usdt = 0.005
    requires = ("url",)
    optional = ()
    example_input = {'url': 'https://example.com/'}
    groups = ("page",)

    def run(self, ctx: NodeContext) -> dict:
        from checks.structured import _types, parse_jsonld
        url = _url_of(ctx)
        page = _fetch(ctx, url)
        nodes, errors = parse_jsonld(page)
        findings = [f for f in registry.run(page)
                    if f.code.startswith(("schema.", "social."))]
        return {"page": _page_meta(page),
                "types": sorted({t for n in nodes for t in _types(n)}),
                "nodes": len(nodes),
                "parse_errors": errors,
                "summary": _summarise(findings),
                "findings": [f.as_dict() for f in findings]}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        return [
            ValidationCheck(name="parse_errors_are_reported_as_findings",
                            passed=(not result["parse_errors"]) or
                            any(f["code"] == "schema.invalid_json" for f in result["findings"])),
            ValidationCheck(name="declared_types_match_findings",
                            passed=bool(result["types"]) == (result["nodes"] > 0)),
        ]


class PageHreflang(_PageNode):
    name = "page.hreflang"
    price_usdt = 0.01
    requires = ("url",)
    optional = ()
    example_input = {'url': 'https://example.com/'}

    def run(self, ctx: NodeContext) -> dict:
        from checks.hreflang import collect_hreflangs
        url = _url_of(ctx)
        page = _fetch(ctx, url)
        tags = collect_hreflangs(page)
        findings = [f for f in registry.run(page) if f.code.startswith("hreflang.")]
        if tags:
            ctx.warn("Reciprocity — whether each alternate links back — is a cross-page property. "
                     "The URLs that would have to be fetched to prove it are listed under "
                     "'requires_crawl'.")
        return {"page": _page_meta(page),
                "alternates": tags,
                "requires_crawl": [t["url"] for t in tags if t["url"]],
                "summary": _summarise(findings),
                "findings": [f.as_dict() for f in findings]}


# --- group B: what a machine actually sees ---------------------------------------------------------

class PageAsAI(Node):
    """The differentiator: raw HTML against the rendered DOM."""
    name = "page.asai"
    price_usdt = 0.02
    requires = ("url",)
    optional = ()
    example_input = {'url': 'https://example.com/'}
    asp_type = "A2MCP"
    engine = "doxa-render"
    engine_version = "1.0"
    deterministic = False

    def run(self, ctx: NodeContext) -> dict:
        url = _url_of(ctx)
        page = _fetch(ctx, url, render=True, timeout=90)
        if not page.rendered:
            # For every other service the raw HTML is still worth something. Here it is not: this
            # service *is* the comparison between raw and rendered, so without a rendered DOM there
            # is no product. Returning an empty finding set would charge for nothing and, worse,
            # could read as "no JS-only content found".
            raise NodeError(ErrorCode.ENGINE_UNAVAILABLE,
                            "The page could not be rendered, so raw and rendered HTML could not be "
                            "compared. This service has no answer without both, and reporting an "
                            "empty result would read as a clean bill of health.")
        findings = [f for f in registry.run(page) if f.code.startswith("asai.")]
        raw_words = len(visible_text(page.html).split())
        rendered_words = len(visible_text(page.rendered_html).split()) if page.rendered else None
        return {"page": _page_meta(page),
                "measured": page.rendered,
                "raw_words": raw_words,
                "rendered_words": rendered_words,
                "summary": _summarise(findings),
                "findings": [f.as_dict() for f in findings]}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        return [
            ValidationCheck(
                name="no_verdict_without_a_rendered_dom",
                passed=result["measured"] or not result["findings"],
                detail="reporting 'server rendered' without rendering would be a false clean"),
            ValidationCheck(
                name="share_is_derived_from_both_documents",
                passed=all("raw_words" in f["detail"] and "rendered_words" in f["detail"]
                           for f in result["findings"] if f["code"].startswith("asai."))),
        ]


class PageBlocked(Node):
    """Present each AI crawler's real user-agent and see who is refused."""
    name = "page.blocked"
    price_usdt = 0.01
    requires = ("url",)
    optional = ('crawlers',)
    example_input = {'url': 'https://example.com/'}
    asp_type = "A2MCP"
    engine = "doxa-probe"
    engine_version = "1.0"
    deterministic = False

    def run(self, ctx: NodeContext) -> dict:
        url = _url_of(ctx)
        page = _fetch(ctx, url, require_auditable=False)
        wanted = (ctx.options or {}).get("crawlers") or list(AI_CRAWLERS)[:6]
        unknown = [c for c in wanted if c not in AI_CRAWLERS]
        if unknown:
            raise NodeError(ErrorCode.INVALID_INPUT,
                            f"Unknown crawler(s): {', '.join(unknown)}. "
                            f"Known: {', '.join(sorted(AI_CRAWLERS))}")
        findings = probe_ai_crawlers(url, page, crawlers=wanted)
        robots_text, robots_status = fetch_robots(url)
        findings += check_robots_for_ai(url, robots_text)
        return {"page": _page_meta(page),
                "crawlers_tested": wanted,
                "robots_txt": {"status": robots_status, "bytes": len(robots_text)},
                "summary": _summarise(findings),
                "findings": [f.as_dict() for f in findings]}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        codes = {f["code"] for f in result["findings"]}
        return [
            ValidationCheck(name="every_requested_crawler_was_probed",
                            passed=bool(result["crawlers_tested"])),
            ValidationCheck(name="a_verdict_was_reached",
                            passed=bool(codes & {"aicrawler.blocked", "aicrawler.degraded",
                                                 "aicrawler.allowed"})),
        ]


class LlmsCheck(Node):
    name = "llms.check"
    price_usdt = 0.005
    requires = ("url",)
    optional = ()
    example_input = {'url': 'https://example.com/'}
    asp_type = "A2MCP"
    engine = "doxa-html"
    engine_version = "1.0"
    deterministic = False

    def run(self, ctx: NodeContext) -> dict:
        url = _url_of(ctx)
        findings = check_llms_txt(url)
        p = urllib.parse.urlsplit(url)
        return {"site": f"{p.scheme}://{p.netloc}",
                "summary": _summarise(findings),
                "findings": [f.as_dict() for f in findings]}


class RobotsCheck(Node):
    name = "robots.check"
    price_usdt = 0.005
    requires = ("url",)
    optional = ('agents',)
    example_input = {'url': 'https://example.com/'}
    asp_type = "A2MCP"
    engine = "doxa-robots"
    engine_version = "1.0"
    deterministic = True

    def run(self, ctx: NodeContext) -> dict:
        from checks.machine import RobotsRules
        url = _url_of(ctx)
        robots_text, status = fetch_robots(url)
        rules = RobotsRules(robots_text)
        path = urllib.parse.urlsplit(url).path or "/"
        agents = (ctx.options or {}).get("agents") or list(AI_CRAWLERS)
        verdicts = []
        for a in agents:
            allowed, why = rules.allowed(a, path)
            verdicts.append({"agent": a, "allowed": allowed, "rule": why})
        findings = check_robots_for_ai(url, robots_text)
        return {"url": url, "path": path,
                "robots_txt": {"status": status, "found": bool(robots_text),
                               "sitemaps": rules.sitemaps},
                "verdicts": verdicts,
                "summary": _summarise(findings),
                "findings": [f.as_dict() for f in findings]}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        return [
            ValidationCheck(name="every_agent_has_a_verdict_and_a_reason",
                            passed=all(v.get("rule") for v in result["verdicts"])),
            ValidationCheck(name="missing_robots_means_allowed",
                            passed=result["robots_txt"]["found"] or
                            all(v["allowed"] for v in result["verdicts"]),
                            detail="a missing robots.txt permits everything; reporting it as a block "
                                   "would send the customer to fix a file that does not exist"),
        ]


# --- group C: AEO ----------------------------------------------------------------------------------

class PageAEO(_PageNode):
    """Answer-first structure, evidence, chunkability, readability, freshness, authorship."""
    name = "page.aeo"
    price_usdt = 0.02
    requires = ("url",)
    optional = ()
    example_input = {'url': 'https://example.com/'}

    AEO_PREFIXES = ("aeo.", "evidence.", "chunk.", "readability.", "freshness.",
                    "multimodal.", "author.", "injection.", "clutter.")

    def run(self, ctx: NodeContext) -> dict:
        url = _url_of(ctx)
        page = _fetch(ctx, url)
        findings = [f for f in registry.run(page)
                    if f.code.startswith(self.AEO_PREFIXES)]
        return {"page": _page_meta(page),
                "summary": _summarise(findings),
                "findings": [f.as_dict() for f in findings]}


class PageChunk(Node):
    name = "page.chunk"
    price_usdt = 0.01
    requires = ("url",)
    optional = ('max_words',)
    example_input = {'url': 'https://example.com/'}
    asp_type = "A2MCP"
    engine = "doxa-html"
    engine_version = "1.0"
    deterministic = True

    def run(self, ctx: NodeContext) -> dict:
        from checks.aeo import citable_spans
        url = _url_of(ctx)
        max_words = int((ctx.options or {}).get("max_words", 220))
        page = _fetch(ctx, url)
        spans = citable_spans(page, max_words=max(40, min(max_words, 600)))
        return {"page": _page_meta(page),
                "spans": spans,
                "totals": {"spans": len(spans),
                           "words": sum(s["words"] for s in spans),
                           "with_heading": sum(1 for s in spans if s["heading"])}}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        spans = result["spans"]
        ordered = all(spans[i]["start"] <= spans[i + 1]["start"] for i in range(len(spans) - 1))
        return [
            ValidationCheck(name="offsets_are_monotonic", passed=ordered),
            ValidationCheck(name="span_lengths_match_their_offsets",
                            passed=all(s["end"] - s["start"] == len(s["text"]) for s in spans)),
            ValidationCheck(name="no_empty_spans",
                            passed=all(s["text"].strip() and s["words"] > 0 for s in spans)),
        ]


class PageReadability(Node):
    name = "page.readability"
    price_usdt = 0.01
    requires = ("url",)
    optional = ()
    example_input = {'url': 'https://example.com/'}
    asp_type = "A2MCP"
    engine = "doxa-html"
    engine_version = "1.0"
    deterministic = True

    def run(self, ctx: NodeContext) -> dict:
        from checks.aeo import readability
        from checks.page_html import body_text
        url = _url_of(ctx)
        page = _fetch(ctx, url)
        metrics = readability(body_text(page))
        findings = [f for f in registry.run(page)
                    if f.code.startswith(("readability.", "clutter."))]
        return {"page": _page_meta(page), "metrics": metrics,
                "summary": _summarise(findings),
                "findings": [f.as_dict() for f in findings]}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        m = result["metrics"]
        return [
            ValidationCheck(name="metrics_are_self_consistent",
                            passed=m.get("flesch") is None or
                            (m["words"] > 0 and m["sentences"] > 0)),
            ValidationCheck(name="flesch_in_plausible_range",
                            passed=m.get("flesch") is None or -100 <= m["flesch"] <= 122,
                            detail=f"flesch={m.get('flesch')}"),
        ]


class GeoScore(Node):
    """The 0–100 AI-readiness score, on geo-optimizer's published weights."""
    name = "geo.score"
    price_usdt = 0.02
    requires = ("url",)
    optional = ()
    example_input = {'url': 'https://example.com/'}
    asp_type = "A2MCP"
    engine = "doxa-geo"
    engine_version = "4.0"
    deterministic = False        # depends on site files fetched live

    def run(self, ctx: NodeContext) -> dict:
        url = _url_of(ctx)
        page = _fetch(ctx, url)
        score = score_geo(page, fetch_site_files=True)
        return {"page": _page_meta(page),
                **score.as_dict(),
                "webmcp_readiness": webmcp_readiness(score),
                "rubric": "geo-optimizer v4.0.0 published weights"}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        sigs = result["signals"]
        cats = result["categories"]
        total_max = sum(c["max"] for c in cats.values())
        return [
            ValidationCheck(name="score_equals_the_sum_of_its_signals",
                            passed=result["score"] == min(100, sum(s["earned"] for s in sigs)),
                            detail=f"score={result['score']}"),
            ValidationCheck(name="no_signal_exceeds_its_maximum",
                            passed=all(0 <= s["earned"] <= s["max"] for s in sigs)),
            ValidationCheck(name="rubric_totals_100",
                            passed=total_max == 100, detail=f"weights sum to {total_max}"),
            ValidationCheck(name="every_signal_states_a_reason",
                            passed=all(s["why"] for s in sigs)),
            ValidationCheck(name="fix_list_covers_every_gap",
                            passed=len(result["fix_order"]) ==
                            sum(1 for s in sigs if s["earned"] < s["max"])),
        ]


ALL_NODES = [PageAudit(), PageLinks(), PageImages(), SchemaValidate(), PageHreflang(),
             PageAsAI(), PageBlocked(), LlmsCheck(), RobotsCheck(),
             PageAEO(), PageChunk(), PageReadability(), GeoScore()]
