"""Structured data — JSON-LD, microdata, RDFa, Open Graph, Twitter cards.

This matters twice over, and the second reason is newer than most audit tools.

A search engine uses schema.org to build a rich result. A language model uses it as the one part of a
page whose meaning is not in doubt: `"price": "49.00"` is unambiguous in a way that `$49` inside a
paragraph never is. A page with correct JSON-LD is quotable; a page without it has to be interpreted.

What is checked here is what can be proven from the document: does it parse, does it carry `@type` and
`@context`, are the properties Google documents as required actually present, and does what it claims
agree with what the page says. Validation is against the published required-property lists, and it
says which spec each requirement comes from rather than asserting rules from memory.
"""
from __future__ import annotations

import json
from typing import Any

from checks.base import Finding, Severity, registry
from checks.page_html import soup
from fetch import Page

# Required and recommended properties, from Google's structured-data documentation for each type.
# Only the types worth auditing are listed; an unlisted type is reported as present, not as wrong.
SCHEMA_RULES: dict[str, dict[str, list[str]]] = {
    "Article":        {"required": ["headline"],
                       "recommended": ["author", "datePublished", "image", "dateModified"]},
    "NewsArticle":    {"required": ["headline"],
                       "recommended": ["author", "datePublished", "image", "dateModified"]},
    "BlogPosting":    {"required": ["headline"],
                       "recommended": ["author", "datePublished", "image", "dateModified"]},
    "Product":        {"required": ["name"],
                       "recommended": ["image", "description", "offers", "aggregateRating", "brand"]},
    "Offer":          {"required": ["price", "priceCurrency"],
                       "recommended": ["availability", "url"]},
    "FAQPage":        {"required": ["mainEntity"], "recommended": []},
    "Question":       {"required": ["name", "acceptedAnswer"], "recommended": []},
    "HowTo":          {"required": ["name", "step"], "recommended": ["totalTime", "image"]},
    "Recipe":         {"required": ["name"],
                       "recommended": ["image", "recipeIngredient", "recipeInstructions",
                                       "cookTime", "nutrition"]},
    "Event":          {"required": ["name", "startDate", "location"],
                       "recommended": ["endDate", "offers", "performer", "eventStatus"]},
    "Organization":   {"required": ["name"], "recommended": ["url", "logo", "sameAs"]},
    "LocalBusiness":  {"required": ["name", "address"],
                       "recommended": ["telephone", "openingHours", "geo", "priceRange"]},
    "Person":         {"required": ["name"], "recommended": ["url", "sameAs", "jobTitle"]},
    "BreadcrumbList": {"required": ["itemListElement"], "recommended": []},
    "VideoObject":    {"required": ["name", "thumbnailUrl", "uploadDate"],
                       "recommended": ["description", "duration", "contentUrl", "transcript"]},
    "SoftwareApplication": {"required": ["name"],
                            "recommended": ["applicationCategory", "operatingSystem", "offers",
                                            "aggregateRating"]},
    "Review":         {"required": ["reviewRating", "author"], "recommended": ["itemReviewed"]},
    "AggregateRating": {"required": ["ratingValue"], "recommended": ["reviewCount", "ratingCount"]},
    "WebSite":        {"required": ["name"], "recommended": ["url", "potentialAction"]},
    "WebPage":        {"required": [], "recommended": ["name", "description", "datePublished"]},
    "Dataset":        {"required": ["name", "description"],
                       "recommended": ["license", "creator", "distribution"]},
    "JobPosting":     {"required": ["title", "description", "datePosted", "hiringOrganization"],
                       "recommended": ["jobLocation", "baseSalary", "employmentType"]},
    "Course":         {"required": ["name", "description"],
                       "recommended": ["provider", "offers", "hasCourseInstance"]},
}


# Types that describe *this page*. Everything else in a @graph — the author, the publisher, the logo,
# the breadcrumb trail — describes something adjacent to it and is expected to be named differently.
PAGE_ENTITIES = {"Article", "NewsArticle", "BlogPosting", "WebPage", "ItemPage", "CollectionPage",
                 "AboutPage", "ContactPage", "ProfilePage", "QAPage", "FAQPage", "Product",
                 "Recipe", "HowTo", "Event", "JobPosting", "Course", "SoftwareApplication",
                 "VideoObject", "Dataset"}


