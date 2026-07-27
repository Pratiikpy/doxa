"""Doxa's fetch layer — one page, captured twice.

Every check in Doxa reads from a `Page`: the raw bytes as an HTTP client sees them, and optionally the
DOM after JavaScript has run. Capturing both is not a nicety — the difference between them *is* the
answer to the single most valuable question this service asks, which is what an AI crawler actually
sees. So the two are captured together and carried in one object rather than fetched twice by
different checks with different guards.

The egress guard is the same one running in Reach and Aletheia: http(s) only, standard ports, every
resolved address must be globally routable, and every redirect hop is revalidated rather than trusted.
A public URL that 302s to 169.254.169.254 is the attack this closes.
"""
from __future__ import annotations

import concurrent.futures
import ipaddress
import re
import socket
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# The user-agents that matter for AI visibility. Sent verbatim when probing whether a site blocks
# them, and matched against robots.txt rules. Sourced from each vendor's published documentation.
AI_CRAWLERS = {
    "GPTBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.1; +https://openai.com/gptbot",
    "OAI-SearchBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; OAI-SearchBot/1.0; +https://openai.com/searchbot",
    "ChatGPT-User": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot",
    "ClaudeBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ClaudeBot/1.0; +claudebot@anthropic.com",
    "Claude-User": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; Claude-User/1.0; +Claude-User@anthropic.com",
    "PerplexityBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot",
    "Google-Extended": "Mozilla/5.0 (compatible; Google-Extended/1.0)",
    "Applebot-Extended": "Mozilla/5.0 (compatible; Applebot-Extended/1.0)",
    "Bytespider": "Mozilla/5.0 (compatible; Bytespider/1.0)",
    "meta-externalagent": "meta-externalagent/1.1",
}

ALLOWED_PORTS = {"", "80", "443", "8080", "8443"}
MAX_BYTES = 5_000_000
DEFAULT_TIMEOUT = 25


class SsrfError(Exception):
    """The URL targets something that is not a public internet address."""


class FetchError(Exception):
    """The page could not be retrieved. Carries why, so a check can report it honestly."""


def _is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_global


def guard_url(url: str) -> None:
    """Validate one hop. Raises SsrfError with the specific reason, never a bare False."""
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https"):
        raise SsrfError(f"blocked scheme: {p.scheme or '(none)'}")
    port = str(p.port) if p.port else ""
    if port not in ALLOWED_PORTS:
        raise SsrfError(f"blocked port: {port}")
    host = p.hostname
    if not host:
        raise SsrfError("no host in URL")
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except OSError:
        raise SsrfError(f"DNS resolution failed for {host}")
    if not infos:
        raise SsrfError(f"no addresses for {host}")
    for info in infos:
        ip = info[4][0]
        if not _is_public(ip):
            raise SsrfError(f"{host} resolves to a non-public address ({ip})")


@dataclass
class Hop:
    url: str
    status: int
    location: str | None = None


@dataclass
class Page:
    """One page, as fetched. Everything a check needs, captured once."""
    url: str                       # the URL finally fetched, after redirects
    requested_url: str             # what the caller asked for
    status: int = 0
    ok: bool = False
    media_type: str = ""
    charset: str = ""
    html: str = ""                 # raw response body, before any JavaScript
    rendered_html: str = ""        # DOM after JavaScript, when rendering was requested
    rendered: bool = False
    headers: dict[str, str] = field(default_factory=dict)
    hops: list[Hop] = field(default_factory=list)
    ttfb_ms: int = 0
    total_ms: int = 0
    size_bytes: int = 0
    blocked_reason: str = ""       # set when the fetch was refused rather than failed
    error: str = ""

    @property
    def is_html(self) -> bool:
        return self.media_type.startswith("text/html")

    @property
    def redirected(self) -> bool:
        return len(self.hops) > 1

    def body(self, prefer_rendered: bool = False) -> str:
        return self.rendered_html if (prefer_rendered and self.rendered_html) else self.html


_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.I)


def _sniff_charset(raw: bytes) -> str:
    """Read the declared charset out of the document itself.

    Only the head is scanned — a charset declaration is required to appear in the first 1024 bytes,
    and scanning a 5 MB body for it would be wasted work.
    """
    if raw[:3] == b"\xef\xbb\xbf":
        return "utf-8-sig"
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"
    m = _META_CHARSET.search(raw[:2048])
    if m:
        try:
            return m.group(1).decode("ascii", errors="ignore").lower()
        except Exception:  # noqa: BLE001
            return ""
    return ""


