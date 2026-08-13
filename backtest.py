#!/usr/bin/env python3
"""Historical ORB backtest over Robinhood minute bars.

Case discovery: every symbol our scanner ever surfaced (events.jsonl) is
checked over the past ~90 days of daily bars for OTHER gap days (open
>= +10% vs prev close, open price $2-20, real volume >= 1M). Each
(symbol, gap-day) becomes a backtest case.

Replay: five strategies (live orb5, full2R, orb15, volx2, dip) run on
that day's 9:30-11:00 minute bars with the SAME rules as production:
range<=10% of price, stop-dist cap $0.50, $100 risk, 2c slip both ways,
pessimistic bar ordering (stop before target when ambiguous).

Output: backtest_results.csv + per-strategy summary.

Usage: .venv/bin/python backtest.py [--max-cases N]
"""

import csv
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import rh

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).parent
OUT = ROOT / "backtest_results.csv"

GAP_MIN = 0.10
PRICE_MIN, PRICE_MAX = 2.0, 20.0
DAY_VOL_MIN = 1_000_000
RANGE_MAX_FRAC = 0.10
STOP_DIST_MAX = 0.50
MIN_STOP = 0.05
RISK = 100.0
SLIP = 0.02
LOOKBACK_DAYS = 90


def scanner_symbols():
    """Every symbol the live scanner ever passed or nearly passed."""
    syms = set()
    for line in (ROOT / "events.jsonl").read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        d = e.get("data") or {}
        if e.get("type") in ("scan.pass", "watchlist.add", "scan.reject",
                             "arm") and d.get("symbol"):
            if "." not in d["symbol"]:  # skip warrants/rights
                syms.add(d["symbol"])
    return sorted(syms)


def gap_days(sym):
    """Days where sym opened >= +10% on real volume in our price band."""
    end = datetime.now(ET)
    start = end - timedelta(days=LOOKBACK_DAYS)
    try:
        days = rh.bars(sym, start.isoformat(), end.isoformat(),
                       interval="day")
    except Exception:
        return []
    out = []
    prev_close = None
    for b in days:
        o, c, v = b.get("open"), b.get("close"), b.get("volume") or 0
        if prev_close and o:
            gap = o / prev_close - 1
            if (gap >= GAP_MIN and PRICE_MIN <= o <= PRICE_MAX
                    and v >= DAY_VOL_MIN):
                out.append(b["begins_at"][:10])
        prev_close = c
    return out


def minute_bars(sym, day):
    s = datetime.fromisoformat(day).replace(hour=9, minute=30, tzinfo=ET)
    e = s.replace(hour=11, minute=0)
    try:
        bars = rh.bars(sym, s.isoformat(), e.isoformat(), interval="minute")
    except Exception:
        return []
    return [b for b in bars if all(k in b for k in
                                   ("open", "high", "low", "close"))]


def opening_range(bars, minutes):
    ob = bars[:minutes]
    if len(ob) < 2:
        return None
    return (max(b["high"] for b in ob), min(b["low"] for b in ob),
            sum(b.get("volume") or 0 for b in ob) / len(ob))


def run_trade(bars, i, entry, stop, partial=True):
    """Walk bars from index i managing the position. Returns (pnl, kind).
    Pessimistic: within one bar, stop is assumed to hit before target."""
    risk = entry - stop
    qty = int(RISK / risk)
    half = qty // 2
    if half < 1:
        return None
    tp1, tp2 = entry + risk, entry + 2 * risk
    realized, open_qty, half_done = 0.0, qty, False
    cur_stop = stop
    for b in bars[i:]:
        if b["low"] <= cur_stop:
            return (realized + (cur_stop - SLIP - entry) * open_qty,
                    "stop" if not half_done else "be_stop")
        if partial and not half_done and b["high"] >= tp1:
            realized += (tp1 - entry) * half
            open_qty -= half
            half_done = True
            cur_stop = entry  # breakeven
            if b["low"] <= cur_stop:  # same-bar reversal, pessimistic
                return (realized + (cur_stop - SLIP - entry) * open_qty,
                        "be_stop")
        if b["high"] >= tp2:
            return realized + (tp2 - entry) * open_qty, "tp2"
    return realized + (bars[-1]["close"] - entry) * open_qty, "cutoff"


