"""Where a domain exists outside its own server — the corpus, and the citations.

Two questions this answers, both without a data vendor and both verifiable by the customer.

**Is your content in Common Crawl?** Common Crawl is the open corpus that a great deal of model
training and grounding is built on. If your pages are not in it, a large class of AI systems has never
seen them, and no amount of on-page work changes that. The index is keyless and queryable per crawl,
so coverage and its trend over time are directly measurable.

**Who actually cites you?** A complete backlink graph is not obtainable without a vendor, and Doxa does
not pretend otherwise — the spec commits to reporting coverage rather than a total. What *is*
obtainable, keyless and exactly, is citation from the sources models demonstrably read: Wikipedia,
Wikidata, Hacker News, GitHub. Each result is a real URL the customer can open. Fewer numbers than a
backlink vendor sells, and every one of them true.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import time
import urllib.parse
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

import requests

UA = {"User-Agent": "Doxa/1.0 (technical SEO audit; contact via the service listing)"}
CC_COLLINFO = "https://index.commoncrawl.org/collinfo.json"

_INDEX_CACHE: dict[str, Any] = {"fetched": 0.0, "indexes": []}
_INDEX_TTL = 3600.0


class SourceUnavailable(RuntimeError):
    """A source could not be reached. Never rendered as "no results" — absence must be provable."""


def _get(url: str, params: dict | None = None, timeout: int = 45) -> requests.Response:
    r = requests.get(url, params=params or {}, headers=UA, timeout=timeout)
    if r.status_code != 200:
        raise SourceUnavailable(f"{urllib.parse.urlsplit(url).netloc} returned HTTP {r.status_code}")
    return r


# --- Common Crawl ---------------------------------------------------------------------------------

_INDEX_CACHE_FILE = Path(os.environ.get("DOXA_CACHE_DIR", ".cache")) / "commoncrawl-indexes.json"


def common_crawl_indexes(limit: int = 8) -> list[dict[str, str]]:
    """The most recent crawl indexes, newest first.

    Cached in memory and on disk, because this list changes a few times a year and the endpoint that
    serves it does not always answer. Without the disk copy, one upstream timeout takes every
    corpus service down with it — a customer would see a failure that has nothing to do with their
    domain. With it, a transient outage costs nothing and a long one is at worst slightly stale.
    """
    now = time.time()
    if _INDEX_CACHE["indexes"] and now - _INDEX_CACHE["fetched"] <= _INDEX_TTL:
        return _INDEX_CACHE["indexes"][:limit]

    try:
        data = _get(CC_COLLINFO, timeout=20).json()
        if not isinstance(data, list) or not data:
            raise SourceUnavailable("the crawl index list came back empty")
        _INDEX_CACHE.update({"fetched": now, "indexes": data})
        try:
            _INDEX_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _INDEX_CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass          # a cache we cannot write is not a reason to fail the request
        return data[:limit]
    except Exception as live_error:  # noqa: BLE001
        if _INDEX_CACHE["indexes"]:
            return _INDEX_CACHE["indexes"][:limit]
        try:
            cached = json.loads(_INDEX_CACHE_FILE.read_text(encoding="utf-8"))
            if cached:
                _INDEX_CACHE.update({"fetched": now, "indexes": cached})
                return cached[:limit]
        except Exception:  # noqa: BLE001
            pass
        raise SourceUnavailable(
            "the Common Crawl index list is unreachable and nothing is cached locally") from live_error


@dataclass
class CorpusPresence:
    domain: str
    per_crawl: list[dict[str, Any]] = field(default_factory=list)
    unique_urls: set[str] = field(default_factory=set)
    checked_indexes: int = 0
    unreachable: list[str] = field(default_factory=list)

    @property
    def crawls_present_in(self) -> int:
        return sum(1 for c in self.per_crawl if c["captures"] > 0)

    @property
    def any_capped(self) -> bool:
        return any(c.get("sample_capped") for c in self.per_crawl)

    def as_dict(self) -> dict[str, Any]:
        return {"domain": self.domain,
                "indexes_checked": self.checked_indexes,
                "crawls_present_in": self.crawls_present_in,
                "unique_urls_seen": len(self.unique_urls),
                "unique_urls_capped": self.any_capped,
                "index_blocks_latest": next((c["index_blocks"] for c in self.per_crawl
                                             if c.get("index_blocks") is not None), None),
                "per_crawl": self.per_crawl,
                "sources_unreachable": self.unreachable,
                "caveat": ("Common Crawl is a large sample of the web, not the whole of it. Absence "
                           "from one crawl is weak evidence; absence from all of them is strong.")}


def common_crawl_presence(domain: str, *, indexes: int = 5, page_limit: int = 200,
                          timeout: int = 25) -> CorpusPresence:
    """How much of this domain the open crawl has actually captured, and how that is trending.

    The indexes are queried concurrently. Serially, each one costs two requests against an index that
    is sometimes very slow, so five indexes could take minutes — long enough that a caller times out
    and the service looks broken rather than thorough. Concurrency makes the wall-clock the slowest
    single index instead of their sum, and the per-request timeout bounds even that.
    """
    host = (urllib.parse.urlsplit(domain if "//" in domain else f"https://{domain}").hostname
            or domain).lower().removeprefix("www.")
    presence = CorpusPresence(domain=host)
    metas = common_crawl_indexes(indexes)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, max(1, len(metas)))) as pool:
        rows = list(pool.map(lambda m: _presence_for_index(m, host, page_limit, timeout), metas))

    for row, urls, unreachable in rows:
        presence.checked_indexes += 1
        presence.unique_urls.update(urls)
        if unreachable:
            presence.unreachable.append(row["crawl"])
        presence.per_crawl.append(row)
    # Newest first, so the trend reads in the order a person expects.
    presence.per_crawl.sort(key=lambda r: r["crawl"], reverse=True)
    return presence


def _presence_for_index(meta: dict, host: str, page_limit: int,
                        timeout: int) -> tuple[dict[str, Any], set[str], bool]:
    """One crawl index: the true scale, then a bounded sample."""
    row: dict[str, Any] = {"crawl": meta["id"], "captures": 0, "status_2xx": 0,
                           "html": 0, "sample": [], "index_blocks": None,
                           "sample_capped": False}
    seen_urls: set[str] = set()

    # The true magnitude first, before sampling. Without it every domain that exceeds the sample
    # limit reports exactly `page_limit` captures, so a site with 400 pages and one with 400,000
    # look identical — and a comparison between two of them is worthless. `blocks` is the number
    # of index blocks the domain occupies: a genuine, uncapped scale signal.
    try:
        n = requests.get(meta["cdx-api"], headers=UA, timeout=timeout,
                         params={"url": f"{host}/*", "output": "json",
                                 "showNumPages": "true"})
        if n.status_code == 200:
            row["index_blocks"] = n.json().get("blocks")
    except Exception:  # noqa: BLE001
        pass

    try:
        r = requests.get(meta["cdx-api"], headers=UA, timeout=timeout,
                         params={"url": f"{host}/*", "output": "json", "limit": page_limit})
        if r.status_code == 404:
            # The index legitimately holds nothing for this domain. That is a zero, not a failure.
            return row, seen_urls, False
        if r.status_code != 200:
            raise SourceUnavailable(f"HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        # Recorded as unreachable, never as zero coverage: the difference matters entirely.
        row["unreachable"] = f"{type(e).__name__}: {str(e)[:80]}"
        return row, seen_urls, True

    for line in r.text.strip().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        row["captures"] += 1
        if str(rec.get("status", "")).startswith("2"):
            row["status_2xx"] += 1
        if "html" in (rec.get("mime") or ""):
            row["html"] += 1
        url = rec.get("url")
        if url:
            seen_urls.add(url)
            if len(row["sample"]) < 5:
                row["sample"].append(url)
    # Stated, never inferred: a capture count equal to the limit is a floor, not a total.
    row["sample_capped"] = row["captures"] >= page_limit
    if row["sample_capped"]:
        row["captures_note"] = (f"at least {page_limit}; the sample was capped. Use "
                                f"index_blocks to compare scale between domains.")
    return row, seen_urls, False

# --- citations from sources models read -------------------------------------------------------------

@dataclass
class Citation:
    source: str
    title: str
    url: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "title": self.title, "url": self.url, **self.detail}


def wikipedia_citations(domain: str, *, limit: int = 25,
                        lang: str = "en") -> tuple[list[Citation], str]:
    """Articles whose external links point at this domain.

    Wikipedia is the single highest-value citation a domain can hold: it is in every training corpus,
    it is what models fall back on for entity facts, and it feeds Wikidata and the knowledge panel.
    """
    host = domain.lower().removeprefix("www.")
    r = _get(f"https://{lang}.wikipedia.org/w/api.php",
             params={"action": "query", "list": "exturlusage", "euquery": host,
                     "eulimit": min(limit * 2, 100), "euprop": "title|url",
                     "eunamespace": 0,          # articles only: talk and user pages are not citations
                     "format": "json"})
    out: list[Citation] = []
    for row in r.json().get("query", {}).get("exturlusage", []):
        title = row.get("title", "")
        out.append(Citation("wikipedia", title,
                            f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
                            {"links_to": row.get("url", "")}))
        if len(out) >= limit:
            break
    return out, f"{lang}.wikipedia.org"


def wikidata_entity(brand: str) -> dict[str, Any] | None:
    """The Wikidata entity for a brand, if one exists.

    A Wikidata item is what lets a model treat you as a *thing* rather than a string. Without one
    there is nothing to attach facts to, and `sameAs` in your schema has nowhere to point.
    """
    r = _get("https://www.wikidata.org/w/api.php",
             params={"action": "wbsearchentities", "search": brand, "language": "en",
                     "format": "json", "limit": 5})
    hits = r.json().get("search", [])
    if not hits:
        return None
    best = hits[0]
    return {"id": best["id"], "label": best.get("label"),
            "description": best.get("description"),
            "url": f"https://www.wikidata.org/wiki/{best['id']}",
            "other_candidates": [{"id": h["id"], "description": h.get("description")}
                                 for h in hits[1:4]]}


def hackernews_citations(domain: str, *, limit: int = 25) -> tuple[list[Citation], str]:
    """Submissions and comments naming the domain, with their scores.

    Hacker News is small but disproportionately represented in training data and in what models cite
    for developer-facing topics.
    """
    host = domain.lower().removeprefix("www.")
    r = _get("https://hn.algolia.com/api/v1/search",
             params={"query": host, "hitsPerPage": min(limit, 50)})
    out: list[Citation] = []
    for h in r.json().get("hits", []):
        out.append(Citation("hackernews", (h.get("title") or h.get("story_title") or "")[:160],
                            f"https://news.ycombinator.com/item?id={h['objectID']}",
                            {"points": h.get("points"), "comments": h.get("num_comments"),
                             "created": h.get("created_at"), "links_to": h.get("url")}))
    return out, "hn.algolia.com"


def github_citations(domain: str, *, limit: int = 20) -> tuple[list[Citation], str]:
    """Repositories that name the domain, ordered by stars.

    Code is heavily represented in training data, and a well-starred repository is a durable citation.
    """
    host = domain.lower().removeprefix("www.")
    r = _get("https://api.github.com/search/repositories",
             params={"q": host, "sort": "stars", "order": "desc",
                     "per_page": min(limit, 50)})
    out: list[Citation] = []
    for h in r.json().get("items", []):
        out.append(Citation("github", h["full_name"], h["html_url"],
                            {"stars": h.get("stargazers_count"),
                             "description": (h.get("description") or "")[:160],
                             "homepage": h.get("homepage")}))
    return out, "api.github.com"


SOURCES = {"wikipedia": wikipedia_citations,
           "hackernews": hackernews_citations,
           "github": github_citations}


def gather_citations(domain: str, *, sources: list[str] | None = None,
                     limit: int = 20) -> dict[str, Any]:
    """Query every source and keep the failures visible.

    A source that could not be reached is listed as unreachable. Folding it into "no citations found"
    would turn our outage into their reputational finding.
    """
    wanted = [n for n in (sources or list(SOURCES)) if n in SOURCES]
    citations: list[Citation] = []
    queried: list[str] = []
    unreachable: list[dict[str, str]] = []

    def ask(name: str):
        try:
            found, _ = SOURCES[name](domain, limit=limit)
            return name, found, ""
        except Exception as e:  # noqa: BLE001
            return name, [], f"{type(e).__name__}: {str(e)[:100]}"

    # The sources are independent, so they are asked at once. Serially, one slow source sets the
    # latency of the whole service for no benefit.
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(wanted))) as pool:
        for name, found, error in pool.map(ask, wanted):
            if error:
                unreachable.append({"source": name, "error": error})
                continue
            citations.extend(found)
            queried.append(name)

    if not queried:
        raise SourceUnavailable(
            "No citation source could be reached, so nothing was measured. This is an outage on our "
            "side and is not a finding about the domain: "
            + "; ".join(u["error"] for u in unreachable))

    by_source: dict[str, int] = {}
    for c in citations:
        by_source[c.source] = by_source.get(c.source, 0) + 1
    return {"domain": domain,
            "sources_queried": queried,
            "sources_unreachable": unreachable,
            "total": len(citations),
            "by_source": by_source,
            "citations": [c.as_dict() for c in citations],
            "caveat": ("These are citations that can be verified by opening the URL. They are not a "
                       "complete backlink profile — no such thing is obtainable without a data "
                       "vendor, and Doxa does not estimate one.")}
