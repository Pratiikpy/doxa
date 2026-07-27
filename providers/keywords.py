"""Keywords, without a data vendor — and without pretending to be one.

Every keyword tool sells a monthly search volume. Almost none of them measure it; they model it, from
a clickstream panel or a leaked sample, and the number is presented with a precision nobody can
justify. Doxa does not sell that number, because it cannot measure it.

What *is* measurable, keyless and exactly:

  * **What people actually type.** Autocomplete is a live product surface built from real queries.
    Expanding a seed alphabetically and by question word recovers a large, genuine long tail.
  * **What people actually ask.** Stack Exchange and Hacker News are archives of real questions with
    real vote counts attached.
  * **Whether interest is rising or falling.** Wikipedia's pageview API gives a monthly series per
    article — a true demand signal for any topic with an article, and it is a measurement rather than
    a model.

From those a **demand index** is composed: a comparative 0–100 score with its inputs itemised. It is
explicitly not search volume, and every response says so. A relative signal you can audit beats an
absolute number you cannot.
"""
from __future__ import annotations

import concurrent.futures
import json
import re
import string
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import requests

BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}
TOOL_UA = {"User-Agent": "Doxa/1.0 (technical SEO audit)"}

QUESTION_WORDS = ("how", "what", "why", "when", "where", "which", "who", "can", "is", "are",
                  "does", "do", "should", "will")
PREPOSITIONS = ("for", "with", "without", "vs", "versus", "near", "like", "to", "in", "on")
# The intents a keyword can carry. Ordered: the first pattern that matches wins, and commercial
# intent is checked before informational because "best crm software" is both and buys the former.
INTENT_PATTERNS = (
    ("transactional", re.compile(r"\b(buy|price|pricing|cost|cheap|discount|coupon|deal|order|"
                                 r"subscription|trial|quote|hire|book)\b", re.I)),
    ("commercial", re.compile(r"\b(best|top|review|reviews|compare|comparison|vs|versus|"
                              r"alternative|alternatives)\b", re.I)),
    ("navigational", re.compile(r"\b(login|log in|sign in|download|app|dashboard|portal|"
                                r"documentation|docs|support)\b", re.I)),
    ("informational", re.compile(r"\b(how|what|why|when|where|which|who|guide|tutorial|examples?|"
                                 r"meaning|definition|is|are|does|can)\b", re.I)),
)


class SourceUnavailable(RuntimeError):
    """A source failed. Never silently rendered as "no keywords found"."""


@dataclass
class Suggestion:
    phrase: str
    sources: set[str] = field(default_factory=set)
    seen: int = 0                 # how many separate expansions surfaced it

    def as_dict(self) -> dict[str, Any]:
        return {"phrase": self.phrase, "sources": sorted(self.sources),
                "times_suggested": self.seen, "intent": classify_intent(self.phrase),
                "words": len(self.phrase.split())}


def classify_intent(phrase: str) -> str:
    for name, rx in INTENT_PATTERNS:
        if rx.search(phrase):
            return name
    return "informational"


# --- autocomplete ------------------------------------------------------------------------------

def _google_suggest(query: str, timeout: int = 15) -> list[str]:
    r = requests.get("https://suggestqueries.google.com/complete/search",
                     params={"client": "firefox", "q": query},
                     headers=BROWSER_UA, timeout=timeout)
    if r.status_code != 200:
        raise SourceUnavailable(f"Google autocomplete returned HTTP {r.status_code}")
    return [str(s) for s in json.loads(r.text)[1]]


def _ddg_suggest(query: str, timeout: int = 15) -> list[str]:
    r = requests.get("https://duckduckgo.com/ac/", params={"q": query},
                     headers=BROWSER_UA, timeout=timeout)
    if r.status_code != 200:
        raise SourceUnavailable(f"DuckDuckGo autocomplete returned HTTP {r.status_code}")
    return [d.get("phrase", "") for d in r.json() if d.get("phrase")]


ENGINES = {"google": _google_suggest, "duckduckgo": _ddg_suggest}


