#!/usr/bin/env python3
"""Backtest: rolling intraday rescan vs. the current open-gap-only scan.

Motivation (2026-07-22): the day's real movers (ZCMD +714%, LABT +268%,
ADVB +165%) never entered the 9:15 universe — they surged at/after the
open. This script asks: if the scanner re-ran every 5 minutes until
10:30 and armed a 5-minute box on detection, would the volume-confirmed
breakout rule have made money on those days?

Unified rule ("roll5-volx2"): at each 5-min poll (9:30..10:30), a
symbol is DETECTED when price >= prev_close +10%, $2-20 band, RVOL >= 5
(cum volume vs 90-day avg pace). On detection: box = next 5 minute
bars; enter on a break of box high whose breakout minute prints >= 2x
the box's average volume; stop = box low (capped $0.50); half @1R,
half @2R, breakeven move; flatten 11:00. A gap-day detection at 9:30
reduces exactly to today's live orb5-volx2 — so the comparison
isolates the value of the rolling rescan.

Baseline ("open-volx2"): same rule but detection allowed ONLY at 9:30
(what production does today).

Case discovery: symbols from local+server events.jsonl PLUS today's
top movers, scanned over ~90 days of daily bars for days where
high >= prev_close * 1.15 and volume >= 1M (catches intraday spikers
that never gapped — the current backtest.py misses these by design).

Usage: .venv/bin/python backtest_intraday.py [--max-cases N]
"""

import csv
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import rh
from backtest import run_trade, RISK, SLIP, STOP_DIST_MAX, MIN_STOP

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).parent
OUT = ROOT / "backtest_intraday_results.csv"

PRICE_MIN, PRICE_MAX = 2.0, 20.0
DAY_VOL_MIN = 1_000_000
HIGH_GAIN_MIN = 0.15      # case filter: day high >= prev close +15%
DETECT_GAIN = 0.10        # detection: price >= prev close +10%
RVOL_MIN = 5.0
CUMVOL_MIN = 100_000
DETECT_LAST_MIN = 60      # last poll 10:30 (60 min after open)
POLL_STEP = 5             # scanner cadence in minutes
BOX_MIN = 5
VOL_X = 2.0
RANGE_MAX_FRAC = 0.10
LOOKBACK_DAYS = 90

EXTRA_SYMBOLS = ["ZCMD", "ADVB", "LABT", "PN", "INLF", "QMLS", "KUST",
                 "SMCC", "ZBAO", "ZKPW"]


def universe():
    syms = set(EXTRA_SYMBOLS)
    scratch = Path("/private/tmp/claude-1102059012/"
                   "-Users-qfu-Documents-workspace-cameron/"
                   "92a6b079-e742-41f0-955c-26b3ed65a0ac/scratchpad/"
                   "server_events.jsonl")
    for path in (ROOT / "events.jsonl", scratch):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            d = e.get("data") or {}
            if d.get("symbol") and "." not in d["symbol"]:
                syms.add(d["symbol"])
    return sorted(syms)


def spike_days(sym):
    """(day, prev_close, avg_daily_vol) where high >= prev_close*1.15."""
    end = datetime.now(ET)
    start = end - timedelta(days=LOOKBACK_DAYS)
    try:
        days = rh.bars(sym, start.isoformat(), end.isoformat(),
                       interval="day")
    except Exception:
        return []
    out = []
    closes_vols = []
    prev_close = None
    for b in days:
        h, c, v = b.get("high"), b.get("close"), b.get("volume") or 0
        if prev_close and h and v >= DAY_VOL_MIN:
            gain = h / prev_close - 1
            if gain >= HIGH_GAIN_MIN and PRICE_MIN <= prev_close * (
                    1 + DETECT_GAIN) <= PRICE_MAX * 2:
                trailing = [x[1] for x in closes_vols[-20:]] or [v]
                out.append((b["begins_at"][:10], prev_close,
                            sum(trailing) / len(trailing)))
        if c:
            closes_vols.append((c, v))
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


def detect_indexes(bars, prev_close, avg_daily_vol, open_only):
    """Minute indexes where the rolling scanner would flag the symbol."""
    idxs = []
    cumvol = 0.0
    for i, b in enumerate(bars):
        px = b["close"]
        cumvol += b.get("volume") or 0
        if open_only and i > 0:
            break
        if i % POLL_STEP or i > DETECT_LAST_MIN:
            continue
        if not (PRICE_MIN <= px <= PRICE_MAX):
            continue
        if px < prev_close * (1 + DETECT_GAIN):
            continue
        elapsed = max(i + 1, 1) / 390
        rvol = cumvol / max(avg_daily_vol * elapsed, 1)
        if i == 0:
            # 9:30 poll ~ premarket scan: gap qualifies by itself
            idxs.append(0)
        elif cumvol >= CUMVOL_MIN and rvol >= RVOL_MIN:
            idxs.append(i)
    return idxs


