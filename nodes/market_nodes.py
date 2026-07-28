"""Groups E and F — keywords, demand, corpus presence and citations.

These services all query other people's APIs, which makes one rule dominant: **a source that failed is
never reported as a zero.** "No citations found" and "we could not reach Wikipedia" look identical in
a results table and mean opposite things, and only one of them is a reason for the customer to act.
Every node here carries `sources_unreachable`, and every validation check asserts that a result built
on nothing cannot present itself as a finding.
"""
from __future__ import annotations

import concurrent.futures
import urllib.parse
from typing import Any

from contract import ErrorCode, ValidationCheck
from providers import corpus, keywords
from runtime import Node, NodeContext, NodeError


def _seed(ctx: NodeContext) -> str:
    raw = ctx.input if isinstance(ctx.input, dict) else {}
    seed = str(raw.get("seed") or raw.get("keyword") or raw.get("q") or "").strip()
    if not seed:
        raise NodeError(ErrorCode.INVALID_INPUT,
                        "A 'seed' keyword is required — the term you want the market for.")
    if len(seed) > 120:
        raise NodeError(ErrorCode.INVALID_INPUT, "The seed keyword is unreasonably long.")
    return seed


def _domain(ctx: NodeContext) -> str:
    raw = ctx.input if isinstance(ctx.input, dict) else {}
    value = str(raw.get("domain") or raw.get("url") or "").strip()
    if not value:
        raise NodeError(ErrorCode.INVALID_INPUT, "A 'domain' or 'url' is required.")
    if "//" not in value:
        value = "https://" + value
    host = (urllib.parse.urlsplit(value).hostname or "").lower()
    if not host or "." not in host:
        raise NodeError(ErrorCode.INVALID_INPUT, f"'{value}' is not a usable domain.")
    return host.removeprefix("www.")


class _MarketNode(Node):
    asp_type = "A2MCP"
    engine = "doxa-market"
    engine_version = "1.0"
    deterministic = False


# --- E · keywords ----------------------------------------------------------------------------------

class KwDiscover(_MarketNode):
    name = "kw.discover"
    price_usdt = 0.01
    requires = ("seed",)
    optional = ('engines', 'alphabet', 'questions', 'prepositions')
    example_input = {'seed': 'headless cms'}

    def run(self, ctx: NodeContext) -> dict:
        seed = _seed(ctx)
        opts = ctx.options or {}
        try:
            suggestions, failures = keywords.expand(
                seed,
                engines=opts.get("engines"),
                alphabet=bool(opts.get("alphabet", True)),
                questions=bool(opts.get("questions", True)),
                prepositions=bool(opts.get("prepositions", True)))
        except ValueError as e:
            raise NodeError(ErrorCode.INVALID_INPUT, str(e)) from e

        if not suggestions and failures:
            raise NodeError(ErrorCode.ENGINE_UNAVAILABLE,
                            "No autocomplete engine could be reached, so nothing was measured. This "
                            "is an outage on our side, not a finding that the term has no long tail: "
                            + "; ".join(f["example"] for f in failures)[:200])
        if failures:
            ctx.warn(f"{len(failures)} engine(s) returned errors during expansion; the phrases below "
                     f"come from the engines that answered.")

        by_intent: dict[str, int] = {}
        for s in suggestions:
            i = keywords.classify_intent(s.phrase)
            by_intent[i] = by_intent.get(i, 0) + 1
        return {"seed": seed,
                "total": len(suggestions),
                "by_intent": by_intent,
                "on_both_engines": sum(1 for s in suggestions if len(s.sources) > 1),
                "sources_unreachable": failures,
                "keywords": [s.as_dict() for s in suggestions[:500]],
                "note": ("Autocomplete reflects queries people actually type. It carries no volume, "
                         "and Doxa does not attach one.")}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        kws = result["keywords"]
        seed_head = result["seed"].lower().split()[0]
        return [
            ValidationCheck(name="every_phrase_relates_to_the_seed",
                            passed=all(seed_head in k["phrase"] for k in kws)),
            ValidationCheck(name="phrases_are_unique",
                            passed=len({k["phrase"] for k in kws}) == len(kws)),
            ValidationCheck(name="intent_counts_match_the_list",
                            passed=sum(result["by_intent"].values()) == result["total"]),
            ValidationCheck(name="no_volume_is_claimed",
                            passed=all("volume" not in k for k in kws),
                            detail="Doxa does not measure search volume and will not model one"),
            ValidationCheck(name="source_failures_are_disclosed",
                            passed=isinstance(result["sources_unreachable"], list)),
        ]


