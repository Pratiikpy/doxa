"""Record one real purchase verbatim, for the demo video's terminal scene.

The video shows an x402 exchange. It has to be a real one — the 402 the live service actually
returned, the authorization the OKX wallet actually signed, and the settlement hash that is actually
on X Layer. This performs a single purchase and writes the exchange to `.exchange.json` exactly as it
happened, so the video can be generated from it rather than typed out by hand.

Run: python scripts/capture_exchange.py
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from paid_sweep import BASE, sign  # noqa: E402

OUT = ROOT / ".exchange.json"
ENDPOINT = "robots.check"
PAYLOAD = {"input": {"url": "https://www.python.org/"}}


def main() -> int:
    url = f"{BASE}/a2mcp/{ENDPOINT}"

    unpaid = requests.post(url, json=PAYLOAD, timeout=120)
    header = next(v for k, v in unpaid.headers.items() if k.upper() == "PAYMENT-REQUIRED")
    challenge = json.loads(base64.b64decode(header))
    accept = challenge["accepts"][0]

    name, auth = sign(header)
    paid = requests.post(url, json=PAYLOAD, headers={name: auth}, timeout=300)
    env = paid.json()
    # The facilitator's settlement receipt comes back base64 in X-PAYMENT-RESPONSE, not in the body.
    settle_header = next((v for k, v in paid.headers.items()
                          if k.upper() == "X-PAYMENT-RESPONSE"), "")
    settled = json.loads(base64.b64decode(settle_header)) if settle_header else {}

    record = {
        "request": {"method": "POST", "url": url, "body": PAYLOAD},
        "challenge": {
            "status": unpaid.status_code,
            "scheme": accept.get("scheme"),
            "network": accept.get("network"),
            "amount": accept.get("amount"),
            "decimals": accept["extra"]["decimals"],
            "asset": accept.get("asset"),
            "payTo": accept.get("payTo"),
            "maxTimeoutSeconds": accept.get("maxTimeoutSeconds"),
        },
        "authorization_header": name,
        "paid": {
            "status": paid.status_code,
            "tx": settled.get("transaction", ""),
            "settled": settled.get("success") is True,
            "level": (env.get("validation") or {}).get("level"),
            "checks": [(t["name"], t["passed"]) for t in (env.get("validation") or {}).get("tests", [])],
            "receipt": {k: env.get("receipt", {}).get(k)
                        for k in ("manifest_sha256", "signature", "public_key")},
            "result": env.get("result"),
        },
    }
    OUT.write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{unpaid.status_code} -> {paid.status_code}   tx {record['paid']['tx']}")
    print(f"wrote {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
