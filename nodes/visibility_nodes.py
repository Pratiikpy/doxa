"""Group D — AI visibility. What models actually say when someone asks about your market.

Every other group in Doxa reads a page. This group asks the question the customer really has: *when a
buyer asks an assistant what to use, do I come up?* Nothing on their own site answers that.

Three commitments hold this group together, and each exists because the obvious implementation would
be dishonest.

**An outage is not an absence.** If the model could not be asked, the answer is "not measured", never
"you were not mentioned". The customer would act on the second one. `LLMUnavailable` propagates as a
failure; it is never caught and rendered as a zero.

**A mention is matched against the page, not guessed.** Asking a model "is this brand mentioned?" and
trusting the yes/no invites both invention and denial. Instead the model's answer text is searched for
the brand and its domain directly, and the matched span is returned as evidence. The reader can check
the claim without trusting either the model or us.

**One sample is not a measurement.** Models are stochastic. A single run that omits a brand proves
nothing, so each prompt is asked `samples` times and the result is a rate with the raw runs attached.
"""
from __future__ import annotations

import concurrent.futures
import re
import urllib.parse
from typing import Any

from bs4 import BeautifulSoup

from checks.challenge import looks_auditable
from contract import ErrorCode, ValidationCheck
from fetch import FetchError, Page, SsrfError, fetch
from providers.llm import LLM, LLMUnavailable, as_object
from runtime import Node, NodeContext, NodeError

MAX_PROMPTS = 12
MAX_SAMPLES = 5


def _brand_from(page: Page | None, explicit: str | None, url: str) -> tuple[str, list[str]]:
    """The brand name and the aliases a mention may legitimately use.

    Taken from what the site says about itself — og:site_name, schema Organization, the title suffix —
    falling back to the domain label. Guessing a brand would make every later match unreliable.
    """
    aliases: list[str] = []
    host = urllib.parse.urlsplit(url).hostname or ""
    label = next((p for p in host.split(".") if p not in ("www", "com", "org", "net", "io", "dev",
                                                          "app", "co", "ai", "xyz")), host)
    if page is not None and page.html:
        s = BeautifulSoup(page.html, "lxml")
        for m in s.find_all("meta"):
            if (m.get("property") or "").lower() == "og:site_name":
                aliases.append((m.get("content") or "").strip())
        for script in s.find_all("script", type=lambda v: v and "ld+json" in v.lower()):
            for name in re.findall(r'"name"\s*:\s*"([^"]{2,60})"', script.string or "")[:4]:
                aliases.append(name.strip())
        if s.title:
            parts = re.split(r"[|\-–—•]", s.title.get_text())
            if len(parts) > 1:
                aliases.append(parts[-1].strip())
    brand = (explicit or "").strip() or next((a for a in aliases if a), label)
    seen: list[str] = []
    for a in [brand, *aliases, label, host]:
        a = (a or "").strip()
        if a and a.lower() not in {x.lower() for x in seen}:
            seen.append(a)
    return brand, seen


def _mentions(text: str, aliases: list[str], host: str) -> dict[str, Any] | None:
    """Find the brand in the model's answer and return the evidence, or None.

    Matched on word boundaries so that "Reach" does not match "reached", and the surrounding sentence
    is returned so a reader can judge the claim rather than take our word for it.
    """
    for alias in aliases:
        if len(alias) < 2:
            continue
        for m in re.finditer(rf"\b{re.escape(alias)}\b", text, re.I):
            start = max(0, text.rfind(".", 0, m.start()) + 1)
            end = text.find(".", m.end())
            end = len(text) if end == -1 else end + 1
            return {"matched": alias, "position": m.start(),
                    "context": text[start:end].strip()[:300]}
    if host and host.lower() in text.lower():
        i = text.lower().find(host.lower())
        return {"matched": host, "position": i, "context": text[max(0, i - 120):i + 120].strip()}
    return None