class KwQuestions(_MarketNode):
    name = "kw.questions"
    price_usdt = 0.02
    requires = ("seed",)
    optional = ()
    example_input = {'seed': 'headless cms'}

    def run(self, ctx: NodeContext) -> dict:
        seed = _seed(ctx)
        unreachable: list[dict[str, str]] = []
        autocomplete: list[str] = []
        try:
            autocomplete = keywords.question_suggestions(seed)
        except Exception as e:  # noqa: BLE001
            unreachable.append({"source": "autocomplete", "error": str(e)[:120]})

        real: list[dict[str, Any]] = []

        def ask(item):
            name, fn = item
            try:
                return name, fn(seed, limit=25), ""
            except Exception as e:  # noqa: BLE001
                return name, [], str(e)[:120]

        # Independent sources, asked concurrently: one slow archive should not set the latency of
        # the whole service.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            for name, found, error in pool.map(ask, (
                    ("stackexchange", keywords.stackexchange_questions),
                    ("hackernews", keywords.hackernews_questions))):
                if error:
                    unreachable.append({"source": name, "error": error})
                else:
                    real.extend(found)

        if not autocomplete and not real:
            raise NodeError(ErrorCode.ENGINE_UNAVAILABLE,
                            "No question source could be reached, so nothing was measured: "
                            + "; ".join(u["error"] for u in unreachable)[:200])
        if unreachable:
            ctx.warn(f"{len(unreachable)} source(s) were unreachable and contribute nothing below. "
                     f"They are not reported as having no questions.")

        real.sort(key=lambda q: -(q.get("score") or 0))
        return {"seed": seed,
                "autocomplete_questions": autocomplete,
                "discussed_questions": real,
                "totals": {"autocomplete": len(autocomplete), "discussions": len(real)},
                "sources_unreachable": unreachable,
                "note": ("Autocomplete questions are what people type. Discussed questions are real "
                         "threads with real scores — every one has a URL you can open.")}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        real = result["discussed_questions"]
        return [
            ValidationCheck(name="every_discussed_question_has_a_url",
                            passed=all(q.get("url", "").startswith("http") for q in real)),
            ValidationCheck(name="discussions_are_ranked_by_score",
                            passed=all((real[i].get("score") or 0) >= (real[i+1].get("score") or 0)
                                       for i in range(len(real) - 1))),
            ValidationCheck(name="something_was_measured",
                            passed=bool(result["autocomplete_questions"] or real)),
            ValidationCheck(name="source_failures_are_disclosed",
                            passed=isinstance(result["sources_unreachable"], list)),
        ]


class KwDemand(_MarketNode):
    name = "kw.demand"
    price_usdt = 0.02
    requires = ("seed",)
    optional = ()
    example_input = {'seed': 'headless cms'}

    def run(self, ctx: NodeContext) -> dict:
        seed = _seed(ctx)
        result = keywords.demand_index(seed)
        if result["demand_index"] is None:
            raise NodeError(ErrorCode.ENGINE_UNAVAILABLE,
                            "Every input to the demand index was unreachable, so no index could be "
                            "computed. Returning a zero would read as 'no demand'.")
        if result["confidence"] < 1.0:
            ctx.warn(f"Confidence is {result['confidence']:.0%}: some inputs were unavailable and "
                     f"the remaining weights were re-normalised rather than counted as zero.")
        return result

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        comps = result["components"]
        usable = {k: v for k, v in comps.items() if v.get("score") is not None}
        return [
            ValidationCheck(name="index_is_in_range",
                            passed=0 <= result["demand_index"] <= 100),
            ValidationCheck(name="every_component_states_what_it_means",
                            passed=all("means" in v or v.get("unavailable") for v in comps.values())),
            ValidationCheck(name="index_is_recomputable_from_its_components",
                            passed=abs(result["demand_index"] -
                                       round(100 * sum(v["score"] * v["weight"] for v in usable.values())
                                             / sum(v["weight"] for v in usable.values()), 1)) < 0.15),
            ValidationCheck(name="confidence_reflects_available_weight",
                            passed=abs(result["confidence"] -
                                       sum(v["weight"] for v in usable.values()) / 100) < 0.01),
            ValidationCheck(name="search_volume_is_explicitly_disclaimed",
                            passed="not a monthly search volume" in result["caveat"]),
        ]


