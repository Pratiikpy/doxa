"""A real purchase, made the way a customer makes one.

This is the evidence OKX asks for before approving a listing: a user makes a payment and receives a
deliverable. Nothing here is simulated. The 402 challenge comes from the deployed service, the
authorization is signed by the OKX agentic wallet through `onchainos payment pay`, settlement runs
through the OKX facilitator on X Layer, and the deliverable is the signed artifact that comes back.

The wallet balance is read before and after, so the payment is proven by the ledger rather than by
our own logs.

Run: python scripts/user_payment_test.py [--endpoint robots.check] [--base URL]
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


def pin_dns(host: str, ip: str) -> None:
    """The public resolver may still hold the old wildcard record. Pin so this tests the deployment
    rather than DNS propagation."""
    original = socket.getaddrinfo

    def patched(h, port, *a, **kw):
        return original(ip if h == host else h, port, *a, **kw)
    socket.getaddrinfo = patched


def run_cli(*args: str, timeout: int = 300) -> dict:
    proc = subprocess.run([ONCHAINOS, *args], capture_output=True, text=True, timeout=timeout)
    out = (proc.stdout or "").strip()
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    raise RuntimeError(f"no JSON from onchainos {' '.join(args)}: "
                       f"{out[-300:]} {proc.stderr[-200:]}")


def usdt_balance() -> float:
    data = run_cli("wallet", "balance")["data"]
    for detail in data.get("details", []):
        for asset in detail.get("tokenAssets", []):
            if asset.get("chainIndex") == "196" and "USD" in asset.get("symbol", ""):
                return float(asset["balance"])
    return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="robots.check")
    ap.add_argument("--host-ip", default="",
                    help="pin the service hostname to this IP, for when a local "
                         "resolver is stale; normally unnecessary")
    ap.add_argument("--input", default='{"url": "https://www.python.org/"}')
    args = ap.parse_args()

    if args.host_ip:
        pin_dns(urllib.parse.urlsplit(BASE).hostname, args.host_ip)

    url = f"{BASE}/a2mcp/{args.endpoint}"
    payload = {"input": json.loads(args.input)}
    print(f"Buying {args.endpoint} from {BASE} as a customer would.\n")

    before = usdt_balance()
    print(f"1. wallet USD₮0 before ......... {before}")

    # --- the 402 --------------------------------------------------------------------------------
    r = requests.post(url, json=payload, timeout=120)
    if r.status_code != 402:
        print(f"   ! expected 402, got {r.status_code}")
        return 1
    header = next((v for k, v in r.headers.items() if k.upper() == "PAYMENT-REQUIRED"), None)
    if not header:
        print("   ! no PAYMENT-REQUIRED header")
        return 1
    challenge = json.loads(base64.b64decode(header))
    accept = challenge["accepts"][0]
    price = int(accept["amount"]) / 10 ** accept["extra"]["decimals"]
    print(f"2. challenge .................. {price} {accept['extra']['name']} "
          f"on {accept['network']} to {accept['payTo'][:10]}…")

    # --- sign the authorization with the agentic wallet -----------------------------------------
    signed = run_cli("payment", "pay", "--payload", header)
    if not signed.get("ok", True) and "data" not in signed:
        print(f"   ! signing failed: {str(signed)[:200]}")
        return 1
    data = signed.get("data", signed)
    auth_header = data.get("authorization_header") or data.get("authorizationHeader")
    header_name = data.get("header_name") or data.get("headerName") or "X-PAYMENT"
    if not auth_header:
        print(f"   ! no authorization header returned: {str(signed)[:250]}")
        return 1
    print(f"3. authorization signed ....... {header_name}, {len(auth_header)} chars, "
          f"wallet {str(data.get('wallet'))[:12]}…")

    # --- replay the request with payment; the server settles before doing the work ---------------
    started = time.perf_counter()
    paid = requests.post(url, headers={header_name: auth_header}, json=payload, timeout=300)
    elapsed = time.perf_counter() - started
    print(f"4. paid request ............... HTTP {paid.status_code} in {elapsed:.1f}s")
    if paid.status_code != 200:
        print(f"   ! {paid.text[:400]}")
        return 1

    env = paid.json()
    if env.get("error"):
        print(f"   ! service error: {env['error']}")
        return 1

    settle = next((v for k, v in paid.headers.items()
                   if k.upper() == "X-PAYMENT-RESPONSE"), "")
    receipt = env.get("receipt") or {}
    verified = Signer.verify(receipt.get("manifest_sha256", ""), receipt.get("signature", ""),
                             receipt.get("public_key", ""))
    validation = env.get("validation") or {}
    tests = validation.get("tests", [])
    failed = [t["name"] for t in tests if not t["passed"]]

    print(f"5. deliverable ................ {len(json.dumps(env.get('result') or {}))} bytes, "
          f"status={env.get('status')}")
    print(f"6. receipt .................... {receipt.get('algo')}, signature verifies = {verified}")
    print(f"7. validation ................. {len(tests) - len(failed)}/{len(tests)} checks passed"
          + (f"  FAILED {failed}" if failed else ""))
    if settle:
        print(f"8. settlement header .......... {settle[:120]}")

    time.sleep(6)
    after = usdt_balance()
    print(f"9. wallet USD₮0 after ......... {after}   (change {after - before:+.6f})")

    result = env.get("result") or {}
    print("\n--- deliverable excerpt ---")
    print(json.dumps(result, indent=1)[:700])

    ok = paid.status_code == 200 and verified and not failed
    print(f"\n{'PASS' if ok else 'FAIL'}: payment made and deliverable received"
          if ok else "\nFAIL: see above")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
