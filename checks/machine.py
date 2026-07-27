"""What a machine actually sees — the checks nobody else sells.

Three questions a site owner cannot answer by looking at their own page in a browser:

  1. How much of this content only exists after JavaScript runs? Most AI crawlers do not run it.
  2. Does my CDN quietly refuse the AI crawlers while serving humans fine?
  3. Does my robots.txt block them — often written years ago, before these agents existed?

The first is measured by fetching the page twice, once raw and once rendered, and diffing the visible
text. The second by presenting each crawler's real published user-agent and comparing the response to
what a browser gets. The third by parsing robots.txt against each agent's token.

None of this needs a data vendor. All of it is invisible to the person who owns the site.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

from bs4 import BeautifulSoup

from checks.base import Finding, Severity, is_html_200, registry
from fetch import AI_CRAWLERS, Page, fetch, fetch_as, guard_url

_WS = re.compile(r"\s+")


def visible_text(html: str) -> str:
    """The text a reader — human or model — would actually get from this HTML."""
    s = BeautifulSoup(html or "", "lxml")
    for tag in s(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    return _WS.sub(" ", s.get_text(" ")).strip()


def _shingles(text: str, n: int = 8) -> set[str]:
    """Overlapping word n-grams, so a diff measures *content*, not incidental reordering."""
    w = text.split()
    if len(w) < n:
        return {" ".join(w)} if w else set()
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


@registry.register("asai", "Content visible without JavaScript",
                   applies=lambda p: p.ok and p.is_html and bool(p.rendered_html))
def check_js_only_content(page: Page) -> list[Finding]:
    """The single most valuable check in Doxa.

    A site renders fine in the owner's browser, so the owner believes the content is there. Most AI
    crawlers fetch the raw HTML and never execute JavaScript. If the article body is injected client
    side, those crawlers see an empty shell — and the owner has no way to notice.

    The measurement is a shingle diff of visible text, not a length ratio. A length ratio calls a page
    that merely reformats its text "90% JS-only"; shingles compare the actual content.
    """
    raw_text = visible_text(page.html)
    rendered_text = visible_text(page.rendered_html)
    raw_words, rendered_words = len(raw_text.split()), len(rendered_text.split())

    raw_sh, rendered_sh = _shingles(raw_text), _shingles(rendered_text)
    only_rendered = rendered_sh - raw_sh
    share = (len(only_rendered) / len(rendered_sh)) if rendered_sh else 0.0

    detail: dict[str, Any] = {
        "raw_words": raw_words,
        "rendered_words": rendered_words,
        "js_only_share": round(share, 3),
        "sample_js_only": sorted(only_rendered)[:5],
    }

    if rendered_words < 20 and raw_words < 20:
        return [Finding("asai.empty", Severity.HIGH,
                        "The page has almost no visible text either before or after JavaScript runs.",
                        detail)]

    # An almost-empty raw document with a full rendered one is the classic client-side-rendered SPA.
    if raw_words < 50 and rendered_words >= 200:
        return [Finding("asai.js_required", Severity.CRITICAL,
                        f"Without JavaScript this page has only {raw_words} words; with it, "
                        f"{rendered_words}. An AI crawler that does not execute JavaScript sees "
                        f"essentially nothing here.", detail)]
    if share >= 0.5:
        return [Finding("asai.mostly_js", Severity.CRITICAL,
                        f"About {share:.0%} of the readable content appears only after JavaScript "
                        f"runs, so most crawlers never see it.", detail)]
    if share >= 0.2:
        return [Finding("asai.partly_js", Severity.HIGH,
                        f"About {share:.0%} of the readable content appears only after JavaScript "
                        f"runs.", detail)]
    if share > 0.02:
        return [Finding("asai.some_js", Severity.LOW,
                        f"A small part of the content ({share:.0%}) is added by JavaScript.", detail)]
    return [Finding("asai.server_rendered", Severity.INFO,
                    "The content is in the HTML itself, so any crawler can read it without running "
                    "JavaScript.", detail)]


def probe_ai_crawlers(url: str, browser_page: Page,
                      crawlers: list[str] | None = None) -> list[Finding]:
    """Present each AI crawler's real user-agent and compare with what a browser received.

    This is an active probe rather than an inference. A CDN rule that challenges GPTBot is invisible
    in robots.txt and invisible to the owner — the only way to know is to knock on the door wearing
    that name.
    """
    from checks.challenge import detect as detect_challenge

    names = crawlers or ["GPTBot", "ClaudeBot", "PerplexityBot", "OAI-SearchBot", "Google-Extended"]
    out: list[Finding] = []
    baseline_challenge = detect_challenge(browser_page)
    baseline_ok = browser_page.ok and not baseline_challenge
    baseline_len = len(visible_text(browser_page.html).split())
    blocked: list[dict[str, Any]] = []
    degraded: list[dict[str, Any]] = []
    unreachable: list[dict[str, Any]] = []

    if not baseline_ok:
        # Without a known-good browser response there is nothing to compare against, and every
        # comparison below would fall through to "all crawlers are treated the same" — which is
        # technically true and reads as a clean bill of health when in fact nothing automated can
        # read the page at all. Say that instead.
        why = (baseline_challenge.message if baseline_challenge
               else f"the page returned HTTP {browser_page.status} to an ordinary browser user-agent")
        return [Finding("aicrawler.baseline_unavailable", Severity.CRITICAL,
                        f"No AI crawler could be tested against a working baseline, because {why} "
                        f"Automated readers of every kind — including the ones that produce citations "
                        f"— are being refused before they reach your content.",
                        {"browser_status": browser_page.status,
                         "challenge": baseline_challenge.as_dict() if baseline_challenge else None,
                         "crawlers_not_tested": names})]

    for name in names:
        try:
            p = fetch_as(url, name, timeout=15)
        except Exception as e:  # noqa: BLE001
            unreachable.append({"crawler": name, "error": type(e).__name__})
            continue
        words = len(visible_text(p.html).split()) if p.is_html else 0
        challenge = detect_challenge(p)
        row = {"crawler": name, "status": p.status, "words": words}
        if challenge:
            # A challenge served with HTTP 200 is still a refusal; status alone would miss it.
            blocked.append(row | {"challenge": challenge.vendor})
        elif p.status in (401, 403, 429) or p.status >= 500:
            blocked.append(row)
        elif baseline_len >= 50 and words < baseline_len * 0.5:
            # Served a 200 but with materially less content — a soft block or a challenge page.
            degraded.append(row)

    if blocked:
        out.append(Finding("aicrawler.blocked", Severity.CRITICAL,
                           f"{len(blocked)} AI crawler(s) are refused by this site while a browser is "
                           f"served normally: {', '.join(b['crawler'] for b in blocked)}. They cannot "
                           f"read the page at all.",
                           {"blocked": blocked, "browser_status": browser_page.status}))
    if degraded:
        out.append(Finding("aicrawler.degraded", Severity.HIGH,
                           f"{len(degraded)} AI crawler(s) get a much smaller page than a browser "
                           f"does, which usually means a bot challenge: "
                           f"{', '.join(d['crawler'] for d in degraded)}.",
                           {"degraded": degraded, "browser_words": baseline_len}))
    if unreachable:
        # Reported as unknown, never folded into the clean result: a crawler we failed to test is not
        # a crawler we proved is welcome.
        out.append(Finding("aicrawler.untested", Severity.LOW,
                           f"{len(unreachable)} crawler(s) could not be tested and are reported as "
                           f"unknown rather than as allowed: "
                           f"{', '.join(u['crawler'] for u in unreachable)}.",
                           {"unreachable": unreachable}))
    tested = [n for n in names if n not in {u["crawler"] for u in unreachable}]
    if not blocked and not degraded and tested:
        out.append(Finding("aicrawler.allowed", Severity.INFO,
                           f"All {len(tested)} AI crawlers tested are served the same page a browser "
                           f"gets.", {"tested": tested}))
    return out


# --- robots.txt ---------------------------------------------------------------------------------

class RobotsRules:
    """A robots.txt parser that answers per-agent, including the AI agents.

    Python's stdlib `urllib.robotparser` is not used: it does not implement the longest-match rule
    that Google and the others actually follow, so it disagrees with real crawler behaviour on any
    file that mixes Allow and Disallow. Since this service signs its answers, "roughly right" is not
    good enough.
    """

    def __init__(self, text: str):
        self.groups: dict[str, list[tuple[str, str]]] = {}
        self.sitemaps: list[str] = []
        self.raw = text or ""
        current: list[str] = []
        for line in self.raw.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, _, value = line.partition(":")
            field, value = field.strip().lower(), value.strip()
            if field == "user-agent":
                if current and self.groups.get(current[-1]):
                    current = []
                current.append(value.lower())
                self.groups.setdefault(value.lower(), [])
            elif field in ("allow", "disallow") and current:
                for ua in current:
                    self.groups.setdefault(ua, []).append((field, value))
            elif field == "sitemap":
                self.sitemaps.append(value)

    def _rules_for(self, agent: str) -> list[tuple[str, str]]:
        a = agent.lower()
        for ua in self.groups:
            if ua and ua != "*" and (ua in a or a in ua):
                return self.groups[ua]
        return self.groups.get("*", [])

    def allowed(self, agent: str, path: str) -> tuple[bool, str]:
        """Longest-match wins; an equal-length Allow beats Disallow, as Google specifies."""
        rules = self._rules_for(agent)
        best: tuple[int, str, str] = (-1, "", "")
        for field, pattern in rules:
            if pattern == "":
                if field == "disallow":
                    continue          # `Disallow:` with no value means allow everything
                continue
            if self._match(pattern, path) and len(pattern) > best[0]:
                best = (len(pattern), field, pattern)
            elif self._match(pattern, path) and len(pattern) == best[0] and field == "allow":
                best = (len(pattern), field, pattern)
        if best[0] < 0:
            return True, "no matching rule"
        return best[1] == "allow", f"{best[1]}: {best[2]}"

    @staticmethod
    def _match(pattern: str, path: str) -> bool:
        if "*" not in pattern and "$" not in pattern:
            return path.startswith(pattern)
        rx = re.escape(pattern).replace(r"\*", ".*")
        if rx.endswith(r"\$"):
            rx = rx[:-2] + "$"
        return re.match(rx, path) is not None


def check_robots_for_ai(url: str, robots_text: str) -> list[Finding]:
    r = RobotsRules(robots_text)
    path = urllib.parse.urlsplit(url).path or "/"
    denied: list[dict[str, str]] = []
    for name in AI_CRAWLERS:
        ok, why = r.allowed(name, path)
        if not ok:
            denied.append({"crawler": name, "rule": why})
    out: list[Finding] = []
    if denied:
        out.append(Finding("robots.ai_disallowed", Severity.CRITICAL,
                           f"robots.txt tells {len(denied)} AI crawler(s) not to fetch this page: "
                           f"{', '.join(d['crawler'] for d in denied)}. If you want to appear in AI "
                           f"answers, this is the first thing to change.",
                           {"denied": denied, "path": path}))
    ok_star, why_star = r.allowed("*", path)
    if not ok_star:
        out.append(Finding("robots.disallowed", Severity.CRITICAL,
                           f"robots.txt disallows this path for all crawlers ({why_star}).",
                           {"rule": why_star, "path": path}))
    if not out:
        out.append(Finding("robots.allowed", Severity.INFO,
                           "robots.txt permits this page for every crawler tested, including the AI "
                           "agents.", {"path": path, "sitemaps": r.sitemaps[:5]}))
    return out


def fetch_robots(site_url: str) -> tuple[str, int]:
    """Fetch robots.txt for a site. A missing file means 'everything is allowed', not an error."""
    p = urllib.parse.urlsplit(site_url)
    robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
    try:
        guard_url(robots_url)
    except Exception:  # noqa: BLE001
        return "", 0
    page = fetch(robots_url, timeout=12)
    return (page.html if page.ok else ""), page.status


# --- llms.txt ------------------------------------------------------------------------------------

def check_llms_txt(site_url: str) -> list[Finding]:
    """`llms.txt` is a young convention: a markdown file at the root telling a model what the site is
    and which pages matter. Absent on most sites; cheap to add; increasingly read."""
    p = urllib.parse.urlsplit(site_url)
    url = f"{p.scheme}://{p.netloc}/llms.txt"
    page = fetch(url, timeout=12)
    if not page.ok:
        return [Finding("llms.missing", Severity.LOW,
                        "There is no /llms.txt. It is a short markdown file that tells a language "
                        "model what this site is and which pages matter.",
                        {"url": url, "status": page.status})]
    text = page.html or ""
    findings: list[Finding] = []
    if not text.lstrip().startswith("#"):
        findings.append(Finding("llms.malformed", Severity.LOW,
                                "/llms.txt exists but does not start with a markdown H1 title, which "
                                "the convention requires.", {"first_line": text[:80]}))
    links = re.findall(r"\]\((https?://[^)]+)\)", text)
    findings.append(Finding("llms.present", Severity.INFO,
                            f"/llms.txt is present ({len(text)} bytes, {len(links)} links).",
                            {"bytes": len(text), "links": len(links)}))
    return findings
