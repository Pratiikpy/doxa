"""A bounded, polite site crawler.

A crawl is the one Doxa operation that can hurt somebody. Every other service makes a handful of
requests; this one can make thousands, against a server the customer may not own. So the limits are
not tuning knobs, they are the design:

  * **robots.txt is obeyed**, using the same longest-match parser the audit sells. Selling a robots
    checker while ignoring robots ourselves would be indefensible.
  * **One host.** Links off-site are recorded and never followed.
  * **A page budget and a wall-clock deadline**, both enforced, and both reported when they bite. A
    crawl that silently stopped at 200 pages and reported "no orphan pages" would be a lie by
    omission.
  * **A delay between requests to the same host**, so a paid audit cannot become a load test.
  * **Every URL is SSRF-guarded**, on every hop, because links come off pages we do not control.

The crawler records the link graph as it goes. Click depth, orphan pages and the internal link
structure are all properties of that graph, and cannot be recovered afterwards from the pages alone.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

from bs4 import BeautifulSoup

from checks.challenge import detect as detect_challenge
from checks.machine import RobotsRules
from fetch import DEFAULT_TIMEOUT, FetchError, Page, SsrfError, fetch

MAX_PAGES_HARD = 2000          # absolute ceiling regardless of what is requested
DEFAULT_DELAY_S = 0.25         # per-worker pause between requests to the same host


def normalise(url: str) -> str:
    """One canonical spelling per page, so the same page is not crawled twice.

    Fragments go, because they never identify a different document. The rest is left alone: query
    strings often do identify a different page, and stripping them would silently merge distinct URLs
    and under-report the site.
    """
    p = urllib.parse.urlsplit(url)
    path = p.path or "/"
    netloc = p.netloc.lower()
    if (p.scheme == "https" and netloc.endswith(":443")) or \
       (p.scheme == "http" and netloc.endswith(":80")):
        netloc = netloc.rsplit(":", 1)[0]
    return urllib.parse.urlunsplit((p.scheme.lower(), netloc, path, p.query, ""))


@dataclass
class CrawledPage:
    url: str
    status: int
    depth: int
    ok: bool
    media_type: str = ""
    title: str = ""
    description: str = ""
    canonical: str = ""
    lang: str = ""
    robots: str = ""
    body_hash: str = ""
    word_count: int = 0
    ttfb_ms: int = 0
    size_bytes: int = 0
    internal_links: list[str] = field(default_factory=list)
    external_links: list[str] = field(default_factory=list)
    nofollow_links: set[str] = field(default_factory=set)
    hreflangs: list[dict] = field(default_factory=list)
    redirect_chain: list[str] = field(default_factory=list)
    challenge: str = ""
    error: str = ""

    @property
    def indexable(self) -> bool:
        return self.ok and "noindex" not in (self.robots or "").lower()

    def as_dict(self) -> dict[str, Any]:
        return {"url": self.url, "status": self.status, "depth": self.depth,
                "title": self.title, "description": self.description,
                "canonical": self.canonical, "indexable": self.indexable,
                "word_count": self.word_count, "ttfb_ms": self.ttfb_ms,
                "internal_links": len(self.internal_links),
                "external_links": len(self.external_links)}


@dataclass
class CrawlResult:
    start_url: str
    pages: list[CrawledPage] = field(default_factory=list)
    # url -> the pages that link to it. The graph must be captured during the crawl; click depth and
    # orphan detection cannot be reconstructed from the page list afterwards.
    inbound: dict[str, list[str]] = field(default_factory=dict)
    robots_found: bool = False
    sitemaps: list[str] = field(default_factory=list)
    stopped_because: str = ""
    queued_not_visited: int = 0
    disallowed: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def truncated(self) -> bool:
        return bool(self.stopped_because)

    def by_url(self) -> dict[str, CrawledPage]:
        return {p.url: p for p in self.pages}

    def summary(self) -> dict[str, Any]:
        return {"pages_crawled": len(self.pages),
                "html_pages": sum(1 for p in self.pages if p.media_type == "text/html"),
                "indexable": sum(1 for p in self.pages if p.indexable),
                "errors": sum(1 for p in self.pages if not p.ok),
                "max_depth": max((p.depth for p in self.pages), default=0),
                "robots_txt": self.robots_found,
                "sitemaps": self.sitemaps[:10],
                "disallowed_by_robots": len(self.disallowed),
                "truncated": self.truncated,
                "stopped_because": self.stopped_because or None,
                "queued_not_visited": self.queued_not_visited,
                "elapsed_s": round(self.elapsed_s, 1)}


def _same_site(a: str, b: str) -> bool:
    ha = (urllib.parse.urlsplit(a).hostname or "").lower().removeprefix("www.")
    hb = (urllib.parse.urlsplit(b).hostname or "").lower().removeprefix("www.")
    return ha == hb


def _extract(page: Page, url: str) -> CrawledPage:
    s = BeautifulSoup(page.html or "", "lxml")
    base = url
    base_tag = s.find("base", href=True)
    if base_tag:
        base = urllib.parse.urljoin(url, base_tag["href"].strip())

    internal: list[str] = []
    external: list[str] = []
    nofollow: set[str] = set()
    for a in s.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if not href or href.startswith("#") or href.lower().startswith(
                ("mailto:", "tel:", "javascript:", "sms:", "data:")):
            continue
        try:
            resolved = normalise(urllib.parse.urljoin(base, href))
        except ValueError:
            continue
        if not resolved.startswith(("http://", "https://")):
            continue
        rel = a.get("rel") or []
        rel = " ".join(rel).lower() if isinstance(rel, list) else str(rel).lower()
        (internal if _same_site(resolved, url) else external).append(resolved)
        if "nofollow" in rel:
            nofollow.add(resolved)

    body = s.body or s
    for tag in body(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", body.get_text(" ")).strip()

    canonical = ""
    for link in s.find_all("link", href=True):
        rels = link.get("rel") or []
        rels = [r.lower() for r in (rels if isinstance(rels, list) else [rels])]
        if "canonical" in rels:
            canonical = normalise(urllib.parse.urljoin(base, link["href"].strip()))
            break

    robots_meta = " ".join((m.get("content") or "") for m in s.find_all("meta")
                           if (m.get("name") or "").lower() in ("robots", "googlebot"))
    if page.headers.get("x-robots-tag"):
        robots_meta += " " + page.headers["x-robots-tag"]

    hreflangs = []
    for link in s.find_all("link", href=True):
        if link.get("hreflang"):
            hreflangs.append({"lang": link["hreflang"].strip(),
                              "url": normalise(urllib.parse.urljoin(base, link["href"].strip()))})

    ch = detect_challenge(page)
    return CrawledPage(
        url=url, status=page.status, depth=0, ok=page.ok, media_type=page.media_type,
        title=(s.title.get_text().strip() if s.title else ""),
        description=next((m.get("content") or "" for m in s.find_all("meta")
                          if (m.get("name") or "").lower() == "description"), "").strip(),
        canonical=canonical,
        lang=((s.html.get("lang") if s.html else "") or "").strip(),
        robots=robots_meta.strip(),
        # Hash the normalised visible text, not the raw HTML: two pages with identical copy and
        # different build hashes in a script tag are duplicates to a reader and to a search engine.
        body_hash=hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "",
        word_count=len(text.split()),
        ttfb_ms=page.ttfb_ms, size_bytes=page.size_bytes,
        internal_links=internal, external_links=external, nofollow_links=nofollow,
        hreflangs=hreflangs,
        redirect_chain=[h.url for h in page.hops],
        challenge=ch.vendor if ch else "")


def crawl(start_url: str, *, max_pages: int = 200, max_depth: int = 5,
          deadline_s: float = 120.0, workers: int = 4, delay_s: float = DEFAULT_DELAY_S,
          respect_robots: bool = True, user_agent: str | None = None,
          on_page: Callable[[CrawledPage], None] | None = None) -> CrawlResult:
    """Breadth-first crawl of one host, bounded by pages, depth and wall clock."""
    start = normalise(start_url)
    max_pages = max(1, min(max_pages, MAX_PAGES_HARD))
    result = CrawlResult(start_url=start)
    began = time.perf_counter()

    rules = RobotsRules("")
    if respect_robots:
        root = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(start))
        try:
            rp = fetch(f"{root}/robots.txt", timeout=12)
            if rp.ok:
                rules = RobotsRules(rp.html)
                result.robots_found = True
                result.sitemaps = rules.sitemaps
        except Exception:  # noqa: BLE001
            pass

    lock = threading.Lock()
    seen: set[str] = {start}
    frontier: list[tuple[str, int]] = [(start, 0)]
    stop = threading.Event()

    def allowed(u: str) -> bool:
        if not respect_robots:
            return True
        path = urllib.parse.urlsplit(u).path or "/"
        ok, _ = rules.allowed(user_agent or "Doxa", path)
        return ok

    def visit(item: tuple[str, int]) -> CrawledPage | None:
        url, depth = item
        if stop.is_set():
            return None
        try:
            page = fetch(url, timeout=DEFAULT_TIMEOUT, render=False)
        except SsrfError as e:
            return CrawledPage(url=url, status=0, depth=depth, ok=False, error=f"refused: {e}")
        except FetchError as e:
            return CrawledPage(url=url, status=0, depth=depth, ok=False, error=str(e)[:160])
        except Exception as e:  # noqa: BLE001
            return CrawledPage(url=url, status=0, depth=depth, ok=False, error=type(e).__name__)
        finally:
            if delay_s:
                time.sleep(delay_s)

        cp = _extract(page, url) if page.is_html else CrawledPage(
            url=url, status=page.status, depth=depth, ok=page.ok, media_type=page.media_type,
            ttfb_ms=page.ttfb_ms, size_bytes=page.size_bytes,
            redirect_chain=[h.url for h in page.hops])
        cp.depth = depth
        return cp

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        while frontier and not stop.is_set():
            if time.perf_counter() - began > deadline_s:
                result.stopped_because = f"the {int(deadline_s)}s deadline was reached"
                break
            if len(result.pages) >= max_pages:
                result.stopped_because = f"the {max_pages}-page budget was reached"
                break

            # The batch is clamped to what is left in the budget. Taking a full batch and checking
            # the budget only at the top of the loop overshoots by up to `workers` pages — a crawl
            # asked for 30 fetched 53. That is a promise broken to the caller and unrequested load
            # on a server we do not own.
            remaining = max_pages - len(result.pages)
            batch = frontier[:max(1, min(workers, remaining))]
            frontier = frontier[len(batch):]
            for cp in pool.map(visit, batch):
                if cp is None:
                    continue
                with lock:
                    if len(result.pages) >= max_pages:
                        frontier.append((cp.url, cp.depth))
                        continue
                    result.pages.append(cp)
                if on_page:
                    on_page(cp)
                if cp.depth >= max_depth:
                    continue
                for link in cp.internal_links:
                    result.inbound.setdefault(link, []).append(cp.url)
                    if link in seen:
                        continue
                    seen.add(link)
                    if not allowed(link):
                        result.disallowed.append(link)
                        continue
                    frontier.append((link, cp.depth + 1))

    result.queued_not_visited = len(frontier)
    if not result.stopped_because and result.queued_not_visited:
        result.stopped_because = f"the depth limit of {max_depth} was reached"
    result.elapsed_s = time.perf_counter() - began
    return result
