"""Images — for the reader who cannot see them, and the model that cannot either.

Ported from SEONaut `internal/issues/page/images.go`, thresholds verbatim: alt text over 100 runes is
too long, an image over 500,000 bytes is too large, `<img>` without both width and height causes layout
shift, a `<picture>` with no `<img>` fallback renders nothing, and `noimageindex` removes every image
on the page from image search.

One addition SEONaut does not make, because it predates the problem: alt text is now the only thing a
text-only crawler gets from an image. A chart with no alt is invisible to a model, and a model cannot
cite what it cannot read. So a missing alt is reported as a comprehension failure, not only an
accessibility one.
"""
from __future__ import annotations

import concurrent.futures
import urllib.parse
from typing import Any

from checks.base import Finding, Severity, registry
from checks.page_html import soup
from fetch import Page, SsrfError, guard_url, head

MAX_ALT_RUNES = 100          # SEONaut: len([]rune(alt)) > 100
MAX_IMAGE_BYTES = 500_000    # SEONaut: pageReport.Size > 500000

# Formats a modern browser handles far better than the one that is usually shipped.
LEGACY_FORMATS = (".bmp", ".tiff", ".tif")
MODERN_FORMATS = (".webp", ".avif")


def _src(img) -> str:
    """The URL a browser would actually load, preferring srcset's first candidate when there is no src."""
    for attr in ("src", "data-src", "data-lazy-src"):
        v = (img.get(attr) or "").strip()
        if v:
            return v
    srcset = (img.get("srcset") or img.get("data-srcset") or "").strip()
    if srcset:
        return srcset.split(",")[0].strip().split(" ")[0]
    return ""


def collect_images(page: Page) -> list[dict[str, Any]]:
    s = soup(page)
    base = page.url or page.requested_url
    base_tag = s.find("base", href=True)
    if base_tag:
        base = urllib.parse.urljoin(base, base_tag["href"].strip())

    out: list[dict[str, Any]] = []
    for img in s.find_all("img"):
        src = _src(img)
        url = urllib.parse.urljoin(base, src) if src else ""
        # `alt` absent and `alt=""` are different: an empty alt is the correct, deliberate markup for
        # a decorative image. Conflating them would report every spacer graphic as a fault.
        has_alt = img.has_attr("alt")
        alt = (img.get("alt") or "").strip()
        out.append({
            "src": src, "url": url, "has_alt": has_alt, "alt": alt,
            "width": (img.get("width") or "").strip(),
            "height": (img.get("height") or "").strip(),
            "loading": (img.get("loading") or "").strip().lower(),
            "in_picture": img.find_parent("picture") is not None,
        })
    return out