# A recommendation item, as models actually format them: a numbered entry, a markdown heading, a table
# row, or a top-level bullet that leads with a bolded name. Deliberately NOT plain sub-bullets — those
# are the feature lines *under* a recommendation.
_NUMBERED = re.compile(r"^\s{0,3}\d+[.)]\s+(\S.*)$")
_HEADING = re.compile(r"^\s{0,3}#{2,4}\s+(\S.*)$")
_TABLE_ROW = re.compile(r"^\s*\|\s*([^|]+?)\s*\|")
_BOLD_BULLET = re.compile(r"^\s{0,3}[-*•]\s+\*\*([^*]+)\*\*")
_TABLE_DIVIDER = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def ranked_items(text: str) -> list[str]:
    """The model's recommendations, in the order it gave them.

    Only structures that unambiguously start a new recommendation count. An earlier version treated
    every bullet as an item, so the feature lines beneath a recommendation were counted too: an answer
    whose literal words were "Top pick: Notion" was reported as rank 6. A wrong rank is worse than no
    rank, because the customer believes it.
    """
    items: list[str] = []
    for line in (text or "").splitlines():
        if _TABLE_DIVIDER.match(line):
            continue
        for rx in (_NUMBERED, _HEADING, _BOLD_BULLET, _TABLE_ROW):
            m = rx.match(line)
            if m:
                label = m.group(1).strip()
                # A heading ending in a colon labels a section ("Other strong options:") rather than
                # naming a recommendation, and counting it pushes every product below it down a place.
                if not label or label.rstrip().endswith(":"):
                    break
                if label.lower().startswith(("tool", "name", "option", "---")):
                    break
                items.append(label)
                break
    return items


def _rank_of(text: str, evidence: dict[str, Any]) -> int | None:
    """Where the brand sits among the model's recommendations, or None when there is no clear order."""
    items = ranked_items(text)
    if len(items) < 3:
        return None
    for i, label in enumerate(items, start=1):
        if re.search(rf"\b{re.escape(evidence['matched'])}\b", label, re.I):
            return i
    return None


def _prominence(text: str, evidence: dict[str, Any]) -> float | None:
    """How early the brand appears, as a fraction of the answer.

    Always computable and never wrong, unlike a rank — useful precisely when the answer is prose with
    no list to rank within.
    """
    if not text or evidence is None:
        return None
    return round(evidence["position"] / max(1, len(text)), 3)


class _VisibilityNode(Node):
    asp_type = "A2MCP"
    engine = "0g-compute"
    engine_version = "1.0"
    deterministic = False

    def engine_available(self) -> bool:
        return LLM().available

    def _setup(self, ctx: NodeContext) -> tuple[str, str, list[str], Page | None]:
        raw = ctx.input if isinstance(ctx.input, dict) else {}
        url = str(raw.get("url") or "").strip()
        brand_in = str(raw.get("brand") or "").strip()
        if not url and not brand_in:
            raise NodeError(ErrorCode.INVALID_INPUT,
                            "Provide a 'url', a 'brand', or both. Without one of them there is "
                            "nothing to look for in the models' answers.")
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url

        page = None
        if url:
            try:
                page = fetch(url, timeout=30)
                ok, why = looks_auditable(page)
                if not ok:
                    # The brand can still be measured; only the self-description is unavailable.
                    ctx.warn(f"The site could not be read ({why}) so the brand name and aliases were "
                             f"taken from the domain alone.")
            except (SsrfError, FetchError) as e:
                ctx.warn(f"The site could not be fetched ({e}); brand aliases came from the domain.")
        brand, aliases = _brand_from(page, brand_in, url or f"https://{brand_in}")
        host = urllib.parse.urlsplit(url).hostname or ""
        return brand, host, aliases, page

    def _ask_many(self, ctx: NodeContext, prompts: list[str], samples: int,
                  system: str = "") -> list[dict[str, Any]]:
        """Ask each prompt `samples` times across the model chain, keeping every raw answer.

        A failure to ask is recorded per run and never silently becomes a negative observation.
        """
        llm = LLM()
        runs: list[dict[str, Any]] = []
        for prompt in prompts:
            for i in range(samples):
                try:
                    c = llm.complete(prompt, system=system, temperature=0.3 if samples > 1 else 0.0,
                                     max_tokens=900)
                    runs.append({"prompt": prompt, "sample": i + 1, "asked": True,
                                 "model": c.model, "answer": c.text,
                                 "tee_verified": c.tee_verified})
                except LLMUnavailable as e:
                    runs.append({"prompt": prompt, "sample": i + 1, "asked": False,
                                 "error": str(e)[:300]})
        if not any(r["asked"] for r in runs):
            raise NodeError(ErrorCode.ENGINE_UNAVAILABLE,
                            "No model could be reached, so nothing was measured. This is an outage "
                            "on our side and says nothing about your visibility. "
                            + str(runs[0].get("error", ""))[:200])
        unanswered = sum(1 for r in runs if not r["asked"])
        if unanswered:
            ctx.warn(f"{unanswered} of {len(runs)} model calls failed and are excluded from the "
                     f"rates below rather than counted as a non-mention.")
        return runs


