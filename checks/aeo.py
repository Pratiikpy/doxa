"""AEO — whether a model can answer *from* this page.

Classic SEO asks whether a page can rank. This asks something different: when a model is composing an
answer and has this page in front of it, can it lift a clean, attributable statement out?

The signals come from the GEO literature — Princeton's KDD 2024 work on generative engine optimisation,
extended by geo-optimizer's rubric — and they are consistent about what earns a citation: a direct
answer near the top, concrete numbers, named sources, structure a chunker can split on, and a clear
author. Padding, hedging and buried conclusions lose to all of it.

Everything here is measured from the document. Where a claim needs the network — robots.txt, llms.txt,
the AI discovery endpoints — it lives in `geo_score.py` instead, so that a check can never quietly
depend on a fetch that failed.
"""
from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from checks.base import Finding, Severity, registry
from checks.page_html import body_text, soup
from fetch import Page

SENTENCE_END = re.compile(r"[.!?]+(?=\s|$)")
VOWEL_RUN = re.compile(r"[aeiouy]+")
# A statistic is a number carrying a unit or magnitude — "5" alone is not evidence, "5%", "$5m",
# "5,000" and "2024" are. This is what the GEO work means by "cite statistics".
STATISTIC = re.compile(
    r"(?<![\w.])(?:"
    r"\d{1,3}(?:,\d{3})+"                        # 1,200,000
    r"|\d+(?:\.\d+)?\s?%"                        # 43%, 43.5 %
    r"|[$€£¥]\s?\d+(?:[.,]\d+)*\s?[kmbt]?\b"     # $49, £1.2m
    r"|\d+(?:\.\d+)?\s?(?:x|×)\b"                # 3x
    r"|\b(?:19|20)\d{2}\b"                       # a year
    r"|\d+(?:\.\d+)?\s?(?:million|billion|trillion|thousand|percent|bps)\b"
    r"|\d+(?:\.\d+)?\s?(?:ms|kb|mb|gb|tb|kg|km|mi|hrs?|hours?|days?|weeks?|months?|years?)\b"
    r")", re.I)

# Domains whose presence in a link is a genuine authority signal rather than a nav item.
AUTHORITY_HINTS = (".gov", ".edu", ".ac.uk", "wikipedia.org", "wikidata.org", "doi.org",
                   "arxiv.org", "pubmed.ncbi.nlm.nih.gov", "nature.com", "science.org",
                   "who.int", "oecd.org", "worldbank.org", "iso.org", "ietf.org", "w3.org",
                   "nist.gov", "acm.org", "ieee.org", "jstor.org", "ssrn.com")

HEDGES = ("arguably", "it could be argued", "some say", "many believe", "it is thought",
          "generally speaking", "in some cases", "it depends", "more or less", "sort of",
          "kind of", "perhaps", "possibly", "we think", "it seems", "tends to")

# The filler that marks generated padding. Each is a phrase that carries no information.
FILLER = ("in today's fast-paced world", "in the ever-evolving", "it is important to note",
          "when it comes to", "at the end of the day", "in conclusion", "last but not least",
          "needless to say", "the world of", "in this article, we will", "let's dive in",
          "unlock the power", "game-changer", "in the digital age", "navigating the")


def _syllables(word: str) -> int:
    """Approximate syllable count — the standard heuristic behind every Flesch implementation."""
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    n = len(VOWEL_RUN.findall(w))
    if w.endswith("e") and not w.endswith(("le", "ee", "ye")) and n > 1:
        n -= 1
    return max(1, n)


def readability(text: str) -> dict[str, Any]:
    """Flesch Reading Ease and the two numbers it is built from.

    206.835 − 1.015·(words/sentences) − 84.6·(syllables/words), Flesch's published coefficients.
    """
    sentences = [s for s in SENTENCE_END.split(text) if s.strip()]
    words = re.findall(r"[A-Za-z']+", text)
    if not sentences or not words:
        return {"flesch": None, "words": len(words), "sentences": len(sentences)}
    syl = sum(_syllables(w) for w in words)
    wps = len(words) / len(sentences)
    spw = syl / len(words)
    return {"flesch": round(206.835 - 1.015 * wps - 84.6 * spw, 1),
            "words_per_sentence": round(wps, 1),
            "syllables_per_word": round(spw, 2),
            "words": len(words), "sentences": len(sentences)}


