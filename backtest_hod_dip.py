#!/usr/bin/env python3
"""Backtest: HOD-momo detection + micro-pullback (dip) entry vs. box
breakout on the same detections.

Detection ("HOD Momo" emulation): at 5-min polls 9:30-10:30, price is
+10% on the day, $2-20, RVOL >= 5, AND (for intraday polls) the last 5
minutes gained >= 3% — the "moving right now" burst that distinguishes
Ross's HOD Momo scanner from a plain gainers list. A 9:30 detection is
the ordinary premarket gap case.

Entries compared on identical detections:
  hod-box: 5-min box after detection, 2x-volume breakout (the rule the
           07/22 rolling backtest showed to be ~zero-edge intraday).
  hod-dip: Ross-style micro pullback — after detection wait for the
           first red/lower-low bar, track the pullback low, enter when
           a green bar reclaims the prior bar's high; stop = pullback
           low (capped $0.50). Invalidated if the pullback exceeds 15%
           off the running high or no entry within 30 minutes.

Management identical to production: half @1R, half @2R, breakeven
stop, pessimistic bar ordering, flatten 11:00.

Usage: .venv/bin/python backtest_hod_dip.py
"""

import csv
import time
from pathlib import Path

from backtest import run_trade, RISK, SLIP, STOP_DIST_MAX, MIN_STOP
from backtest_intraday import (universe, spike_days, minute_bars,
                               PRICE_MIN, PRICE_MAX, DETECT_GAIN,
                               RVOL_MIN, CUMVOL_MIN, DETECT_LAST_MIN,
                               POLL_STEP, BOX_MIN, VOL_X,
                               RANGE_MAX_FRAC)

ROOT = Path(__file__).parent
OUT = ROOT / "backtest_hod_dip_results.csv"

BURST_MIN = 0.03          # last-5-min gain for an intraday HOD alert
DIP_MAX_FRAC = 0.15       # abandon if pullback exceeds 15% off high
DIP_WINDOW = 30           # minutes to find the dip entry


def hod_detect(bars, prev_close, avg_daily_vol):
    idxs = []
    cumvol = 0.0
    for i, b in enumerate(bars):
        px = b["close"]
        cumvol += b.get("volume") or 0
        if i % POLL_STEP or i > DETECT_LAST_MIN:
            continue
        if not (PRICE_MIN <= px <= PRICE_MAX):
            continue
        if px < prev_close * (1 + DETECT_GAIN):
            continue
        if i == 0:
            idxs.append(0)
            continue
        elapsed = (i + 1) / 390
        rvol = cumvol / max(avg_daily_vol * elapsed, 1)
        if cumvol < CUMVOL_MIN or rvol < RVOL_MIN:
            continue
        burst = px / bars[max(i - 5, 0)]["close"] - 1
        if burst >= BURST_MIN:
            idxs.append(i)
    return idxs


def box_attempt(bars, det):
    box = bars[det:det + BOX_MIN]
    if len(box) < BOX_MIN:
        return None
    hi = max(b["high"] for b in box)
    lo = min(b["low"] for b in box)
    avg_vol = sum(b.get("volume") or 0 for b in box) / len(box)
    if (hi - lo) > hi * RANGE_MAX_FRAC:
        return ("veto", "range_too_wide")
    bi = next((i for i in range(det + BOX_MIN, len(bars))
               if bars[i]["high"] > hi), None)
    if bi is None:
        return None
    if (bars[bi].get("volume") or 0) < VOL_X * (avg_vol or 1):
        return ("veto", "vol_reject")
    entry = round(min(bars[bi]["high"], hi + 0.01) + SLIP, 2)
    stop = round(max(lo, entry - STOP_DIST_MAX), 2)
    if entry - stop < MIN_STOP:
        return ("veto", "stop_too_tight")
    return ("trade", bi, entry, stop)


def dip_attempt(bars, det):
    ref_hi = bars[det]["high"]
    pull_low = None
    prev = bars[det]
    for j in range(det + 1, min(det + DIP_WINDOW + 1, len(bars))):
        b = bars[j]
        ref_hi = max(ref_hi, b["high"])
        if pull_low is None:
            if b["close"] < b["open"] or b["low"] < prev["low"]:
                pull_low = b["low"]
        else:
            pull_low = min(pull_low, b["low"])
            if pull_low < ref_hi * (1 - DIP_MAX_FRAC):
                return ("veto", "dip_too_deep")
            if b["high"] > prev["high"] and b["close"] > b["open"]:
                entry = round(min(b["high"], prev["high"] + 0.01)
                              + SLIP, 2)
                stop = round(max(pull_low, entry - STOP_DIST_MAX), 2)
                if entry - stop < MIN_STOP:
                    return ("veto", "stop_too_tight")
                return ("trade", j, entry, stop)
        prev = b
    return None


def replay(bars, prev_close, avg_daily_vol):
    out = []
    dets = hod_detect(bars, prev_close, avg_daily_vol)
    for label, attempt in (("hod-box", box_attempt),
                           ("hod-dip", dip_attempt)):
        row = None
        for det in dets:
            r = attempt(bars, det)
            if r is None:
                continue
            if r[0] == "veto":
                row = (label, None, None, det, r[1])
                continue
            _, bi, entry, stop = r
            res = run_trade(bars, bi, entry, stop, partial=True)
            if res is None:
                row = (label, None, None, det, "too_small")
                continue
            pnl, kind = res
            row = (label, pnl, entry, det, kind)
            break
        out.append(row or (label, None, None, None, "no_detect"))
    return out


def main():
    syms = universe()
    print(f"{len(syms)} symbols in universe")
    cases = []
    for s in syms:
        for day, pc, av in spike_days(s):
            cases.append((s, day, pc, av))
        time.sleep(0.3)
    print(f"{len(cases)} (symbol, spike-day) cases; running ALL")

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
        if n % 40 == 0:
            print(f"  ...{n}/{len(cases)} days replayed")
        time.sleep(0.3)

    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "symbol", "strategy",
                                          "detect_min", "entry",
                                          "exit_kind", "pnl", "r"])
        w.writeheader()
        w.writerows(rows)
    print(f"\n{len(rows)} rows -> {OUT.name} ({skipped} days skipped)")

    print(f"\n{'strategy':<9}{'trades':>7}{'win%':>7}{'avgR':>7}"
          f"{'totR':>8}   exits")
    for name in ("hod-box", "hod-dip"):
        t = [r for r in rows if r["strategy"] == name and r["r"] != ""]
        if not t:
            print(f"{name:<9}      0")
            continue
        rs = [float(r["r"]) for r in t]
        wins = sum(1 for x in rs if x > 0)
        kinds = {}
        for r in t:
            kinds[r["exit_kind"]] = kinds.get(r["exit_kind"], 0) + 1
        intra = [float(r["r"]) for r in t if r["detect_min"] != "0"
                 and r["detect_min"] != 0]
        print(f"{name:<9}{len(rs):>7}{100*wins/len(rs):>6.0f}%"
              f"{sum(rs)/len(rs):>7.2f}{sum(rs):>8.1f}   {kinds}")
        if intra:
            w2 = sum(1 for x in intra if x > 0)
            print(f"  intraday-only: {len(intra)} trades  "
                  f"win {100*w2/len(intra):.0f}%  "
                  f"avgR {sum(intra)/len(intra):+.2f}  "
                  f"totR {sum(intra):+.1f}")


if __name__ == "__main__":
    main()
