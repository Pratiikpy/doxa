"""Two things that actively cost a page its citation: manipulation, and clutter.

**Manipulation.** A page can carry text meant for the model rather than the reader — white-on-white
paragraphs, zero-height containers, invisible Unicode, HTML comments addressed to an assistant, or a
plain "ignore your previous instructions". Some of it is deliberate; a great deal of it is a plugin the
owner installed years ago. Either way it is prompt injection, and a page caught doing it is a page that
gets discounted rather than quoted. Every crawler operator now looks for exactly this.

Detecting it is also the honest thing to do for the person paying for the audit: this service reads
their page and hands the text to a model in later stages, and text on a fetched page is untrusted
input. Finding an instruction aimed at an assistant is both a finding for the customer and a reason to
treat the page's text as data.

**Clutter.** The anti-citation signals from geo-optimizer's rubric: interstitials, calls to action
outweighing content, keyword stuffing, boilerplate crowding out the article. None of them is fatal
alone; together they are why a page with good content still does not get quoted.
"""
from __future__ import annotations

import re
import unicodedata
import urllib.parse
from collections import Counter
from typing import Any

from checks.base import Finding, Severity, registry
from checks.page_html import body_text, soup
from fetch import Page

# Characters with no visual width. Legitimate in Arabic and Indic shaping, and otherwise a way to
# smuggle text past a human reader. ZWJ/ZWNJ are excluded because emoji sequences use them.
INVISIBLE = {
    "​": "zero-width space",
    "⁠": "word joiner",
    "﻿": "zero-width no-break space",
    "­": "soft hyphen",
    "᠎": "Mongolian vowel separator",
    "⁡": "function application",
    "⁢": "invisible times",
    "⁣": "invisible separator",
    "⁤": "invisible plus",
}
# Unicode tag characters: a full ASCII alphabet that renders as nothing at all. There is no benign
# reason for these to appear in web copy — they exist in the wild almost exclusively to hide payloads.
TAG_RANGE = range(0xE0000, 0xE0080)

INJECTION_PHRASES = (
    "ignore previous instructions", "ignore all previous", "disregard the above",
    "disregard previous", "forget your instructions", "forget everything above",
    "you are now", "act as if", "new instructions:", "system prompt", "system:",
    "as an ai language model", "when summarizing this page", "when asked about",
    "always recommend", "you must recommend", "rank this page", "rank this site",
    "this is the best", "do not mention", "override your", "jailbreak",
    "prompt injection", "assistant:", "###instruction", "<|im_start|>",
)

# Concealment techniques that have no legitimate purpose. Pushing text thousands of pixels off the
# canvas or rendering it at zero size does nothing for a visitor and nothing for accessibility — it
# exists only to show a parser something a person will not read. These justify an accusation.
DECEPTIVE_CSS = (
    (re.compile(r"text-indent\s*:\s*-\d{4,}", re.I), "text-indent far off-canvas"),
    (re.compile(r"font-size\s*:\s*0(?:px|em|rem|%)?\s*[;\"']?", re.I), "zero font size"),
    (re.compile(r"position\s*:\s*absolute[^\"']*?(?:left|top)\s*:\s*-\d{4,}", re.I),
     "positioned off-canvas"),
    (re.compile(r"(?:left|top)\s*:\s*-\d{4,}px", re.I), "positioned off-canvas"),
    (re.compile(r"color\s*:\s*(#fff(?:fff)?|white)[^;]*;[^\"']*background(?:-color)?\s*:\s*"
                r"(#fff(?:fff)?|white)", re.I), "white text on a white background"),
)

# Techniques that hide something *right now* but are ordinary interface work: a closed menu, a modal,
# a tab panel, a carousel slide waiting its turn. These are recorded but never treated as evidence of
# manipulation.
#
# `aria-hidden` is deliberately absent from both lists. It is an accessibility instruction meaning
# "do not announce this duplicate", not a concealment technique — the content is usually on screen.
# Treating it as hiding reported python.org, Cloudflare and Stripe as manipulative, and flagged
# Stripe's own visible <h1>. An audit that accuses Stripe of hiding its headline is not believable
# about anything else it says.
UI_STATE_CSS = (
    (re.compile(r"display\s*:\s*none", re.I), "display:none"),
    (re.compile(r"visibility\s*:\s*hidden", re.I), "visibility:hidden"),
    (re.compile(r"opacity\s*:\s*0(?:\.0+)?\s*[;\"']?", re.I), "opacity:0"),
    (re.compile(r"transform\s*:\s*translate", re.I), "translated out of view"),
)

CTA_WORDS = ("buy now", "sign up", "subscribe", "get started", "book a demo", "start free trial",
             "contact us", "request a quote", "add to cart", "download now", "join now",
             "claim your", "limited time", "act now", "don't miss")