def _broadly_agrees(name: str, title: str) -> bool:
    """Does this entity name plausibly describe the same thing as the page title?

    Deliberately generous. Titles carry suffixes ("… • Yoast"), separators and truncation, so an exact
    match is far too strict; the question is only whether the two are talking about the same subject.
    """
    a, b = name.lower().strip(), title.lower().strip()
    if a in b or b in a:
        return True
    wa, wb = set(a.split()), set(b.split())
    return len(wa & wb) >= max(1, min(len(wa), len(wb)) // 3)


def _flatten(node: Any, out: list[dict], context: Any = None) -> None:
    """Walk JSON-LD into a flat list of typed nodes.

    Real pages nest: a @graph of nodes, an Article whose author is a Person, a Product whose offers is
    an Offer. Checking only the top level would miss most of what is actually declared, so every typed
    object at any depth is collected.

    `@context` is carried down from ancestors, because JSON-LD inherits it — the node inside a `@graph`
    is governed by the context declared beside that `@graph`, not on the node itself. Without this,
    every document written by Yoast or RankMath, which is a large share of the web, would be reported
    as having no @context. That is the shape real pages ship in, and calling it broken would be a
    confident falsehood about correct markup.
    """
    if isinstance(node, list):
        for item in node:
            _flatten(item, out, context)
    elif isinstance(node, dict):
        context = node.get("@context", context)
        if "@type" in node:
            out.append(node if "@context" in node or context is None
                       else {**node, "@context": context, "@context_inherited": True})
        for key, value in node.items():
            if key != "@type" and isinstance(value, (dict, list)):
                _flatten(value, out, context)


def _types(node: dict) -> list[str]:
    t = node.get("@type")
    if isinstance(t, list):
        return [str(x).split("/")[-1] for x in t]
    return [str(t).split("/")[-1]] if t else []


def parse_jsonld(page: Page) -> tuple[list[dict], list[dict]]:
    """Return (parsed nodes, parse errors). A block that does not parse is itself the finding."""
    s = soup(page)
    nodes: list[dict] = []
    errors: list[dict] = []
    for i, tag in enumerate(s.find_all("script", type=lambda v: v and "ld+json" in v.lower())):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            errors.append({"block": i, "error": "empty block"})
            continue
        try:
            _flatten(json.loads(raw), nodes)
        except json.JSONDecodeError as e:
            errors.append({"block": i, "error": f"line {e.lineno} column {e.colno}: {e.msg}",
                           "excerpt": raw.strip()[:200]})
    return nodes, errors


@registry.register("schema", "Structured data")
def check_structured_data(page: Page) -> list[Finding]:
    nodes, errors = parse_jsonld(page)
    s = soup(page)
    out: list[Finding] = []

    for err in errors:
        out.append(Finding("schema.invalid_json", Severity.HIGH,
                           f"A JSON-LD block does not parse ({err['error']}), so every rich result it "
                           f"was meant to produce is lost.", err))

    microdata = s.find_all(attrs={"itemtype": True})
    rdfa = s.find_all(attrs={"typeof": True})

    if not nodes and not errors:
        if microdata or rdfa:
            out.append(Finding("schema.legacy_only", Severity.LOW,
                               f"Structured data is present but only as "
                               f"{'microdata' if microdata else 'RDFa'}. JSON-LD is the format Google "
                               f"recommends and the one a language model parses most reliably.",
                               {"microdata": len(microdata), "rdfa": len(rdfa)}))
        else:
            out.append(Finding("schema.missing", Severity.HIGH,
                               "There is no structured data on the page. This is the one part of a page "
                               "whose meaning is unambiguous to a machine — without it every fact has "
                               "to be inferred from prose.", {}))
        return out

    found_types: list[str] = []
    for node in nodes:
        found_types.extend(_types(node))

    if nodes and not any("@context" in n for n in nodes):
        out.append(Finding("schema.no_context", Severity.HIGH,
                           "The JSON-LD has no @context, so none of the properties resolve to "
                           "schema.org and the block is ignored.", {"types": found_types[:10]}))

    for node in nodes:
        for t in _types(node):
            rule = SCHEMA_RULES.get(t)
            if not rule:
                continue
            missing_req = [p for p in rule["required"] if not node.get(p)]
            if missing_req:
                out.append(Finding("schema.missing_required", Severity.HIGH,
                                   f"{t} is missing the required propert"
                                   f"{'y' if len(missing_req) == 1 else 'ies'} "
                                   f"{', '.join(missing_req)}. Google will not build a rich result "
                                   f"from it.",
                                   {"type": t, "missing": missing_req,
                                    "present": sorted(k for k in node if not k.startswith("@"))}))
            missing_rec = [p for p in rule["recommended"] if not node.get(p)]
            if missing_rec:
                out.append(Finding("schema.missing_recommended", Severity.LOW,
                                   f"{t} is valid but omits {', '.join(missing_rec[:4])}"
                                   f"{'…' if len(missing_rec) > 4 else ''}, which the rich result uses.",
                                   {"type": t, "missing": missing_rec}))

    # Structured data that disagrees with the visible page is worse than none: Google treats it as
    # spam, and a model that trusts it repeats something the page does not say.
    #
    # Only the page's *own* primary entity is compared. A @graph legitimately contains the author as a
    # Person, the publisher as an Organization, breadcrumbs as ListItems and the logo as an
    # ImageObject — none of which share the page title, and none of which are a contradiction.
    # Comparing every node reported yoast.com as contradicting itself because its author is called
    # Joost de Valk while the page is called "SEO for everyone".
    title = (s.title.get_text().strip() if s.title else "")
    primary = [n for n in nodes if set(_types(n)) & PAGE_ENTITIES]
    names = [str(n.get("headline") or n.get("name") or "").strip() for n in primary]
    names = [n for n in names if len(n) > 10]
    if title and names and not any(_broadly_agrees(n, title) for n in names):
        out.append(Finding("schema.contradicts_page", Severity.HIGH,
                           "The structured data names something different from the page title. Markup "
                           "that disagrees with the visible page is treated as spam.",
                           {"schema": names[:3], "title": title[:120]}))

    if not [f for f in out if f.severity in (Severity.CRITICAL, Severity.HIGH)]:
        out.append(Finding("schema.present", Severity.INFO,
                           f"Structured data is present and valid: "
                           f"{', '.join(sorted(set(found_types))[:8])}.",
                           {"types": sorted(set(found_types)), "nodes": len(nodes)}))
    return out


@registry.register("social", "Sharing metadata")
def check_social_cards(page: Page) -> list[Finding]:
    """Open Graph and Twitter cards decide what a link looks like when a person — or an agent —
    shares it. Missing ones do not hurt ranking; they lose the click."""
    s = soup(page)
    og = {(m.get("property") or "").lower(): (m.get("content") or "").strip()
          for m in s.find_all("meta") if (m.get("property") or "").lower().startswith("og:")}
    tw = {(m.get("name") or "").lower(): (m.get("content") or "").strip()
          for m in s.find_all("meta") if (m.get("name") or "").lower().startswith("twitter:")}
    out: list[Finding] = []

    missing_og = [k for k in ("og:title", "og:description", "og:image") if not og.get(k)]
    if missing_og == ["og:title", "og:description", "og:image"]:
        out.append(Finding("social.no_opengraph", Severity.LOW,
                           "There are no Open Graph tags, so a shared link shows whatever the platform "
                           "guesses rather than a title, description and image you chose.", {}))
    elif missing_og:
        out.append(Finding("social.incomplete_opengraph", Severity.LOW,
                           f"Open Graph is partly set up but missing {', '.join(missing_og)}.",
                           {"missing": missing_og, "present": sorted(og)}))
    if og and not tw.get("twitter:card"):
        out.append(Finding("social.no_twitter_card", Severity.LOW,
                           "There is no twitter:card, so X falls back to a small preview instead of a "
                           "large image.", {"opengraph": sorted(og)}))
    if not out and (og or tw):
        out.append(Finding("social.ok", Severity.INFO,
                           "Sharing metadata is complete.",
                           {"opengraph": sorted(og), "twitter": sorted(tw)}))
    return out