def expand(seed: str, *, engines: list[str] | None = None, alphabet: bool = True,
           questions: bool = True, prepositions: bool = True,
           workers: int = 6) -> tuple[list[Suggestion], list[dict[str, str]]]:
    """Expand a seed into the long tail people actually type.

    The seed is submitted bare, then suffixed a–z, then prefixed with each question word and
    preposition. Each variation is a separate live query, which is why this is bounded and
    concurrent.
    """
    seed = seed.strip()
    if not seed:
        raise ValueError("a seed keyword is required")

    variations = [seed]
    if alphabet:
        variations += [f"{seed} {c}" for c in string.ascii_lowercase]
    if questions:
        variations += [f"{w} {seed}" for w in QUESTION_WORDS]
    if prepositions:
        variations += [f"{seed} {p}" for p in PREPOSITIONS]

    wanted = engines or list(ENGINES)
    jobs = [(name, v) for name in wanted if name in ENGINES for v in variations]
    found: dict[str, Suggestion] = {}
    failures: list[dict[str, str]] = []

    def one(job: tuple[str, str]) -> tuple[str, list[str], str]:
        name, variation = job
        try:
            return name, ENGINES[name](variation), ""
        except Exception as e:  # noqa: BLE001
            return name, [], f"{type(e).__name__}: {str(e)[:80]}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for name, phrases, error in pool.map(one, jobs):
            if error:
                failures.append({"engine": name, "error": error})
                continue
            for p in phrases:
                p = " ".join(p.lower().split())
                if not p or seed.lower().split()[0] not in p:
                    continue
                s = found.setdefault(p, Suggestion(phrase=p))
                s.sources.add(name)
                s.seen += 1

    # Failures are collapsed per engine: 60 identical timeouts is one fact, not sixty.
    collapsed: dict[str, dict[str, Any]] = {}
    for f in failures:
        row = collapsed.setdefault(f["engine"], {"engine": f["engine"], "failures": 0,
                                                 "example": f["error"]})
        row["failures"] += 1
    ordered = sorted(found.values(), key=lambda s: (-s.seen, s.phrase))
    return ordered, list(collapsed.values())


# --- real questions ------------------------------------------------------------------------------

def stackexchange_questions(query: str, *, site: str = "stackoverflow",
                            limit: int = 25) -> list[dict[str, Any]]:
    r = requests.get("https://api.stackexchange.com/2.3/search/advanced",
                     params={"order": "desc", "sort": "votes", "q": query, "site": site,
                             "pagesize": min(limit, 50), "filter": "default"},
                     headers=TOOL_UA, timeout=30)
    if r.status_code != 200:
        raise SourceUnavailable(f"Stack Exchange returned HTTP {r.status_code}")
    return [{"question": i["title"], "url": i["link"], "score": i.get("score", 0),
             "answered": bool(i.get("is_answered")), "views": i.get("view_count"),
             "tags": i.get("tags", [])[:5], "source": f"stackexchange:{site}"}
            for i in r.json().get("items", [])]


def hackernews_questions(query: str, *, limit: int = 25) -> list[dict[str, Any]]:
    r = requests.get("https://hn.algolia.com/api/v1/search",
                     params={"query": query, "tags": "story", "hitsPerPage": min(limit, 50)},
                     headers=TOOL_UA, timeout=30)
    if r.status_code != 200:
        raise SourceUnavailable(f"Hacker News returned HTTP {r.status_code}")
    return [{"question": h.get("title") or "", "url": f"https://news.ycombinator.com/item?id={h['objectID']}",
             "score": h.get("points", 0), "comments": h.get("num_comments", 0),
             "created": h.get("created_at"), "source": "hackernews"}
            for h in r.json().get("hits", []) if h.get("title")]


def question_suggestions(seed: str) -> list[str]:
    """Autocomplete restricted to question forms — People-Also-Ask, recovered from the source."""
    out: list[str] = []
    for w in QUESTION_WORDS:
        try:
            out.extend(p for p in _google_suggest(f"{w} {seed}") if p.lower().startswith(w))
        except SourceUnavailable:
            continue
    return sorted({" ".join(p.lower().split()) for p in out})