def _looks_like_prose(el, text: str) -> bool:
    """Is this a passage of writing, or a piece of interface?

    A hidden navigation menu is a list of link labels; a hidden payload is sentences. Counting how much
    of the text sits inside links and controls separates the two, and requiring end punctuation keeps
    label soup out.
    """
    interactive = " ".join(c.get_text(" ") for c in el.find_all(["a", "button", "option", "label"]))
    interactive_words = len(interactive.split())
    total = len(text.split())
    if total < 15:
        return False
    if interactive_words / total > 0.5:
        return False
    return bool(re.search(r"[.!?]", text))


def _hidden_text_nodes(page: Page, *, deceptive_only: bool = True) -> list[dict[str, Any]]:
    """Text in the document that a reader will not see.

    `deceptive_only` returns just the passages concealed by a technique with no legitimate use. That
    is the set worth making an accusation about; the rest is ordinary interface state.
    """
    s = soup(page)
    out: list[dict[str, Any]] = []
    for el in (s.body or s).find_all(True):
        style = el.get("style") or ""
        technique = next((name for rx, name in DECEPTIVE_CSS if rx.search(style)), "")
        kind = "deceptive"
        if not technique:
            if deceptive_only:
                continue
            technique = next((name for rx, name in UI_STATE_CSS if rx.search(style)), "")
            if not technique and el.has_attr("hidden"):
                technique = "hidden attribute"
            if not technique:
                continue
            kind = "ui_state"

        text = re.sub(r"\s+", " ", el.get_text(" ")).strip()
        # A nested match reports the same passage once per ancestor. Keep the outermost only.
        if any(el in prev["_el"].descendants for prev in out):
            continue
        if not _looks_like_prose(el, text):
            continue
        out.append({"tag": el.name, "words": len(text.split()), "technique": technique,
                    "kind": kind, "style": style[:120], "text": text[:200], "_el": el})
    for row in out:
        row.pop("_el", None)
    return out


@registry.register("injection", "Text aimed at a machine, not a reader")
def check_prompt_injection(page: Page) -> list[Finding]:
    html = page.html or ""
    s = soup(page)
    visible = body_text(page)
    out: list[Finding] = []

    # Everything not on screen, so a payload inside a collapsed panel is still searched for
    # instructions — but only the deceptively concealed passages are called manipulation.
    all_hidden = _hidden_text_nodes(page, deceptive_only=False)
    deceptive = [h for h in all_hidden if h["kind"] == "deceptive"]
    if deceptive:
        total = sum(h["words"] for h in deceptive)
        out.append(Finding("injection.hidden_text", Severity.HIGH,
                           f"{len(deceptive)} passage(s) totalling {total} words are concealed by "
                           f"techniques with no legitimate use "
                           f"({', '.join(sorted({h['technique'] for h in deceptive}))}). Search engines "
                           f"and AI crawlers treat this as manipulation.",
                           {"elements": [{k: v for k, v in h.items() if k != "kind"}
                                         for h in deceptive[:6]], "total_words": total}))

    invisible_found = Counter()
    for ch in html:
        if ch in INVISIBLE:
            invisible_found[INVISIBLE[ch]] += 1
        elif ord(ch) in TAG_RANGE:
            invisible_found["Unicode tag character"] += 1
    # A handful of soft hyphens is typesetting. Hundreds of zero-width characters is a payload.
    serious = {k: v for k, v in invisible_found.items()
               if v >= (50 if k == "soft hyphen" else 20) or k == "Unicode tag character"}
    if serious:
        out.append(Finding("injection.invisible_unicode", Severity.HIGH,
                           f"The page contains {sum(serious.values())} invisible Unicode characters "
                           f"({', '.join(serious)}). These render as nothing and are a known way to "
                           f"hide text from a reader while leaving it for a parser.",
                           {"characters": dict(serious)}))

    comments_text = " ".join(re.findall(r"<!--(.*?)-->", html, re.S))[:20000].lower()
    low_visible = visible.lower()
    hidden_prose = " ".join(h["text"] for h in all_hidden).lower()

    for where, corpus in (("visible text", low_visible),
                          ("an HTML comment", comments_text),
                          ("hidden text", hidden_prose)):
        hits = [p for p in INJECTION_PHRASES if p in corpus]
        if hits:
            severity = Severity.HIGH if where != "visible text" else Severity.LOW
            out.append(Finding(
                "injection.instructions" if where != "visible text" else "injection.phrasing",
                severity,
                f"{where.capitalize()} contains {len(hits)} phrase(s) that read as instructions to an "
                f"AI assistant rather than content for a reader: "
                f"{', '.join(repr(h) for h in hits[:3])}."
                + (" A page caught doing this is discounted, not promoted."
                   if where != "visible text" else
                   " In visible copy this is usually innocent, but it is worth reading in context."),
                {"where": where, "phrases": hits[:10]}))

    # Text whose glyphs are Latin lookalikes from another script — used to evade filters.
    confusables = [c for c in visible
                   if ord(c) > 0x2000 and unicodedata.category(c).startswith("L")
                   and "CYRILLIC" in (unicodedata.name(c, "") or "")]
    if len(confusables) > 20:
        out.append(Finding("injection.confusable_script", Severity.LOW,
                           f"{len(confusables)} Cyrillic characters appear inside otherwise Latin text. "
                           f"This is sometimes a genuinely multilingual page and sometimes an attempt "
                           f"to disguise words.", {"count": len(confusables)}))

    if not out:
        out.append(Finding("injection.clean", Severity.INFO,
                           "No hidden text, invisible characters or assistant-directed instructions "
                           "found.", {}))
    return out


