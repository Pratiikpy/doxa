"""Is the answer actually *right*?

A 200 with a signed receipt proves the service ran. It does not prove the answer is correct. This
script takes the deliverables captured by `paid_sweep.py` and checks them against facts established
independently — python.org fetched directly, its robots.txt read, its markup counted — so a wrong
answer fails here rather than being discovered by a reviewer.

Each expectation below states what must be true and why. Where a service cannot be checked against an
external fact, it is checked for internal consistency instead: totals matching their lists, rates
matching their samples, evidence present for every claim.

Run: python scripts/audit_outcomes.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DELIVERABLES = ROOT / ".deliverables.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}
PAGE = "https://www.python.org/"


def ground_truth() -> dict:
    """Facts about python.org, established here rather than taken from the deliverable."""
    r = requests.get(PAGE, headers=UA, timeout=45)
    s = BeautifulSoup(r.text, "lxml")
    desc = next((m.get("content") for m in s.find_all("meta")
                 if (m.get("name") or "").lower() == "description"), "") or ""
    canonical = s.find("link", rel=lambda v: v and "canonical" in
                       (v if isinstance(v, list) else [v]))
    robots = requests.get(PAGE + "robots.txt", headers=UA, timeout=30).text
    llms = requests.get(PAGE + "llms.txt", headers=UA, timeout=25)
    return {
        "title": s.title.get_text().strip(),
        "title_len": len(s.title.get_text().strip()),
        "desc_len": len(desc),
        "has_canonical": canonical is not None,
        "lang": (s.html.get("lang") if s.html else "") or "",
        "h1": len(s.find_all("h1")),
        "images": len(s.find_all("img")),
        "images_without_alt": sum(1 for i in s.find_all("img") if not i.has_attr("alt")),
        "links": len(s.find_all("a", href=True)),
        "jsonld": len(s.find_all("script", type=lambda v: v and "ld+json" in v.lower())),
        "has_viewport": bool([m for m in s.find_all("meta")
                              if (m.get("name") or "") == "viewport"]),
        "robots_mentions_ai": any(b in robots for b in ("GPTBot", "ClaudeBot", "PerplexityBot")),
        "llms_txt_exists": llms.status_code == 200,
    }


def codes(d: dict) -> set[str]:
    return {f["code"] for f in (d.get("findings") or [])}


def check(name: str, ok: bool, detail: str = "") -> tuple[str, bool, str]:
    return name, bool(ok), detail


def audit(deliverables: dict, gt: dict) -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []
    got = deliverables.get

    # --- page.audit, against the markup we counted ourselves -----------------------------------
    d = got("page.audit") or {}
    c = codes(d)
    out.append(check("page.audit reports the real HTTP status",
                     d.get("page", {}).get("status") == 200))
    out.append(check("title 21 chars is NOT called short (threshold is <20)",
                     "title.short" not in c, f"title is {gt['title_len']} chars"))
    out.append(check(f"description of {gt['desc_len']} chars IS called short (<80)",
                     "description.short" in c))
    out.append(check("missing canonical is reported",
                     (not gt["has_canonical"]) == any(x.startswith("canonical.") for x in c)
                     or "canonical.missing" in c,
                     f"page has canonical: {gt['has_canonical']}"))
    out.append(check(f"{gt['h1']} H1s reported as multiple", "h1.multiple" in c))
    out.append(check("declared lang means no language fault",
                     not any(x.startswith("language.") for x in c) or not gt["lang"]))
    out.append(check("viewport present means no viewport fault",
                     ("viewport.missing" in c) != gt["has_viewport"]))
    out.append(check(f"{gt['links']} links (>100) reported as too many", "links.too_many" in c))
    out.append(check("images all have alt, so no missing-alt fault",
                     ("images.no_alt" in c) == (gt["images_without_alt"] > 0)))
    out.append(check("every critical/high finding carries evidence",
                     all(f.get("detail") is not None for f in d.get("findings", [])
                         if f["severity"] in ("critical", "high"))))

    # --- robots.check, against the real robots.txt ----------------------------------------------
    d = got("robots.check") or {}
    verdicts = {v["agent"]: v for v in d.get("verdicts", [])}
    out.append(check("every AI agent gets a verdict and a stated rule",
                     bool(verdicts) and all(v.get("rule") for v in verdicts.values())))
    out.append(check("robots.txt does not restrict AI bots, so all are allowed",
                     all(v["allowed"] for v in verdicts.values()) != gt["robots_mentions_ai"],
                     f"robots.txt mentions AI bots: {gt['robots_mentions_ai']}"))

    # --- llms.check, against the real 404 --------------------------------------------------------
    d = got("llms.check") or {}
    out.append(check("llms.txt absence matches reality",
                     ("llms.missing" in codes(d)) != gt["llms_txt_exists"]))

    # --- schema.validate, against the JSON-LD we counted ----------------------------------------
    d = got("schema.validate") or {}
    out.append(check("JSON-LD block count matches the page",
                     (d.get("nodes", 0) > 0) == (gt["jsonld"] > 0),
                     f"page has {gt['jsonld']} ld+json blocks, service saw {d.get('nodes')} nodes"))
    out.append(check("declared types are listed when nodes exist",
                     bool(d.get("types")) == (d.get("nodes", 0) > 0)))

    # --- page.links: totals must agree with the lists -------------------------------------------
    d = got("page.links") or {}
    r = d.get("result", {})
    classified = r.get("ok", 0) + len(r.get("broken", [])) + len(r.get("redirecting", [])) \
        + len(r.get("blocked", []))
    out.append(check("every checked link is classified exactly once",
                     classified == r.get("checked", -1),
                     f"{classified} classified of {r.get('checked')}"))
    out.append(check("truncation is disclosed when the cap bites",
                     (not r.get("truncated")) or
                     any(f["code"] == "links.truncated" for f in d.get("findings", []))))

    # --- page.asai: the differentiator, on a known client-rendered page --------------------------
    d = got("page.asai") or {}
    asai = next((f for f in d.get("findings", []) if f["code"].startswith("asai.")), None)
    out.append(check("TodoMVC (client-rendered) is identified as JS-dependent",
                     asai is not None and asai["code"] in ("asai.js_required", "asai.mostly_js"),
                     asai["code"] if asai else "no asai finding"))
    out.append(check("the JS share is derived from both documents",
                     asai is not None and asai["detail"]["rendered_words"] >
                     asai["detail"]["raw_words"],
                     f"raw={asai['detail']['raw_words']} rendered={asai['detail']['rendered_words']}"
                     if asai else ""))

    # --- geo.score: the number must equal its own breakdown -------------------------------------
    d = got("geo.score") or {}
    sigs = d.get("signals", [])
    cats = d.get("categories", {})
    out.append(check("score equals the sum of its signals",
                     d.get("score") == min(100, sum(s["earned"] for s in sigs))))
    out.append(check("the rubric weights total exactly 100",
                     sum(c["max"] for c in cats.values()) == 100,
                     str(sum(c["max"] for c in cats.values()))))
    out.append(check("no signal exceeds its own maximum",
                     all(0 <= s["earned"] <= s["max"] for s in sigs)))
    out.append(check("every gap appears in the fix list",
                     len(d.get("fix_order", [])) ==
                     sum(1 for s in sigs if s["earned"] < s["max"])))

    # --- page.chunk: offsets must be real --------------------------------------------------------
    d = got("page.chunk") or {}
    spans = d.get("spans", [])
    out.append(check("span lengths match their offsets",
                     all(s["end"] - s["start"] == len(s["text"]) for s in spans)))
    out.append(check("no chunk is navigation chrome",
                     not any("Skip to" in s["text"][:40] for s in spans)))

    # --- ai.visibility: every claimed mention must be evidenced -----------------------------------
    d = got("ai.visibility") or {}
    runs = [r for s in d.get("by_prompt", []) for r in s["runs"]]
    out.append(check("every reported mention quotes the sentence that made it",
                     all(r.get("evidence", {}).get("context") for r in runs
                         if r.get("asked") and r.get("mentioned"))))
    out.append(check("the prompts never name the brand",
                     all(d.get("brand", "").lower() not in p.lower()
                         for p in d.get("prompts_asked", []))))
    out.append(check("mention rate matches the runs",
                     all(s["mention_rate"] is None or
                         abs(s["mention_rate"] - s["mentioned"] / s["asked"]) < 1e-6
                         for s in d.get("by_prompt", []))))

    # --- content.charts: nothing may be published that the page does not say ---------------------
    d = got("content.charts") or {}
    page_text = requests.get(PAGE, headers=UA, timeout=45).text
    import re as _re
    plain = _re.sub(r"[^a-z0-9]+", " ", BeautifulSoup(page_text, "lxml").get_text(" ").lower())
    unverifiable = [f for f in d.get("figures", [])
                    if _re.sub(r"[^a-z0-9]+", " ", str(f.get("value", "")).lower()).strip()
                    not in plain]
    out.append(check("every published figure appears in the page",
                     not unverifiable,
                     f"{len(unverifiable)} unverifiable: {[f.get('value') for f in unverifiable][:3]}"))
    out.append(check("the JSON-LD matches the figure list",
                     (d.get("dataset_jsonld") is None and not d.get("figures"))
                     or (d.get("dataset_jsonld") is not None and
                         len(d["dataset_jsonld"]["variableMeasured"]) == len(d["figures"]))))

    # --- content.audit: a quote must be real ------------------------------------------------------
    d = got("content.audit") or {}
    out.append(check("any reported quote was verified against the page",
                     d.get("answer_quote") is None or d.get("answer_quote_verified") is True))
    out.append(check("'answered' is only claimed with a verified quote",
                     (not d.get("answered")) or bool(d.get("answer_quote"))))

    # --- site.audit: a partial crawl must not present as complete ---------------------------------
    d = got("site.audit") or {}
    cov = d.get("coverage", {})
    out.append(check("coverage agrees with the crawl summary",
                     cov.get("complete") == (not d.get("crawl", {}).get("truncated"))))
    out.append(check("crawl did not exceed the page budget it was given",
                     cov.get("pages_crawled", 0) <= 8, f"{cov.get('pages_crawled')} of 8 requested"))

    # --- kw.*: counts must agree with their lists --------------------------------------------------
    d = got("kw.discover") or {}
    out.append(check("intent counts sum to the keyword total",
                     sum(d.get("by_intent", {}).values()) == d.get("total", -1)))
    out.append(check("no keyword carries an invented search volume",
                     all("volume" not in k for k in d.get("keywords", []))))
    d = got("kw.demand") or {}
    usable = {k: v for k, v in (d.get("components") or {}).items() if v.get("score") is not None}
    if usable:
        expected = round(100 * sum(v["score"] * v["weight"] for v in usable.values())
                         / sum(v["weight"] for v in usable.values()), 1)
        out.append(check("demand index is recomputable from its components",
                         abs(d.get("demand_index", -1) - expected) < 0.15))
    out.append(check("demand index disclaims search volume",
                     "not a monthly search volume" in (d.get("caveat") or "")))

    # --- links.inbound: every citation must be openable ---------------------------------------------
    d = got("links.inbound") or {}
    out.append(check("every citation is a real URL",
                     all(c.get("url", "").startswith("http") for c in d.get("citations", []))))
    out.append(check("citation totals match the list",
                     d.get("total") == len(d.get("citations", []))))
    out.append(check("completeness is disclaimed",
                     "not a complete backlink profile" in (d.get("caveat") or "")))

    # --- seo.engagement: the quote must be justified -------------------------------------------------
    d = got("seo.engagement") or {}
    phases = d.get("phases", [])
    out.append(check("every phase cites the findings that justify it",
                     bool(phases) and all(p["evidence_codes"] for p in phases)))
    out.append(check("the estimate equals the sum of its phases",
                     abs(d.get("estimate", {}).get("one_measurement_pass_usdt", -1)
                         - sum(p["cost_per_pass_usdt"] for p in phases)) < 1e-6))
    out.append(check("no finding is silently dropped from the plan",
                     isinstance(d.get("unaddressed_signals"), list)))

    # --- badge / report: the artifact must be real ----------------------------------------------------
    d = got("badge") or {}
    out.append(check("badge is valid SVG showing the measured score",
                     str(d.get("svg", "")).startswith("<svg")
                     and f"{d.get('score')}/100" in d.get("svg", "")))
    d = got("report.pdf") or {}
    art = d.get("artifact") or {}
    out.append(check("PDF is content-addressed and non-trivial",
                     str(art.get("sha256", "")).startswith("sha256:") and art.get("bytes", 0) > 1000))

    # --- every service: no raw exception text anywhere in any deliverable -------------------------
    blob = json.dumps(deliverables)
    for forbidden in ("Traceback (most recent call last)", "object at 0x", "urllib3.connection"):
        out.append(check(f"no {forbidden!r} anywhere in any deliverable", forbidden not in blob))
    return out


def main() -> int:
    if not DELIVERABLES.exists():
        print(f"{DELIVERABLES.name} not found — run scripts/paid_sweep.py first")
        return 1
    deliverables = json.loads(DELIVERABLES.read_text(encoding="utf-8"))
    delivered = {k: v for k, v in deliverables.items() if v}
    print(f"Auditing {len(delivered)} deliverables against independently established facts.\n")

    gt = ground_truth()
    print("ground truth (python.org, fetched here):")
    print("  " + json.dumps(gt) + "\n")

    results = audit(deliverables, gt)
    failed = [r for r in results if not r[1]]
    for name, ok, detail in results:
        if not ok:
            print(f"  [FAIL] {name}" + (f"  — {detail}" if detail else ""))
    print(f"\n{len(results) - len(failed)}/{len(results)} outcome checks passed")
    if failed:
        print("\nfailures above are content defects, not endpoint failures")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