def first_paragraph(page: Page) -> str:
    """The first substantial paragraph — what a model reads before deciding the page is relevant."""
    s = soup(page)
    root = s.body or s
    for p in root.find_all(["p", "li"]):
        t = re.sub(r"\s+", " ", p.get_text(" ")).strip()
        if len(t.split()) >= 12:
            return t
    return ""


@registry.register("aeo", "Answer-first structure")
def check_answer_structure(page: Page) -> list[Finding]:
    """Does the page answer its own question before it explains itself?

    The strongest and most consistently reported GEO finding is front-loading: a model composing an
    answer reads the top of the page, and a conclusion buried under 600 words of preamble does not get
    quoted. This measures where the answer actually sits.
    """
    s = soup(page)
    text = body_text(page)
    words = text.split()
    out: list[Finding] = []
    if len(words) < 50:
        return out

    h1 = s.find("h1")
    heading = re.sub(r"\s+", " ", h1.get_text(" ")).strip() if h1 else ""
    intro = first_paragraph(page)

    if not intro:
        out.append(Finding("aeo.no_lead", Severity.HIGH,
                           "There is no substantial opening paragraph. A model reading this page has "
                           "nothing to quote as a summary answer.", {}))
    else:
        lead_words = len(intro.split())
        # A question-shaped heading with an answer directly beneath it is the ideal shape.
        asks_question = heading.endswith("?") or heading.lower().startswith(
            ("how ", "what ", "why ", "when ", "where ", "who ", "is ", "are ", "can ", "should ",
             "does ", "do "))
        if asks_question and lead_words > 80:
            out.append(Finding("aeo.answer_buried", Severity.HIGH,
                               f"The heading asks a question but the opening paragraph runs "
                               f"{lead_words} words before answering it. Put the answer in the first "
                               f"two sentences and explain afterwards.",
                               {"heading": heading[:120], "lead_words": lead_words}))
        elif lead_words > 120:
            out.append(Finding("aeo.long_lead", Severity.LOW,
                               f"The opening paragraph is {lead_words} words. A shorter lead is far "
                               f"more likely to be quoted whole.",
                               {"lead_words": lead_words}))

    # Front-loading, from the geo-optimizer rubric: is the substance in the first 30%?
    cut = max(1, len(words) * 3 // 10)
    head_text, tail_text = " ".join(words[:cut]), " ".join(words[cut:])
    head_stats = len(STATISTIC.findall(head_text))
    tail_stats = len(STATISTIC.findall(tail_text))
    if tail_stats >= 3 and head_stats == 0:
        out.append(Finding("aeo.not_front_loaded", Severity.LOW,
                           f"All {tail_stats} concrete figures on the page appear after the first "
                           f"30%. Moving one or two up makes the opening quotable.",
                           {"head_statistics": head_stats, "tail_statistics": tail_stats}))
    return out


@registry.register("evidence", "Evidence a model can quote")
def check_evidence(page: Page) -> list[Finding]:
    """Numbers and named sources. Both are repeatedly shown to raise citation rate; both are cheap to
    add and almost always absent from the pages that complain about not being cited."""
    text = body_text(page)
    if len(text.split()) < 100:
        return []
    s = soup(page)
    out: list[Finding] = []

    stats = STATISTIC.findall(text)
    per_100 = len(stats) / max(1, len(text.split()) / 100)
    if not stats:
        out.append(Finding("evidence.no_statistics", Severity.HIGH,
                           "The page contains no figures — no percentages, amounts, dates or "
                           "measurements. A model asked for specifics has nothing here to lift.", {}))
    elif per_100 < 0.5:
        out.append(Finding("evidence.few_statistics", Severity.LOW,
                           f"There are {len(stats)} concrete figures in "
                           f"{len(text.split())} words. Pages that get cited carry noticeably more.",
                           {"statistics": len(stats), "per_100_words": round(per_100, 2)}))

    authority = []
    for a in s.find_all("a", href=True):
        href = a["href"].lower()
        if href.startswith(("http://", "https://")) and any(d in href for d in AUTHORITY_HINTS):
            authority.append(href[:160])
    if not authority:
        out.append(Finding("evidence.no_sources", Severity.LOW,
                           "Nothing on the page links to an authoritative source — no standards body, "
                           "paper, government or reference site. Citing sources is one of the few "
                           "levers repeatedly shown to raise how often a page is itself cited.", {}))
    else:
        out.append(Finding("evidence.sources_present", Severity.INFO,
                           f"{len(authority)} link(s) to authoritative sources.",
                           {"sample": authority[:8]}))

    quotes = s.find_all(["blockquote", "q", "cite"])
    if quotes:
        out.append(Finding("evidence.quotes_present", Severity.INFO,
                           f"{len(quotes)} quotation or citation element(s), which chunk cleanly as "
                           f"attributable spans.", {"count": len(quotes)}))
    return out


@registry.register("chunk", "Chunkability")
def check_chunkability(page: Page) -> list[Finding]:
    """Can this page be split into clean, self-contained, citable spans?

    Retrieval systems do not read pages, they read chunks. A page that is one 3,000-word wall of text
    gets split arbitrarily, and an arbitrary split severs the sentence that carried the answer from the
    heading that gave it context. Headings, lists and tables are the seams that make a chunk coherent.
    """
    s = soup(page)
    text = body_text(page)
    words = text.split()
    if len(words) < 150:
        return []
    out: list[Finding] = []

    headings = s.find_all(["h2", "h3", "h4"])
    lists = s.find_all(["ul", "ol"])
    tables = s.find_all("table")

    if not headings:
        out.append(Finding("chunk.no_sections", Severity.HIGH,
                           f"{len(words)} words with no subheadings at all. A retrieval system has no "
                           f"natural place to split this, so it will cut mid-argument.",
                           {"words": len(words)}))
    else:
        # Words per section: the practical measure of whether a chunk will hold one idea.
        per_section = len(words) / (len(headings) + 1)
        if per_section > 500:
            out.append(Finding("chunk.sections_too_long", Severity.LOW,
                               f"There are about {int(per_section)} words per section. Sections under "
                               f"roughly 300 words survive chunking as a single coherent passage.",
                               {"words": len(words), "sections": len(headings),
                                "words_per_section": int(per_section)}))

    if not lists and not tables:
        out.append(Finding("chunk.no_structure", Severity.LOW,
                           "There are no lists or tables. Structured elements are extracted and quoted "
                           "far more readily than the same content written as prose.",
                           {"words": len(words)}))
    else:
        out.append(Finding("chunk.structured", Severity.INFO,
                           f"{len(lists)} list(s) and {len(tables)} table(s) give a chunker clean "
                           f"seams.", {"lists": len(lists), "tables": len(tables)}))

    # A paragraph beyond ~150 words will be split mid-thought by nearly every chunker.
    long_paras = [p for p in (s.body or s).find_all("p")
                  if len(p.get_text(" ").split()) > 150]
    if long_paras:
        out.append(Finding("chunk.long_paragraphs", Severity.LOW,
                           f"{len(long_paras)} paragraph(s) run over 150 words and will be split "
                           f"mid-thought.",
                           {"count": len(long_paras),
                            "longest": max(len(p.get_text(' ').split()) for p in long_paras)}))
    return out


def citable_spans(page: Page, *, max_words: int = 220) -> list[dict[str, Any]]:
    """Split the page the way a retrieval system would, and hand back the spans with offsets.

    This is the `page.chunk` service. Every span carries the heading it sits under, so a quoted span
    keeps its context, and character offsets into the extracted text, so a caller can prove exactly
    which words a claim came from.
    """
    s = BeautifulSoup(page.html or "", "lxml")
    root = s.body or s
    for tag in root(["script", "style", "noscript", "template", "svg", "nav", "footer", "header",
                     "aside", "form", "dialog"]):
        tag.decompose()
    for el in root.find_all(attrs={"role": lambda v: v in ("navigation", "banner", "search",
                                                           "complementary", "contentinfo")}):
        el.decompose()

    # Prefer the region the page itself declares as its content. Without this the first "citable"
    # span off MDN was "Skip to main content Skip to search" — chrome quoted back to the customer as
    # though a model might cite it.
    main = root.find("main") or root.find("article") or root.find(attrs={"role": "main"})
    if main is not None and len(main.get_text(" ").split()) >= 50:
        root = main

    spans: list[dict[str, Any]] = []
    heading = ""
    buffer: list[str] = []
    offset = 0

    def flush() -> None:
        nonlocal buffer, offset
        if not buffer:
            return
        body = " ".join(buffer).strip()
        if body:
            spans.append({"heading": heading, "text": body, "words": len(body.split()),
                          "start": offset, "end": offset + len(body), "source": "block"})
            offset += len(body) + 1
        buffer = []

    for el in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "blockquote", "pre"]):
        text = re.sub(r"\s+", " ", el.get_text(" ")).strip()
        if not text:
            continue
        if el.name in ("h1", "h2", "h3", "h4"):
            flush()
            heading = text
            continue
        if len(" ".join(buffer + [text]).split()) > max_words:
            flush()
        buffer.append(text)
    flush()

    if spans:
        return spans

    # No block elements, but that does not mean no content.
    #
    # Measured on a 955-byte page that fetched cleanly at HTTP 200 with several readable paragraphs:
    # zero spans, zero words, and nothing in the response saying why. The buyer paid for a chunk
    # extract and got an empty list they could not distinguish from "your page has no citable
    # content" — or from the service being broken.
    #
    # The gap is not exotic. The walk above needs <p>/<li>/<td> and finds nothing in a plain-text
    # document, a raw markdown file, an llms.txt — a file Doxa sells a separate check for, so a
    # customer arriving here with one is the expected path, not a corner case — or a rendered
    # single-page app that lays its prose out in bare <div>s.
    #
    # So: fall back to the extracted text, split on blank lines the way a retrieval system would when
    # markup gives it nothing to go on. Same span shape, same honest offsets. Callers are told which
    # route produced their spans via `chunking_method` on the node, because a paragraph inferred from
    # a blank line is a weaker claim than one taken from a <p> and should not be presented as equal.
    flat = re.sub(r"[ \t]+", " ", root.get_text("\n")).strip()
    for block in re.split(r"\n\s*\n+", flat):
        chunk = re.sub(r"\s+", " ", block).strip()
        if len(chunk.split()) < 4:                 # a stray word is not a citable passage
            continue
        while chunk:
            words = chunk.split()
            head = " ".join(words[:max_words])
            spans.append({"heading": heading, "text": head, "words": len(head.split()),
                          "start": offset, "end": offset + len(head), "source": "text"})
            offset += len(head) + 1
            chunk = " ".join(words[max_words:])
    return spans