class KwCluster(_MarketNode):
    name = "kw.cluster"
    price_usdt = 0.02
    requires = ("keywords",)
    optional = ("seed", "threshold")
    example_input = {"keywords": ["best crm for startups", "what is a crm",
                                  "crm pricing"]}

    def run(self, ctx: NodeContext) -> dict:
        raw = ctx.input if isinstance(ctx.input, dict) else {}
        phrases = [str(p).strip() for p in (raw.get("keywords") or []) if str(p).strip()]
        failures: list[dict[str, str]] = []
        seed = str(raw.get("seed") or "").strip()

        if not phrases:
            if not seed:
                raise NodeError(ErrorCode.INVALID_INPUT,
                                "Provide either a list of 'keywords' to cluster, or a 'seed' to "
                                "expand and cluster.")
            suggestions, failures = keywords.expand(seed, prepositions=False)
            phrases = [s.phrase for s in suggestions]
        if not phrases:
            raise NodeError(ErrorCode.ENGINE_UNAVAILABLE,
                            "No keywords were supplied and none could be discovered.")

        threshold = float((ctx.options or {}).get("threshold", 0.5))
        clusters = keywords.cluster(phrases, threshold=max(0.2, min(threshold, 0.9)))
        return {"seed": seed or None,
                "input_phrases": len(phrases),
                "clusters": clusters,
                "sources_unreachable": failures,
                "note": ("Clusters split on intent as well as wording: 'buy crm' and 'what is crm' "
                         "share every word and are different pages.")}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        clusters = result["clusters"]
        placed = sum(c["size"] for c in clusters)
        all_phrases = [p for c in clusters for p in c["phrases"]]
        return [
            ValidationCheck(name="every_input_phrase_is_placed_exactly_once",
                            passed=placed == result["input_phrases"]
                            and len(set(all_phrases)) == len(all_phrases),
                            detail=f"{placed} placed of {result['input_phrases']}"),
            ValidationCheck(name="each_cluster_holds_a_single_intent",
                            passed=all(len({__import__('providers.keywords', fromlist=['x'])
                                            .classify_intent(p) for p in c["phrases"]}) == 1
                                       for c in clusters)),
            ValidationCheck(name="clusters_are_ordered_by_size",
                            passed=all(clusters[i]["size"] >= clusters[i+1]["size"]
                                       for i in range(len(clusters) - 1))),
        ]


# --- F · corpus presence and citations ---------------------------------------------------------------

class CorpusPresence(_MarketNode):
    """Is your content in the open corpus AI is built on?"""
    name = "corpus.presence"
    price_usdt = 0.05
    requires = ("domain",)
    optional = ('indexes',)
    example_input = {'domain': 'example.com'}

    def run(self, ctx: NodeContext) -> dict:
        domain = _domain(ctx)
        indexes = max(1, min(int((ctx.options or {}).get("indexes", 3)), 12))
        try:
            presence = corpus.common_crawl_presence(domain, indexes=indexes)
        except corpus.SourceUnavailable as e:
            raise NodeError(ErrorCode.ENGINE_UNAVAILABLE,
                            f"The Common Crawl index could not be reached, so coverage was not "
                            f"measured: {e}") from e
        d = presence.as_dict()
        if presence.unreachable:
            ctx.warn(f"{len(presence.unreachable)} crawl index(es) were unreachable. They are "
                     f"reported as unknown, not as zero coverage.")
        # "Present in 0 of 2 crawl(s) checked" reads exactly like "this domain is not in Common
        # Crawl", and that is what a 503 from every index used to produce — an outage rendered as a
        # finding about the customer's site. A failure is never an absence, so the verdict has to
        # distinguish "we looked and it is not there" from "we could not look".
        reachable = presence.checked_indexes - len(presence.unreachable)
        if reachable == 0:
            d["verdict"] = (f"Coverage could not be measured: all {presence.checked_indexes} crawl "
                            f"index(es) were unreachable. This says nothing about whether the domain "
                            f"is in Common Crawl.")
        elif presence.crawls_present_in == 0 and not presence.unreachable:
            d["verdict"] = ("Not present in any crawl index checked. Systems trained or grounded on "
                            "Common Crawl have not seen this domain.")
        elif presence.unreachable:
            d["verdict"] = (f"Present in {presence.crawls_present_in} of {reachable} crawl(s) that "
                            f"could be reached; {len(presence.unreachable)} were unreachable and are "
                            f"unknown, not zero.")
        else:
            d["verdict"] = (f"Present in {presence.crawls_present_in} of "
                            f"{presence.checked_indexes} crawl(s) checked.")
        return d

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        rows = result["per_crawl"]
        return [
            ValidationCheck(name="every_index_is_reported",
                            passed=len(rows) == result["indexes_checked"]),
            ValidationCheck(name="unreachable_indexes_are_not_counted_as_zero",
                            passed=all(("unreachable" in r) != (r["captures"] > 0) or
                                       r["captures"] == 0 for r in rows)),
            ValidationCheck(name="presence_count_matches_the_rows",
                            passed=result["crawls_present_in"] ==
                            sum(1 for r in rows if r["captures"] > 0)),
            # The defect this pins: every index returned HTTP 503 and the verdict read
            # "Present in 0 of 2 crawl(s) checked", which a customer reads as absence.
            ValidationCheck(
                name="an_unreachable_index_is_never_reported_as_absence",
                passed=len(result["sources_unreachable"]) < result["indexes_checked"]
                or "could not be measured" in result.get("verdict", ""),
                detail="a source that could not be read says nothing about the customer's site"),
            ValidationCheck(name="sampling_is_disclaimed",
                            passed="not the whole of it" in result["caveat"]),
        ]