def strategies(bars):
    """Yield (name, pnl, entry, kind) for each strategy on this day."""
    out = []
    for name, rng_min, partial, vol_x, dip, micro in (
            ("orb5", 5, True, None, False, False),
            ("orb5-full2R", 5, False, None, False, False),
            ("orb15", 15, True, None, False, False),
            ("orb5-volx2", 5, True, 2.0, False, False),
            ("orb5-dip", 5, True, None, True, False),
            # new experiments (nochase is identical to volx2 here — its
            # edge is a live-only chase artifact the backtest can't model)
            ("volx2-1.5x", 5, True, 1.5, False, False),
            ("micro-dip", 5, True, None, True, True)):
        orange = opening_range(bars, rng_min)
        if not orange:
            continue
        hi, lo, avg_vol = orange
        if (hi - lo) > hi * RANGE_MAX_FRAC:
            out.append((name, None, None, "range_too_wide"))
            continue
        start = rng_min
        # find breakout bar
        bi = next((i for i in range(start, len(bars))
                   if bars[i]["high"] > hi), None)
        if bi is None:
            out.append((name, None, None, "no_trigger"))
            continue
        if vol_x and (bars[bi].get("volume") or 0) < vol_x * (avg_vol or 1):
            out.append((name, None, None, "vol_reject"))
            continue
        if dip:
            # after the break, wait for a retest of the range high;
            # invalidated if price loses the range low first. micro-dip
            # tracks the pullback low and stops there (tighter than OR-low)
            entry_i = None
            pull_low = None
            for i in range(bi + 1, len(bars)):
                if bars[i]["low"] <= lo:
                    break
                if micro:
                    if bars[i]["low"] <= hi:  # pulling back below range high
                        pull_low = min(pull_low if pull_low is not None
                                       else bars[i]["low"], bars[i]["low"])
                    if pull_low is not None and bars[i]["high"] > hi:
                        entry_i = i  # reclaim after a dip
                        break
                elif bars[i]["low"] <= hi + 0.01:
                    entry_i = i
                    break
            if entry_i is None:
                out.append((name, None, None, "no_retest"))
                continue
            entry = round(hi + 0.01 + SLIP, 2)
            stop_base = pull_low if micro else lo
            bi = entry_i
        else:
            entry = round(min(bars[bi]["high"], hi + 0.01) + SLIP, 2)
            stop_base = lo
        stop = round(max(stop_base, entry - STOP_DIST_MAX), 2)
        if entry - stop < MIN_STOP:
            out.append((name, None, None, "stop_too_tight"))
            continue
        res = run_trade(bars, bi, entry, stop, partial)
        if res is None:
            out.append((name, None, None, "too_small"))
            continue
        pnl, kind = res
        out.append((name, pnl, entry, kind))
    return out


def main():
    max_cases = 400
    if "--max-cases" in sys.argv:
        max_cases = int(sys.argv[sys.argv.index("--max-cases") + 1])
    syms = scanner_symbols()
    print(f"{len(syms)} symbols from scanner history: {', '.join(syms)}")
    cases = []
    for s in syms:
        for d in gap_days(s):
            cases.append((s, d))
        time.sleep(0.3)
    print(f"{len(cases)} (symbol, gap-day) cases found")
    cases = cases[:max_cases]

    rows = []
    skipped = 0
    for n, (sym, day) in enumerate(cases, 1):
        bars = minute_bars(sym, day)
        if len(bars) < 10:
            skipped += 1
            continue
        for name, pnl, entry, kind in strategies(bars):
            rows.append({"date": day, "symbol": sym, "strategy": name,
                         "entry": entry, "exit_kind": kind,
                         "pnl": round(pnl, 2) if pnl is not None else "",
                         "r": round(pnl / RISK, 2) if pnl is not None else ""})
        if n % 20 == 0:
            print(f"  ...{n}/{len(cases)} days replayed")
        time.sleep(0.3)

    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "symbol", "strategy",
                                          "entry", "exit_kind", "pnl", "r"])
        w.writeheader()
        w.writerows(rows)
    print(f"\n{len(rows)} rows -> {OUT.name} ({skipped} days skipped: no bars)")

    # summary
    print(f"\n{'strategy':<14}{'trades':>7}{'win%':>7}{'avgR':>7}"
          f"{'totR':>8}   exits")
    for name in ("orb5", "orb5-full2R", "orb15", "orb5-volx2", "orb5-dip",
                 "volx2-1.5x", "micro-dip"):
        t = [r for r in rows if r["strategy"] == name and r["r"] != ""]
        nt = [r for r in rows if r["strategy"] == name and r["r"] == ""]
        if not t:
            print(f"{name:<14}{0:>7}   (no trades; "
                  f"{len(nt)} no-trigger/filtered)")
            continue
        rs = [float(r["r"]) for r in t]
        wins = sum(1 for x in rs if x > 0)
        kinds = {}
        for r in t:
            kinds[r["exit_kind"]] = kinds.get(r["exit_kind"], 0) + 1
        print(f"{name:<14}{len(rs):>7}{100*wins/len(rs):>6.0f}%"
              f"{sum(rs)/len(rs):>7.2f}{sum(rs):>8.1f}   {kinds}")


if __name__ == "__main__":
    main()