@registry.register("readability", "Readability and padding")
def check_readability(page: Page) -> list[Finding]:
    text = body_text(page)
    if len(text.split()) < 120:
        return []
    r = readability(text)
    low = text.lower()
    out: list[Finding] = []

    if r["flesch"] is not None:
        if r["flesch"] < 30:
            out.append(Finding("readability.very_hard", Severity.LOW,
                               f"Flesch reading ease is {r['flesch']} — heavy going, at roughly "
                               f"graduate level. Averaging {r['words_per_sentence']} words per "
                               f"sentence is the main cause.", r))
        elif r["flesch"] > 90:
            out.append(Finding("readability.very_simple", Severity.INFO,
                               f"Flesch reading ease is {r['flesch']}, which is very simple prose.", r))
        else:
            out.append(Finding("readability.ok", Severity.INFO,
                               f"Flesch reading ease is {r['flesch']}.", r))

    if r.get("words_per_sentence", 0) > 30:
        out.append(Finding("readability.long_sentences", Severity.LOW,
                           f"Sentences average {r['words_per_sentence']} words. Long sentences survive "
                           f"chunking and summarisation badly.", r))

    found_filler = [f for f in FILLER if f in low]
    if len(found_filler) >= 3:
        out.append(Finding("readability.filler", Severity.LOW,
                           f"The page uses {len(found_filler)} stock filler phrases "
                           f"({', '.join(repr(f) for f in found_filler[:3])}…). These are the strongest "
                           f"surface markers of padding, and they displace the specifics that get "
                           f"quoted.", {"phrases": found_filler}))

    found_hedges = [h for h in HEDGES if h in low]
    if len(found_hedges) >= 4:
        out.append(Finding("readability.hedging", Severity.LOW,
                           f"The page hedges {len(found_hedges)} times. A model looking for a definite "
                           f"answer will prefer a page that gives one.",
                           {"phrases": found_hedges[:8]}))
    return out