class AIVisibility(_VisibilityNode):
    """Ask what a buyer would ask, and measure whether the brand comes up."""
    name = "ai.visibility"
    price_usdt = 0.10
    requires = ("url",)
    optional = ("brand", "prompts", "samples")
    example_input = {"url": "https://example.com/"}

    def run(self, ctx: NodeContext) -> dict:
        brand, host, aliases, page = self._setup(ctx)
        opts = ctx.options or {}
        samples = max(1, min(int(opts.get("samples", 3)), MAX_SAMPLES))
        prompts = [str(p).strip() for p in (ctx.input.get("prompts") or []) if str(p).strip()]
        if not prompts:
            prompts = self._derive_prompts(ctx, brand, page)
        prompts = prompts[:MAX_PROMPTS]

        runs = self._ask_many(ctx, prompts, samples)
        by_prompt: dict[str, dict[str, Any]] = {}
        for r in runs:
            slot = by_prompt.setdefault(r["prompt"], {"prompt": r["prompt"], "asked": 0,
                                                      "mentioned": 0, "ranks": [], "runs": []})
            if not r["asked"]:
                slot["runs"].append({"sample": r["sample"], "asked": False, "error": r["error"]})
                continue
            slot["asked"] += 1
            ev = _mentions(r["answer"], aliases, host)
            rank = _rank_of(r["answer"], ev) if ev else None
            if ev:
                slot["mentioned"] += 1
                if rank:
                    slot["ranks"].append(rank)
            slot["runs"].append({"sample": r["sample"], "asked": True, "model": r["model"],
                                 "mentioned": bool(ev), "rank": rank,
                                 "prominence": _prominence(r["answer"], ev) if ev else None,
                                 "evidence": ev, "answer": r["answer"][:1200]})
        for slot in by_prompt.values():
            slot["mention_rate"] = (round(slot["mentioned"] / slot["asked"], 3)
                                    if slot["asked"] else None)
            slot["best_rank"] = min(slot["ranks"]) if slot["ranks"] else None

        answered = sum(s["asked"] for s in by_prompt.values())
        mentioned = sum(s["mentioned"] for s in by_prompt.values())
        return {
            "brand": brand, "aliases": aliases, "site": host or None,
            "prompts_asked": list(by_prompt),
            "samples_per_prompt": samples,
            "overall": {"answers_measured": answered,
                        "answers_mentioning_brand": mentioned,
                        "mention_rate": round(mentioned / answered, 3) if answered else None},
            "by_prompt": list(by_prompt.values()),
            "note": ("Mentions are found by searching each model's answer for the brand and its "
                     "domain; the matched sentence is included as evidence for every hit."),
        }

    def _derive_prompts(self, ctx: NodeContext, brand: str, page: Page | None) -> list[str]:
        """Ask a model what a *customer* would ask — never naming the brand in the question.

        A prompt containing the brand name guarantees the brand appears in the answer and measures
        nothing. These have to be the questions a buyer asks before they know the brand exists.
        """
        description = ""
        if page is not None and page.html:
            s = BeautifulSoup(page.html, "lxml")
            desc = next((m.get("content") or "" for m in s.find_all("meta")
                         if (m.get("name") or "").lower() == "description"), "")
            title = s.title.get_text().strip() if s.title else ""
            description = f"{title}. {desc}".strip()
        if not description:
            description = brand
            ctx.warn("The site gave no title or description, so the buyer questions were derived "
                     "from the brand name alone and may be broad.")

        llm = LLM()
        base = ("A company is described below. Write the 6 questions a potential customer would type "
                "into an AI assistant while looking for this kind of product, BEFORE they know this "
                "company exists.\n\n"
                "Rules: never name the company or its domain. Ask about the problem or the category, "
                "not the brand. Make them the sort of thing a real buyer types.\n\n"
                f"Description: {description[:600]}\n\n"
                'Return JSON: {"questions": ["...", "..."]}')
        # Two attempts, because the discard rule can legitimately empty the list: a model that names
        # the brand in all six questions leaves nothing usable, and failing there would charge the
        # customer for a call that produced nothing when a corrected retry usually succeeds. The
        # second attempt is told exactly what was wrong with the first.
        attempts = [base,
                    base + (f"\n\nIMPORTANT: your previous answer used the word \"{brand}\" in every "
                            f"question. A question naming the company guarantees it appears in the "
                            f"answer and therefore measures nothing. Describe the problem and the "
                            f"product category only.")]
        last_error = ""
        for i, prompt in enumerate(attempts):
            try:
                data, _ = llm.complete_json(prompt, max_tokens=700)
            except (LLMUnavailable, ValueError) as e:
                last_error = str(e)[:150]
                continue
            qs = [str(q).strip() for q in as_object(data, "questions").get("questions", [])
                  if str(q).strip()]
            leaked = [q for q in qs if brand.lower() in q.lower()]
            qs = [q for q in qs if q not in leaked]
            if leaked:
                ctx.warn(f"{len(leaked)} generated question(s) named the brand and were discarded; a "
                         f"question containing the brand guarantees a mention and measures nothing.")
            if qs:
                if i:
                    ctx.warn("The first set of questions all named the brand; they were regenerated.")
                return qs
            last_error = "every generated question named the brand"

        raise NodeError(ErrorCode.ENGINE_FAILED,
                        f"No buyer questions could be derived after two attempts ({last_error}), and "
                        f"asking with none would measure nothing. Supply 'prompts' explicitly to "
                        f"proceed.")

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        slots = result["by_prompt"]
        every_hit_has_evidence = all(
            (not r.get("mentioned")) or (r.get("evidence") and r["evidence"].get("context"))
            for s in slots for r in s["runs"] if r.get("asked"))
        rates_match_counts = all(
            s["mention_rate"] is None or
            abs(s["mention_rate"] - s["mentioned"] / s["asked"]) < 1e-6 for s in slots)
        no_brand_in_prompts = all(result["brand"].lower() not in p.lower()
                                  for p in result["prompts_asked"])
        failures_excluded = all(
            s["asked"] == sum(1 for r in s["runs"] if r.get("asked")) for s in slots)
        return [
            ValidationCheck(name="every_reported_mention_carries_the_matched_sentence",
                            passed=every_hit_has_evidence,
                            detail="a mention without evidence is unverifiable"),
            ValidationCheck(name="rates_are_derived_from_the_runs",
                            passed=rates_match_counts),
            ValidationCheck(name="prompts_never_name_the_brand",
                            passed=no_brand_in_prompts,
                            detail="naming the brand guarantees a mention and measures nothing"),
            ValidationCheck(name="failed_calls_are_excluded_not_counted_as_absence",
                            passed=failures_excluded,
                            detail="an outage must never be reported as invisibility"),
            ValidationCheck(name="at_least_one_answer_was_measured",
                            passed=result["overall"]["answers_measured"] > 0),
        ]


