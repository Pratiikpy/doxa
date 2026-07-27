"""Is this the page, or is it the bouncer?

A bot-mitigation product does not usually return a clean error. It returns an HTML document — often
with HTTP 200 — that says "verifying your browser", and every audit tool that does not recognise it
proceeds to audit the interstitial. The result is a confident report about a document the customer has
never seen: no meta description, one link, thin content, no schema. Every finding is true about the
challenge page and worthless about the site.

This was not hypothetical. While testing, Vercel began challenging our fetcher on react.dev, and the
link service duly reported that react.dev has exactly one link — a link to
`vercel.link/security-checkpoint`. It was a signed, paid, entirely wrong answer.

So detection happens before any check runs, and the signatures below are the response headers and
document markers each vendor actually emits. Where a vendor is identified by a header, that is
preferred: headers are not translated, themed or A/B tested the way challenge copy is.

For most services a challenge means the audit cannot be performed and must say so. For `page.blocked`
it is the answer the customer is buying.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from fetch import Page

# (vendor, header name, value substring). A header match is conclusive.
HEADER_SIGNATURES: tuple[tuple[str, str, str], ...] = (
    ("Vercel", "x-vercel-mitigated", "challenge"),
    ("Cloudflare", "cf-mitigated", "challenge"),
    ("Imperva Incapsula", "x-iinfo", ""),
    ("DataDome", "x-datadome", ""),
    ("DataDome", "x-dd-b", ""),
    ("AWS WAF", "x-amzn-waf-action", ""),
    ("Akamai", "x-akamai-request-id", "__nope__"),   # present on all Akamai traffic; never conclusive
)

# (vendor, regex over the document). Used when no header identifies the vendor.
BODY_SIGNATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Vercel", re.compile(r"Vercel Security Checkpoint", re.I)),
    ("Cloudflare", re.compile(r"<title>\s*Just a moment", re.I)),
    ("Cloudflare", re.compile(r"Attention Required!\s*\|\s*Cloudflare", re.I)),
    ("Cloudflare", re.compile(r"cf-browser-verification|cf_chl_opt|challenge-platform", re.I)),
    ("Cloudflare", re.compile(r"Checking your browser before accessing", re.I)),
    ("Imperva Incapsula", re.compile(r"Incapsula incident ID|_Incapsula_Resource", re.I)),
    ("DataDome", re.compile(r"datadome|dd_?cookie|geo\.captcha-delivery\.com", re.I)),
    ("PerimeterX / HUMAN", re.compile(r"px-captcha|_pxhd|perimeterx", re.I)),
    ("Akamai", re.compile(r"Access Denied.{0,400}Reference\s*#\d|ak_bmsc|_abck", re.I | re.S)),
    ("Sucuri", re.compile(r"Sucuri WebSite Firewall|sucuri_cloudproxy", re.I)),
    ("AWS WAF", re.compile(r"Request blocked\.\s*We can't connect to the server", re.I)),
    ("Kasada", re.compile(r"kpsdk|kasada", re.I)),
    ("Queue-it", re.compile(r"queue-it\.net", re.I)),
    ("generic", re.compile(r"(?:enable javascript|javascript is required)[^<]{0,80}"
                           r"(?:to continue|to view this)", re.I)),
    ("generic", re.compile(r"(?:verifying|checking) (?:that )?you(?:r browser)?\s*(?:are|is)?"
                           r"[^<]{0,40}human", re.I)),
)

# A CAPTCHA is conclusive on its own, whatever the vendor.
CAPTCHA = re.compile(r"g-recaptcha|hcaptcha|turnstile|recaptcha/api\.js|challenges\.cloudflare\.com",
                     re.I)


@dataclass
class Challenge:
    vendor: str
    evidence: str
    status: int
    captcha: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"vendor": self.vendor, "evidence": self.evidence,
                "status": self.status, "captcha": self.captcha}

    @property
    def message(self) -> str:
        return (f"{self.vendor} is challenging automated requests to this URL "
                f"(HTTP {self.status}; {self.evidence}). What came back is the challenge page, not "
                f"the site, so auditing it would describe a document your visitors never see.")


def detect(page: Page) -> Challenge | None:
    """Return the challenge that intercepted this fetch, or None if we got the real page."""
    headers = {k.lower(): (v or "") for k, v in (page.headers or {}).items()}
    for vendor, header, needle in HEADER_SIGNATURES:
        if needle == "__nope__":
            continue
        value = headers.get(header)
        if value is not None and (not needle or needle.lower() in value.lower()):
            return Challenge(vendor, f"response header {header}: {value or 'present'}", page.status)

    html = page.html or ""
    if not html:
        return None
    # Only the head and the first stretch of body: a challenge page is small, and a real article that
    # merely mentions Cloudflare must not be mistaken for one.
    head = html[:20000]
    captcha = bool(CAPTCHA.search(head))

    for vendor, rx in BODY_SIGNATURES:
        m = rx.search(head)
        if not m:
            continue
        # A real page that discusses these products is long and rich; an interstitial is neither.
        if len(html) > 120_000:
            continue
        if vendor == "generic" and page.ok and len(html) > 40_000:
            continue
        return Challenge(vendor if vendor != "generic" else "an anti-bot service",
                         f"document matched {m.group(0)[:70]!r}", page.status, captcha)

    if captcha and not page.ok:
        return Challenge("an anti-bot service", "a CAPTCHA widget was served", page.status, True)
    return None


def looks_auditable(page: Page) -> tuple[bool, str]:
    """Can findings about this document honestly be attributed to the customer's page?

    Kept separate from `detect` because there is a second way to audit the wrong thing: a plain error
    page. A 404 or a 502 is a real answer about the URL, but running twenty content checks against the
    error document and reporting "no meta description" is noise at best and misleading at worst.
    """
    ch = detect(page)
    if ch:
        return False, ch.message
    if page.status and not page.ok:
        return False, (f"The URL returned HTTP {page.status}, so there is no page to audit. That "
                       f"status is itself the finding.")
    # No status at all means nothing was ever returned — DNS did not resolve, the connection was
    # refused, or the fetch was blocked. Falling through to the content-type branch reported that as
    # "returned an unknown content type rather than HTML", which describes a response that never
    # happened and sends the customer to check a header instead of their hostname. The real reason
    # was already on the page object; it simply was not read.
    if not page.status:
        why = page.blocked_reason or page.error
        return False, (f"The URL could not be fetched, so there is no page to audit: "
                       f"{why or 'no response was received'}.")
    if not page.is_html:
        return False, (f"The URL returned {page.media_type or 'an unknown content type'} rather than "
                       f"HTML, so the page checks do not apply.")
    return True, ""
