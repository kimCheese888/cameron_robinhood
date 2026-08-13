#!/usr/bin/env python3
"""Shadow-variant scoreboard — running A/B tracking (read-only).

Aggregates variants.csv into a per-strategy table (trades, win%, total R,
$, avg R, last date) so we can watch the shadow experiments accumulate
toward the promotion bar (>=30 trades AND a robust R edge, per ROLLOUT).

Usage:  .venv/bin/python variant_scoreboard.py
        .venv/bin/python variant_scoreboard.py --since 2026-08-12
"""

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
VARIANTS = ROOT / "variants.csv"
EVENTS = ROOT / "events.jsonl"
PROMOTE_MIN_TRADES = 30

# newest experiments first so they're easy to find at the top
ORDER = ["micro-dip", "volx2-nochase", "volx2-1.5x",
         "orb5-dip", "orb5-plain", "orb5-full2R", "orb15"]


def main():
    since = None
    if "--since" in sys.argv:
        since = sys.argv[sys.argv.index("--since") + 1]

    agg = defaultdict(lambda: {"n": 0, "win": 0, "r": 0.0, "pnl": 0.0,
                               "last": ""})
    if VARIANTS.exists():
        for row in csv.DictReader(open(VARIANTS)):
            if since and row["date"] < since:
                continue
            v = agg[row["variant"]]
            r = float(row["r"])
            v["n"] += 1
            v["win"] += (r > 0)
            v["r"] += r
            v["pnl"] += float(row["pnl"])
            v["last"] = max(v["last"], row["date"])

    # live volx2 lives on the paper account, not variants.csv — count its
    # real entries from the journal so it shows on the same board
    live_n = 0
    live_last = ""
    if EVENTS.exists():
        for line in EVENTS.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "trigger" and (not since
                                               or e["ts"][:10] >= since):
                live_n += 1
                live_last = e["ts"][:10]

    print(f"{'variant':<15}{'trades':>7}{'win%':>6}{'totR':>8}{'tot$':>8}"
          f"{'avgR':>7}{'last':>12}{'':>4}")
    names = ORDER + [k for k in agg if k not in ORDER]
    for name in names:
        if name not in agg:
            continue
        v = agg[name]
        n = v["n"]
        ready = "  READY" if n >= PROMOTE_MIN_TRADES else ""
        print(f"{name:<15}{n:>7}{100*v['win']/n:>5.0f}%{v['r']:>+8.2f}"
              f"{v['pnl']:>+8.0f}{v['r']/n:>+7.2f}{v['last']:>12}{ready}")
    print(f"{'live volx2*':<15}{live_n:>7}{'—':>6}{'—':>8}{'—':>8}"
          f"{'—':>7}{live_last or '—':>12}")
    print(f"\n* live volx2 trades the paper account (real fills); its P&L "
          f"is in Alpaca, not variants.csv.")
    print(f"READY = >= {PROMOTE_MIN_TRADES} trades (promotion sample "
          f"reached; then check R edge vs incumbent — see ROLLOUT Phase 2).")
    if since:
        print(f"(filtered to trades on/after {since})")


if __name__ == "__main__":
    main()
