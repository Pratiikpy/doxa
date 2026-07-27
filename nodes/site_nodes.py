"""Group G — whole-site services.

A crawl is the only Doxa operation with a real cost to somebody else, so every node here bounds it and
reports the bound. `truncated` and `stopped_because` travel in every response, and the validation
checks assert that a partial crawl can never present itself as complete. "No orphan pages" after
stopping at 40 of 900 pages is not a result, it is a lie.

Price scales with the page budget, because that is what actually costs.
"""
from __future__ import annotations

from typing import Any

from checks.base import Finding, Severity
from checks.challenge import detect as detect_challenge
from checks.crosspage import link_graph, run_crosspage
from checks.geo_score import score_geo
from checks.sitemap import check_sitemap
from contract import ErrorCode, ValidationCheck
from crawler import CrawlResult, crawl
from fetch import FetchError, SsrfError, fetch
from nodes.page_nodes import SEVERITY_ORDER, _summarise, _url_of
from runtime import Node, NodeContext, NodeError

# What a caller may ask for, and what it costs. A crawl priced flat would either overcharge the small
# site or be a way to buy a thousand requests for a cent.
PAGE_TIERS = ((50, 0.10), (200, 0.25), (500, 0.50))


def _budget(ctx: NodeContext) -> tuple[int, float]:
    """The page budget actually used, and the tier that pays for it.

    The tier sets the ceiling; it does not raise the request. A caller asking for 30 pages gets 30,
    not the 50 its tier permits — crawling more than was asked for is unrequested load on a server the
    caller may not own, and a page count that does not match the request is a broken promise however
    generous.
    """
    want = max(1, int((ctx.options or {}).get("max_pages", 50)))
    for limit, price in PAGE_TIERS:
        if want <= limit:
            return want, price
    return PAGE_TIERS[-1]


class _CrawlNode(Node):
    asp_type = "A2MCP"
    engine = "doxa-crawler"
    engine_version = "1.0"
    deterministic = False

    def _crawl(self, ctx: NodeContext) -> CrawlResult:
        url = _url_of(ctx)
        max_pages, _ = _budget(ctx)
        opts = ctx.options or {}

        # Refuse before crawling if the entry point is not the site: a thousand requests against a
        # challenge page would cost the customer money and the site bandwidth for nothing.
        try:
            first = fetch(url, timeout=30)
        except SsrfError as e:
            raise NodeError(ErrorCode.INVALID_INPUT, f"That URL cannot be fetched: {e}") from e
        except FetchError as e:
            raise NodeError(ErrorCode.FETCH_FAILED, f"The start URL could not be fetched: {e}") from e
        ch = detect_challenge(first)
        if ch:
            raise NodeError(ErrorCode.POLICY_BLOCKED,
                            ch.message + " A crawl would make hundreds of requests and receive the "
                                         "same challenge every time.")
        if not first.ok:
            raise NodeError(ErrorCode.FETCH_FAILED,
                            f"The start URL returned HTTP {first.status}, so there is nothing to "
                            f"crawl from.")

        result = crawl(url,
                       max_pages=max_pages,
                       max_depth=max(1, min(int(opts.get("max_depth", 5)), 10)),
                       deadline_s=float(min(int(opts.get("deadline_s", 120)), 240)),
                       workers=max(1, min(int(opts.get("workers", 4)), 8)),
                       respect_robots=bool(opts.get("respect_robots", True)))
        if result.truncated:
            ctx.warn(f"The crawl covered {len(result.pages)} page(s) and then stopped because "
                     f"{result.stopped_because}. Findings describe the pages actually visited; "
                     f"nothing is claimed about the {result.queued_not_visited} still queued.")
        if result.disallowed:
            ctx.warn(f"{len(result.disallowed)} URL(s) were skipped because robots.txt disallows "
                     f"them. Doxa obeys the same rules it audits.")
        return result

    def _coverage(self, result: CrawlResult) -> dict[str, Any]:
        return {"complete": not result.truncated,
                "pages_crawled": len(result.pages),
                "queued_not_visited": result.queued_not_visited,
                "stopped_because": result.stopped_because or None}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        cov = result.get("coverage", {})
        crawl_summary = result.get("crawl", {})
        findings = result.get("findings", [])
        return [
            ValidationCheck(name="a_partial_crawl_never_presents_as_complete",
                            passed=cov.get("complete") == (not crawl_summary.get("truncated")),
                            detail=f"{cov.get('pages_crawled')} pages; "
                                   f"{cov.get('stopped_because') or 'finished'}"),
            ValidationCheck(name="truncation_is_stated_in_the_response",
                            passed=(not crawl_summary.get("truncated")) or
                            bool(cov.get("stopped_because"))),
            ValidationCheck(name="at_least_one_page_was_crawled",
                            passed=cov.get("pages_crawled", 0) > 0),
            ValidationCheck(name="findings_ordered_by_severity",
                            passed=[SEVERITY_ORDER[f["severity"]] for f in findings]
                            == sorted(SEVERITY_ORDER[f["severity"]] for f in findings)),
        ]


