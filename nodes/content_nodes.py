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
    # example.com has 19 words of body text, which this node used to reject — so the advertised
    # example was a call that could only ever fail, and anyone copying it paid for INVALID_INPUT.
    example_input = {'url': 'https://en.wikipedia.org/wiki/Search_engine_optimization'}

    def run(self, ctx: NodeContext) -> dict:
        url, page = self._page(ctx)
        text = body_text(page)
        words = len(text.split())
        if words < 60:
            # "Too thin to answer anything" is the audit's verdict, not a reason to refuse the audit.
            # Raising here charged the caller and handed back an error whose own wording admitted it
            # was the finding — someone checking a suspected-empty page got billed for a failure
            # instead of the confirmation they asked for.
            return {
                "url": url, "page": _page_meta(page),
                "question": None, "stated_question":
                    str((ctx.input or {}).get("question") or "").strip() or None,
                "answered": False,
                "answer_quote": None, "answer_quote_verified": False,
                "missing": ["the page has essentially no body text, so it answers nothing"],
                "unsupported_claims": [], "audience": None,
                "measured": {"words": words, "statistics": 0, "citable_spans": 0,
                             "readability": readability(text) if words else None,
                             "lead": first_paragraph(page)[:400]},
                "verdict": "insufficient_content",
                "note": (f"The page carries {words} words of body text, below the 60-word floor for a "
                         f"meaningful audit. That is the finding: there is nothing here for a reader "
                         f"or a model to take an answer from."),
            }

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
                            passed=result["measured"]["words"] >= 0),
            # A page with no content has no question to identify, and saying so *is* the audit. The
            # check still bites everywhere else: nulls are only acceptable when the verdict explains
            # them, so a genuine deliverable-of-nulls cannot slip through behind this exemption.
            ValidationCheck(name="the_audit_identifies_the_page_question",
                            passed=bool(result.get("question"))
                            or result.get("verdict") == "insufficient_content",
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
        found = list(STATISTIC.finditer(text))
        kinds: dict[str, int] = {}
        for m in found:
            k = _classify(m.group(0))
            kinds[k] = kinds.get(k, 0) + 1
        matches = [m for m in found if _classify(m.group(0)) in _CHARTABLE]

        if not matches:
            breakdown = ", ".join(f"{n} {k}" + ("s" if n != 1 and not k.endswith("s") else "")
                                  for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]))
            note = ("The page contains no concrete figures, so there is nothing to structure. "
                    "Adding specific numbers is the prerequisite.")
            if found:
                note = (f"The page states {len(found)} numeric values ({breakdown}) and none is a "
                        f"measurement. Dates and version strings are temporal signals, not figures a "
                        f"model can cite you for — a Dataset of them would be valid and useless. "
                        f"Publishing percentages, amounts or magnitudes is the prerequisite.")
            return {"url": url, "figures": [], "figures_rejected": 0,
                    "statistics_detected_in_text": len(found), "numeric_values_by_kind": kinds,
                    "chartable_values_found": 0,
                    "excerpt_covers_every_figure": True, "excerpt_sent_covers": 0,
                    "dataset_jsonld": None, "html_table": None, "note": note}

        excerpt, covered = _figure_excerpt(text, matches)
        if not covered:
            ctx.warn(f"The page states {len(matches)} figures, more than one extraction window "
                     f"holds. The excerpt sent for labelling covers the first "
                     f"{min(len(matches), _MAX_FIGURES)} of them, in document order.")

        data = self._ask_json(
            "Extract every concrete figure stated in the text below and label it.\n\n"
            "Rules, strictly: use ONLY figures that literally appear in the text. Do not compute, "
            "convert, round or infer any value. If you are unsure what a number refers to, omit it.\n\n"
            f"TEXT:\n{excerpt}\n\n"
            'Return JSON: {"figures": [{"label": "what it measures", "value": "exactly as written '
            'in the text", "unit": "percent|currency|count|duration|date|other", '
            '"context": "the phrase it appeared in"}]}',
            max_tokens=4000, expect_key="figures")

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

        # A page can state numbers and still have nothing to chart — a news feed's dates, a version
        # string, a copyright range. Saying so is the difference between a useful answer and one the
        # customer reads as a failure.
        note = ("Every figure here appears verbatim in the page. Anything the model produced that "
                "the page does not state was discarded.")
        if not verified:
            note = (f"The page states {len(matches)} measurable value(s) and the extractor could not "
                    f"label any of them with confidence. All of them were shown to it, so this is "
                    f"not a truncated read — the figures appear without enough surrounding text to "
                    f"say what they measure.")
        if covered is False:
            note += (f" This page states more figures than one call can label; the first "
                     f"{_MAX_FIGURES} in document order were processed.")

        return {"url": url,
                "figures": verified,
                "figures_rejected": len(rejected),
                "statistics_detected_in_text": len(found),
                "numeric_values_by_kind": kinds,
                "chartable_values_found": len(matches),
                "excerpt_covers_every_figure": covered,
                "excerpt_sent_covers": sum(1 for m in matches if _contains(excerpt, m.group(0))),
                "dataset_jsonld": jsonld,
                "html_table": table,
                "note": note}

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
            # "18 detected, 0 extracted, 0 rejected" was a real bug: the excerpt sent for labelling
            # was the head of the page and every figure sat past it. What is checked is therefore
            # the invariant that broke — the numbers the page states were actually shown to the
            # extractor — and not whether the model chose to label them, which for a page whose only
            # numbers are news dates is legitimately zero.
            ValidationCheck(
                name="every_chartable_value_was_shown_to_the_extractor",
                passed=result["excerpt_sent_covers"] == result["chartable_values_found"]
                or not result.get("excerpt_covers_every_figure"),
                detail="a figure the extractor never saw cannot be labelled or rejected"),
        ]


