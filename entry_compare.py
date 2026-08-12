#!/usr/bin/env python3
"""Head-to-head entry comparison (read-only, offline): breakout vs dip.

Replays two entry styles on real 1-min bars over the SAME stocks/days:
  - breakout : buy OR_high+slip on the break (orb5-plain / live style)
  - dip      : after the break, wait for price to come back to OR_high and
               buy there; dead if it loses OR_low first (orb5-dip style)
Both stop at OR_low (capped 0.50), half@1R / half@2R, flatten 11:00 ET.

Universes:
  rejects  = names our gates blocked (arm.drop / trigger.veto)
  all      = every watchlist.add name

Answers: does the dip entry actually beat the breakout entry — the
decisive test for promoting orb5-dip to live.

Usage:  .venv/bin/python entry_compare.py
"""

import json
from collections import defaultdict
from datetime import datetime

import executor
import rh
import scanner

SLIP = 0.03
STOP_CAP = 0.50
MIN_STOP = 0.05
R_DOLLARS = 100.0


def day_bars(sym, day):
    start, end = f"{day}T13:30:00+00:00", f"{day}T15:00:00+00:00"
    base = datetime.fromisoformat(start)
    try:
        rb = rh.bars(sym, start, end)
        out = []
        for b in rb:
            t = datetime.fromisoformat((b.get("begins_at") or "")
                                       .replace("Z", "+00:00"))
            out.append((int((t - base).total_seconds() // 60),
                        float(b["high"]), float(b["low"]), float(b["close"])))
        if len(out) >= 6:
            return sorted(out)
    except Exception:
        pass
    try:
        bars = scanner.api(f"/v2/stocks/{sym}/bars", timeframe="1Min",
                           start=start, end=end, feed="iex",
                           limit=200).get("bars") or []
    except Exception:
        bars = []
    out = [(int((datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
                 - base).total_seconds() // 60),
            float(b["h"]), float(b["l"]), float(b["c"])) for b in bars]
    return sorted(out)


def _manage(post, start_i, entry, stop, risk):
    """Half@1R / half@2R / breakeven / cutoff, from bar index start_i."""
    tp1, tp2 = round(entry + risk, 2), round(entry + 2 * risk, 2)
    half = be = False
    r = 0.0
    for _, hi, lo, _c in post[start_i:]:
        cur = entry if be else stop
        if lo <= cur:
            return r + (0.0 if half else -1.0)
        if not half and hi >= tp1:
            r += 0.5
            half = be = True
        if half and hi >= tp2:
            return r + 1.0
    last = post[-1][3]
    return round(r + (0.5 if half else 1.0) * (last - entry) / risk, 2)


def _or(bars):
    orb = [b for b in bars if 0 <= b[0] < 5]
    post = [b for b in bars if b[0] >= 6]
    if len(orb) < 2 or not post:
        return None
    return max(b[1] for b in orb), min(b[2] for b in orb), post


def sim_breakout(bars):
    o = _or(bars)
    if not o:
        return None
    or_hi, or_lo, post = o
    entry = round(or_hi + SLIP, 2)
    stop = round(max(or_lo, entry - STOP_CAP), 2)
    risk = round(entry - stop, 2)
    if risk < MIN_STOP:
        return None
    for i, (_, hi, _l, _c) in enumerate(post):
        if hi >= entry:
            return _manage(post, i, entry, stop, risk)
    return None  # never broke


def sim_dip(bars):
    o = _or(bars)
    if not o:
        return None
    or_hi, or_lo, post = o
    broke = False
    for i, (_, hi, lo, _c) in enumerate(post):
        if not broke:
            if hi > or_hi:
                broke = True
            continue
        if lo <= or_lo:              # lost the range low before dipping back
            return None
        if lo <= or_hi + 0.01:       # dipped back to range high -> buy
            entry = round(or_hi + 0.01 + SLIP, 2)
            stop = round(max(or_lo, entry - STOP_CAP), 2)
            risk = round(entry - stop, 2)
            if risk < MIN_STOP:
                return None
            return _manage(post, i + 1, entry, stop, risk)
    return None  # broke but never dipped back (runner) -> no trade


def universe_rejects(rows):
    out = []
    seen = set()
    for e in rows:
        if e["type"] in ("arm.drop", "trigger.veto"):
            s = (e.get("data") or {}).get("symbol")
            k = (e["ts"][:10], s)
            if s and k not in seen:
                seen.add(k)
                out.append(k)
    return out


def universe_all(rows):
    out, seen = [], set()
    for e in rows:
        if e["type"] == "watchlist.add":
            s = (e.get("data") or {}).get("symbol")
            k = (e["ts"][:10], s)
            if s and k not in seen:
                seen.add(k)
                out.append(k)
    return out


def run(name, universe, detail=False):
    tb = td = 0.0
    nb = nd = 0
    rows = []
    for day, sym in universe:
        bars = day_bars(sym, day)
        rb, rd = sim_breakout(bars), sim_dip(bars)
        if rb is not None:
            tb += rb
            nb += 1
        if rd is not None:
            td += rd
            nd += 1
        rows.append((day, sym, rb, rd))
    print(f"\n=== {name} ({len(universe)} names) ===")
    print(f"  breakout : {nb:>2} trades  {tb:>+6.2f}R  ${tb*R_DOLLARS:>+7.0f}")
    print(f"  dip      : {nd:>2} trades  {td:>+6.2f}R  ${td*R_DOLLARS:>+7.0f}")
    if detail:
        print(f"  {'day':<11}{'sym':<7}{'breakout':>10}{'dip':>8}")
        for day, sym, rb, rd in sorted(rows):
            fb = f"{rb:+.2f}" if rb is not None else "  -"
            fd = f"{rd:+.2f}" if rd is not None else "  -"
            print(f"  {day:<11}{sym:<7}{fb:>10}{fd:>8}")


def main():
    scanner.load_env()
    rows = [json.loads(l) for l in open(executor.ROOT / "events.jsonl")]
    run("REJECTS (gate-blocked names)", universe_rejects(rows), detail=True)
    run("ALL watchlist names", universe_all(rows))


if __name__ == "__main__":
    main()