@registry.register("freshness", "Freshness signals")
def check_freshness(page: Page) -> list[Finding]:
    """Can a machine tell how old this is? An undated page is discounted on any question where
    currency matters, and the page cannot argue back."""
    s = soup(page)
    signals: dict[str, Any] = {}

    for m in s.find_all("meta"):
        prop = ((m.get("property") or m.get("name") or "")).lower()
        if prop in ("article:published_time", "article:modified_time", "date", "last-modified",
                    "og:updated_time", "datepublished", "datemodified"):
            signals[prop] = (m.get("content") or "").strip()[:40]
    for t in s.find_all("time"):
        if t.get("datetime"):
            signals.setdefault("time_element", t["datetime"][:40])
    if page.headers.get("last-modified"):
        signals["http_last_modified"] = page.headers["last-modified"][:40]

    for script in s.find_all("script", type=lambda v: v and "ld+json" in v.lower()):
        raw = script.string or ""
        for key in ("datePublished", "dateModified"):
            m = re.search(rf'"{key}"\s*:\s*"([^"]{{4,40}})"', raw)
            if m:
                signals.setdefault(f"jsonld_{key}", m.group(1))

    if not signals:
        return [Finding("freshness.undated", Severity.LOW,
                        "Nothing on the page says when it was written or last changed — no dateline, "
                        "no time element, no dateModified, no Last-Modified header. On any question "
                        "where currency matters this page cannot compete with one that is dated.", {})]
    return [Finding("freshness.dated", Severity.INFO,
                    f"The page carries {len(signals)} freshness signal(s).", signals)]


