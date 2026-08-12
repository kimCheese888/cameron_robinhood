#!/usr/bin/env python3
"""Fully automated ORB day-trading session (paper account).

Timeline (US/Eastern):
  09:15  premarket scan -> pick watchlist (top gappers, sane spreads)
  09:30  market open, let the 5-min opening range form
  09:35  arm ORB triggers: break of opening-range high -> split-bracket entry
         (executor.py sizing: half TP at 1R, half at 2R, shared stop at range low)
  ...    every cycle: manage stops (breakeven after TP1), circuit breaker
  11:00  pencils down: flatten everything, write session summary, exit

Run:  autotrader.py          # sleeps until next session, then trades
      autotrader.py --test   # build watchlist + fetch bars now, no orders
"""

import fcntl
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import executor
import journal
import rh
import scanner

ET = ZoneInfo("America/New_York")

WATCHLIST_SIZE = 4
MAX_ENTRIES_PER_DAY = 4
SPREAD_MAX = 0.20          # skip books wider than this
RANGE_MAX_FRAC = 0.10      # skip if opening range > 10% of price
STOP_DIST_MAX = 0.50       # cap risk distance even if range is wider
ENTRY_SLIP = 0.03          # marketable limit: last price + this
POLL_SECS = 15
MANAGE_EVERY = 4           # manage/breaker check every N polls
CUTOFF_HM = (11, 0)


def log(msg):
    print(f"[{datetime.now(ET):%H:%M:%S}] {msg}", flush=True)


def now_et():
    return datetime.now(ET)


def sleep_until(target):
    while (secs := (target - now_et()).total_seconds()) > 0:
        time.sleep(min(secs, 300))


def next_session_day():
    d = now_et()
    if (d.hour, d.minute) >= CUTOFF_HM:  # past today's cutoff -> next day
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.date()


def at(day, h, m):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=ET)


def build_watchlist():
    rows = scanner.scan()
    if rows and rh.available():
        try:  # full funnel view in the app, alongside the final picks
            rh.sync_watchlist("Cameron Scan", [r["symbol"] for r in rows])
        except Exception as e:
            journal.event("rh.error", f"scan watchlist sync: {str(e)[:100]}")
    picks = []
    for r in sorted(rows, key=lambda x: -x["gap_pct"]):
        if r["spread"] != "" and r["spread"] > SPREAD_MAX:
            journal.event("watchlist.skip",
                          f"{r['symbol']}: spread ${r['spread']} > "
                          f"${SPREAD_MAX} — book too thin, round trip too "
                          "expensive", symbol=r["symbol"], spread=r["spread"])
            continue
        picks.append(r)
        if len(picks) == WATCHLIST_SIZE:
            break
    for p in picks:
        journal.event("watchlist.add",
                      f"{p['symbol']} ${p['price']} gap {p['gap_pct']}% "
                      f"rvol {p['rvol']} | {p.get('news', '')[:80]}", **p)
    if picks and rh.available():
        try:  # one dated list per trading day — history browsable in-app
            wl = f"ORB {now_et():%m/%d}"
            res = rh.sync_watchlist(wl, [p["symbol"] for p in picks])
            journal.event("rh.watchlist", f"synced Robinhood watchlist "
                          f"'{wl}': added {res['added'] or 'none'}, "
                          f"removed {res['removed'] or 'none'}")
        except Exception as e:
            journal.event("rh.error",
                          f"watchlist sync failed: {str(e)[:120]}")
    return picks


def opening_range(symbol, day):
    """(high, low, avg 1-min volume) of the 9:30-9:35 range."""
    if rh.available():  # consolidated bars beat the sparse IEX feed
        try:
            rb = rh.bars(symbol, at(day, 9, 30).isoformat(),
                         at(day, 9, 35).isoformat())
            if rb:
                vols = [float(b.get("volume") or 0) for b in rb]
                return (max(float(b["high"]) for b in rb),
                        min(float(b["low"]) for b in rb),
                        sum(vols) / len(vols) if any(vols) else None)
        except Exception as e:
            journal.event("rh.error", f"{symbol}: RH bars failed, trying "
                          f"IEX: {str(e)[:100]}", symbol=symbol)
    bars = scanner.api(f"/v2/stocks/{symbol}/bars", timeframe="1Min",
                       start=at(day, 9, 30).isoformat(),
                       end=at(day, 9, 35).isoformat(),
                       feed="iex", limit=10).get("bars") or []
    if not bars:
        return None
    vols = [b.get("v") or 0 for b in bars]
    return (max(b["h"] for b in bars), min(b["l"] for b in bars),
            sum(vols) / len(vols) if any(vols) else None)