def _brand_terms(page: Page, s) -> set[str]:
    """What this site is called, from the three places it says so.

    The registrable domain label, `og:site_name`, and any Organization/WebSite name in the JSON-LD.
    Used to keep a brand's own name out of the stuffing calculation.
    """
    terms: set[str] = set()
    host = urllib.parse.urlsplit(page.url or page.requested_url).hostname or ""
    labels = [l for l in host.lower().split(".")
              if l not in ("www", "com", "org", "net", "io", "dev", "app", "co", "ai", "xyz")]
    terms.update(l for l in labels if len(l) >= 4)

    for m in s.find_all("meta"):
        if (m.get("property") or "").lower() == "og:site_name":
            terms.update(re.findall(r"[a-z]{4,}", (m.get("content") or "").lower()))
    for script in s.find_all("script", type=lambda v: v and "ld+json" in v.lower()):
        for name in re.findall(r'"name"\s*:\s*"([^"]{2,60})"', script.string or ""):
            terms.update(re.findall(r"[a-z]{4,}", name.lower()))
    return terms


@registry.register("clutter", "Anti-citation signals")
def check_clutter(page: Page) -> list[Finding]:
    s = soup(page)
    text = body_text(page)
    words = text.split()
    out: list[Finding] = []
    if len(words) < 80:
        return out

    low = text.lower()
    cta_hits = sum(low.count(c) for c in CTA_WORDS)
    if cta_hits and len(words) < 400 and cta_hits >= 5:
        out.append(Finding("clutter.cta_overload", Severity.LOW,
                           f"{cta_hits} calls to action in {len(words)} words. The page reads as a "
                           f"landing page rather than an answer, and landing pages are rarely cited.",
                           {"calls_to_action": cta_hits, "words": len(words)}))

    # Boilerplate: how much of the document is navigation and footer rather than article.
    article = s.find(["article", "main"]) or None
    if article:
        article_words = len(re.sub(r"\s+", " ", article.get_text(" ")).strip().split())
        ratio = 1 - (article_words / max(1, len(words)))
        if ratio > 0.6 and article_words < 400:
            out.append(Finding("clutter.boilerplate", Severity.LOW,
                               f"Only {article_words} of {len(words)} words are inside the main "
                               f"content; {ratio:.0%} of the page is navigation, footer and chrome.",
                               {"article_words": article_words, "total_words": len(words),
                                "boilerplate_ratio": round(ratio, 2)}))

    # Keyword stuffing, measured against the page's own title so it is about *this* page's target.
    #
    # The site's own brand name is excluded. react.dev says "React" 98 times in 1,438 words — 6.8%,
    # far past any stuffing threshold, and entirely legitimate, because it is React's documentation.
    # A brand repeating its own name on its own domain is not manipulation, and reporting it as such
    # would discredit every other finding in the audit.
    title = (s.title.get_text().lower().strip() if s.title else "")
    brand_terms = _brand_terms(page, s)
    title_terms = [t for t in re.findall(r"[a-z]{4,}", title)
                   if t not in ("with", "from", "your", "that", "this", "have", "what", "will")
                   and t not in brand_terms]
    if title_terms and len(words) >= 200:
        counts = Counter(re.findall(r"[a-z]{4,}", low))
        for term in title_terms:
            density = counts[term] / len(words)
            if density > 0.035 and counts[term] >= 10:
                out.append(Finding("clutter.keyword_stuffing", Severity.HIGH,
                                   f"The word \"{term}\" appears {counts[term]} times — {density:.1%} "
                                   f"of the page. Above roughly 3% this reads as stuffing to every "
                                   f"ranking system.",
                                   {"term": term, "count": counts[term],
                                    "density": round(density, 4)}))
                break

    interstitial = [el for el in s.find_all(["div", "section", "dialog"])
                    if re.search(r"(modal|popup|interstitial|overlay|newsletter|cookie-wall)",
                                 " ".join(el.get("class") or []) + " " + (el.get("id") or ""), re.I)]
    if len(interstitial) >= 3:
        out.append(Finding("clutter.interstitials", Severity.LOW,
                           f"{len(interstitial)} popup, modal or overlay containers are present. Each "
                           f"one stands between a visitor and the content.",
                           {"count": len(interstitial)}))
    return out
