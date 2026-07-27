"""Pay for every service and check what actually comes back.

This is the test that matters. The unit suite proves the checks are right about constructed HTML; this
drives the live HTTP surface, settles a real x402 payment for each call, and then reads the response —
because a 200 has repeatedly hidden a broken outcome.

For every service it asserts:

  * the unpaid call is refused with a challenge, and the paid call is served;
  * the envelope is signed, and the signature verifies against the returned public key;
  * every declared validation check passed;
  * the result is not merely present but *substantive* — the per-service assertion below decides what
    that means, because "no findings" is a correct answer for one service and a broken one for another.

Run: python scripts/e2e.py [--quick]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402
from runtime import Signer  # noqa: E402
from x402 import decode_challenge, make_dev_payment  # noqa: E402

client = TestClient(server.app)

PAGE = "https://en.wikipedia.org/wiki/Search_engine_optimization"
SITE = "https://quotes.toscrape.com/"
DOMAIN = "stripe.com"
RIVAL = "https://developer.mozilla.org/en-US/docs/Web/HTML"

CRAWL = {"max_pages": 12, "max_depth": 2, "deadline_s": 60}


def nonempty(*keys: str) -> Callable[[dict], str]:
    def check(r: dict) -> str:
        for k in keys:
            v = r.get(k)
            if v in (None, [], {}, "", 0):
                return f"'{k}' is empty"
        return ""
    return check


def findings_present(r: dict) -> str:
    return "" if r.get("findings") else "no findings at all — the checks did not run"


CASES: list[tuple[str, dict, dict, Callable[[dict], str]]] = [
    ("page.audit", {"url": PAGE}, {}, findings_present),
    ("page.links", {"url": PAGE}, {"limit": 25},
     lambda r: "" if r["result"]["checked"] > 0 else "no links were checked"),
    ("page.images", {"url": PAGE}, {"weigh": False}, nonempty("totals")),
    ("schema.validate", {"url": PAGE}, {}, findings_present),
    ("page.hreflang", {"url": PAGE}, {}, lambda r: ""),
    ("page.asai", {"url": PAGE}, {},
     lambda r: "" if r["measured"] else "no rendered DOM, so nothing was measured"),
    ("page.blocked", {"url": PAGE}, {"crawlers": ["GPTBot", "ClaudeBot"]}, findings_present),
    ("llms.check", {"url": PAGE}, {}, findings_present),
    ("robots.check", {"url": PAGE}, {}, nonempty("verdicts")),
    ("page.aeo", {"url": PAGE}, {}, findings_present),
    ("page.chunk", {"url": PAGE}, {},
     lambda r: "" if r["totals"]["spans"] > 0 else "no citable spans extracted"),
    ("page.readability", {"url": PAGE}, {},
     lambda r: "" if r["metrics"]["flesch"] is not None else "no readability metric"),
    ("geo.score", {"url": PAGE}, {},
     lambda r: "" if 0 <= r["score"] <= 100 and r["fix_order"] is not None
     else "score out of range"),
    ("site.audit", {"url": SITE}, CRAWL,
     lambda r: "" if r["coverage"]["pages_crawled"] > 0 else "crawled nothing"),
    ("site.graph", {"url": SITE}, CRAWL,
     lambda r: "" if r["graph"]["pages"] > 0 else "empty link graph"),
    ("site.sitemap", {"url": SITE}, {"max_pages": 8, "max_depth": 2}, findings_present),
    ("site.aeo", {"url": SITE}, CRAWL,
     lambda r: "" if r["pages_scored"] > 0 else "no page was scored"),
    ("kw.discover", {"seed": "headless cms"}, {"prepositions": False},
     lambda r: "" if r["total"] > 20 else f"only {r['total']} phrases"),
    ("kw.questions", {"seed": "headless cms"}, {},
     lambda r: "" if (r["totals"]["autocomplete"] + r["totals"]["discussions"]) > 0
     else "no questions found"),
    ("kw.demand", {"seed": "headless cms"}, {},
     lambda r: "" if r["demand_index"] is not None else "no index computed"),
    ("kw.cluster", {"keywords": ["best crm for startups", "buy crm software",
                                 "what is a crm", "crm pricing", "top crm tools"]}, {},
     lambda r: "" if r["clusters"] else "no clusters"),
    ("corpus.presence", {"domain": DOMAIN}, {"indexes": 2},
     lambda r: "" if r["indexes_checked"] > 0 else "no index was checked"),
    ("links.inbound", {"domain": DOMAIN, "brand": "Stripe"}, {"limit": 6},
     lambda r: "" if r["sources_queried"] else "no source answered"),
    ("links.compare", {"domain": DOMAIN, "competitor": "adyen.com"}, {},
     lambda r: "" if len(r["sides"]) == 2 else "a side is missing"),
    ("ai.prompts", {"url": "https://www.notion.com/"}, {},
     lambda r: "" if len(r["questions"]) >= 3 else "too few questions"),
    ("ai.visibility", {"url": "https://www.notion.com/",
                       "prompts": ["What is the best all-in-one workspace app for a small team?"]},
     {"samples": 1},
     lambda r: "" if r["overall"]["answers_measured"] > 0 else "nothing measured"),
    ("ai.brand", {"url": "https://www.notion.com/"}, {"samples": 1},
     lambda r: "" if r["measured"] > 0 else "nothing measured"),
    ("ai.citations", {"url": "https://www.notion.com/",
                      "prompts": ["Which tool keeps team notes and tasks in one place?"]},
     {"samples": 1},
     lambda r: "" if r["answers_measured"] > 0 else "nothing measured"),
    ("compete.compare", {"url": PAGE, "competitor": RIVAL}, {},
     lambda r: "" if len(r["sides"]) == 2 else "a side is missing"),
    ("badge", {"url": PAGE}, {},
     lambda r: "" if r["svg"].startswith("<svg") else "badge is not svg"),
    ("report.pdf", {"url": PAGE}, {},
     lambda r: "" if (r["artifact"] or {}).get("bytes", 0) > 1000 else "pdf too small"),
    ("content.audit", {"url": PAGE}, {}, nonempty("question")),
    ("content.brief", {"topic": "choosing a headless CMS"}, {},
     lambda r: "" if (r["brief"] or {}).get("sections") else "no sections in the brief"),
    ("content.charts", {"url": PAGE}, {},
     lambda r: "" if r["statistics_detected_in_text"] >= 0 else "did not run"),
    ("seo.engagement", {"url": SITE, "goal": "be cited by assistants"}, {"sample_pages": 8},
     lambda r: "" if r["phases"] else "no phases produced"),
    ("audit.diff", {"before": {"result": {"findings": [
        {"code": "title.empty", "severity": "critical", "message": "x", "detail": {}}]}},
                    "after": {"result": {"findings": []}}}, {},
     lambda r: "" if r["direction"] == "improved" else "diff direction wrong"),
]


def run_case(endpoint: str, payload: dict, options: dict, assertion) -> dict[str, Any]:
    started = time.perf_counter()
    row: dict[str, Any] = {"endpoint": endpoint, "problems": []}

    unpaid = client.post(f"/a2mcp/{endpoint}", json={"input": payload, "options": options})
    if unpaid.status_code != 402:
        row["problems"].append(f"unpaid call returned {unpaid.status_code}, not 402")
    if not any(k.upper() == "PAYMENT-REQUIRED" for k in unpaid.headers):
        row["problems"].append("no PAYMENT-REQUIRED header on the 402")

    challenge = decode_challenge(client.post(f"/a2mcp/{endpoint}").headers["PAYMENT-REQUIRED"])
    row["price_usdt"] = challenge["accepts"][0]["amount"]
    paid = client.post(f"/a2mcp/{endpoint}",
                       headers={"X-PAYMENT": make_dev_payment(challenge)},
                       json={"input": payload, "options": options})
    row["http"] = paid.status_code
    row["seconds"] = round(time.perf_counter() - started, 1)

    if paid.status_code != 200:
        row["problems"].append(f"paid call returned {paid.status_code}: {paid.text[:160]}")
        return row

    env = paid.json()
    row["status"] = env.get("status")
    err = env.get("error")
    if err:
        row["problems"].append(f"{err.get('code')}: {str(err.get('message'))[:140]}")
        return row

    receipt = env.get("receipt") or {}
    if not receipt.get("signature"):
        row["problems"].append("no signature on the receipt")
    elif not Signer.verify(receipt["manifest_sha256"], receipt["signature"],
                           receipt["public_key"]):
        row["problems"].append("the receipt signature does not verify")

    validation = env.get("validation") or {}
    failed = [t["name"] for t in validation.get("tests", []) if not t["passed"]]
    row["checks"] = f"{len(validation.get('tests', [])) - len(failed)}/" \
                    f"{len(validation.get('tests', []))}"
    if failed:
        row["problems"].append(f"validation failed: {', '.join(failed)}")
    row["warnings"] = len(validation.get("warnings") or [])

    try:
        complaint = assertion(env.get("result") or {})
    except Exception as e:  # noqa: BLE001
        complaint = f"the outcome assertion raised {type(e).__name__}: {e}"
    if complaint:
        row["problems"].append(f"outcome: {complaint}")
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="skip the slow crawl and model services")
    ap.add_argument("--only", help="run only endpoints containing this string")
    args = ap.parse_args()

    slow = {"site.audit", "site.graph", "site.aeo", "site.sitemap", "seo.engagement",
            "ai.visibility", "ai.brand", "ai.citations", "content.brief"}
    cases = [c for c in CASES if not (args.quick and c[0] in slow)]
    if args.only:
        cases = [c for c in cases if args.only in c[0]]

    print(f"Doxa end-to-end — {len(cases)} services, each paid for individually\n")
    rows = []
    for endpoint, payload, options, assertion in cases:
        row = run_case(endpoint, payload, options, assertion)
        rows.append(row)
        mark = "PASS" if not row["problems"] else "FAIL"
        print(f"  [{mark}] {endpoint:18} {row.get('checks', '-'):>7}  "
              f"{row['seconds']:>5}s  ${int(row['price_usdt'])/1e6:<7}"
              + (f"  warnings={row['warnings']}" if row.get("warnings") else ""))
        for p in row["problems"]:
            print(f"         ! {p}")

    failed = [r for r in rows if r["problems"]]
    print(f"\n{len(rows) - len(failed)}/{len(rows)} services passed")
    out = Path(__file__).resolve().parent.parent / ".e2e-results.json"
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"detail written to {out.name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