_rh_down_logged = False


def last_price(symbol):
    """Robinhood NBBO last (consolidated, fresh) with IEX fallback."""
    global _rh_down_logged
    if rh.available():
        try:
            px = rh.last_price(symbol)
            if px:
                _rh_down_logged = False
                return px
        except Exception as e:
            if not _rh_down_logged:
                journal.event("rh.error", "robinhood quote failed, falling "
                              f"back to IEX: {str(e)[:120]}")
                _rh_down_logged = True
    t = scanner.api(f"/v2/stocks/{symbol}/trades/latest",
                    feed="iex").get("trade")
    return t["p"] if t else None


# --- shadow variants: same signals, different rules, virtual fills ------
# The live strategy (5-min ORB, half@1R/half@2R) trades the paper account;
# these run on identical real-time quotes with simulated fills so results
# are attributable per-variant. Completed trades -> variants.csv + journal.
LIVE_VOL_X = 2.0  # live entries need the breakout minute >= 2x OR volume
# (backtest 92 gap days: this filter took win rate 58% -> 80%, avg R
# +0.07 -> +0.44 — the volume-confirmed variant was promoted to live on
# 2026-07-22 and the unfiltered rule demoted to shadow "orb5-plain")

SHADOW_VARIANTS = {
    "orb5-plain":  {"range_end": (9, 35), "partial": True},  # old live rule
    "orb5-full2R": {"range_end": (9, 35), "partial": False},
    "orb15":       {"range_end": (9, 45), "partial": True},
    # Ross's bread-and-butter: don't chase the break — buy the first dip
    # back to the range high, invalidate if the dip loses the range low
    "orb5-dip":    {"range_end": (9, 35), "partial": True, "dip": True},
    # --- 2026-08-12 experiments: the audit found entry mechanics (not
    # selection or the filters) are the weak link. These probe fixes:
    # like live volx2 but fill at a range-high limit instead of chasing
    # the post-confirmation print (the $0.04 that cost us TP1 on WLDS)
    "volx2-nochase": {"range_end": (9, 35), "partial": True,
                      "vol_x": 2.0, "nochase": True},
    # looser volume gate — does 1.5x keep most of the protection while
    # letting live actually participate (2x traded once in two weeks)?
    "volx2-1.5x":    {"range_end": (9, 35), "partial": True, "vol_x": 1.5},
    # the TRUE Ross micro-pullback: dip below the range high then buy the
    # reclaim, stop at the pullback low (tighter than orb5-dip's OR-low)
    "micro-dip":     {"range_end": (9, 35), "partial": True,
                      "dip": True, "micro": True},
}
SHADOW_SLIP = 0.02          # assumed fill slippage vs trigger print
SHADOW_RISK = 100.0         # same $ risk as live, for comparable P&L
VARIANTS_CSV = Path(__file__).parent / "variants.csv"

# --- hod-dip: intraday HOD-momo alerts + micro-pullback entry -----------
# Second radar layer (Ross's "HOD Momo" scanner). The RH server-side scan
# "Cameron HOD" flags $2-20 names +10% on the day that just moved +3% in
# 5 minutes. Backtest (508 spike days): pullback entries on these alerts
# went 19 trades / 74% win / +4.8R while box-breakout entries on the SAME
# alerts made nothing — so this strategy buys the first dip, never the
# breakout. It places REAL paper orders (client_order_id prefix "hod-")
# so bracket mechanics/fills/slippage are exercised for real; risk shares
# the account-wide -$300 breaker but has its own entry budget.
HOD_VARIANT = "hod-dip"
HOD_SCAN_EVERY_S = 300      # poll the server-side scan every 5 min
HOD_START_HM = (9, 40)      # let the open shake out first
HOD_LAST_HM = (10, 30)      # no new alerts after this (cutoff needs runway)
HOD_MAX_ALERTS = 6
HOD_MAX_ENTRIES = 2         # real-order budget per day
HOD_DIP_WINDOW_MIN = 30     # minutes to find the pullback entry
HOD_DIP_MAX_FRAC = 0.15     # abandon if the dip exceeds 15% off the high


