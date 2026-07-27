"""Group I — content.

Doxa does not write copy. Plenty of services will generate an article; almost none will tell you
whether the one you have answers the question it claims to, or hand you a specification precise enough
that a writer produces something citable.

So these services produce **specifications and verdicts, not prose**:

  * `content.audit` reads the page, works out what question it is trying to answer, and reports
    whether it actually answers it — with the passage that does, or the gap where it should.
  * `content.brief` turns a target question into a page spec: the structure, the questions to cover,
    the schema to emit, the entities to name.
  * `content.charts` converts figures already in the page into a table and JSON-LD a model can quote
    exactly, rather than inferring them from prose.

`content.charts` never invents a number. It extracts figures that are literally present and refuses
any the source does not contain — a fabricated statistic published as structured data would be far
worse than no structured data at all.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from bs4 import BeautifulSoup

from checks.aeo import STATISTIC, citable_spans, first_paragraph, readability
from checks.challenge import looks_auditable
from checks.page_html import body_text, soup
from contract import ErrorCode, ValidationCheck
from fetch import FetchError, SsrfError, fetch
from nodes.page_nodes import _page_meta, _url_of
from providers.llm import LLM, LLMUnavailable, as_object
from runtime import Node, NodeContext, NodeError


# All model attempts for one paid call must finish inside this. The x402 challenge promises
# the caller 300 seconds; leaving 80 in reserve means a slow model never expires the window
# a customer has already paid into.
WHOLE_CALL_BUDGET_S = 220


class _ContentNode(Node):
    asp_type = "A2MCP"
    engine = "0g-compute"
    engine_version = "1.0"
    deterministic = False

    def engine_available(self) -> bool:
        return LLM().available

    def _page(self, ctx: NodeContext):
        url = _url_of(ctx)
        try:
            page = fetch(url, timeout=30)
        except (SsrfError, FetchError) as e:
            raise NodeError(ErrorCode.FETCH_FAILED, f"The page could not be fetched: {e}") from e
        ok, why = looks_auditable(page)
        if not ok:
            raise NodeError(ErrorCode.POLICY_BLOCKED, why)
        return url, page

    def _ask_json(self, prompt: str, *, max_tokens: int = 1400, expect_key: str = "items",
                  require: tuple[str, ...] = (), timeout: int = 70) -> dict:
        """Ask for JSON of a specific shape, and refuse to return the wrong shape as if it were right.

        A model that answers with a different set of keys is the failure mode that actually happens
        here, and it is dangerous precisely because it looks like success: every `.get()` returns
        None, the deliverable serialises fine, and the customer pays for a page of nulls. So the
        required keys are checked, a corrective retry names exactly what was missing, and if the shape
        is still wrong the call fails rather than delivering emptiness.

        Only the fields that make the deliverable *useful* belong in `require`. Demanding every
        optional field voids otherwise good answers over a detail the customer did not need.

        `WHOLE_CALL_BUDGET_S` bounds all attempts together, so retrying can never push the response
        past the payment window the challenge promised the caller.
        """
        correction = ("\n\nYour previous answer was missing these required keys: "
                      + ", ".join(require)
                      + ". Return a single JSON object containing every key listed above, at the top "
                        "level. Do not nest them inside another object, and do not return a bare "
                        "array.")
        attempts = [prompt] + ([prompt + correction] * 2 if require else [])

        started = time.perf_counter()
        last_seen: list[str] = []
        used = 0
        for attempt in attempts:
            remaining = WHOLE_CALL_BUDGET_S - (time.perf_counter() - started)
            if remaining < 20:
                break                     # not enough left to finish; stop rather than half-try
            used += 1
            try:
                data, _ = LLM().complete_json(attempt, max_tokens=max_tokens,
                                              timeout=min(timeout, int(remaining)),
                                              deadline_s=remaining)
            except LLMUnavailable as e:
                # Every model refused or was unreachable. That is our outage, not a finding.
                raise NodeError(ErrorCode.ENGINE_UNAVAILABLE,
                                f"No model could be reached, so nothing was produced: {e}") from e
            except ValueError as e:
                last_seen = [f"unparseable: {e}"]
                continue
            # Models return the bare list about as often as the wrapped object, and a salvaged
            # truncation may have lost the wrapper entirely. Normalise before any caller uses .get.
            obj = as_object(data, expect_key)
            missing = [k for k in require if not obj.get(k)]
            if not missing:
                return obj
            last_seen = missing

        raise NodeError(
            ErrorCode.ENGINE_FAILED,
            f"The model did not return the required field(s) in {used} attempt(s) "
            f"({', '.join(str(x) for x in last_seen)}). Rather than return a result with those "
            f"fields empty, this call failed.")


class ContentAudit(_ContentNode):
    """Does this page answer the question it is trying to answer?"""
    name = "content.audit"
    price_usdt = 0.05
    requires = ("url",)
    optional = ('question',)
    example_input = {'url': 'https://example.com/'}

    def run(self, ctx: NodeContext) -> dict:
        url, page = self._page(ctx)
        text = body_text(page)
        if len(text.split()) < 60:
            raise NodeError(ErrorCode.INVALID_INPUT,
                            f"The page has only {len(text.split())} words of body text. There is not "
                            f"enough content to audit — the finding is that the page is empty.")

        s = soup(page)
        title = s.title.get_text().strip() if s.title else ""
        h1 = s.find("h1")
        heading = h1.get_text(" ").strip() if h1 else ""
        stated_question = str((ctx.input or {}).get("question") or "").strip()
        spans = citable_spans(page)

        data = self._ask_json(
            "You are auditing a web page for whether it answers its own question. Use ONLY the text "
            "provided; do not use outside knowledge and do not invent facts.\n\n"
            f"TITLE: {title}\nHEADING: {heading}\n"
            + (f"THE READER'S QUESTION: {stated_question}\n" if stated_question else "")
            + f"\nPAGE TEXT (truncated):\n{text[:4500]}\n\n"
            "Return JSON with exactly these keys:\n"
            '{"question": "the single question this page is trying to answer",\n'
            ' "answered": true or false,\n'
            ' "answer_quote": "the exact sentence from the page that answers it, or null",\n'
            ' "missing": ["specific things a reader still would not know after reading"],\n'
            ' "unsupported_claims": ["claims the page makes with no figure, source or example"],\n'
            ' "audience": "who this page is written for"}',
            max_tokens=2600, expect_key="findings", require=("question",))

        quote = (data or {}).get("answer_quote")
        # A quote the page does not contain is a fabrication, and this service exists to catch
        # exactly that failure in other people's pages. Verified against the source, not trusted.
        quote_verified = bool(quote) and _contains(text, quote)
        if quote and not quote_verified:
            ctx.warn("The model returned an answer quote that does not appear verbatim in the page. "
                     "It has been discarded rather than reported.")

        return {"url": url, "page": _page_meta(page),
                "question": (data or {}).get("question"),
                "stated_question": stated_question or None,
                "answered": bool((data or {}).get("answered")) and quote_verified,
                "answer_quote": quote if quote_verified else None,
                "answer_quote_verified": quote_verified,
                "missing": (data or {}).get("missing") or [],
                "unsupported_claims": (data or {}).get("unsupported_claims") or [],
                "audience": (data or {}).get("audience"),
                "measured": {"words": len(text.split()),
                             "statistics": len(STATISTIC.findall(text)),
                             "citable_spans": len(spans),
                             "readability": readability(text),
                             "lead": first_paragraph(page)[:400]},
                "note": ("The answer quote is checked against the page before it is reported. A "
                         "quote the page does not contain is discarded, not shown.")}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        return [
            ValidationCheck(name="any_reported_quote_is_present_in_the_page",
                            passed=(result["answer_quote"] is None)
                            or result["answer_quote_verified"],
                            detail="an unverified quote is a fabrication"),
            ValidationCheck(name="answered_requires_a_verified_quote",
                            passed=(not result["answered"]) or bool(result["answer_quote"])),
            ValidationCheck(name="the_page_was_measured_not_only_described",
                            passed=result["measured"]["words"] > 0),
            ValidationCheck(name="the_audit_identifies_the_page_question",
                            passed=bool(result.get("question")),
                            detail="a deliverable of nulls is not an audit"),
            ValidationCheck(name="optional_fields_are_null_rather_than_invented",
                            passed=result.get("audience") is None
                            or isinstance(result.get("audience"), str),
                            detail="audience is a useful extra, not part of the contract; when the "
                                   "model omits it the field is null rather than filled in"),
        ]


def _contains(haystack: str, needle: str) -> bool:
    """Whitespace- and case-insensitive containment, so formatting differences do not reject a real
    quote while an invented one still fails."""
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    n, h = norm(needle), norm(haystack)
    if not n:
        return False
    if n in h:
        return True
    # A quote clipped mid-sentence should still count if a long run of it is present verbatim.
    words = n.split()
    return len(words) > 12 and " ".join(words[:12]) in h


class ContentBrief(_ContentNode):
    """A page specification precise enough that a writer produces something citable."""
    name = "content.brief"
    price_usdt = 0.10
    requires = ("topic",)
    optional = ()
    example_input = {"topic": "choosing a headless CMS"}

    def run(self, ctx: NodeContext) -> dict:
        raw = ctx.input if isinstance(ctx.input, dict) else {}
        topic = str(raw.get("topic") or raw.get("question") or raw.get("seed") or "").strip()
        if not topic:
            raise NodeError(ErrorCode.INVALID_INPUT,
                            "A 'topic' or 'question' is required — the thing the page should answer.")

        # Grounded in real questions where they can be gathered, so the brief covers what people
        # actually ask rather than what a model imagines they ask.
        real_questions: list[str] = []
        unreachable: list[str] = []
        try:
            from providers.keywords import hackernews_questions, question_suggestions
            real_questions = question_suggestions(topic)[:12]
            real_questions += [q["question"] for q in hackernews_questions(topic, limit=8)]
        except Exception as e:  # noqa: BLE001
            unreachable.append(f"question sources: {type(e).__name__}")
            ctx.warn("Real questions could not be gathered, so the brief is derived from the topic "
                     "alone and covers less of what people actually ask.")

        data = self._ask_json(
            "Write a content brief for a page that must be quotable by AI assistants.\n\n"
            f"TOPIC: {topic}\n"
            + (f"REAL QUESTIONS PEOPLE ASK ABOUT THIS:\n" + "\n".join(f"- {q}" for q in real_questions[:20])
               if real_questions else "")
            + "\n\nThe page must answer its question in the first two sentences, carry concrete "
              "figures, and split into sections a retrieval system can chunk cleanly.\n\n"
              "Return JSON with exactly these keys:\n"
              '{"target_question": "the one question the page answers",\n'
              ' "answer_first_paragraph": "a 2-sentence direct answer to open with",\n'
              ' "sections": [{"heading": "...", "covers": "...", "words": 200}],\n'
              ' "questions_to_answer": ["..."],\n'
              ' "entities_to_mention": ["specific named things the page should reference"],\n'
              ' "schema_types": ["schema.org types this page should emit"],\n'
              ' "figures_to_source": ["the kinds of concrete numbers this page needs"],\n'
              ' "internal_links": ["what this page should link to on the same site"]}',
            max_tokens=2600, expect_key="sections",
            require=("target_question", "answer_first_paragraph", "sections", "schema_types"))

        sections = (data or {}).get("sections") or []
        return {"topic": topic,
                "grounded_in_real_questions": len(real_questions),
                "sources_unreachable": unreachable,
                "brief": data,
                "estimated_words": sum(int(s.get("words") or 0) for s in sections
                                       if isinstance(s, dict)),
                "note": ("This is a specification, not copy. Doxa does not write the page — it "
                         "states what the page has to contain to be citable.")}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        brief = result["brief"] or {}
        sections = brief.get("sections") or []
        return [
            ValidationCheck(name="the_brief_names_a_single_target_question",
                            passed=bool(brief.get("target_question"))),
            ValidationCheck(name="it_opens_with_a_direct_answer",
                            passed=bool(brief.get("answer_first_paragraph")),
                            detail="answer-first structure is the strongest citation signal"),
            ValidationCheck(name="every_section_has_a_heading_and_a_purpose",
                            passed=all(isinstance(s, dict) and s.get("heading") and s.get("covers")
                                       for s in sections)),
            ValidationCheck(name="sections_are_chunkable",
                            passed=all(int(s.get("words") or 0) <= 500 for s in sections
                                       if isinstance(s, dict)),
                            detail="a section over ~500 words is split mid-argument when chunked"),
            ValidationCheck(name="schema_is_specified",
                            passed=bool(brief.get("schema_types"))),
        ]


class ContentCharts(_ContentNode):
    """Turn the figures already in a page into a table and JSON-LD a model can quote exactly."""
    name = "content.charts"
    price_usdt = 0.05
    requires = ("url",)
    optional = ()
    example_input = {'url': 'https://example.com/'}

    def run(self, ctx: NodeContext) -> dict:
        url, page = self._page(ctx)
        text = body_text(page)
        found = STATISTIC.findall(text)
        if not found:
            return {"url": url, "figures": [], "dataset_jsonld": None,
                    "html_table": None,
                    "note": ("The page contains no concrete figures, so there is nothing to "
                             "structure. Adding specific numbers is the prerequisite.")}

        data = self._ask_json(
            "Extract every concrete figure stated in the text below and label it.\n\n"
            "Rules, strictly: use ONLY figures that literally appear in the text. Do not compute, "
            "convert, round or infer any value. If you are unsure what a number refers to, omit it.\n\n"
            f"TEXT:\n{text[:4000]}\n\n"
            'Return JSON: {"figures": [{"label": "what it measures", "value": "exactly as written '
            'in the text", "unit": "percent|currency|count|duration|date|other", '
            '"context": "the phrase it appeared in"}]}',
            max_tokens=2600, expect_key="figures")

        raw_figures = (data or {}).get("figures") or []
        verified, rejected = [], []
        for f in raw_figures:
            if not isinstance(f, dict):
                continue
            value = str(f.get("value") or "").strip()
            label = str(f.get("label") or "").strip()
            # A figure the page does not contain would be a fabricated statistic published as
            # structured data — strictly worse than publishing none. A figure with no value at all
            # (which a truncated response can produce) has nothing to verify and is dropped too.
            if value and label and _contains(text, value):
                verified.append(f)
            else:
                rejected.append(f)
        if rejected:
            ctx.warn(f"{len(rejected)} extracted figure(s) did not appear verbatim in the page and "
                     f"were discarded. Doxa will not publish a number the source does not state.")

        jsonld = {
            "@context": "https://schema.org",
            "@type": "Dataset",
            "name": f"Figures stated on {url}",
            "description": "Figures extracted verbatim from the page, for machine reading.",
            "url": url,
            "variableMeasured": [
                {"@type": "PropertyValue", "name": f.get("label"), "value": f.get("value"),
                 "unitText": f.get("unit")} for f in verified],
        } if verified else None

        rows = "".join(
            f"<tr><td>{_esc(f.get('label'))}</td><td>{_esc(f.get('value'))}</td>"
            f"<td>{_esc(f.get('unit'))}</td></tr>" for f in verified)
        table = (f"<table><caption>Figures stated on this page</caption><thead><tr>"
                 f"<th>Measure</th><th>Value</th><th>Unit</th></tr></thead>"
                 f"<tbody>{rows}</tbody></table>") if verified else None

        return {"url": url,
                "figures": verified,
                "figures_rejected": len(rejected),
                "statistics_detected_in_text": len(found),
                "dataset_jsonld": jsonld,
                "html_table": table,
                "note": ("Every figure here appears verbatim in the page. Anything the model "
                         "produced that the page does not state was discarded.")}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        figures = result["figures"]
        jsonld = result["dataset_jsonld"]
        return [
            ValidationCheck(name="every_published_figure_has_a_label_and_a_value",
                            passed=all(f.get("label") and f.get("value") for f in figures)),
            ValidationCheck(name="jsonld_matches_the_figure_list",
                            passed=(jsonld is None and not figures)
                            or (jsonld is not None
                                and len(jsonld["variableMeasured"]) == len(figures))),
            ValidationCheck(name="jsonld_is_serialisable",
                            passed=jsonld is None or bool(json.dumps(jsonld))),
            ValidationCheck(name="unverifiable_figures_were_discarded",
                            passed=isinstance(result["figures_rejected"], int),
                            detail="a fabricated statistic in structured data is worse than none"),
        ]


def _esc(v: Any) -> str:
    import html
    return html.escape(str(v if v is not None else ""))


ALL_NODES = [ContentAudit(), ContentBrief(), ContentCharts()]