def attempt(bars, det, label):
    """Box after detection, volume-confirmed breakout, managed trade."""
    box = bars[det:det + BOX_MIN]
    if len(box) < BOX_MIN:
        return None
    hi = max(b["high"] for b in box)
    lo = min(b["low"] for b in box)
    avg_vol = sum(b.get("volume") or 0 for b in box) / len(box)
    if (hi - lo) > hi * RANGE_MAX_FRAC:
        return (label, None, None, det, "range_too_wide")
    bi = next((i for i in range(det + BOX_MIN, len(bars))
               if bars[i]["high"] > hi), None)
    if bi is None:
        return None
    if (bars[bi].get("volume") or 0) < VOL_X * (avg_vol or 1):
        return (label, None, None, det, "vol_reject")
    entry = round(min(bars[bi]["high"], hi + 0.01) + SLIP, 2)
    stop = round(max(lo, entry - STOP_DIST_MAX), 2)
    if entry - stop < MIN_STOP:
        return (label, None, None, det, "stop_too_tight")
    res = run_trade(bars, bi, entry, stop, partial=True)
    if res is None:
        return (label, None, None, det, "too_small")
    pnl, kind = res
    return (label, pnl, entry, det, kind)


def replay(bars, prev_close, avg_daily_vol):
    """Yield one row per strategy: first detection that produces a
    definitive outcome (trade, or veto that consumed the symbol)."""
    out = []
    for label, open_only in (("open-volx2", True), ("roll5-volx2", False)):
        rows = None
        for det in detect_indexes(bars, prev_close, avg_daily_vol,
                                  open_only):
            rows = attempt(bars, det, label)
            if rows and rows[1] is not None:
                break  # traded — one entry per symbol per day
            # veto/no-box: production would keep polling -> next detection
        out.append(rows or (label, None, None, None, "no_detect"))
    return out


def main():
    max_cases = 400
    if "--max-cases" in sys.argv:
        max_cases = int(sys.argv[sys.argv.index("--max-cases") + 1])
    syms = universe()
    print(f"{len(syms)} symbols in universe")
    cases = []
    for s in syms:
        for day, pc, av in spike_days(s):
            cases.append((s, day, pc, av))
        time.sleep(0.3)
    print(f"{len(cases)} (symbol, spike-day) cases found")
    cases = cases[:max_cases]

    rows, skipped = [], 0
    for n, (sym, day, pc, av) in enumerate(cases, 1):
        bars = minute_bars(sym, day)
        if len(bars) < 15:
            skipped += 1
            continue
        for label, pnl, entry, det, kind in replay(bars, pc, av):
            rows.append({"date": day, "symbol": sym, "strategy": label,
                         "detect_min": det, "entry": entry,
                         "exit_kind": kind,
                         "pnl": round(pnl, 2) if pnl is not None else "",
                         "r": round(pnl / RISK, 2)
                              if pnl is not None else ""})
        if n % 20 == 0:
            print(f"  ...{n}/{len(cases)} days replayed")
        time.sleep(0.3)

    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "symbol", "strategy",
                                          "detect_min", "entry",
                                          "exit_kind", "pnl", "r"])
        w.writeheader()
        w.writerows(rows)
    print(f"\n{len(rows)} rows -> {OUT.name} ({skipped} days skipped)")

    print(f"\n{'strategy':<13}{'trades':>7}{'win%':>7}{'avgR':>7}"
          f"{'totR':>8}   exits")
    for name in ("open-volx2", "roll5-volx2"):
        t = [r for r in rows if r["strategy"] == name and r["r"] != ""]
        nt = [r for r in rows if r["strategy"] == name and r["r"] == ""]
        if not t:
            print(f"{name:<13}{0:>7}   ({len(nt)} no-trade)")
            continue
        rs = [float(r["r"]) for r in t]
        wins = sum(1 for x in rs if x > 0)
        kinds = {}
        for r in t:
            kinds[r["exit_kind"]] = kinds.get(r["exit_kind"], 0) + 1
        late = sum(1 for r in t if (r["detect_min"] or 0) > 0)
        print(f"{name:<13}{len(rs):>7}{100*wins/len(rs):>6.0f}%"
              f"{sum(rs)/len(rs):>7.2f}{sum(rs):>8.1f}   {kinds}"
              f"  ({late} intraday detections)")


if __name__ == "__main__":
    main()
