"""The check contract.

A check looks at a fetched `Page` and returns zero or more `Finding`s. Findings carry a stable machine
code, never only prose, because a code can be counted, diffed between two audits, asserted in a test,
and acted on by an agent. Prose alone can do none of those.

Codes and thresholds are ported from SEONaut (MIT, `internal/issues/`), which is the most complete
tested taxonomy of technical SEO failures in the open — 79 codes across 21 single-page and 9 cross-page
reporters. Where a threshold appears below it was read out of their source, not chosen by us:

    title           short < 20      long > 60
    description     short < 80      long > 160
    content         thin  < 200 words
    depth           > 4 clicks from home
    ttfb            > 800 ms
    links           > 100 on a page
    alt text        > 100 characters
    image           > 500,000 bytes

Severity follows SEONaut's three levels. It is a judgement about impact, not about certainty: a
CRITICAL finding is one that removes a page from search or loses a visitor, not one we are merely
confident about.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from fetch import Page


class Severity(str, enum.Enum):
    CRITICAL = "critical"   # the page cannot be indexed, or the visitor is lost
    HIGH = "high"           # ranking or comprehension is materially damaged
    LOW = "low"             # worth fixing, no immediate loss
    INFO = "info"           # observation, explicitly not a fault


@dataclass
class Finding:
    code: str                       # stable machine identifier, e.g. "title.empty"
    severity: Severity
    message: str                    # one plain sentence a human can act on
    detail: dict[str, Any] = field(default_factory=dict)   # the evidence behind the claim

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity.value,
                "message": self.message, "detail": self.detail}


@dataclass
class Check:
    """One named check. `applies` decides relevance; `run` produces findings."""
    code_prefix: str
    title: str
    run: Callable[[Page], list[Finding]]
    applies: Callable[[Page], bool] = lambda p: True
    group: str = "page"

    def __call__(self, page: Page) -> list[Finding]:
        if not self.applies(page):
            return []
        return self.run(page) or []


# --- guards, matching SEONaut's reporter preconditions exactly ------------------------------------

def is_html_200(page: Page) -> bool:
    """Most content checks only make sense on an HTML page that actually returned successfully.

    SEONaut guards every content reporter with `Crawled && MediaType == text/html && 200 <= status <
    300`. Without this a 404 page reports a missing title, a missing H1 and thin content — three
    findings that are noise, when the single real finding is that the page 404s.
    """
    return page.ok and page.is_html and not page.error


def is_html(page: Page) -> bool:
    return page.is_html and not page.error


class CheckRegistry:
    """Checks register themselves, so adding one never touches the orchestrator.

    Duplicate prefixes are a hard error rather than a warning: two checks emitting the same code would
    make findings ambiguous, and an ambiguous code cannot be diffed or asserted.
    """

    def __init__(self) -> None:
        self._checks: dict[str, Check] = {}

    def add(self, check: Check) -> None:
        if check.code_prefix in self._checks:
            raise ValueError(f"duplicate check prefix {check.code_prefix!r}")
        self._checks[check.code_prefix] = check

    def register(self, code_prefix: str, title: str, *, group: str = "page",
                 applies: Callable[[Page], bool] = is_html_200):
        def deco(fn: Callable[[Page], list[Finding]]):
            self.add(Check(code_prefix=code_prefix, title=title, run=fn,
                           applies=applies, group=group))
            return fn
        return deco

    def all(self) -> list[Check]:
        return list(self._checks.values())

    def by_group(self, group: str) -> list[Check]:
        return [c for c in self._checks.values() if c.group == group]

    def run(self, page: Page, groups: Iterable[str] | None = None) -> list[Finding]:
        wanted = set(groups) if groups else None
        out: list[Finding] = []
        for c in self._checks.values():
            if wanted and c.group not in wanted:
                continue
            out.extend(c(page))
        order = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.LOW: 2, Severity.INFO: 3}
        out.sort(key=lambda f: (order[f.severity], f.code))
        return out

    def __len__(self) -> int:
        return len(self._checks)


registry = CheckRegistry()
