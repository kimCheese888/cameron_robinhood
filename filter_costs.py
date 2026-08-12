#!/usr/bin/env python3
"""Filter opportunity-cost analysis (read-only, offline).

For every stock our gates rejected (arm.drop / trigger.veto), replay what
the live ORB bracket trade WOULD have done that day on real 1-min bars —
so we can see, per gate, how much loss each gate saved us vs how much
profit it cost us. Answers "which gate should we loosen?".

Trade model = the live rule WITHOUT the rejecting filter:
  entry = OR_high + 0.03, stop = max(OR_low, entry-0.50),
  half off at +1R, stop->breakeven, other half at +2R, flatten 11:00 ET.
R accounting: full position = 1.0R risk; $ = R * RISK_PER_TRADE.

Bars: Robinhood consolidated preferred, Alpaca IEX fallback (IEX is ~2%
of tape on these names — treat magnitudes as indicative, signs reliable).

Usage:  .venv/bin/python filter_costs.py
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import executor
import rh
import scanner

SLIP = 0.03
STOP_CAP = 0.50
MIN_STOP = 0.05
RISK_DOLLARS = 100.0
GATES = {"range_too_wide", "spread_at_open", "veto"}  # skip no_bars (no data)


def gate_of(e):
    if e["type"] == "trigger.veto":
        return "veto"
    return (e.get("data") or {}).get("reason", "?")


def day_bars(sym, day):
    """1-min bars 09:30-11:00 ET (13:30-15:00 UTC, EDT) as
    (minute_from_930, high, low, close). RH first, IEX fallback."""
    # NB: use +00:00, not "Z" — rh.bars -> _utc_z -> datetime.fromisoformat,
    # and Python 3.10 fromisoformat rejects a trailing "Z".
    start = f"{day}T13:30:00+00:00"
    end = f"{day}T15:00:00+00:00"
    base = datetime.fromisoformat(f"{day}T13:30:00+00:00")
    out = []
    try:
        rb = rh.bars(sym, start, end)
        for b in rb:
            ts = (b.get("begins_at") or "").replace("Z", "+00:00")
            t = datetime.fromisoformat(ts)
            m = int((t - base).total_seconds() // 60)
            out.append((m, float(b["high"]), float(b["low"]),
                        float(b["close"])))
    except Exception:
        out = []
    if len(out) >= 6:
        return sorted(out)
    # IEX fallback
    try:
        bars = scanner.api(f"/v2/stocks/{sym}/bars", timeframe="1Min",
                           start=start, end=end, feed="iex",
                           limit=200).get("bars") or []
    except Exception:
        bars = []
    out = []
    for b in bars:
        t = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
        m = int((t - datetime.fromisoformat(f"{day}T13:30:00+00:00"))
                .total_seconds() // 60)
        out.append((m, float(b["h"]), float(b["l"]), float(b["c"])))
    return sorted(out)


def sim(bars):
    """Return realized R of the breakout bracket, or None if it never
    would have entered / was un-sizable."""
    orb = [b for b in bars if 0 <= b[0] < 5]
    post = [b for b in bars if b[0] >= 6]
    if len(orb) < 2 or not post:
        return None
    or_hi = max(b[1] for b in orb)
    or_lo = min(b[2] for b in orb)
    entry = round(or_hi + SLIP, 2)
    stop = round(max(or_lo, entry - STOP_CAP), 2)
    risk = round(entry - stop, 2)
    if risk < MIN_STOP:
        return None
    tp1, tp2 = round(entry + risk, 2), round(entry + 2 * risk, 2)
    in_pos = half = be = False
    r = 0.0
    for _, hi, lo, _c in post:
        if not in_pos:
            if hi >= entry:
                in_pos = True
            else:
                continue
        cur_stop = entry if be else stop
        if lo <= cur_stop:                      # stop first (conservative)
            r += 0.0 if half else -1.0          # remaining half at BE = 0
            return r
        if not half and hi >= tp1:
            r += 0.5                             # half off at +1R
            half = be = True
        if half and hi >= tp2:
            r += 1.0                             # other half at +2R
            return r
    last = post[-1][3]                           # cutoff flatten at close
    frac = 0.5 if half else 1.0
    r += frac * (last - entry) / risk
    return round(r, 2)


def main():
    scanner.load_env()
    rows = [json.loads(l) for l in open(executor.ROOT / "events.jsonl")]
    cases = [(e["ts"][:10], (e.get("data") or {}).get("symbol"), gate_of(e))
             for e in rows
             if e["type"] in ("arm.drop", "trigger.veto")
             and gate_of(e) in GATES
             and (e.get("data") or {}).get("symbol")]
    seen = set()
    agg = defaultdict(lambda: {"n": 0, "traded": 0, "R": 0.0,
                               "saved": 0.0, "missed": 0.0, "rows": []})
    for day, sym, gate in cases:
        if (day, sym, gate) in seen:
            continue
        seen.add((day, sym, gate))
        g = agg[gate]
        g["n"] += 1
        r = sim(day_bars(sym, day))
        if r is None:
            g["rows"].append((day, sym, "no-entry/no-data", None))
            continue
        g["traded"] += 1
        g["R"] += r
        if r >= 0:
            g["missed"] += r
        else:
            g["saved"] += -r
        g["rows"].append((day, sym, "", r))

    print(f"{'gate':<16}{'cases':>6}{'wouldTrade':>11}{'netR':>8}"
          f"{'net$':>8}{'saved$':>8}{'missed$':>8}")
    tot = defaultdict(float)
    for gate, g in agg.items():
        net = g["R"]
        print(f"{gate:<16}{g['n']:>6}{g['traded']:>11}{net:>+8.2f}"
              f"{net * RISK_DOLLARS:>+8.0f}"
              f"{g['saved'] * RISK_DOLLARS:>+8.0f}"
              f"{-g['missed'] * RISK_DOLLARS:>+8.0f}")
        tot["net"] += net
        tot["saved"] += g["saved"]
        tot["missed"] += g["missed"]
    print(f"{'TOTAL':<16}{'':>6}{'':>11}{tot['net']:>+8.2f}"
          f"{tot['net'] * RISK_DOLLARS:>+8.0f}"
          f"{tot['saved'] * RISK_DOLLARS:>+8.0f}"
          f"{-tot['missed'] * RISK_DOLLARS:>+8.0f}")
    print("\nnet$ = if we had traded all rejects; saved$ = losses the gate "
          "avoided; missed$ = profits the gate cost us.\n")
    for gate, g in agg.items():
        print(f"--- {gate} (detail) ---")
        for day, sym, note, r in sorted(g["rows"]):
            tag = note if note else f"{r:+.2f}R  (${r*RISK_DOLLARS:+.0f})"
            print(f"  {day}  {sym:<6} {tag}")


if __name__ == "__main__":
    main()