@registry.register("images", "Images")
def check_images(page: Page) -> list[Finding]:
    imgs = collect_images(page)
    s = soup(page)
    out: list[Finding] = []

    # Checked before the no-images shortcut below. A <picture> whose <img> fallback is missing
    # contains no <img> by definition, so bailing out on an empty image list would skip exactly the
    # page this check exists to catch.
    empty_picture = [p for p in s.find_all("picture") if not p.find("img")]
    if empty_picture:
        out.append(Finding("images.picture_without_img", Severity.HIGH,
                           f"{len(empty_picture)} <picture> element(s) contain no <img>. Without the "
                           f"fallback the browser renders nothing at all.",
                           {"count": len(empty_picture)}))
    if not imgs:
        return out

    missing_alt = [i for i in imgs if not i["has_alt"]]
    if missing_alt:
        out.append(Finding("images.no_alt", Severity.HIGH,
                           f"{len(missing_alt)} of {len(imgs)} images have no alt attribute. A screen "
                           f"reader and a language model both get nothing from these — whatever the "
                           f"image says is invisible to them.",
                           {"count": len(missing_alt), "total": len(imgs),
                            "sample": [i["src"][:120] for i in missing_alt[:10]]}))

    long_alt = [i for i in imgs if len(i["alt"]) > MAX_ALT_RUNES]
    if long_alt:
        out.append(Finding("images.long_alt", Severity.LOW,
                           f"{len(long_alt)} image(s) have alt text over {MAX_ALT_RUNES} characters. "
                           f"Alt text is a label, not a caption — put the long version in the page.",
                           {"count": len(long_alt),
                            "sample": [{"src": i["src"][:80], "length": len(i["alt"])}
                                       for i in long_alt[:10]]}))

    no_size = [i for i in imgs if not i["width"] or not i["height"]]
    if no_size:
        out.append(Finding("images.no_dimensions", Severity.LOW,
                           f"{len(no_size)} image(s) have no width and height attributes, so the page "
                           f"jumps as they load. That shift is measured by Core Web Vitals as CLS.",
                           {"count": len(no_size),
                            "sample": [i["src"][:120] for i in no_size[:10]]}))

    robots = " ".join((m.get("content") or "").lower() for m in s.find_all("meta")
                      if (m.get("name") or "").lower() in ("robots", "googlebot"))
    if "noimageindex" in robots or "noimageindex" in (page.headers.get("x-robots-tag", "")).lower():
        out.append(Finding("images.noimageindex", Severity.HIGH,
                           "The page sets noimageindex, so none of its images can appear in image "
                           "search.", {"images": len(imgs)}))

    legacy = [i for i in imgs if i["url"].lower().split("?")[0].endswith(LEGACY_FORMATS)]
    if legacy:
        out.append(Finding("images.legacy_format", Severity.LOW,
                           f"{len(legacy)} image(s) use an outdated format. WebP or AVIF is typically "
                           f"a third of the bytes for the same quality.",
                           {"sample": [i["src"][:120] for i in legacy[:10]]}))

    # Lazy-loading the first image delays the largest paint rather than helping it.
    if imgs and imgs[0]["loading"] == "lazy":
        out.append(Finding("images.lazy_first", Severity.LOW,
                           "The first image on the page is lazy-loaded, which usually delays the "
                           "largest contentful paint rather than improving it.",
                           {"src": imgs[0]["src"][:120]}))
    return out


def weigh_images(page: Page, *, limit: int = 40, workers: int = 8,
                 timeout: int = 12) -> list[Finding]:
    """Fetch each image's headers to find the heavy ones. Paid, bounded, and SSRF-guarded like links."""
    imgs = [i for i in collect_images(page) if i["url"].startswith(("http://", "https://"))]
    unique: dict[str, dict] = {}
    for i in imgs:
        unique.setdefault(i["url"].split("#")[0], i)
    targets = list(unique.values())
    truncated = max(0, len(targets) - limit)
    targets = targets[:limit]

    def weigh(i: dict) -> dict:
        try:
            guard_url(i["url"])
        except (SsrfError, Exception):  # noqa: B014
            return i | {"bytes": 0, "error": "refused"}
        try:
            r = head(i["url"], timeout=timeout)
            return i | {"bytes": int(r.get("content_length") or 0), "status": int(r.get("status") or 0),
                        "type": r.get("content_type", "")}
        except Exception as e:  # noqa: BLE001
            return i | {"bytes": 0, "error": type(e).__name__}

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        weighed = list(pool.map(weigh, targets))

    out: list[Finding] = []
    heavy = sorted([w for w in weighed if w.get("bytes", 0) > MAX_IMAGE_BYTES],
                   key=lambda w: -w["bytes"])
    if heavy:
        total = sum(w["bytes"] for w in heavy)
        out.append(Finding("images.too_large", Severity.HIGH,
                           f"{len(heavy)} image(s) are over {MAX_IMAGE_BYTES // 1000} kB — "
                           f"{total // 1000} kB in total. On a phone connection this is the whole "
                           f"reason the page feels slow.",
                           {"images": [{"src": w["src"][:120], "kb": w["bytes"] // 1000}
                                       for w in heavy[:15]], "total_kb": total // 1000}))
    broken = [w for w in weighed if w.get("status", 200) >= 400]
    if broken:
        out.append(Finding("images.broken", Severity.HIGH,
                           f"{len(broken)} image(s) do not load at all.",
                           {"images": [{"src": w["src"][:120], "status": w.get("status")}
                                       for w in broken[:15]]}))
    if truncated:
        out.append(Finding("images.truncated", Severity.INFO,
                           f"Only the first {limit} unique images were weighed; {truncated} were not.",
                           {"weighed": limit, "skipped": truncated}))
    return out