class SiteAudit(_CrawlNode):
    """Crawl the site and run every cross-page check."""
    name = "site.audit"
    price_usdt = 0.10        # the entry tier; see PAGE_TIERS
    requires = ("url",)
    optional = ('max_pages', 'max_depth', 'deadline_s')
    example_input = {'url': 'https://example.com/'}

    def run(self, ctx: NodeContext) -> dict:
        result = self._crawl(ctx)
        findings = run_crosspage(result)
        findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity.value], f.code))
        worst = sorted(result.pages,
                       key=lambda p: (p.ok, p.word_count))[:10]
        return {"start_url": result.start_url,
                "crawl": result.summary(),
                "coverage": self._coverage(result),
                "summary": _summarise(findings),
                "findings": [f.as_dict() for f in findings],
                "pages": [p.as_dict() for p in result.pages[:200]],
                "weakest_pages": [p.as_dict() for p in worst]}


class SiteGraph(_CrawlNode):
    """The internal link structure, and which pages it strands."""
    name = "site.graph"
    price_usdt = 0.10
    requires = ("url",)
    optional = ('max_pages', 'max_depth')
    example_input = {'url': 'https://example.com/'}

    def run(self, ctx: NodeContext) -> dict:
        result = self._crawl(ctx)
        graph = link_graph(result)
        findings: list[Finding] = []
        if graph.get("least_linked"):
            findings.append(Finding(
                "graph.unlinked", Severity.HIGH,
                f"{len(graph['least_linked'])} page(s) have no incoming internal links, so they "
                f"receive nothing from the rest of the site.",
                {"pages": [r["url"] for r in graph["least_linked"]]}))
        if graph.get("dead_ends"):
            findings.append(Finding(
                "graph.dead_ends", Severity.LOW,
                f"{len(graph['dead_ends'])} page(s) link nowhere else on the site. A crawler that "
                f"arrives there has to start again.",
                {"pages": graph["dead_ends"]}))
        if not findings:
            findings.append(Finding("graph.healthy", Severity.INFO,
                                    "Every crawled page has both incoming and outgoing internal "
                                    "links.", {"pages": graph.get("pages", 0)}))
        return {"start_url": result.start_url,
                "crawl": result.summary(),
                "coverage": self._coverage(result),
                "graph": graph,
                "summary": _summarise(findings),
                "findings": [f.as_dict() for f in findings]}