def _variants_csv(row):
    import csv
    new = not VARIANTS_CSV.exists()
    with open(VARIANTS_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "variant", "symbol",
                                          "entry", "exit_kind", "pnl", "r"])
        if new:
            w.writeheader()
        w.writerow(row)


def shadow_arm(day, symbols, cfg):
    armed = {}
    for sym in symbols:
        try:
            rb = rh.bars(sym, at(day, 9, 30).isoformat(),
                         at(day, *cfg["range_end"]).isoformat())
        except Exception:
            rb = []
        if not rb:
            continue
        hi = max(float(b["high"]) for b in rb)
        lo = min(float(b["low"]) for b in rb)
        if (hi - lo) > hi * RANGE_MAX_FRAC:
            continue
        vols = [float(b.get("volume") or 0) for b in rb]
        armed[sym] = {"hi": hi, "lo": lo,
                      "avg_vol": sum(vols) / len(vols) if any(vols) else None}
    return armed


def _breakout_vol(sym, avg_vol):
    """Measured breakout-minute volume vs the opening-range average.
    Returns (recent, avg_vol, nbars, why): `recent` is the max 1-min
    volume in the last 2 completed bars, or None when the datum is
    missing (`why` says which case). Pure measurement — no decision."""
    if not avg_vol:
        return (None, avg_vol, 0, "no_avg_vol")
    try:
        end = now_et()
        rb = rh.bars(sym, (end - timedelta(minutes=3)).isoformat(),
                     end.isoformat())
        if not rb:
            return (None, avg_vol, 0, "no_bars")
        recent = max(float(b.get("volume") or 0) for b in rb[-2:])
        return (recent, avg_vol, len(rb), "ok")
    except Exception as e:
        return (None, avg_vol, 0, f"err:{str(e)[:40]}")


def _breakout_vol_ok(sym, avg_vol, mult):
    recent, avg_vol, _, _ = _breakout_vol(sym, avg_vol)
    return recent is not None and recent >= mult * avg_vol


def _dip_signal(bars):
    """Micro-pullback rule (same as backtest_hod_dip): after the alert,
    wait for the first red/lower-low bar, track the pullback low, enter
    when a green bar reclaims the prior bar's high. bars = completed
    1-min bars since the alert. Returns ("trade", entry, stop),
    ("veto", why) or None (keep waiting)."""
    if len(bars) < 2:
        return None
    ref_hi = float(bars[0]["high"])
    pull_low = None
    prev = bars[0]
    for b in bars[1:]:
        hi, lo = float(b["high"]), float(b["low"])
        o, c = float(b["open"]), float(b["close"])
        ref_hi = max(ref_hi, hi)
        if pull_low is None:
            if c < o or lo < float(prev["low"]):
                pull_low = lo
        else:
            pull_low = min(pull_low, lo)
            if pull_low < ref_hi * (1 - HOD_DIP_MAX_FRAC):
                return ("veto", "dip_too_deep")
            if hi > float(prev["high"]) and c > o:
                entry = round(min(hi, float(prev["high"]) + 0.01)
                              + SHADOW_SLIP, 2)
                stop = round(max(pull_low, entry - STOP_DIST_MAX), 2)
                if entry - stop < executor.MIN_STOP_DIST:
                    return ("veto", "stop_too_tight")
                return ("trade", entry, stop)
        prev = b
    return None


def _completed_bars(sym, start):
    """1-min bars from start to the last COMPLETED minute (the forming
    bar would make the dip signal flap)."""
    cut = now_et().replace(second=0, microsecond=0)
    rb = rh.bars(sym, start.isoformat(), now_et().isoformat())
    out = []
    for b in rb:
        ts = (b.get("begins_at") or "").replace("Z", "+00:00")
        try:
            if datetime.fromisoformat(ts) < cut:
                out.append(b)
        except ValueError:
            continue
    return out


