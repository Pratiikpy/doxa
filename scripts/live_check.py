"""Drive the deployed service over the public internet, exactly as a customer would.

`scripts/e2e.py` exercises the app in-process. This one goes over the wire to the registered URL:
real DNS, real TLS, real x402 handshake, real deliverable. It is the evidence OKX asks for — that a
payment was made and a deliverable came back — and it is the only test that can catch a fault which
exists only in the deployment.

Run: python scripts/live_check.py [--base https://doxa.ivaronix.xyz] [--all]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime import Signer  # noqa: E402
from x402 import make_dev_payment  # noqa: E402

DEFAULT_BASE = os.environ.get("DOXA_BASE_URL", "https://doxa.ivaronix.xyz")

# One representative call per group, with the outcome that proves it actually did the work.
SAMPLE = [
    ("page.audit", {"url": "https://en.wikipedia.org/wiki/Search_engine_optimization"}, {},
     lambda r: bool(r.get("findings"))),
    ("page.asai", {"url": "https://todomvc.com/examples/react/dist/"}, {},
     lambda r: r.get("measured") is True),
    ("robots.check", {"url": "https://en.wikipedia.org/wiki/Search_engine_optimization"}, {},
     lambda r: bool(r.get("verdicts"))),
    ("geo.score", {"url": "https://en.wikipedia.org/wiki/Search_engine_optimization"}, {},
     lambda r: isinstance(r.get("score"), int)),
    ("page.chunk", {"url": "https://en.wikipedia.org/wiki/Search_engine_optimization"}, {},
     lambda r: r["totals"]["spans"] > 0),
    ("kw.discover", {"seed": "headless cms"}, {"prepositions": False},
     lambda r: r["total"] > 20),
    ("corpus.presence", {"domain": "stripe.com"}, {"indexes": 2},
     lambda r: r["indexes_checked"] > 0),
    ("links.inbound", {"domain": "stripe.com", "brand": "Stripe"}, {"limit": 5},
     lambda r: bool(r["sources_queried"])),
    ("site.audit", {"url": "https://quotes.toscrape.com/"},
     {"max_pages": 10, "max_depth": 2, "deadline_s": 60},
     lambda r: r["coverage"]["pages_crawled"] > 0),
    ("ai.visibility", {"url": "https://www.notion.com/",
                       "prompts": ["What is the best all-in-one workspace app for a small team?"]},
     {"samples": 1}, lambda r: r["overall"]["answers_measured"] > 0),
    ("badge", {"url": "https://en.wikipedia.org/wiki/Search_engine_optimization"}, {},
     lambda r: r["svg"].startswith("<svg")),
    ("report.pdf", {"url": "https://en.wikipedia.org/wiki/Search_engine_optimization"}, {},
     lambda r: (r.get("artifact") or {}).get("bytes", 0) > 1000),
]


def call(base: str, endpoint: str, payload: dict, options: dict, check) -> dict:
    started = time.perf_counter()
    row = {"endpoint": endpoint, "problems": []}
    url = f"{base}/a2mcp/{endpoint}"

    unpaid = requests.post(url, json={"input": payload, "options": options}, timeout=180)
    if unpaid.status_code != 402:
        row["problems"].append(f"unpaid call returned {unpaid.status_code}, expected 402")
    header = next((v for k, v in unpaid.headers.items() if k.upper() == "PAYMENT-REQUIRED"), None)
    if not header:
        row["problems"].append("no PAYMENT-REQUIRED header")
        row["seconds"] = round(time.perf_counter() - started, 1)
        return row

    challenge = json.loads(base64.b64decode(header))
    row["price_usdt"] = int(challenge["accepts"][0]["amount"]) / 1e6
    row["payTo"] = challenge["accepts"][0]["payTo"]
    row["resource_url"] = challenge["resource"]["url"]

    paid = requests.post(url, headers={"X-PAYMENT": make_dev_payment(challenge)},
                         json={"input": payload, "options": options}, timeout=300)
    row["http"] = paid.status_code
    row["seconds"] = round(time.perf_counter() - started, 1)
    if paid.status_code != 200:
        row["problems"].append(f"paid call returned {paid.status_code}: {paid.text[:150]}")
        return row

    env = paid.json()
    if env.get("error"):
        row["problems"].append(f"{env['error']['code']}: {str(env['error']['message'])[:140]}")
        return row

    receipt = env.get("receipt") or {}
    if not receipt.get("signature"):
        row["problems"].append("deliverable carried no signature")
    elif not Signer.verify(receipt["manifest_sha256"], receipt["signature"],
                           receipt["public_key"]):
        row["problems"].append("the receipt signature does not verify")
    row["signed"] = bool(receipt.get("signature"))
    row["public_key"] = (receipt.get("public_key") or "")[:16] + "…"

    tests = (env.get("validation") or {}).get("tests", [])
    failed = [t["name"] for t in tests if not t["passed"]]
    row["checks"] = f"{len(tests) - len(failed)}/{len(tests)}"
    if failed:
        row["problems"].append(f"validation failed: {', '.join(failed)}")

    try:
        if not check(env.get("result") or {}):
            row["problems"].append("the deliverable is empty or does not contain what was promised")
    except Exception as e:  # noqa: BLE001
        row["problems"].append(f"outcome check raised {type(e).__name__}: {e}")
    row["deliverable_bytes"] = len(json.dumps(env.get("result") or {}))
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--all", action="store_true", help="every registered service, not the sample")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    health = requests.get(f"{base}/health", timeout=60)
    print(f"{base}  health {health.status_code} {health.text.strip()[:80]}\n")

    cases = SAMPLE
    if args.all:
        listed = requests.get(f"{base}/services", timeout=60).json()["services"]
        known = {c[0] for c in SAMPLE}
        cases = list(SAMPLE) + [
            (s["endpoint"], {"url": "https://en.wikipedia.org/wiki/Search_engine_optimization"},
             {}, lambda r: True)
            for s in listed if s["endpoint"] not in known]

    rows = []
    for endpoint, payload, options, check in cases:
        row = call(base, endpoint, payload, options, check)
        rows.append(row)
        mark = "PASS" if not row["problems"] else "FAIL"
        print(f"  [{mark}] {endpoint:18} {row.get('checks','-'):>7} "
              f"{row['seconds']:>6}s  ${row.get('price_usdt','?'):<6} "
              f"signed={row.get('signed')}  {row.get('deliverable_bytes','?')}B")
        for p in row["problems"]:
            print(f"         ! {p}")

    failed = [r for r in rows if r["problems"]]
    print(f"\n{len(rows) - len(failed)}/{len(rows)} live calls passed against {base}")
    if rows and rows[0].get("payTo"):
        print(f"payTo: {rows[0]['payTo']}")
        print(f"resource url: {rows[0]['resource_url']}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
