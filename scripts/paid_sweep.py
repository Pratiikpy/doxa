"""Buy all 36 services with real money, and check what comes back.

Every call here is a genuine x402 v2 purchase: a 402 from the deployed service, an authorization
signed by the OKX agentic wallet, settlement through the OKX facilitator on X Layer, and a signed
deliverable in return. Each settlement transaction hash is recorded so any of them can be checked on
a block explorer.

This is the report to hand OKX with a listing: not "the endpoints return 200", but "here is a
transaction per service and the deliverable it bought".

Run: python scripts/paid_sweep.py [--only page.] [--base URL]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import urllib.parse
import subprocess
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime import Signer  # noqa: E402

BASE = os.environ.get("DOXA_BASE_URL", "https://doxa.ivaronix.xyz")
# The OKX agentic-wallet CLI. Override with DOXA_ONCHAINOS if it is not on PATH.
ONCHAINOS = os.environ.get("DOXA_ONCHAINOS", "onchainos")

PAGE = "https://www.python.org/"
SITE = "https://quotes.toscrape.com/"
CRAWL = {"max_pages": 8, "max_depth": 2, "deadline_s": 60}

# input + the assertion that proves the deliverable is real, per service.
CASES: dict[str, tuple[dict, dict, object]] = {
    "page.audit": ({"url": PAGE}, {}, lambda r: bool(r["findings"])),
    "page.links": ({"url": PAGE}, {"limit": 20}, lambda r: r["result"]["checked"] > 0),
    "page.images": ({"url": PAGE}, {"weigh": False}, lambda r: "totals" in r),
    "schema.validate": ({"url": PAGE}, {}, lambda r: bool(r["findings"])),
    "page.hreflang": ({"url": PAGE}, {}, lambda r: "findings" in r),
    "page.asai": ({"url": "https://todomvc.com/examples/react/dist/"}, {},
                  lambda r: r["measured"] is True),
    "page.blocked": ({"url": PAGE}, {"crawlers": ["GPTBot", "ClaudeBot"]},
                     lambda r: bool(r["findings"])),
    "llms.check": ({"url": PAGE}, {}, lambda r: bool(r["findings"])),
    "robots.check": ({"url": PAGE}, {}, lambda r: bool(r["verdicts"])),
    "page.aeo": ({"url": PAGE}, {}, lambda r: bool(r["findings"])),
    "page.chunk": ({"url": PAGE}, {}, lambda r: r["totals"]["spans"] > 0),
    "page.readability": ({"url": PAGE}, {}, lambda r: r["metrics"]["flesch"] is not None),
    "geo.score": ({"url": PAGE}, {}, lambda r: 0 <= r["score"] <= 100),
    "site.audit": ({"url": SITE}, CRAWL, lambda r: r["coverage"]["pages_crawled"] > 0),
    "site.graph": ({"url": SITE}, CRAWL, lambda r: r["graph"]["pages"] > 0),
    "site.sitemap": ({"url": SITE}, {"max_pages": 6, "max_depth": 2},
                     lambda r: bool(r["findings"])),
    "site.aeo": ({"url": SITE}, CRAWL, lambda r: r["pages_scored"] > 0),
    "kw.discover": ({"seed": "headless cms"}, {"prepositions": False}, lambda r: r["total"] > 20),
    "kw.questions": ({"seed": "headless cms"}, {},
                     lambda r: sum(r["totals"].values()) > 0),
    "kw.demand": ({"seed": "headless cms"}, {}, lambda r: r["demand_index"] is not None),
    "kw.cluster": ({"keywords": ["best crm for startups", "buy crm software", "what is a crm",
                                 "crm pricing", "top crm tools"]}, {},
                   lambda r: bool(r["clusters"])),
    "corpus.presence": ({"domain": "stripe.com"}, {"indexes": 2},
                        lambda r: r["indexes_checked"] > 0),
    "links.inbound": ({"domain": "stripe.com", "brand": "Stripe"}, {"limit": 5},
                      lambda r: bool(r["sources_queried"])),
    "links.compare": ({"domain": "stripe.com", "competitor": "adyen.com"}, {},
                      lambda r: len(r["sides"]) == 2),
    "ai.prompts": ({"url": "https://www.notion.com/"}, {}, lambda r: len(r["questions"]) >= 3),
    "ai.visibility": ({"url": "https://www.notion.com/",
                       "prompts": ["What is the best all-in-one workspace app for a small team?"]},
                      {"samples": 1}, lambda r: r["overall"]["answers_measured"] > 0),
    "ai.brand": ({"url": "https://www.notion.com/"}, {"samples": 1}, lambda r: r["measured"] > 0),
    "ai.citations": ({"url": "https://www.notion.com/",
                      "prompts": ["Which tool keeps team notes and tasks in one place?"]},
                     {"samples": 1}, lambda r: r["answers_measured"] > 0),
    "compete.compare": ({"url": PAGE, "competitor": "https://www.ruby-lang.org/en/"}, {},
                        lambda r: len(r["sides"]) == 2),
    "audit.diff": ({"before": {"result": {"findings": [
        {"code": "title.empty", "severity": "critical", "message": "x", "detail": {}}]}},
        "after": {"result": {"findings": []}}}, {}, lambda r: r["direction"] == "improved"),
    "badge": ({"url": PAGE}, {}, lambda r: r["svg"].startswith("<svg")),
    "report.pdf": ({"url": PAGE}, {}, lambda r: (r["artifact"] or {}).get("bytes", 0) > 1000),
    "content.audit": ({"url": PAGE}, {}, lambda r: bool(r["question"])),
    "content.brief": ({"topic": "choosing a headless CMS"}, {},
                      lambda r: bool((r["brief"] or {}).get("sections"))),
    "content.charts": ({"url": PAGE}, {}, lambda r: "figures" in r),
    "seo.engagement": ({"url": SITE, "goal": "be cited by assistants"}, {"sample_pages": 6},
                       lambda r: bool(r["phases"])),
}


def pin_dns(host: str, ip: str) -> None:
    original = socket.getaddrinfo
    socket.getaddrinfo = lambda h, p, *a, **k: original(ip if h == host else h, p, *a, **k)


def sign(header_value: str) -> tuple[str, str]:
    proc = subprocess.run([ONCHAINOS, "payment", "pay", "--payload", header_value],
                          capture_output=True, text=True, timeout=300)
    for line in reversed((proc.stdout or "").splitlines()):
        if line.strip().startswith("{"):
            d = json.loads(line).get("data", {})
            return d.get("header_name", "PAYMENT-SIGNATURE"), d.get("authorization_header", "")
    raise RuntimeError(f"wallet did not sign: {proc.stdout[-200:]} {proc.stderr[-200:]}")


def buy(endpoint: str, payload: dict, options: dict, assertion) -> dict:
    started = time.perf_counter()
    row: dict = {"endpoint": endpoint, "problems": []}
    url = f"{BASE}/a2mcp/{endpoint}"
    body = {"input": payload, "options": options}

    r = requests.post(url, json=body, timeout=120)
    if r.status_code != 402:
        row["problems"].append(f"expected 402, got {r.status_code}")
        return row
    challenge_header = next(v for k, v in r.headers.items() if k.upper() == "PAYMENT-REQUIRED")
    challenge = json.loads(base64.b64decode(challenge_header))
    accept = challenge["accepts"][0]
    row["price"] = int(accept["amount"]) / 10 ** accept["extra"]["decimals"]

    name, auth = sign(challenge_header)
    if not auth:
        row["problems"].append("wallet returned no authorization")
        return row

    paid = requests.post(url, headers={name: auth}, json=body, timeout=400)
    row["http"] = paid.status_code
    row["seconds"] = round(time.perf_counter() - started, 1)
    if paid.status_code != 200:
        row["problems"].append(f"paid call returned {paid.status_code}: {paid.text[:160]}")
        return row

    settle_header = next((v for k, v in paid.headers.items()
                          if k.upper() == "X-PAYMENT-RESPONSE"), "")
    if settle_header:
        settled = json.loads(base64.b64decode(settle_header))
        row["tx"] = settled.get("transaction", "")
        row["settled"] = settled.get("success") is True
    if not row.get("settled"):
        row["problems"].append("no settlement confirmation returned")

    env = paid.json()
    if env.get("error"):
        row["problems"].append(f"{env['error']['code']}: {str(env['error']['message'])[:120]}")
        return row

    receipt = env.get("receipt") or {}
    if not Signer.verify(receipt.get("manifest_sha256", ""), receipt.get("signature", ""),
                         receipt.get("public_key", "")):
        row["problems"].append("receipt signature does not verify")
    tests = (env.get("validation") or {}).get("tests", [])
    failed = [t["name"] for t in tests if not t["passed"]]
    row["checks"] = f"{len(tests) - len(failed)}/{len(tests)}"
    if failed:
        row["problems"].append(f"validation failed: {failed}")

    result = env.get("result") or {}
    row["bytes"] = len(json.dumps(result))
    row["deliverable"] = result
    try:
        if not assertion(result):
            row["problems"].append("deliverable did not contain what was promised")
    except Exception as e:  # noqa: BLE001
        row["problems"].append(f"assertion raised {type(e).__name__}: {e}")
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--host-ip", default="",
                    help="pin the service hostname to this IP, for when a local "
                         "resolver is stale; normally unnecessary")
    args = ap.parse_args()
    if args.host_ip:
        pin_dns(urllib.parse.urlsplit(BASE).hostname, args.host_ip)

    services = [s["endpoint"] for s in requests.get(f"{BASE}/services", timeout=60).json()["services"]]
    todo = [e for e in services if args.only in e]
    missing = [e for e in todo if e not in CASES]
    if missing:
        print(f"no test case defined for: {missing}")
        return 1

    print(f"Buying {len(todo)} services with real x402 payments on X Layer.\n")
    rows, spent = [], 0.0
    for endpoint in todo:
        payload, options, assertion = CASES[endpoint]
        row = buy(endpoint, payload, options, assertion)
        rows.append(row)
        spent += row.get("price", 0)
        mark = "PASS" if not row["problems"] else "FAIL"
        print(f"  [{mark}] {endpoint:18} {row.get('checks','-'):>6} "
              f"{row.get('seconds','?'):>6}s ${row.get('price','?'):<6} "
              f"{row.get('bytes','?'):>7}B  tx {str(row.get('tx',''))[:18]}…")
        for p in row["problems"]:
            print(f"         ! {p}")

    failed = [r for r in rows if r["problems"]]
    print(f"\n{len(rows) - len(failed)}/{len(rows)} services bought and delivered")
    print(f"total spent: {spent:.3f} USD₮0 across {sum(1 for r in rows if r.get('tx'))} settlements")
    # A filtered run must not discard the rest of the record. `--only robots.check` used to replace
    # thirty-six rows with one, silently destroying the evidence the proof deck is generated from;
    # the deck would then have rendered "1/1 delivered" from a run that bought everything.
    root = Path(__file__).resolve().parent.parent
    deliverables_path, out = root / ".deliverables.json", root / ".paid-sweep.json"
    fresh_deliverables = {r["endpoint"]: r.pop("deliverable", None) for r in rows}
    bought = {r["endpoint"] for r in rows}

    if args.only:
        previous = json.loads(out.read_text(encoding="utf-8")) if out.exists() else []
        rows = [r for r in previous if r["endpoint"] not in bought] + rows
        if deliverables_path.exists():
            kept = json.loads(deliverables_path.read_text(encoding="utf-8"))
            kept.update(fresh_deliverables)
            fresh_deliverables = kept
        order = {name: i for i, name in enumerate(CASES)}
        rows.sort(key=lambda r: order.get(r["endpoint"], len(order)))

    deliverables_path.write_text(json.dumps(fresh_deliverables, indent=1, ensure_ascii=False),
                                 encoding="utf-8")
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"transaction hashes written to {out.name} ({len(rows)} rows on record)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