def _shadow_close(name, sym, pos, price, kind, day):
    pnl = pos["realized"] + (price - pos["entry"]) * pos["qty_open"]
    r = pnl / (pos["risk_ps"] * pos["qty"])
    row = {"date": str(day), "variant": name, "symbol": sym,
           "entry": pos["entry"], "exit_kind": kind,
           "pnl": round(pnl, 2), "r": round(r, 2)}
    journal.event("variant.exit",
                  f"{name} {sym}: {kind} -> ${pnl:+.2f} ({r:+.2f}R)", **row)
    _variants_csv(row)


def try_call(fn, *args):
    """executor.* uses sys.exit on rejects; contain it, return ok flag."""
    try:
        fn(*args)
        return True
    except (SystemExit, RuntimeError, requests.RequestException) as e:
        journal.event("call.failed",
                      f"{getattr(fn, '__name__', fn)} failed: {str(e)[:150]}")
        return False


def run_session(day):
    journal.event("session.start", f"ORB session {day} — strategy config",
                  day=str(day), live="orb5-volx2",
                  live_vol_x=LIVE_VOL_X,
                  hod="hod-dip paper orders (tag hod-, "
                      f"max {HOD_MAX_ENTRIES}/day)",
                  watchlist_size=WATCHLIST_SIZE,
                  max_entries=MAX_ENTRIES_PER_DAY, spread_max=SPREAD_MAX,
                  range_max_frac=RANGE_MAX_FRAC, stop_dist_max=STOP_DIST_MAX,
                  entry_slip=ENTRY_SLIP, cutoff=f"{CUTOFF_HM[0]}:{CUTOFF_HM[1]:02d} ET",
                  risk_per_trade=executor.RISK_PER_TRADE,
                  daily_max_loss=executor.DAILY_MAX_LOSS)
    sleep_until(at(day, 9, 15))
    log("premarket scan...")
    watch = build_watchlist()
    if not watch:
        pos, orders = [], []
        try:
            pos = executor.api("GET", "/v2/positions")
            orders = executor.api("GET", "/v2/orders", status="open",
                                  limit=100)
        except Exception:
            pass
        if not pos and not orders:
            if rh.available():
                # nothing gapped premarket, but the HOD-momo scan can
                # still flag intraday movers — stay for the shadow
                journal.event("session.hodwatch", "empty watchlist — no "
                              "premarket gappers; staying on for HOD-momo "
                              "shadow detection until "
                              f"{HOD_LAST_HM[0]}:{HOD_LAST_HM[1]:02d}")
            else:
                journal.event("session.end", "empty watchlist — nothing "
                              "gapping today, exiting without trading")
                return
        # crash-restart case: the rescan can come up empty while live
        # positions/brackets from the first pass still exist — NEVER
        # abandon them (day orders expire at the close, which would
        # leave a naked overnight position)
        journal.event("session.adopt", f"empty watchlist but {len(pos)} "
                      f"position(s) / {len(orders)} open order(s) at the "
                      "broker — supervising them until cutoff")

    sleep_until(at(day, 9, 36))
    armed = {}
    for p in watch:
        sym = p["symbol"]
        # re-check the spread now that the book is live — premarket
        # NBBO snapshots flicker wildly on these names
        if rh.available():
            try:
                q = rh.quotes([sym]).get(sym) or {}
                if q.get("spread") is not None and q["spread"] > SPREAD_MAX:
                    journal.event("arm.drop", f"{sym}: NBBO spread "
                                  f"${q['spread']} at the open > "
                                  f"${SPREAD_MAX} — book stayed thin",
                                  symbol=sym, reason="spread_at_open",
                                  spread=q["spread"])
                    continue
            except Exception:
                pass  # spread recheck is best-effort
        orange = opening_range(p["symbol"], day)
        if not orange:
            journal.event("arm.drop", f"{p['symbol']}: no 9:30-9:35 bars on "
                          "IEX feed — can't define opening range",
                          symbol=p["symbol"], reason="no_bars")
            continue
        hi, lo, avg_vol = orange
        if (hi - lo) > hi * RANGE_MAX_FRAC:
            journal.event("arm.drop", f"{p['symbol']}: opening range "
                          f"{lo}-{hi} wider than {RANGE_MAX_FRAC:.0%} of "
                          "price — stop would be meaningless",
                          symbol=p["symbol"], reason="range_too_wide",
                          or_high=hi, or_low=lo)
            continue
        armed[sym] = {"hi": hi, "lo": lo, "avg_vol": avg_vol,
                      "broke_at": None}
        journal.event("arm", f"{sym}: armed — break above {hi} must print "
                      f"{LIVE_VOL_X}x OR volume to buy, stop base {lo}",
                      symbol=sym, or_high=hi, or_low=lo, avg_vol=avg_vol)

    # note: entries are poll-triggered (not server-side resting orders)
    # because the volume confirmation needs a decision at trigger time —
    # backtest showed the filter is worth ~10x the polling latency cost
    try:  # adopt any resting entries left over from a crashed pass
        resting = len([o for o in
                       executor.api("GET", "/v2/orders", status="open",
                                    limit=100) if o["side"] == "buy"])
    except Exception:
        resting = 0

    cutoff = at(day, *CUTOFF_HM)
    try:  # restart mid-session: keep managing whatever we already hold
        entries = len(executor.api("GET", "/v2/positions"))
    except Exception:
        entries = 0

    shadow = {}
    hod = None
    if rh.available():
        shadow = {name: {"cfg": cfg, "armed": {}, "open": {},
                         "armed_done": False}
                  for name, cfg in SHADOW_VARIANTS.items()}
        hod = {"alerts": {}, "last_scan": None, "seen": set(), "entries": 0}
    else:
        journal.event("variant.skip", "robinhood unavailable — shadow "
                      "variants disabled today")
    watch_syms = [p["symbol"] for p in watch]

    def shadows_active():
        return any(not st["armed_done"] or st["armed"] or st["open"]
                   for st in shadow.values())

    def hod_active():
        return hod is not None and (hod["alerts"] or hod["entries"]
                                    or now_et() < at(day, *HOD_LAST_HM))

    def hod_poll():
        """Second radar: poll the server-side HOD-momo scan, then walk
        each alert's minute bars looking for the micro-pullback entry."""
        now = now_et()
        if (now >= at(day, *HOD_START_HM) and now < at(day, *HOD_LAST_HM)
                and len(hod["seen"]) < HOD_MAX_ALERTS
                and (hod["last_scan"] is None or
                     (now - hod["last_scan"]).total_seconds()
                     >= HOD_SCAN_EVERY_S)):
            hod["last_scan"] = now
            try:
                hits = rh.hod_tickers()
            except Exception as e:
                journal.event("rh.error",
                              f"HOD scan failed: {str(e)[:100]}")
                hits = []
            for sym in hits:
                if (sym in hod["seen"] or sym in watch_syms
                        or len(hod["seen"]) >= HOD_MAX_ALERTS):
                    continue
                hod["seen"].add(sym)
                try:
                    q = rh.quotes([sym]).get(sym) or {}
                except Exception:
                    q = {}
                if (q.get("spread") is not None
                        and q["spread"] > SPREAD_MAX):
                    journal.event("hod.skip", f"{sym}: HOD alert but "
                                  f"spread ${q['spread']} > ${SPREAD_MAX}",
                                  symbol=sym, spread=q["spread"])
                    continue
                hod["alerts"][sym] = {"t0": now}
                journal.event("hod.alert", f"{sym}: HOD-momo scan hit "
                              "(+10% day, +3% last 5min) — watching for "
                              "the first pullback", symbol=sym,
                              last=q.get("last"))
        for sym in list(hod["alerts"]):
            a = hod["alerts"][sym]
            age_min = (now - a["t0"]).total_seconds() / 60
            if age_min > HOD_DIP_WINDOW_MIN:
                del hod["alerts"][sym]
                journal.event("hod.expire", f"{sym}: no pullback entry "
                              f"within {HOD_DIP_WINDOW_MIN} min — alert "
                              "expired", symbol=sym)
                continue
            if now.second >= 30 or (now - a["t0"]).total_seconds() < 120:
                continue  # evaluate once per minute, after bars settle
            try:
                bars = _completed_bars(sym, a["t0"] - timedelta(minutes=1))
            except Exception:
                continue
            sig = _dip_signal(bars)
            if sig is None:
                continue
            del hod["alerts"][sym]
            if sig[0] == "veto":
                journal.event("hod.veto", f"{sym}: {sig[1]} — pullback "
                              "setup invalidated", symbol=sym,
                              reason=sig[1])
                continue
            _, entry_sig, stop_sig = sig
            if hod["entries"] >= HOD_MAX_ENTRIES:
                journal.event("hod.skip", f"{sym}: pullback entry signal "
                              f"but daily HOD budget ({HOD_MAX_ENTRIES}) "
                              "used", symbol=sym)
                continue
            try:
                px = last_price(sym)
            except Exception:
                px = None
            if px is None:
                continue
            risk_sig = entry_sig - stop_sig
            if px > entry_sig + 0.5 * risk_sig:
                # bar-close confirmation cost us the location — chasing
                # >0.5R above the reclaim breaks the R math, let it go
                journal.event("hod.veto", f"{sym}: price {px} ran >0.5R "
                              f"past the reclaim entry {entry_sig} while "
                              "confirming — not chasing", symbol=sym,
                              last=px, entry_sig=entry_sig)
                continue
            entry = round(max(px, entry_sig) + ENTRY_SLIP, 2)
            stop = round(max(stop_sig, entry - STOP_DIST_MAX), 2)
            journal.event("hod.trigger", f"{sym}: pullback complete — "
                          f"green bar reclaimed prior high, buy {entry} "
                          f"(reclaim {entry_sig}, last {px}), stop {stop} "
                          "(pullback low)", symbol=sym, last=px,
                          entry=entry, stop=stop, entry_sig=entry_sig)
            if try_call(executor.buy, sym, entry, stop, None, "hod"):
                hod["entries"] += 1

    ticks = 0
    fails = {}
    while now_et() < cutoff and (armed or entries or resting
                                 or shadows_active() or hod_active()):
        ticks += 1
        tick_px = {}

        def get_px(sym):
            if sym not in tick_px:
                try:
                    tick_px[sym] = last_price(sym)
                except Exception:
                    tick_px[sym] = None
            return tick_px[sym]

        for sym in list(armed):
            info = armed[sym]
            try:
                px = last_price(sym)
                tick_px[sym] = px
                fails[sym] = 0
            except (RuntimeError, requests.RequestException) as e:
                fails[sym] = fails.get(sym, 0) + 1
                if fails[sym] in (1, 20):  # log first + escalation, not spam
                    journal.event("poll.error",
                                  f"{sym}: price poll failed x{fails[sym]} "
                                  f"({str(e)[:120]}) — retrying",
                                  symbol=sym, consecutive=fails[sym])
                if fails[sym] >= 40:  # ~10 min dead: give up on this symbol
                    journal.event("arm.drop", f"{sym}: 40 consecutive poll "
                                  "failures — disarming",
                                  symbol=sym, reason="poll_dead")
                    del armed[sym]
                continue
            if px is None:
                continue
            if info["broke_at"] is None:
                if px > info["hi"]:
                    info["broke_at"] = now_et()
                    journal.event("trigger.pending",
                                  f"{sym}: {px} broke OR high {info['hi']} "
                                  "— waiting for the breakout minute to "
                                  "close to confirm volume", symbol=sym,
                                  last=px, or_high=info["hi"])
                continue
            # confirmation phase: let the breakout minute bar complete
            bar_end = (info["broke_at"].replace(second=0, microsecond=0)
                       + timedelta(minutes=1, seconds=5))
            if now_et() < bar_end:
                continue
            del armed[sym]  # decision point — one shot per symbol
            # measure once; the boolean below is byte-identical to
            # _breakout_vol_ok — the extra fields are diagnostics only
            recent, avg_vol, nbars, why = _breakout_vol(sym, info["avg_vol"])
            need = (LIVE_VOL_X * avg_vol) if avg_vol else None
            ratio = (recent / avg_vol) if (recent is not None and avg_vol) \
                else None
            vol_ok = recent is not None and recent >= LIVE_VOL_X * avg_vol
            would_entry = round(px + ENTRY_SLIP, 2)
            if not vol_ok:
                detail = (f"breakout vol {recent:.0f} vs need {need:.0f} "
                          f"({ratio:.2f}x, want {LIVE_VOL_X}x OR avg "
                          f"{avg_vol:.0f})" if recent is not None else
                          f"breakout volume unavailable ({why})")
                journal.event("trigger.veto",
                              f"{sym}: {detail} — false-breakout filter "
                              "says pass", symbol=sym, avg_vol=avg_vol,
                              recent_vol=recent, need=need, ratio=ratio,
                              nbars=nbars, why=why, last=px,
                              would_entry=would_entry, or_high=info["hi"])
                continue
            entry = round(px + ENTRY_SLIP, 2)
            stop = round(max(info["lo"], entry - STOP_DIST_MAX), 2)
            journal.event("trigger",
                          f"{sym}: VOLUME-CONFIRMED break of {info['hi']} "
                          f"({ratio:.2f}x OR avg vol) -> buy {entry} "
                          f"(last {px}+{ENTRY_SLIP}), stop {stop}",
                          symbol=sym, last=px, or_high=info["hi"],
                          or_low=info["lo"], entry=entry, stop=stop,
                          recent_vol=recent, avg_vol=avg_vol, ratio=ratio)
            if try_call(executor.buy, sym, entry, stop):
                entries += 1
            if entries >= MAX_ENTRIES_PER_DAY:
                armed.clear()
                journal.event("session.limit", "max entries "
                              f"({MAX_ENTRIES_PER_DAY}) reached — no "
                              "new arms today")
        if hod is not None:  # second radar: intraday HOD-momo alerts
            try:
                hod_poll()
            except Exception as e:
                journal.event("poll.error",
                              f"hod poll failed: {str(e)[:120]}")
        # --- shadow variants: arm, trigger, manage on the same quotes ---
        for name, st in shadow.items():
            cfg = st["cfg"]
            if (not st["armed_done"] and now_et() >=
                    at(day, *cfg["range_end"]) + timedelta(minutes=1)):
                st["armed"] = shadow_arm(day, watch_syms, cfg)
                st["armed_done"] = True
                journal.event("variant.arm", f"{name}: armed "
                              f"{sorted(st['armed']) or 'nothing'}",
                              variant=name,
                              ranges={s: v for s, v in st["armed"].items()})
            for sym in list(st["armed"]):
                px = get_px(sym)
                info = st["armed"][sym]
                if px is None:
                    continue
                if cfg.get("dip"):
                    if info.get("phase") != "dipwait":
                        if px > info["hi"]:  # break seen -> now want the dip
                            info["phase"] = "dipwait"
                            journal.event("variant.break", f"{name} {sym}: "
                                          f"broke {info['hi']} — waiting "
                                          "for first dip back to range "
                                          "high", variant=name, symbol=sym)
                        continue
                    if px <= info["lo"]:  # dip swallowed the whole range
                        st["armed"].pop(sym)
                        journal.event("variant.skip", f"{name} {sym}: dip "
                                      "lost the range low — setup dead",
                                      variant=name, symbol=sym)
                        continue
                    if cfg.get("micro"):
                        # Ross micro-pullback: dip below the range high, then
                        # buy the reclaim; stop goes at the pullback low
                        if px <= info["hi"]:
                            info["dipped"] = True
                            info["pull_low"] = min(
                                info.get("pull_low", px), px)
                            continue  # still pulling back
                        if not info.get("dipped"):
                            continue  # extended; hasn't pulled back yet
                        # dipped, now reclaiming the range high: buy it
                    else:
                        if px > info["hi"] + 0.01:
                            continue  # still extended; keep waiting
                        # first dip reached the range high: buy it
                elif px <= info["hi"]:
                    continue
                st["armed"].pop(sym)  # one shot, like live
                if cfg.get("vol_x") and not _breakout_vol_ok(
                        sym, info["avg_vol"], cfg["vol_x"]):
                    journal.event("variant.skip", f"{name} {sym}: breakout "
                                  f"volume below {cfg['vol_x']}x OR average "
                                  "— no confirmation", variant=name,
                                  symbol=sym)
                    continue
                base = info["hi"] if cfg.get("nochase") else px
                entry = round(base + SHADOW_SLIP, 2)
                stop_base = info.get("pull_low") if cfg.get("micro") \
                    else info["lo"]
                stop = round(max(stop_base or info["lo"],
                                 entry - STOP_DIST_MAX), 2)
                risk = entry - stop
                if risk < executor.MIN_STOP_DIST:
                    continue
                qty = int(SHADOW_RISK / risk)
                if qty < 2:
                    continue
                st["open"][sym] = {
                    "entry": entry, "stop": stop, "risk_ps": risk,
                    "tp1": round(entry + risk, 2),
                    "tp2": round(entry + 2 * risk, 2),
                    "qty": qty, "qty_open": qty,
                    "realized": 0.0, "half_done": False}
                journal.event("variant.entry", f"{name} {sym}: entry "
                              f"{entry}, stop {stop}, {qty} sh",
                              variant=name, symbol=sym, entry=entry,
                              stop=stop, qty=qty)
            for sym in list(st["open"]):
                pos = st["open"][sym]
                px = get_px(sym)
                if px is None:
                    continue
                if px <= pos["stop"]:
                    _shadow_close(name, sym, st["open"].pop(sym),
                                  pos["stop"], "stop", day)
                elif (cfg.get("partial", True) and not pos["half_done"]
                        and px >= pos["tp1"]):
                    half = pos["qty"] // 2
                    pos["realized"] += (pos["tp1"] - pos["entry"]) * half
                    pos["qty_open"] -= half
                    pos["half_done"] = True
                    pos["stop"] = pos["entry"]
                    journal.event("variant.tp1", f"{name} {sym}: half off "
                                  f"at {pos['tp1']}, stop -> breakeven",
                                  variant=name, symbol=sym)
                elif px >= pos["tp2"]:
                    _shadow_close(name, sym, st["open"].pop(sym),
                                  pos["tp2"], "tp2", day)

        if ticks % MANAGE_EVERY == 0:
            try:
                executor.manage()
            except executor.Breaker:
                journal.event("session.end", "circuit breaker tripped — "
                              "session terminated early")
                return
            except (RuntimeError, requests.RequestException) as e:
                journal.event("poll.error", f"manage failed, will retry: "
                              f"{str(e)[:120]}")
        time.sleep(POLL_SECS)

    journal.event("session.cutoff", "time cutoff reached — flattening "
                  "everything, momentum edge is gone after the open")
    for name, st in shadow.items():  # settle shadow books at last price
        for sym in list(st["open"]):
            pos = st["open"].pop(sym)
            px = None
            try:
                px = last_price(sym)
            except Exception:
                pass
            _shadow_close(name, sym, pos, px or pos["entry"], "cutoff", day)
    try_call(executor.flatten)
    try_call(executor.status)
    journal.event("session.end", "session complete", entries=entries)