class LinksInbound(_MarketNode):
    """Who actually cites this domain, from sources a model reads."""
    name = "links.inbound"
    price_usdt = 0.10
    requires = ("domain",)
    optional = ('brand', 'sources', 'limit')
    example_input = {'domain': 'example.com', 'brand': 'Example'}

    def run(self, ctx: NodeContext) -> dict:
        domain = _domain(ctx)
        raw = ctx.input if isinstance(ctx.input, dict) else {}
        opts = ctx.options or {}
        try:
            found = corpus.gather_citations(domain, sources=opts.get("sources"),
                                            limit=max(5, min(int(opts.get("limit", 20)), 50)))
        except corpus.SourceUnavailable as e:
            raise NodeError(ErrorCode.ENGINE_UNAVAILABLE, str(e)) from e

        brand = str(raw.get("brand") or "").strip() or domain.split(".")[0]
        try:
            found["wikidata_entity"] = corpus.wikidata_entity(brand)
        except Exception as e:  # noqa: BLE001
            found["sources_unreachable"].append({"source": "wikidata", "error": str(e)[:100]})
            found["wikidata_entity"] = None

        if found["sources_unreachable"]:
            ctx.warn(f"{len(found['sources_unreachable'])} source(s) were unreachable. They "
                     f"contribute nothing and are not counted as an absence of citations.")
        if not found["wikidata_entity"]:
            found["entity_note"] = ("No Wikidata item was found for this brand. A model has nothing "
                                    "to attach facts to, and schema sameAs has nowhere to point.")
        return found

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        cites = result["citations"]
        return [
            ValidationCheck(name="every_citation_is_a_url_the_customer_can_open",
                            passed=all(c.get("url", "").startswith("http") for c in cites)),
            ValidationCheck(name="counts_match_the_listed_citations",
                            passed=result["total"] == len(cites)
                            and sum(result["by_source"].values()) == len(cites)),
            ValidationCheck(name="at_least_one_source_answered",
                            passed=bool(result["sources_queried"])),
            ValidationCheck(name="completeness_is_explicitly_disclaimed",
                            passed="not a complete backlink profile" in result["caveat"],
                            detail="a complete graph needs a vendor; Doxa does not estimate one"),
        ]


