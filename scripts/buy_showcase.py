"""Buy the model-backed services at their real defaults, for the proof deck's showcase.

`paid_sweep.py` deliberately runs `ai.*` with `samples: 1` — thirty-six purchases at three samples
each would take an hour and buy nothing the sweep is trying to prove, which is that every endpoint
charges and delivers.

But `samples: 1` is not what a customer gets. The default is three, and a mention rate computed from
one answer is not a rate at all — showcasing that would misrepresent the service downward, which is
its own kind of dishonesty. So the deck's showcase card uses a separate, real purchase at the
default, with its own settlement hash, recorded here.

Run: python scripts/buy_showcase.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from paid_sweep import buy, pin_dns  # noqa: E402  (same directory, imported for its purchase path)

OUT = ROOT / ".showcase.json"

# endpoint -> (input, options, assertion). Chosen to show the service doing the thing it is for.
CASES = {
    "ai.visibility": (
        {"url": "https://www.notion.com/",
         "prompts": ["What is the best all-in-one workspace app for a small team?",
                     "Which tool should I use for company documentation and wikis?",
                     "What do people use instead of Confluence?"]},
        {"samples": 3},
        lambda r: r["overall"]["answers_measured"] >= 6,
    ),
}


def main() -> int:
    recorded = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    for endpoint, (payload, options, assertion) in CASES.items():
        print(f"buying {endpoint} at its default settings…")
        row = buy(endpoint, payload, options, assertion)
        if row.get("problems"):
            print(f"  FAILED: {row['problems']}")
            return 1
        recorded[endpoint] = row
        print(f"  [PASS] {row['checks']}  {row['seconds']}s  ${row['price']}  tx {row['tx'][:18]}…")
    OUT.write_text(json.dumps(recorded, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