def main():
    # single-instance lock: a second copy multiplies every position
    lock = open(Path(__file__).parent / ".autotrader.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another autotrader is already running — refusing to start")
    lock.write(str(os.getpid()))
    lock.flush()

    executor.load_env()
    if "--test" in sys.argv:
        log("TEST MODE: watchlist + bars only, no orders")
        watch = build_watchlist()
        day = now_et().date()
        for p in watch:
            log(f"{p['symbol']} opening range today: {opening_range(p['symbol'], day)}")
        return
    while True:  # one session per trading day, forever
        day = next_session_day()
        for attempt in range(1, 11):  # crash -> retry SAME day until cutoff
            try:
                run_session(day)
                break
            except Exception as e:
                journal.event("session.crash",
                              f"attempt {attempt}: {type(e).__name__}: "
                              f"{str(e)[:200]}")
                if now_et() >= at(day, *CUTOFF_HM) or attempt == 10:
                    journal.event("session.abandon", "past cutoff or too "
                                  "many crashes — flattening, done for today")
                    try_call(executor.flatten)
                    break
                # resting bracket orders (stop+TP) live on the exchange,
                # so a 60s gap is protected — restart the session
                journal.event("session.retry", "brackets remain live "
                              "server-side; restarting session in 60s")
                time.sleep(60)
        # park until past today's cutoff so the next loop picks tomorrow
        sleep_until(at(day, CUTOFF_HM[0], CUTOFF_HM[1]) + timedelta(minutes=5))


if __name__ == "__main__":
    main()