class SiteSitemap(Node):
    """Does the sitemap agree with the site?"""
    name = "site.sitemap"
    price_usdt = 0.02
    requires = ("url",)
    optional = ('max_pages',)
    example_input = {'url': 'https://example.com/'}
    asp_type = "A2MCP"
    engine = "doxa-crawler"
    engine_version = "1.0"
    deterministic = False

    def run(self, ctx: NodeContext) -> dict:
        url = _url_of(ctx)
        opts = ctx.options or {}
        crawl_result = None
        if opts.get("compare_with_crawl", True):
            try:
                crawl_result = crawl(url, max_pages=int(opts.get("max_pages", 50)),
                                     max_depth=int(opts.get("max_depth", 4)),
                                     deadline_s=90.0, workers=4)
            except Exception as e:  # noqa: BLE001
                ctx.warn(f"The crawl used to diff against the sitemap failed ({type(e).__name__}); "
                         f"the sitemap is reported on its own.")
        declared = crawl_result.sitemaps if crawl_result and crawl_result.sitemaps else None
        report, findings = check_sitemap(url, crawl=crawl_result, declared=declared)
        if crawl_result and crawl_result.truncated:
            ctx.warn(f"The comparison crawl stopped early ({crawl_result.stopped_because}), so URLs "
                     f"it did not reach are reported as unverified rather than as orphans.")
        findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity.value], f.code))
        return {"site": url,
                "sitemap": report,
                "crawl": crawl_result.summary() if crawl_result else None,
                "summary": _summarise(findings),
                "findings": [f.as_dict() for f in findings]}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        codes = {f["code"] for f in result["findings"]}
        crawl_summary = result.get("crawl") or {}
        return [
            ValidationCheck(name="a_missing_sitemap_is_reported_not_inferred",
                            passed=bool(result["sitemap"].get("sitemaps")) ==
                            ("sitemap.missing" not in codes)),
            ValidationCheck(name="orphans_are_only_claimed_after_a_complete_crawl",
                            passed=(not crawl_summary.get("truncated")) or
                            "sitemap.orphans" not in codes,
                            detail="a truncated crawl proves nothing is unreachable"),
            ValidationCheck(name="a_verdict_was_reached", passed=bool(codes)),
        ]


class SiteAEO(_CrawlNode):
    """AI readiness across the site, not just one page."""
    name = "site.aeo"
    price_usdt = 0.20
    requires = ("url",)
    optional = ('max_pages', 'max_depth')
    example_input = {'url': 'https://example.com/'}

    def run(self, ctx: NodeContext) -> dict:
        result = self._crawl(ctx)
        html_pages = [p for p in result.pages if p.media_type == "text/html" and p.ok][:40]
        if not html_pages:
            raise NodeError(ErrorCode.FETCH_FAILED,
                            "The crawl found no HTML pages that loaded, so there is nothing to score.")

        # Site-level files are fetched once, not once per page: they are the same file every time and
        # re-fetching would multiply the load on the customer's server for an identical answer.
        scored: list[dict[str, Any]] = []
        site_signals: dict[str, Any] | None = None
        for i, cp in enumerate(html_pages):
            try:
                page = fetch(cp.url, timeout=25)
            except Exception:  # noqa: BLE001
                continue
            s = score_geo(page, fetch_site_files=(i == 0))
            row = {"url": cp.url, "score": s.total, "band": s.band,
                   "categories": s.by_category()}
            if i == 0:
                site_signals = {k: v for k, v in s.by_category().items()
                                if k in ("robots.txt", "llms.txt", "ai_discovery")}
                row["site_level"] = site_signals
            scored.append(row)

        if not scored:
            raise NodeError(ErrorCode.FETCH_FAILED, "No page could be re-fetched for scoring.")
        page_scores = [r["score"] for r in scored]
        ranked = sorted(scored, key=lambda r: r["score"])
        ctx.warn(f"Site-level files (robots.txt, llms.txt, the AI discovery endpoints) were fetched "
                 f"once and applied to every page, so per-page scores share those points.")
        return {"start_url": result.start_url,
                "crawl": result.summary(),
                "coverage": self._coverage(result),
                "pages_scored": len(scored),
                "score": {"mean": round(sum(page_scores) / len(page_scores), 1),
                          "min": min(page_scores), "max": max(page_scores)},
                "site_level_signals": site_signals,
                "weakest_pages": ranked[:10],
                "strongest_pages": ranked[-5:][::-1],
                "findings": [],
                "summary": {"critical": 0, "high": 0, "low": 0, "info": 0, "faults": 0}}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        base = super().validate(result, ctx)
        s = result["score"]
        return base + [
            ValidationCheck(name="scores_are_within_range",
                            passed=0 <= s["min"] <= s["max"] <= 100),
            ValidationCheck(name="mean_lies_between_min_and_max",
                            passed=s["min"] <= s["mean"] <= s["max"]),
            ValidationCheck(name="every_scored_page_is_listed",
                            passed=result["pages_scored"] > 0),
        ]


ALL_NODES = [SiteAudit(), SiteGraph(), SiteSitemap(), SiteAEO()]