# --- demand ---------------------------------------------------------------------------------------

def wikipedia_interest(topic: str, *, months: int = 12) -> dict[str, Any] | None:
    """A monthly pageview series for the article that best matches this topic.

    A measurement, not a model. Where an article exists this is the strongest honest demand signal
    available without a vendor, and the trend is more useful than the level.
    """
    s = requests.get("https://en.wikipedia.org/w/api.php",
                     params={"action": "query", "list": "search", "srsearch": topic,
                             "srlimit": 1, "format": "json"},
                     headers=TOOL_UA, timeout=30)
    if s.status_code != 200:
        raise SourceUnavailable(f"Wikipedia search returned HTTP {s.status_code}")
    hits = s.json().get("query", {}).get("search", [])
    if not hits:
        return None
    title = hits[0]["title"]

    import datetime as _dt
    end = _dt.date.today().replace(day=1)
    start = end - _dt.timedelta(days=31 * months)
    url = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/"
           f"all-access/user/{urllib.parse.quote(title.replace(' ', '_'), safe='')}/monthly/"
           f"{start:%Y%m%d}/{end:%Y%m%d}")
    p = requests.get(url, headers=TOOL_UA, timeout=30)
    if p.status_code != 200:
        return {"article": title, "views": None,
                "note": f"pageview series unavailable (HTTP {p.status_code})"}
    items = p.json().get("items", [])
    series = [{"month": i["timestamp"][:6], "views": i["views"]} for i in items]
    if len(series) < 4:
        return {"article": title, "series": series, "trend": None}
    half = len(series) // 2
    first = sum(x["views"] for x in series[:half]) / max(1, half)
    second = sum(x["views"] for x in series[half:]) / max(1, len(series) - half)
    change = (second - first) / first if first else 0.0
    return {"article": title,
            "url": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
            "series": series,
            "monthly_average": int(sum(x["views"] for x in series) / len(series)),
            "trend": ("rising" if change > 0.15 else "falling" if change < -0.15 else "steady"),
            "change_ratio": round(change, 3)}


