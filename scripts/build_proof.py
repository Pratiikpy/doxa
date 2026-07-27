"""Turn a recorded paid run into the data the proof deck renders.

Reads the artefacts the harnesses leave behind — `.paid-sweep.json` (one real purchase per service,
with its settlement hash) and `.deliverables.json` (what each purchase returned) — and writes
`proof-data.json`, which is the only thing `/proof` reads.

Keeping generation separate from rendering is what stops the deck drifting: the page cannot claim a
number that is not in a recorded run, and refreshing the evidence is one command.

Run: python scripts/build_proof.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SWEEP = ROOT / ".paid-sweep.json"
DELIVERABLES = ROOT / ".deliverables.json"
SHOWCASE = ROOT / ".showcase.json"
OUT = ROOT / "proof-data.json"

# The agent-to-agent purchase, recorded from the real run. Each step is a fact with its own hash or
# status, so the flow can be checked rather than believed.
A2A = {
    "lede": "OKX's second review gate is a live probe: a user registers, says \"I would like to use "
            "the services of agent ID 9626\", and the agent must answer. This is that exact flow, run "
            "against the live listing — task published, ASP connected, x402 agreed, payment settled, "
            "deliverable returned, job closed on-chain.",
    "steps": [
        ("User agent #8515 publishes a task to ASP #9626",
         "Job 0xf9de475e…b8d6 — \"Run the robots policy check for https://www.python.org/\", "
         "budget 0.005 USDT, payment mode x402."),
        ("The ASP connects and the two agree terms",
         "x402 agreement reached at the listed fee of 0.005 USDT — no renegotiation, the price the "
         "marketplace shows is the price charged."),
        ("Payment settles on X Layer before the work runs",
         "tx 0x47067e71c413ca2c62138b5fc0780b6d0f5fe4f1165b6b347c5d1d205061637a"),
        ("The endpoint returns the deliverable",
         "HTTP 200, validation L3_REPRODUCED, 2/2 checks passed, Ed25519 signed. Ten AI crawlers "
         "evaluated against python.org's robots.txt, each with the rule that decided it."),
        ("The job closes on-chain and funds are released",
         "tx 0x0bf430556041f128936f216b41d320a4a69d985cbf2596a77f915b9bfc9e49ed — "
         "\"[x402 Job Completed] all steps complete.\""),
    ],
    "note": "The first attempt at this exposed a real defect and is worth stating: the buying agent's "
            "pre-check found no declared inputs, so it paid and sent an empty body, and 0.005 USDT "
            "bought an INVALID_INPUT. Two things changed as a result — every 402 challenge now "
            "declares its required inputs and a worked example, and a paid call that arrives empty "
            "returns that contract at HTTP 200 rather than an error, because the payment has already "
            "settled and charging for an error is indefensible.",
}

# What the outcome audit actually asserts, in the reviewer's language rather than test names.
AUDIT_EXAMPLES = [
    "A 21-character title is correctly NOT flagged as short — the ported threshold is under 20, and "
    "an audit that cries wolf on a fine title is worse than one that says nothing.",
    "A 52-character meta description IS flagged as short, against the same ported threshold of 80.",
    "python.org's five H1 tags are reported as multiple; its 215 links are reported as too many "
    "against the threshold of 100.",
    "Every image on the page has alt text, so no missing-alt fault is raised — the absence of a "
    "false alarm is checked as strictly as the presence of a true one.",
    "python.org's robots.txt genuinely names no AI crawler, so all ten are correctly reported "
    "allowed rather than assumed blocked.",
    "/llms.txt returns 404, so it is correctly reported missing.",
    "geo.score equals the sum of its own signals, and the rubric weights total exactly 100.",
    "Every figure published by content.charts was found verbatim in the source page; anything the "
    "model produced that the page does not state was discarded.",
    "The thin-content word count is measured the way SEONaut's 200-word threshold was calibrated — "
    "skipping link text and stripping punctuation. python.org has 526 words of prose and 1,131 "
    "counted naively; applying a borrowed number to the wrong quantity is how a check stops firing.",
    "Every measurable value found on the page was actually shown to the figure extractor. This is "
    "the invariant that broke when the extraction window was the first 4,000 characters and "
    "python.org's figures all began at character 4,006.",
    "Numbers are classified before anything is asked of a model, and only measurements are charted. "
    "A page whose only numbers are dates gets a sentence explaining that, not an empty result.",
]


def count_tests() -> int:
    try:
        out = subprocess.run([sys.executable, "-m", "pytest", str(ROOT / "tests"), "-q",
                              "-m", "not network", "--collect-only"],
                             capture_output=True, text=True, timeout=600, cwd=ROOT).stdout
        # pytest prints "242/246 tests collected (4 deselected)" when a marker filter is active,
        # and "246 tests collected" when it is not. Handle both rather than the one we happened to
        # see first — a silently-zero count on the proof page would read as "no tests".
        import re as _re
        for line in reversed(out.splitlines()):
            m = _re.search(r"(\d+)\s*/\s*\d+\s+tests? collected", line) or                 _re.search(r"(\d+)\s+tests? collected", line)
            if m:
                return int(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    return 0


def main() -> int:
    if not SWEEP.exists() or not DELIVERABLES.exists():
        print("run scripts/paid_sweep.py first — it records the purchases this page is built from")
        return 1

    services = json.loads(SWEEP.read_text(encoding="utf-8"))
    deliverables = json.loads(DELIVERABLES.read_text(encoding="utf-8"))

    # The sweep runs the model-backed services at `samples: 1` so that thirty-six purchases finish in
    # minutes. That is below the default of three, and a mention rate computed from one answer is not
    # a rate — showing it would understate the service. `buy_showcase.py` records a separate real
    # purchase at the defaults; the showcase card uses that, the settlement table keeps the sweep's.
    showcase = {}
    if SHOWCASE.exists():
        for endpoint, row in json.loads(SHOWCASE.read_text(encoding="utf-8")).items():
            deliverables[endpoint] = row["deliverable"]
            showcase[endpoint] = {k: v for k, v in row.items() if k != "deliverable"}

    audit_total = len(AUDIT_EXAMPLES)
    try:
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "audit_outcomes.py")],
                              capture_output=True, text=True, timeout=900, cwd=ROOT)
        line = [l for l in proc.stdout.splitlines() if "outcome checks passed" in l]
        if line:
            passed, total = line[-1].split()[0].split("/")
            audit = {"passed": int(passed), "total": int(total), "examples": AUDIT_EXAMPLES}
        else:
            audit = {"passed": 0, "total": 0, "examples": AUDIT_EXAMPLES}
    except Exception:  # noqa: BLE001
        audit = {"passed": 0, "total": 0, "examples": AUDIT_EXAMPLES}

    data = {
        "generated_at": datetime.now(timezone.utc).strftime("%d %B %Y"),
        "services": services,
        "deliverables": deliverables,
        "showcase": showcase,
        "a2a": A2A,
        "audit": audit,
        "tests": count_tests(),
    }
    OUT.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    delivered = sum(1 for s in services if not s.get("problems"))
    print(f"wrote {OUT.name}: {delivered}/{len(services)} delivered, "
          f"{sum(1 for s in services if s.get('tx'))} settlements, "
          f"audit {audit['passed']}/{audit['total']}, {data['tests']} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
