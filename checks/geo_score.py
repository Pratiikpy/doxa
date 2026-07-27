"""The GEO score — 0 to 100, across eight categories.

The weights are geo-optimizer's published rubric (`docs/scoring-rubric.md`, v4.0.0), used verbatim so
that a Doxa score and a `geo audit` score mean the same thing. They are stated as data below rather
than buried in branches, so anyone can read what a number was made of, and so the breakdown returned
to the caller is generated from the same table that produced the score.

    robots.txt   18      llms.txt        18      schema JSON-LD  16      meta tags   14
    content      12      brand & entity  10      signals          6      discovery    6

A score alone is a vanity metric. Every signal returned here carries `earned`, `max`, and the reason,
so the answer to "why 61?" is always in the response — and `geo.fix_order` sorts what is missing by
points per unit of effort, which is the only part a customer actually acts on.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

from bs4 import BeautifulSoup

from checks.machine import RobotsRules
from checks.structured import parse_jsonld, _types
from fetch import Page, fetch

# The four bots that produce live citations rather than training data. geo-optimizer weights these
# above all other AI agents, and it is right to: these are the ones that put a link in an answer.
CITATION_BOTS = ("OAI-SearchBot", "ClaudeBot", "Claude-SearchBot", "PerplexityBot")
OTHER_AI_BOTS = ("GPTBot", "Google-Extended", "CCBot", "anthropic-ai", "Applebot-Extended",
                 "Bytespider", "Amazonbot", "meta-externalagent", "cohere-ai", "Diffbot",
                 "ImagesiftBot", "Omgilibot", "PetalBot", "YouBot", "Timpibot")

KG_DOMAINS = ("wikipedia.org", "wikidata.org", "linkedin.com", "crunchbase.com", "github.com",
              "twitter.com", "x.com", "facebook.com")

# Effort is our own addition, not geo-optimizer's: points alone tell a customer what is missing but
# not what to do first. "file" means writing one static file, "markup" means editing a template,
# "content" means someone has to write.
EFFORT = {"file": 1, "markup": 2, "content": 4}


@dataclass
class Signal:
    key: str
    points: int
    earned: int
    why: str
    category: str = ""
    effort: str = "markup"

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "earned": self.earned, "max": self.points,
                "why": self.why, "category": self.category, "effort": self.effort}


@dataclass
class GeoScore:
    signals: list[Signal] = field(default_factory=list)
    fetched: dict[str, Any] = field(default_factory=dict)

    def add(self, key: str, points: int, earned: int, why: str, category: str,
            effort: str = "markup") -> None:
        self.signals.append(Signal(key, points, earned, why, category, effort))

    @property
    def total(self) -> int:
        return min(100, sum(s.earned for s in self.signals))

    @property
    def band(self) -> str:
        t = self.total
        return ("Excellent" if t >= 86 else "Good" if t >= 68
                else "Foundation" if t >= 36 else "Critical")

    def by_category(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for s in self.signals:
            c = out.setdefault(s.category, {"earned": 0, "max": 0})
            c["earned"] += s.earned
            c["max"] += s.points
        return out

    def fix_order(self) -> list[dict[str, Any]]:
        """What to do next, best return first — points gained per unit of effort."""
        missing = [s for s in self.signals if s.earned < s.points]
        return [s.as_dict() | {"points_available": s.points - s.earned,
                               "value": round((s.points - s.earned) / EFFORT[s.effort], 2)}
                for s in sorted(missing,
                                key=lambda s: -((s.points - s.earned) / EFFORT[s.effort]))]

    def as_dict(self) -> dict[str, Any]:
        return {"score": self.total, "band": self.band,
                "categories": self.by_category(),
                "signals": [s.as_dict() for s in self.signals],
                "fix_order": self.fix_order(),
                "fetched": self.fetched}


def _root(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _try_fetch(url: str, timeout: int = 10) -> Page | None:
    try:
        p = fetch(url, timeout=timeout, render=False)
        return p if p.ok else None
    except Exception:  # noqa: BLE001
        return None


def score_geo(page: Page, *, fetch_site_files: bool = True) -> GeoScore:
    """Score one page plus its site-level files.

    `fetch_site_files=False` scores only what the document proves, and the site-level signals are
    recorded as unknown rather than as zero. Scoring a missing fetch as a failure would invent a
    number lower than the truth, and this service signs its numbers.
    """
    s = BeautifulSoup(page.html or "", "lxml")
    score = GeoScore()
    root = _root(page.url or page.requested_url)
    path = urllib.parse.urlsplit(page.url or page.requested_url).path or "/"

    # --- 1. robots.txt (18) -----------------------------------------------------------------
    if fetch_site_files:
        robots = _try_fetch(f"{root}/robots.txt")
        if robots is not None:
            score.add("robots_found", 5, 5, "robots.txt exists and is reachable.", "robots.txt",
                      "file")
            rules = RobotsRules(robots.html)
            allowed_citation = [b for b in CITATION_BOTS if rules.allowed(b, path)[0]]
            allowed_other = [b for b in OTHER_AI_BOTS if rules.allowed(b, path)[0]]
            score.fetched["robots"] = {"citation_bots_allowed": allowed_citation,
                                       "other_ai_bots_allowed": len(allowed_other),
                                       "sitemaps": rules.sitemaps[:5]}
            if len(allowed_citation) == len(CITATION_BOTS):
                score.add("robots_citation_ok", 13, 13,
                          "All four citation bots may crawl this page.", "robots.txt", "file")
            elif allowed_citation or allowed_other:
                blocked = [b for b in CITATION_BOTS if b not in allowed_citation]
                # Partial credit, explicitly not cumulative with citation_ok — geo-optimizer's rule.
                score.add("robots_some_allowed", 13, 10,
                          f"Some AI bots are allowed, but {', '.join(blocked)} "
                          f"{'is' if len(blocked) == 1 else 'are'} blocked. These are the ones that "
                          f"put a link in an answer.", "robots.txt", "file")
            else:
                score.add("robots_citation_ok", 13, 0,
                          "Every AI citation bot is blocked by robots.txt. Nothing else in this "
                          "audit can matter until that changes.", "robots.txt", "file")
        else:
            # A missing robots.txt permits everything, so the bots are not blocked — but the file
            # itself is absent, which is what the 5 points are for.
            score.add("robots_found", 5, 0, "There is no robots.txt.", "robots.txt", "file")
            score.add("robots_citation_ok", 13, 13,
                      "With no robots.txt, every crawler is permitted by default.", "robots.txt",
                      "file")

    # --- 2. llms.txt (18) -------------------------------------------------------------------
    if fetch_site_files:
        llms = _try_fetch(f"{root}/llms.txt")
        if llms is None:
            for key, pts, why in (
                    ("llms_found", 5, "There is no /llms.txt."),
                    ("llms_h1", 2, "No /llms.txt, so it has no H1."),
                    ("llms_blockquote", 1, "No /llms.txt, so it has no summary blockquote."),
                    ("llms_sections", 2, "No /llms.txt, so it has no sections."),
                    ("llms_links", 2, "No /llms.txt, so it lists no pages."),
                    ("llms_depth", 2, "No /llms.txt."),
                    ("llms_depth_high", 2, "No /llms.txt."),
                    ("llms_full", 2, "There is no /llms-full.txt.")):
                score.add(key, pts, 0, why, "llms.txt", "file")
        else:
            body = llms.html or ""
            words = len(body.split())
            score.add("llms_found", 5, 5, "/llms.txt is present.", "llms.txt", "file")
            score.add("llms_h1", 2, 2 if re.search(r"^#\s+\S", body, re.M) else 0,
                      "The file opens with an H1 title." if re.search(r"^#\s+\S", body, re.M)
                      else "The file has no H1 title, which the convention requires.",
                      "llms.txt", "file")
            has_bq = bool(re.search(r"^>\s+\S", body, re.M))
            score.add("llms_blockquote", 1, 1 if has_bq else 0,
                      "A blockquote summarises the site." if has_bq
                      else "No blockquote summary of what the site is.", "llms.txt", "file")
            n_sections = len(re.findall(r"^##\s+\S", body, re.M))
            score.add("llms_sections", 2, 2 if n_sections else 0,
                      f"{n_sections} section heading(s)." if n_sections
                      else "No H2 sections, so the file has no structure.", "llms.txt", "file")
            n_links = len(re.findall(r"\]\((https?://[^)]+)\)", body))
            score.add("llms_links", 2, 2 if n_links else 0,
                      f"{n_links} link(s) to pages." if n_links
                      else "The file links to no pages.", "llms.txt", "file")
            score.add("llms_depth", 2, 2 if words >= 1000 else 0,
                      f"{words} words." + ("" if words >= 1000 else " Under 1,000 is thin."),
                      "llms.txt", "content")
            score.add("llms_depth_high", 2, 2 if words >= 5000 else 0,
                      "Comprehensive index (5,000+ words)." if words >= 5000
                      else "Not a comprehensive index (under 5,000 words).", "llms.txt", "content")
            full = _try_fetch(f"{root}/llms-full.txt")
            score.add("llms_full", 2, 2 if full else 0,
                      "/llms-full.txt is present." if full else "There is no /llms-full.txt.",
                      "llms.txt", "file")
            score.fetched["llms_txt"] = {"words": words, "sections": n_sections, "links": n_links}

    # --- 3. schema JSON-LD (16) --------------------------------------------------------------
    nodes, _errors = parse_jsonld(page)
    types = {t for n in nodes for t in _types(n)}
    score.add("schema_any_valid", 2, 2 if nodes else 0,
              f"{len(nodes)} JSON-LD node(s) parse cleanly." if nodes
              else "No valid JSON-LD on the page.", "schema", "markup")
    richest = max((len([k for k in n if not k.startswith("@")]) for n in nodes), default=0)
    score.add("schema_richness", 3, 3 if richest >= 5 else 0,
              f"The richest node declares {richest} properties." if richest >= 5
              else f"The richest node declares only {richest} properties; five or more is the "
                   f"threshold for a rich result.", "schema", "markup")
    for key, pts, wanted, label in (("schema_faq", 3, {"FAQPage"}, "FAQPage"),
                                    ("schema_article", 3, {"Article", "NewsArticle", "BlogPosting"},
                                     "Article or BlogPosting"),
                                    ("schema_organization", 3, {"Organization", "LocalBusiness",
                                                                "Corporation"}, "Organization"),
                                    ("schema_website", 2, {"WebSite"}, "WebSite")):
        got = bool(types & wanted)
        score.add(key, pts, pts if got else 0,
                  f"{label} schema present." if got else f"No {label} schema.", "schema", "markup")
    score.fetched["schema_types"] = sorted(types)

    # --- 4. meta tags (14) --------------------------------------------------------------------
    title = (s.title.get_text().strip() if s.title else "")
    score.add("meta_title", 5, 5 if title else 0,
              f"Title present ({len(title)} characters)." if title else "No title tag.",
              "meta", "markup")
    desc = next((m.get("content") or "" for m in s.find_all("meta")
                 if (m.get("name") or "").lower() == "description"), "").strip()
    score.add("meta_description", 2, 2 if desc else 0,
              f"Meta description present ({len(desc)} characters)." if desc
              else "No meta description.", "meta", "markup")
    canonical = s.find("link", rel=lambda v: v and "canonical" in
                       ([r.lower() for r in v] if isinstance(v, list) else str(v).lower()))
    score.add("meta_canonical", 3, 3 if canonical else 0,
              "Canonical URL declared." if canonical else "No canonical URL.", "meta", "markup")
    og = {(m.get("property") or "").lower() for m in s.find_all("meta")
          if (m.get("property") or "").lower().startswith("og:")}
    has_og = {"og:title", "og:description"} <= og
    score.add("meta_og", 4, 4 if has_og else 0,
              "Open Graph title and description present." if has_og
              else "Open Graph title and description are not both set.", "meta", "markup")

    # --- 5. content quality (12) --------------------------------------------------------------
    from checks.aeo import STATISTIC          # imported here to keep the module graph acyclic
    from checks.page_html import body_text

    text = body_text(page)
    words = text.split()
    h1s = s.find_all("h1")
    score.add("content_h1", 2, 2 if h1s else 0,
              f"{len(h1s)} H1 heading(s)." if h1s else "No H1 heading.", "content", "markup")
    stats = STATISTIC.findall(text)
    score.add("content_numbers", 1, 1 if stats else 0,
              f"{len(stats)} concrete figure(s) in the text." if stats
              else "No statistics, percentages or dates in the text.", "content", "content")
    ext_links = [a for a in s.find_all("a", href=True)
                 if a["href"].startswith(("http://", "https://"))
                 and urllib.parse.urlsplit(a["href"]).netloc not in
                 (urllib.parse.urlsplit(page.url or "").netloc, "")]
    score.add("content_links", 1, 1 if ext_links else 0,
              f"{len(ext_links)} external link(s)." if ext_links
              else "The page cites no external sources.", "content", "content")
    score.add("content_word_count", 2, 2 if len(words) >= 300 else 0,
              f"{len(words)} words." + ("" if len(words) >= 300 else " Under 300 is thin."),
              "content", "content")
    hierarchy = bool(s.find_all("h2")) and bool(s.find_all("h3"))
    score.add("content_heading_hierarchy", 2, 2 if hierarchy else 0,
              "H2 and H3 headings both present." if hierarchy
              else "The page lacks a full H2/H3 heading hierarchy.", "content", "markup")
    structured = bool(s.find_all(["ul", "ol", "table"]))
    score.add("content_lists_or_tables", 2, 2 if structured else 0,
              "Lists or tables present." if structured
              else "No lists or tables — everything is prose.", "content", "content")
    front_loaded = False
    if words:
        cut = max(1, len(words) * 3 // 10)
        front_loaded = bool(STATISTIC.search(" ".join(words[:cut]))) or bool(
            s.find(["ul", "ol", "table"]))
    score.add("content_front_loading", 2, 2 if front_loaded else 0,
              "Key information appears in the first 30% of the page." if front_loaded
              else "The first 30% of the page carries no concrete detail.", "content", "content")

    # --- 6. signals (6) -----------------------------------------------------------------------
    lang = ((s.html.get("lang") if s.html else "") or "").strip()
    score.add("signals_lang", 3, 3 if lang else 0,
              f'<html lang="{lang}"> is set.' if lang else "No lang attribute on <html>.",
              "signals", "markup")
    feed = s.find("link", type=lambda v: v and ("rss" in v.lower() or "atom" in v.lower()))
    score.add("signals_rss", 2, 2 if feed else 0,
              "A feed is discoverable." if feed else "No RSS or Atom feed is linked.",
              "signals", "markup")
    fresh = bool(page.headers.get("last-modified")) or bool(
        re.search(r'"dateModified"', page.html or ""))
    score.add("signals_freshness", 1, 1 if fresh else 0,
              "A modification date is published." if fresh
              else "Nothing states when the page last changed.", "signals", "markup")

    # --- 7. AI discovery (6) ------------------------------------------------------------------
    if fetch_site_files:
        for key, pts, url_path, must_parse in (
                ("ai_discovery_well_known", 2, "/.well-known/ai.txt", False),
                ("ai_discovery_summary", 2, "/ai/summary.json", True),
                ("ai_discovery_faq", 1, "/ai/faq.json", True),
                ("ai_discovery_service", 1, "/ai/service.json", True)):
            got = _try_fetch(f"{root}{url_path}")
            valid = bool(got)
            if got and must_parse:
                try:
                    json.loads(got.html)
                except Exception:  # noqa: BLE001
                    valid = False
            score.add(key, pts, pts if valid else 0,
                      f"{url_path} is present and valid." if valid
                      else (f"{url_path} exists but is not valid JSON." if got
                            else f"{url_path} is not published."),
                      "ai_discovery", "file")

    # --- 8. brand and entity (10) --------------------------------------------------------------
    brand_names = set()
    for n in nodes:
        if {"Organization", "LocalBusiness", "Corporation", "WebSite"} & set(_types(n)):
            if n.get("name"):
                brand_names.add(str(n["name"]).strip().lower())
    og_site = next((m.get("content") or "" for m in s.find_all("meta")
                    if (m.get("property") or "").lower() == "og:site_name"), "").strip().lower()
    title_tail = re.split(r"[|\-–—]", title)[-1].strip().lower() if title else ""
    candidates = {c for c in (og_site, title_tail) if c}
    coherent = bool(brand_names and candidates and
                    any(any(b in c or c in b for b in brand_names) for c in candidates))
    score.add("brand_entity_coherence", 3, 3 if coherent else 0,
              "The brand name is consistent across title, schema and Open Graph." if coherent
              else "The brand name is not stated consistently across title, schema and Open Graph, "
                   "so a model cannot be sure what this organisation is called.", "brand", "markup")

    sameas = []
    for n in nodes:
        v = n.get("sameAs")
        sameas.extend(v if isinstance(v, list) else [v] if v else [])
    kg = [u for u in sameas if any(d in str(u).lower() for d in KG_DOMAINS)]
    score.add("brand_kg_readiness", 3, 3 if kg else 0,
              f"{len(kg)} sameAs link(s) to knowledge-graph sources." if kg
              else "No sameAs links to Wikipedia, Wikidata, LinkedIn or similar, so nothing ties this "
                   "site to a known entity.", "brand", "markup")

    hrefs = " ".join((a.get("href") or "").lower() for a in s.find_all("a", href=True))
    has_about = bool(re.search(r"/(about|about-us|company|team)\b", hrefs))
    has_contact = bool(re.search(r"/(contact|contact-us|support)\b", hrefs))
    score.add("brand_about_contact", 2, 2 if (has_about and has_contact) else 0,
              "About and contact pages are discoverable." if (has_about and has_contact)
              else f"Missing a discoverable {'about' if not has_about else 'contact'} page.",
              "brand", "content")

    has_geo = bool({"LocalBusiness", "PostalAddress", "Place"} & types) or bool(
        [n for n in nodes if n.get("address")])
    score.add("brand_geo_identity", 1, 1 if has_geo else 0,
              "A geographic identity is declared." if has_geo
              else "No address or geographic signal.", "brand", "markup")

    heading_words = " ".join(h.get_text(" ") for h in s.find_all(["h1", "h2", "h3"])).lower()
    topical = bool(title_tail or brand_names) and bool(
        set(re.findall(r"[a-z]{5,}", heading_words)) &
        set(re.findall(r"[a-z]{5,}", (title + " " + desc).lower())))
    score.add("brand_topic_authority", 1, 1 if topical else 0,
              "Headings, title and description share a consistent topic." if topical
              else "Headings, title and description do not share a clear common topic.",
              "brand", "content")

    return score


def webmcp_readiness(score: GeoScore) -> dict[str, str]:
    """geo-optimizer's four-level indicator. Deliberately excluded from the score, as they exclude it."""
    got = {s.key for s in score.signals if s.earned > 0}
    discovery = {"ai_discovery_well_known", "ai_discovery_summary", "ai_discovery_faq"}
    if discovery <= got and {"llms_depth", "schema_richness"} <= got:
        level = "advanced"
    elif discovery <= got:
        level = "ready"
    elif got & {"ai_discovery_well_known", "ai_discovery_summary"}:
        level = "basic"
    else:
        level = "none"
    return {"level": level,
            "meaning": {"none": "No machine-readable AI context endpoints.",
                        "basic": "Some AI discovery signals, incomplete.",
                        "ready": "The full AI discovery suite is present.",
                        "advanced": "Full discovery plus rich schema and a deep llms.txt."}[level]}