class AIBrand(_VisibilityNode):
    """How do models describe this brand — accurately, or from stale or invented knowledge?"""
    name = "ai.brand"
    price_usdt = 0.10
    requires = ("url",)
    optional = ("brand", "samples")
    example_input = {"url": "https://example.com/"}

    def run(self, ctx: NodeContext) -> dict:
        brand, host, aliases, page = self._setup(ctx)
        samples = max(1, min(int((ctx.options or {}).get("samples", 3)), MAX_SAMPLES))
        prompts = [f"What is {brand}? Describe what they do, who they are for, and anything notable "
                   f"about them. If you do not know, say so plainly rather than guessing."]
        runs = self._ask_many(ctx, prompts, samples)

        # What the site says about itself, so the model's description can be compared with the source.
        self_description = ""
        if page is not None and page.html:
            s = BeautifulSoup(page.html, "lxml")
            desc = next((m.get("content") or "" for m in s.find_all("meta")
                         if (m.get("name") or "").lower() == "description"), "")
            self_description = f"{s.title.get_text().strip() if s.title else ''}. {desc}".strip()

        described: list[dict[str, Any]] = []
        for r in runs:
            if not r["asked"]:
                described.append({"sample": r["sample"], "asked": False, "error": r["error"]})
                continue
            text = r["answer"]
            disclaims = bool(re.search(
                r"\b(?:i (?:do not|don't) (?:have|know)|no information|not familiar|unable to find|"
                r"cannot find|as of my (?:last )?(?:knowledge|training|update))\b", text, re.I))
            described.append({"sample": r["sample"], "asked": True, "model": r["model"],
                              "recognised": not disclaims,
                              "description": text[:1500]})

        recognised = sum(1 for d in described if d.get("recognised"))
        measured = sum(1 for d in described if d.get("asked"))
        return {"brand": brand, "site": host or None,
                "self_description": self_description or None,
                "measured": measured,
                "recognition_rate": round(recognised / measured, 3) if measured else None,
                "descriptions": described,
                "note": ("'Recognised' means the model produced a description rather than saying it "
                         "does not know. Whether the description is *correct* is for you to judge "
                         "against your own copy, shown above as self_description.")}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        d = result["descriptions"]
        return [
            ValidationCheck(name="every_description_is_returned_in_full_for_inspection",
                            passed=all(x.get("description") for x in d if x.get("asked"))),
            ValidationCheck(name="recognition_rate_matches_the_samples",
                            passed=result["recognition_rate"] is None or
                            abs(result["recognition_rate"] -
                                sum(1 for x in d if x.get("recognised")) / result["measured"]) < 1e-6),
            ValidationCheck(name="accuracy_is_not_claimed",
                            passed="correct" in result["note"],
                            detail="the service reports what models say, not whether it is true"),
        ]