def _parse_content_type(value: str) -> tuple[str, str]:
    parts = [p.strip() for p in (value or "").split(";")]
    media = parts[0].lower() if parts else ""
    charset = ""
    for p in parts[1:]:
        if p.lower().startswith("charset="):
            charset = p.split("=", 1)[1].strip().strip('"').lower()
    return media, charset


def fetch(url: str, *, user_agent: str = UA, timeout: int = DEFAULT_TIMEOUT,
          max_redirects: int = 5, render: bool = False,
          extra_headers: dict[str, str] | None = None) -> Page:
    """Fetch a URL, following redirects manually and validating every hop.

    Redirects are followed by hand rather than by requests, because a single up-front SSRF check is
    defeated by a public URL that redirects to a private one. Each hop is guarded before it is taken.
    """
    requested = (url or "").strip()
    if not re.match(r"^https?://", requested, re.I):
        raise FetchError("needs a full http(s) URL")

    page = Page(url=requested, requested_url=requested)
    current = requested
    headers = {"User-Agent": user_agent,
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
               # brotli is refused deliberately: requests cannot decode it without an extra package,
               # and a feed or page that arrives as `br` would otherwise fail with a confusing error.
               "Accept-Encoding": "gzip, deflate",
               "Accept-Language": "en-US,en;q=0.9"}
    if extra_headers:
        headers.update(extra_headers)

    t_start = time.perf_counter()
    session = requests.Session()
    try:
        for _ in range(max_redirects + 1):
            guard_url(current)
            t0 = time.perf_counter()
            try:
                r = session.get(current, headers=headers, allow_redirects=False,
                                timeout=timeout, stream=True)
            except requests.exceptions.SSLError as e:
                raise FetchError(f"TLS failure: {str(e)[:160]}")
            except requests.exceptions.ConnectTimeout:
                raise FetchError(f"connection timed out after {timeout}s")
            except requests.exceptions.ReadTimeout:
                raise FetchError(f"read timed out after {timeout}s")
            except requests.exceptions.RequestException as e:
                raise FetchError(f"{type(e).__name__}: {str(e)[:160]}")

            ttfb = int((time.perf_counter() - t0) * 1000)
            if not page.hops:
                page.ttfb_ms = ttfb
            loc = r.headers.get("location") if r.status_code in (301, 302, 303, 307, 308) else None
            page.hops.append(Hop(url=current, status=r.status_code, location=loc))

            if loc:
                r.close()
                nxt = urllib.parse.urljoin(current, loc)
                if nxt == current:
                    page.error = "redirect loop: location points at itself"
                    break
                current = nxt
                continue

            page.url = current
            page.status = r.status_code
            page.headers = {k.lower(): v for k, v in r.headers.items()}
            page.media_type, page.charset = _parse_content_type(r.headers.get("content-type", ""))
            raw = b""
            for chunk in r.iter_content(65536):
                raw += chunk
                if len(raw) > MAX_BYTES:
                    break
            r.close()
            page.size_bytes = len(raw)
            # Encoding comes from the header, then from a <meta charset> in the bytes we already
            # hold. requests' own `apparent_encoding` cannot be used here: it re-reads `r.content`,
            # which raises once the body has been consumed by iter_content.
            enc = page.charset or _sniff_charset(raw) or "utf-8"
            try:
                page.html = raw.decode(enc, errors="replace")
            except LookupError:
                page.html = raw.decode("utf-8", errors="replace")
            if not page.charset:
                page.charset = enc
            page.ok = 200 <= r.status_code < 300
            break
        else:
            page.error = f"too many redirects (>{max_redirects})"
    except SsrfError as e:
        page.blocked_reason = str(e)
        page.error = f"blocked: {e}"
    except FetchError as e:
        page.error = str(e)
    finally:
        session.close()
        page.total_ms = int((time.perf_counter() - t_start) * 1000)

    if render and page.ok and page.is_html:
        page.rendered_html, page.rendered = _render(page.url, timeout)
    return page


_LAST_RENDER_ERROR: str = ""


def render_error() -> str:
    """Why the last render failed. Kept so a caller can report the cause instead of a bare False."""
    return _LAST_RENDER_ERROR