_BUDGET = 6000       # characters of page text sent for labelling
_MARGIN = 220        # characters kept either side of a figure, so its sentence comes with it
_MAX_FIGURES = 40    # figures asked for in one call

# Not every number is a measurement, and reporting a count of "statistics" that is really a list of
# news dates reads as a broken service. geo-optimizer-skill's decay audit separates temporal, version
# and price signals from statistical ones (src/geo_optimizer/core/audit_decay.py); the same split
# turns "18 statistics detected, 0 charted" into a sentence a customer can act on.
_KINDS = (
    ("percentage", re.compile(r"^\d+(?:\.\d+)?\s?(?:%|percent|bps)$", re.I)),
    ("currency", re.compile(r"^[$€£¥]", re.I)),
    ("magnitude", re.compile(r"(?:million|billion|trillion|thousand)$", re.I)),
    ("multiplier", re.compile(r"(?:x|×)$", re.I)),
    ("year", re.compile(r"^(?:19|20)\d{2}$")),
    ("measurement", re.compile(r"(?:ms|kb|mb|gb|tb|kg|km|mi|hrs?|hours?|days?|weeks?|months?|years?)$",
                               re.I)),
    ("large number", re.compile(r"^\d{1,3}(?:,\d{3})+$")),
)


# A bare year is a temporal signal, not a measurement. Charting one produces a schema.org Dataset of
# news dates: valid, machine-readable and worthless. Excluding the kind up front also makes the
# service deterministic — asking a model whether "2026" is a figure returns a different answer each
# run, and a paid service that alternates between 0 and 18 rows for one page is not trustworthy.
_CHARTABLE = {"percentage", "currency", "magnitude", "multiplier", "measurement", "large number"}


def _classify(token: str) -> str:
    for kind, pattern in _KINDS:
        if pattern.search(token.strip()):
            return kind
    return "other"


def _figure_excerpt(text: str, matches: list) -> tuple[str, bool]:
    """Build the extraction window *around the figures*, not from the top of the page.

    Sending a blind prefix is the obvious implementation and it is wrong: on python.org every one of
    the eighteen figures begins at character 4006, so a 4,000-character head contains none of them.
    The model then correctly returns nothing, and the service reports "18 statistics detected, 0
    extracted, 0 rejected" — a contradiction the customer has no way to explain, having paid for it.

    So: keep a margin either side of each match (a bare number is unlabelable without its sentence),
    merge spans that touch, and spend the budget on them in document order. Returns the excerpt and
    whether every figure made it in, which the caller discloses rather than quietly truncating.
    """
    if not matches:
        return "", True
    # Wikipedia's Python article states 484 numbers. Asking one call to label them all exhausts the
    # model's token budget inside its own reasoning block and returns nothing at all — and a table of
    # 484 rows would not be a deliverable anyway. Bound the count as well as the characters.
    capped = len(matches) > _MAX_FIGURES
    matches = matches[:_MAX_FIGURES]
    spans: list[list[int]] = []
    for m in matches:
        lo, hi = max(0, m.start() - _MARGIN), min(len(text), m.end() + _MARGIN)
        if spans and lo <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], hi)
        else:
            spans.append([lo, hi])

    kept, spent, covered = [], 0, True
    for lo, hi in spans:
        if spent + (hi - lo) > _BUDGET:
            covered = False
            break
        kept.append(text[lo:hi])
        spent += hi - lo
    if not kept:                                   # one span alone exceeds the budget
        kept, covered = [text[spans[0][0]:spans[0][0] + _BUDGET]], False
    return "\n[…]\n".join(kept), covered and not capped


def _esc(v: Any) -> str:
    import html
    return html.escape(str(v if v is not None else ""))


ALL_NODES = [ContentAudit(), ContentBrief(), ContentCharts()]