class AICitations(_VisibilityNode):
    """Who gets recommended instead — the competitors a model names when you are absent."""
    name = "ai.citations"
    price_usdt = 0.10
    requires = ("url",)
    optional = ("brand", "prompts", "samples")
    example_input = {"url": "https://example.com/"}

    def run(self, ctx: NodeContext) -> dict:
        brand, host, aliases, page = self._setup(ctx)
        opts = ctx.options or {}
        samples = max(1, min(int(opts.get("samples", 3)), MAX_SAMPLES))
        prompts = [str(p).strip() for p in (ctx.input.get("prompts") or []) if str(p).strip()]
        if not prompts:
            prompts = AIVisibility()._derive_prompts(ctx, brand, page)[:4]

        runs = self._ask_many(ctx, prompts[:MAX_PROMPTS], samples)
        rivals: dict[str, dict[str, Any]] = {}
        llm = LLM()

        def extract(r: dict) -> tuple[dict, Any]:
            """One extraction per answer. These are independent, so they run together — serially the
            service cost one model round-trip per sample and grew linearly with the sample size."""
            try:
                data, _ = llm.complete_json(
                    "List every company, product or website named in the text below. Return only "
                    "names that actually appear in it — do not add any others.\n\n"
                    f"TEXT:\n{r['answer'][:4000]}\n\n"
                    'Return JSON: {"names": ["...", "..."]}', max_tokens=700)
                return r, data
            except (LLMUnavailable, ValueError):
                return r, None

        answered = [r for r in runs if r["asked"]]
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, max(1, len(answered)))) as pool:
            extracted = list(pool.map(extract, answered))

        for r, data in extracted:
            if data is None:
                ctx.warn("One answer could not be parsed for named competitors and was skipped.")
                continue
            for name in as_object(data, "names").get("names", []):
                name = str(name).strip()
                # Only names actually present in the answer: the extraction model is not permitted to
                # introduce a competitor that was never recommended.
                if not name or not re.search(rf"\b{re.escape(name)}\b", r["answer"], re.I):
                    continue
                if any(a.lower() == name.lower() for a in aliases):
                    continue
                slot = rivals.setdefault(name.lower(), {"name": name, "times_named": 0,
                                                        "prompts": set()})
                slot["times_named"] += 1
                slot["prompts"].add(r["prompt"])

        table = sorted(({"name": v["name"], "times_named": v["times_named"],
                         "prompts": sorted(v["prompts"])} for v in rivals.values()),
                       key=lambda x: -x["times_named"])
        measured = sum(1 for r in runs if r["asked"])
        you = sum(1 for r in runs if r["asked"] and _mentions(r["answer"], aliases, host))
        return {"brand": brand, "site": host or None,
                "answers_measured": measured,
                "answers_naming_you": you,
                "competitors": table[:40],
                "note": ("Every competitor listed was verified to appear literally in the model's "
                         "answer; names the extraction step invented are discarded.")}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        return [
            ValidationCheck(name="competitors_are_ranked_by_frequency",
                            passed=all(result["competitors"][i]["times_named"] >=
                                       result["competitors"][i + 1]["times_named"]
                                       for i in range(len(result["competitors"]) - 1))),
            ValidationCheck(name="the_brand_is_not_listed_as_its_own_competitor",
                            passed=all(c["name"].lower() != result["brand"].lower()
                                       for c in result["competitors"])),
            ValidationCheck(name="answers_were_measured",
                            passed=result["answers_measured"] > 0),
        ]


class AIPrompts(_VisibilityNode):
    """The questions worth targeting — derived from what buyers ask, not from a keyword tool."""
    name = "ai.prompts"
    price_usdt = 0.05
    requires = ("url",)
    optional = ("brand",)
    example_input = {"url": "https://example.com/"}

    def run(self, ctx: NodeContext) -> dict:
        brand, host, aliases, page = self._setup(ctx)
        questions = AIVisibility()._derive_prompts(ctx, brand, page)
        return {"brand": brand, "site": host or None,
                "questions": questions,
                "note": ("These are buyer questions that never name the brand, so they can be used "
                         "as a baseline for ai.visibility.")}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        qs = result["questions"]
        return [
            ValidationCheck(name="questions_were_produced", passed=bool(qs)),
            ValidationCheck(name="no_question_names_the_brand",
                            passed=all(result["brand"].lower() not in q.lower() for q in qs)),
            ValidationCheck(name="questions_are_distinct",
                            passed=len({q.lower() for q in qs}) == len(qs)),
        ]


ALL_NODES = [AIVisibility(), AIBrand(), AICitations(), AIPrompts()]