def _render_blocking(url: str, timeout: int) -> tuple[str, bool]:
    global _LAST_RENDER_ERROR
    try:
        from scrapling.fetchers import StealthyFetcher
    except Exception as e:  # noqa: BLE001
        _LAST_RENDER_ERROR = f"the browser engine is not installed: {type(e).__name__}"
        return "", False
    try:
        page = StealthyFetcher.fetch(
            url, timeout=max(timeout, 45) * 1000, headless=True, network_idle=True,
            block_images=True, disable_resources=True,
        )
        _LAST_RENDER_ERROR = ""
        return (getattr(page, "html_content", "") or ""), True
    except TypeError:
        # Older Scrapling signatures reject the resource-blocking kwargs. Correctness first: render
        # without them rather than silently returning no rendered DOM, which would make page.asai
        # report "nothing is JS-only" for every JS-only site.
        try:
            page = StealthyFetcher.fetch(url, timeout=max(timeout, 45) * 1000, headless=True)
            _LAST_RENDER_ERROR = ""
            return (getattr(page, "html_content", "") or ""), True
        except Exception as e:  # noqa: BLE001
            _LAST_RENDER_ERROR = f"{type(e).__name__}: {str(e)[:200]}"
            return "", False
    except Exception as e:  # noqa: BLE001
        _LAST_RENDER_ERROR = f"{type(e).__name__}: {str(e)[:200]}"
        return "", False


def _render(url: str, timeout: int) -> tuple[str, bool]:
    """Render with a real browser so JavaScript-injected content is visible.

    Images, fonts and media are not needed to read a DOM, and blocking them is most of the cost of a
    page load. This is the one idea worth taking from Lightpanda, which is AGPL and therefore cannot
    be linked into this service: a browser driven by an agent does not need to paint.

    The render always runs on a worker thread, and that is not an optimisation. Playwright's
    synchronous API refuses to start when an asyncio loop is already running in the calling thread —
    which is exactly the situation inside an ASGI request handler. Called directly from the server
    it raised every time, was swallowed by the exception handler below, and `page.asai` reported
    "could not render" for every URL in production while working perfectly from a script. A worker
    thread has no running loop, so the sync API is legal there.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        try:
            return pool.submit(_render_blocking, url, timeout).result(
                timeout=max(timeout, 45) + 30)
        except concurrent.futures.TimeoutError:
            global _LAST_RENDER_ERROR
            _LAST_RENDER_ERROR = f"the render did not finish within {max(timeout, 45) + 30}s"
            return "", False


def fetch_as(url: str, crawler: str, *, timeout: int = 15) -> Page:
    """Fetch presenting a named AI crawler's user-agent, to see how the site treats it."""
    ua = AI_CRAWLERS.get(crawler)
    if ua is None:
        raise FetchError(f"unknown crawler {crawler!r}; known: {', '.join(sorted(AI_CRAWLERS))}")
    return fetch(url, user_agent=ua, timeout=timeout, render=False)


def head(url: str, *, timeout: int = 12, user_agent: str = UA,
         max_redirects: int = 5) -> dict[str, Any]:
    """Cheap existence/type probe for links and images — no body downloaded.

    Redirects are followed by hand, guarding every hop, exactly as `fetch` does. Letting requests
    follow them with `allow_redirects=True` would guard only the URL we were given: a link on an
    audited page could then redirect to `http://169.254.169.254/` and we would dutifully fetch the
    cloud metadata service. The link came off a page we do not control, so each hop is fresh
    untrusted input.
    """
    guard_url(url)
    current = url
    hops = 0
    try:
        while True:
            r = requests.head(current, headers={"User-Agent": user_agent}, timeout=timeout,
                              allow_redirects=False)
            if r.status_code in (403, 405, 501):
                # Some servers refuse HEAD outright. A refusal of the METHOD is not a broken link, so
                # fall back to a ranged GET rather than reporting a false 4xx.
                r = requests.get(current, headers={"User-Agent": user_agent,
                                                   "Range": "bytes=0-2047"},
                                 timeout=timeout, allow_redirects=False, stream=True)
                r.close()
            if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
                location = r.headers.get("location")
                if not location or hops >= max_redirects:
                    break
                current = urllib.parse.urljoin(current, location)
                guard_url(current)
                hops += 1
                continue
            break
        return {"status": r.status_code, "url": current,
                "content_type": _parse_content_type(r.headers.get("content-type", ""))[0],
                "content_length": int(r.headers.get("content-length") or 0),
                "redirects": hops,
                "redirected": current.rstrip("/") != url.rstrip("/")}
    except SsrfError as e:
        return {"status": 0, "url": current, "error": f"refused: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"status": 0, "url": current, "error": f"{type(e).__name__}: {str(e)[:120]}"}