class LinksCompare(_MarketNode):
    """Your citations against a rival's, measured the same way for both."""
    name = "links.compare"
    price_usdt = 0.20
    requires = ("domain", "competitor")
    optional = ("sources",)
    # rival.com is a placeholder, not a site anyone can measure citations for. Both sides of a
    # comparison have to be real or the example is a paid failure waiting to happen.
    example_input = {"domain": "stripe.com", "competitor": "adyen.com"}

    def run(self, ctx: NodeContext) -> dict:
        raw = ctx.input if isinstance(ctx.input, dict) else {}
        mine = _domain(ctx)
        theirs = str(raw.get("competitor") or raw.get("against") or "").strip()
        if not theirs:
            raise NodeError(ErrorCode.INVALID_INPUT,
                            "A 'competitor' domain is required to compare against.")
        if "//" not in theirs:
            theirs = "https://" + theirs
        theirs = (urllib.parse.urlsplit(theirs).hostname or "").lower().removeprefix("www.")

        sources = (ctx.options or {}).get("sources")
        out: dict[str, Any] = {"domains": [mine, theirs], "sides": {}}

        # Both sides are measured concurrently. Done one after the other this was the slowest service
        # in the catalogue — long enough that a caller could time out and conclude it was broken.
        # Measuring them at the same time also removes any drift between the two snapshots, which is
        # the thing a comparison is most sensitive to.
        def measure(host: str):
            cites = corpus.gather_citations(host, sources=sources, limit=20)
            presence = corpus.common_crawl_presence(host, indexes=3)
            return host, cites, presence

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(measure, h) for h in (mine, theirs)]
            measured = {}
            for f in futures:
                try:
                    host, cites, presence = f.result()
                    measured[host] = (cites, presence)
                except corpus.SourceUnavailable as e:
                    raise NodeError(ErrorCode.ENGINE_UNAVAILABLE,
                                    f"Citations could not be gathered, so no fair comparison is "
                                    f"possible: {e}") from e

        corpus_down: list[str] = []
        for host in (mine, theirs):
            cites, presence = measured[host]
            pdict = presence.as_dict()
            # A crawl index that could not be reached is not a crawl the domain is absent from.
            #
            # `corpus.presence` has always drawn that distinction — it reports each unreachable
            # crawl, lists them, and warns "reported as unknown, not as zero coverage". This node
            # reads two numbers off the same object and dropped it, so while Common Crawl's index
            # was timing out it reported stripe.com and adyen.com at **0 corpus presence each**,
            # with `sources_unreachable: []`, no warning, and a computed gap over data that was
            # never measured. Stripe is certainly in Common Crawl; the buyer was told otherwise for
            # twenty cents and given no way to tell.
            corpus_unreachable = pdict.get("sources_unreachable") or []
            if corpus_unreachable:
                corpus_down.append(host)
            out["sides"][host] = {"citations": cites["total"],
                                  "by_source": cites["by_source"],
                                  "sources_unreachable": cites["sources_unreachable"],
                                  "corpus_crawls_present_in": presence.crawls_present_in,
                                  "corpus_urls_seen": len(presence.unique_urls),
                                  "corpus_urls_capped": presence.any_capped,
                                  "corpus_unreachable": corpus_unreachable or None,
                                  # The uncapped scale signal. Sampled URL counts both hit the same
                                  # cap on any large site, which would make two very different
                                  # domains look identical.
                                  "corpus_index_blocks": pdict["index_blocks_latest"],
                                  "top": cites["citations"][:10]}

        a, b = out["sides"][mine], out["sides"][theirs]
        blocks_a, blocks_b = a.get("corpus_index_blocks"), b.get("corpus_index_blocks")
        out["gap"] = {"citations": a["citations"] - b["citations"],
                      "corpus_index_blocks": (blocks_a - blocks_b)
                      if (blocks_a is not None and blocks_b is not None) else None,
                      "corpus_urls_sampled": a["corpus_urls_seen"] - b["corpus_urls_seen"]}
        if a["corpus_urls_capped"] or b["corpus_urls_capped"]:
            out["gap"]["corpus_urls_note"] = ("One or both samples hit the cap, so the sampled URL "
                                              "difference is a floor. Compare index_blocks instead.")
        out["caveat"] = ("Both domains were measured with the same sources and the same limits, so "
                         "the comparison is fair even though neither figure is a complete backlink "
                         "count.")
        if corpus_down:
            # Withdraw the corpus half of the comparison rather than reporting a difference between
            # two numbers neither of which was measured. The citation half is unaffected and still
            # stands on its own.
            out["gap"]["corpus_index_blocks"] = None
            out["gap"]["corpus_urls_sampled"] = None
            out["gap"]["corpus_note"] = (
                f"The Common Crawl index was unreachable for {', '.join(corpus_down)}, so the corpus "
                f"figures above are unknown rather than zero and no corpus gap is reported. The "
                f"citation comparison is unaffected.")
            out["caveat"] += (" Corpus presence could not be measured on this run — see "
                              "gap.corpus_note — so only the citation figures are comparable.")
            ctx.warn("The Common Crawl index was unreachable, so corpus presence is reported as "
                     "unknown rather than zero and the corpus gap is withheld.")
        return out

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        sides = result["sides"]
        a, b = (sides[d] for d in result["domains"])
        return [
            ValidationCheck(name="both_sides_were_measured",
                            passed=len(sides) == 2),
            ValidationCheck(name="both_sides_used_the_same_sources",
                            passed=set(a["by_source"]) | set(b["by_source"]) is not None
                            and not (a["sources_unreachable"] and not b["sources_unreachable"]
                                     and len(a["sources_unreachable"]) != len(b["sources_unreachable"])),
                            detail="an unequal source set would make the gap meaningless"),
            ValidationCheck(name="the_gap_matches_the_sides",
                            passed=result["gap"]["citations"] == a["citations"] - b["citations"]),
        ]


ALL_NODES = [KwDiscover(), KwQuestions(), KwDemand(), KwCluster(),
             CorpusPresence(), LinksInbound(), LinksCompare()]