@registry.register("multimodal", "Images and video a model can use")
def check_multimodal(page: Page) -> list[Finding]:
    """Video and audio are opaque to a text model unless something describes them. A 20-minute
    explainer with no transcript contributes nothing to an answer."""
    s = soup(page)
    out: list[Finding] = []
    videos = s.find_all(["video", "iframe"])
    embeds = [v for v in videos
              if v.name == "video" or any(h in (v.get("src") or "").lower()
                                          for h in ("youtube", "vimeo", "wistia", "loom"))]
    if embeds:
        has_transcript = bool(re.search(r"transcript", body_text(page), re.I)) or \
            bool(s.find_all("track"))
        if not has_transcript:
            out.append(Finding("multimodal.no_transcript", Severity.LOW,
                               f"There {'is' if len(embeds) == 1 else 'are'} {len(embeds)} video "
                               f"embed(s) and no transcript or caption track. Everything said in the "
                               f"video is invisible to a text model.",
                               {"embeds": len(embeds)}))
        else:
            out.append(Finding("multimodal.transcript_present", Severity.INFO,
                               "Video content has an accompanying transcript or caption track.",
                               {"embeds": len(embeds)}))

    figures = s.find_all("figure")
    uncaptioned = [f for f in figures if not f.find("figcaption")]
    if uncaptioned:
        out.append(Finding("multimodal.no_caption", Severity.LOW,
                           f"{len(uncaptioned)} figure(s) have no caption. A caption is what makes an "
                           f"image quotable rather than merely decorative.",
                           {"count": len(uncaptioned)}))
    return out


@registry.register("author", "Authorship and accountability")
def check_authorship(page: Page) -> list[Finding]:
    """Who stands behind this? E-E-A-T is not a scoring knob, but a named, linked author with a
    biography is the difference between a page a model attributes and one it treats as anonymous."""
    s = soup(page)
    text = body_text(page)
    signals: dict[str, Any] = {}

    for m in s.find_all("meta"):
        if (m.get("name") or "").lower() in ("author", "article:author", "twitter:creator"):
            signals["meta_author"] = (m.get("content") or "").strip()[:80]
    for el in s.find_all(attrs={"rel": True}):
        rels = el.get("rel")
        if "author" in [r.lower() for r in (rels if isinstance(rels, list) else [rels])]:
            signals["rel_author"] = (el.get("href") or el.get_text() or "").strip()[:80]
    for script in s.find_all("script", type=lambda v: v and "ld+json" in v.lower()):
        m = re.search(r'"author"\s*:\s*(\{[^}]*"name"\s*:\s*"([^"]+)"|"([^"]+)")',
                      script.string or "")
        if m:
            signals["schema_author"] = (m.group(2) or m.group(3) or "")[:80]
    if re.search(r"\bby\s+[A-Z][a-z]+\s+[A-Z][a-z]+", text[:1500]):
        signals["byline_in_text"] = True

    if not signals:
        return [Finding("author.missing", Severity.LOW,
                        "The page names no author — no meta author, no schema author, no byline. "
                        "Anonymous content is attributed less often and trusted less.", {})]
    return [Finding("author.present", Severity.INFO,
                    "The page identifies an author.", signals)]