def demand_index(seed: str, *, suggestions: list[Suggestion] | None = None) -> dict[str, Any]:
    """A comparative 0–100 demand signal, with every input shown.

    Composed of four measurable things: how much long tail autocomplete carries, how consistently the
    engines agree, how much genuine community discussion exists, and which way Wikipedia interest is
    moving. Weights are stated so the number can be argued with.

    It is deliberately NOT a search volume. Two keywords can be compared with it; it cannot be turned
    into a traffic forecast, and the response says so.
    """
    components: dict[str, Any] = {}
    unreachable: list[dict[str, str]] = []

    if suggestions is None:
        try:
            suggestions, failures = expand(seed, alphabet=True, questions=True, prepositions=False)
            unreachable.extend(failures)
        except Exception as e:  # noqa: BLE001
            suggestions = []
            unreachable.append({"engine": "autocomplete", "error": str(e)[:100]})

    tail = len(suggestions)
    # 120 distinct suggestions is a rich tail; the curve flattens above that.
    components["long_tail"] = {"suggestions": tail, "score": min(1.0, tail / 120),
                               "weight": 35,
                               "means": "how much of a long tail autocomplete carries for this seed"}

    agreed = sum(1 for s in suggestions if len(s.sources) > 1)
    components["engine_agreement"] = {
        "phrases_on_both_engines": agreed,
        "score": (agreed / tail) if tail else 0.0, "weight": 20,
        "means": "how often both engines surface the same phrase, which filters noise"}

    discussion = 0
    try:
        so = stackexchange_questions(seed, limit=30)
        hn = hackernews_questions(seed, limit=30)
        discussion = len(so) + len(hn)
        components["community"] = {
            "stackexchange": len(so), "hackernews": len(hn),
            "top_score": max([q["score"] for q in so + hn] or [0]),
            "score": min(1.0, discussion / 50), "weight": 25,
            "means": "how much real, dated discussion exists"}
    except Exception as e:  # noqa: BLE001
        unreachable.append({"source": "community", "error": str(e)[:100]})
        components["community"] = {"score": None, "weight": 25, "unavailable": True}

    try:
        wiki = wikipedia_interest(seed)
        if wiki and wiki.get("monthly_average"):
            level = min(1.0, wiki["monthly_average"] / 50_000)
            direction = {"rising": 1.0, "steady": 0.6, "falling": 0.25}.get(wiki.get("trend"), 0.6)
            components["encyclopaedic_interest"] = {
                "article": wiki["article"], "monthly_average": wiki["monthly_average"],
                "trend": wiki["trend"], "score": round((level + direction) / 2, 3), "weight": 20,
                "means": "measured Wikipedia pageviews for the closest article, and their direction"}
        else:
            components["encyclopaedic_interest"] = {
                "score": 0.0, "weight": 20,
                "means": "no Wikipedia article matches this seed, which is itself a signal"}
    except Exception as e:  # noqa: BLE001
        unreachable.append({"source": "wikipedia", "error": str(e)[:100]})
        components["encyclopaedic_interest"] = {"score": None, "weight": 20, "unavailable": True}

    # Unavailable components are dropped and the remaining weights re-normalised, so a source outage
    # lowers confidence rather than silently lowering the score.
    usable = {k: v for k, v in components.items() if v.get("score") is not None}
    total_weight = sum(v["weight"] for v in usable.values())
    index = round(100 * sum(v["score"] * v["weight"] for v in usable.values()) / total_weight, 1) \
        if total_weight else None

    return {"seed": seed,
            "demand_index": index,
            "confidence": round(total_weight / 100, 2),
            "components": components,
            "sources_unreachable": unreachable,
            "caveat": ("This is a comparative demand index, not a monthly search volume. Doxa does "
                       "not measure search volume and will not model one — use this to rank "
                       "keywords against each other, not to forecast traffic.")}


# --- clustering ------------------------------------------------------------------------------------

def _tokens(phrase: str) -> set[str]:
    stop = {"the", "a", "an", "for", "of", "to", "in", "on", "and", "or", "is", "are", "with",
            "best", "top", "how", "what", "why", "vs", "my", "your"}
    return {w for w in re.findall(r"[a-z0-9']+", phrase.lower()) if w not in stop and len(w) > 1}


def cluster(phrases: list[str], *, threshold: float = 0.5) -> list[dict[str, Any]]:
    """Group phrases by shared meaning and intent.

    Jaccard overlap on content words, with intent as a hard boundary: "buy crm" and "what is crm"
    share every word and are not the same job. Splitting on intent is what makes the clusters map
    onto pages rather than onto vocabulary.
    """
    items = [(p, _tokens(p), classify_intent(p)) for p in phrases if p.strip()]
    clusters: list[dict[str, Any]] = []
    for phrase, toks, intent in sorted(items, key=lambda x: -len(x[1])):
        placed = False
        for c in clusters:
            if c["intent"] != intent:
                continue
            overlap = len(toks & c["_tokens"]) / max(1, len(toks | c["_tokens"]))
            if overlap >= threshold:
                c["phrases"].append(phrase)
                c["_tokens"] |= toks
                placed = True
                break
        if not placed:
            clusters.append({"intent": intent, "phrases": [phrase], "_tokens": set(toks)})

    out = []
    for c in sorted(clusters, key=lambda c: -len(c["phrases"])):
        shared = sorted(c["_tokens"] & set.intersection(*[_tokens(p) for p in c["phrases"]])) \
            if len(c["phrases"]) > 1 else sorted(c["_tokens"])
        out.append({"label": " ".join(shared[:4]) or c["phrases"][0],
                    "intent": c["intent"],
                    "size": len(c["phrases"]),
                    "phrases": sorted(c["phrases"])})
    return out
