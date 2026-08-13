#!/usr/bin/env python3
"""Paper-trading executor — Ross Cameron style trade management.

Usage:
  executor.py buy SYMBOL ENTRY STOP   # split bracket entry (half 1R, half 2R)
  executor.py manage                  # move stop to breakeven after first half exits
  executor.py status                  # account, positions, open orders, daily P&L
  executor.py flatten                 # KILL SWITCH: cancel all orders, close all positions

Risk model: fixed $ risk per trade. shares = RISK_PER_TRADE / (entry - stop),
split into two bracket orders: TP1 at entry+1R, TP2 at entry+2R. After TP1
fills, `manage` patches the remaining stop leg to breakeven.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import truststore

truststore.inject_into_ssl()  # corporate TLS proxy: trust system keychain CAs

import requests

import journal

ROOT = Path(__file__).parent

# Multi-account support: an extra instance sets CAMERON_ACCOUNT (key-env
# suffix, e.g. "_15X") and CAMERON_INSTANCE (isolated state-file suffix).
# Both default to "" so the primary volx2 instance is unchanged.
ACCOUNT = os.environ.get("CAMERON_ACCOUNT", "")
INSTANCE = os.environ.get("CAMERON_INSTANCE", "")


def reject(reason, **data):
    journal.event("order.reject", reason, **data)
    sys.exit(reason)

# --- risk config -------------------------------------------------------
RISK_PER_TRADE = 100.0      # $ lost if full position stops out
DAILY_MAX_LOSS = 300.0      # circuit breaker: flatten + refuse new entries
MAX_POSITIONS = 3           # 2 ORB slots + 1 for the hod-dip second radar
MAX_SHARES = 4000           # sanity cap for tight stops
MIN_STOP_DIST = 0.05        # reject stops tighter than this (noise)


def load_env():
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def api(method, path, **payload):
    base = os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
    r = requests.request(
        method, f"{base}{path}",
        headers={
            "APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID" + ACCOUNT],
            "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY" + ACCOUNT],
        },
        json=payload if method in ("POST", "PATCH") else None,
        params=payload if method == "GET" else None,
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"API {r.status_code}: {r.text}")
    return r.json() if r.text else {}


class Breaker(Exception):
    """Daily max loss hit — distinct from transient errors."""


BASELINE = ROOT / (".day_baseline" + INSTANCE + ".json")


def daily_pnl():
    """Daily P&L vs a TRUSTWORTHY baseline. Alpaca's last_equity has been
    observed as 0 after paper-account maintenance, which would blind the
    circuit breaker — so we snapshot our own baseline at the first check
    of each day and use it whenever last_equity is implausible."""
    a = api("GET", "/v2/account")
    eq = float(a["equity"])
    last = float(a.get("last_equity") or 0)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snap = {}
    try:
        snap = json.loads(BASELINE.read_text())
    except (OSError, ValueError):
        pass
    if snap.get("date") != today:
        snap = {"date": today, "equity": eq}
        try:
            BASELINE.write_text(json.dumps(snap))
        except OSError:
            pass
    plausible = eq * 0.5 <= last <= eq * 2
    if not plausible:
        journal.event("account.anomaly",
                      f"last_equity {last} implausible vs equity {eq} — "
                      f"breaker baseline from local snapshot "
                      f"{snap['equity']}", last_equity=last, equity=eq,
                      baseline=snap["equity"])
    return eq - (last if plausible else snap["equity"]), a


def circuit_breaker():
    """Returns account if trading is allowed; hard-exits otherwise."""
    pnl, a = daily_pnl()
    if pnl <= -DAILY_MAX_LOSS:
        journal.event("breaker.trip",
                      f"daily P&L ${pnl:.2f} <= -${DAILY_MAX_LOSS} — "
                      "flattening and refusing new entries",
                      pnl=round(pnl, 2), limit=DAILY_MAX_LOSS)
        flatten()
        raise Breaker(f"daily P&L ${pnl:.2f}")
    return a


def buy(symbol, entry, stop, trigger=None, tag="orb"):
    """Split-bracket entry. With trigger set, places a resting stop-limit
    (server-side breakout trigger at `trigger`, fill capped at `entry`) —
    no polling latency. Without it, a plain marketable limit.
    `tag` prefixes client_order_ids so strategies stay attributable on
    the shared paper account (orb-* = ORB live, hod-* = HOD second radar)."""
    if entry <= stop:
        reject("entry must be above stop (long-only strategy)",
               symbol=symbol, entry=entry, stop=stop)
    risk = entry - stop
    if risk < MIN_STOP_DIST:
        reject(f"{symbol}: stop too tight ({risk:.3f} < {MIN_STOP_DIST}) — "
               "noise would stop us out", symbol=symbol, risk=round(risk, 3))

    circuit_breaker()
    positions = api("GET", "/v2/positions")
    if len(positions) >= MAX_POSITIONS:
        reject(f"{symbol}: already holding {len(positions)} positions "
               f"(max {MAX_POSITIONS})", symbol=symbol)
    if any(p["symbol"] == symbol for p in positions):
        reject(f"already in {symbol} — no adding", symbol=symbol)

    shares = min(int(RISK_PER_TRADE / risk), MAX_SHARES)
    half = shares // 2
    if half < 1:
        reject(f"{symbol}: position too small to split", symbol=symbol)

    legs = [("TP1", half, round(entry + risk, 2)),          # 1R
            ("TP2", shares - half, round(entry + 2 * risk, 2))]  # 2R
    journal.event("trade.sizing",
                  f"{symbol}: risk ${RISK_PER_TRADE:.0f} / ${risk:.2f} per "
                  f"share = {shares} sh, split {half}/{shares - half} "
                  f"(TP 1R/2R, shared stop {stop})",
                  symbol=symbol, entry=entry, stop=stop, shares=shares,
                  risk_per_share=round(risk, 2), tag=tag)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    for name, qty, target in legs:
        params = dict(
            symbol=symbol, qty=str(qty), side="buy",
            type="stop_limit" if trigger else "limit",
            limit_price=str(entry), time_in_force="day",
            order_class="bracket",
            take_profit={"limit_price": str(target)},
            stop_loss={"stop_price": str(stop)},
            # deterministic id: broker rejects duplicate submission of the
            # same leg on the same day (stray process, retry, crash-replay)
            client_order_id=f"{tag}-{symbol}-{name}-{day}")
        if trigger:
            params["stop_price"] = str(trigger)
        o = api("POST", "/v2/orders", **params)
        journal.event("order.submit",
                      f"{symbol} {name}: buy {qty} limit {entry}, "
                      f"target {target}, stop {stop} [{o['status']}]",
                      symbol=symbol, leg=name, qty=qty, limit=entry,
                      target=target, stop=stop, order_id=o["id"],
                      status=o["status"])


def manage():
    """If a take-profit leg filled today, move remaining stops to breakeven."""
    circuit_breaker()
    positions = {p["symbol"]: p for p in api("GET", "/v2/positions")}
    closed = api("GET", "/v2/orders", status="closed", limit=100)
    tp_filled = {o["symbol"] for o in closed
                 if o.get("filled_at") and o["side"] == "sell"
                 and o["type"] == "limit"
                 and o["filled_at"][:10] == datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    moved = 0
    # bracket stop legs sit in 'held' status, which status=open misses —
    # list everything recent and filter on live-ish statuses ourselves
    live = ("new", "accepted", "held", "partially_filled")
    recent = api("GET", "/v2/orders", status="all", limit=200,
                 after=datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z"))
    for o in recent:
        if o["status"] not in live:
            continue
        sym = o["symbol"]
        if (sym in tp_filled and sym in positions
                and o["type"] == "stop" and o["side"] == "sell"):
            be = round(float(positions[sym]["avg_entry_price"]), 2)
            if float(o["stop_price"]) < be:
                api("PATCH", f"/v2/orders/{o['id']}", stop_price=str(be))
                journal.event("stop.breakeven",
                              f"{sym}: TP1 filled -> stop moved "
                              f"{o['stop_price']} -> {be} (trade is now "
                              "risk-free)", symbol=sym,
                              old_stop=float(o["stop_price"]), new_stop=be)
                moved += 1
    print(f"{moved} stop(s) moved" if moved else "nothing to manage")


def status():
    pnl, a = daily_pnl()
    print(f"equity ${float(a['equity']):,.0f} | daily P&L ${pnl:+,.2f} "
          f"(breaker at -${DAILY_MAX_LOSS:.0f}) | bp ${float(a['buying_power']):,.0f}")
    for p in api("GET", "/v2/positions"):
        print(f"  POS {p['symbol']}: {p['qty']} @ {p['avg_entry_price']} "
              f"(unrealized ${float(p['unrealized_pl']):+.2f})")
    for o in api("GET", "/v2/orders", status="open", limit=100):
        px = o.get("limit_price") or o.get("stop_price") or "mkt"
        print(f"  ORD {o['symbol']}: {o['side']} {o['qty']} {o['type']} @ {px} "
              f"[{o['status']}] {o['id'][:8]}")


def flatten():
    api("DELETE", "/v2/orders")
    closed = []
    for p in api("GET", "/v2/positions"):
        api("DELETE", f"/v2/positions/{p['symbol']}")
        closed.append(f"{p['symbol']} x{p['qty']}")
    journal.event("flatten", "cancelled all orders, closed all positions"
                  + (f": {', '.join(closed)}" if closed else " (was flat)"),
                  closed=closed)


def main():
    load_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    try:
        if cmd == "buy":
            buy(sys.argv[2].upper(), float(sys.argv[3]), float(sys.argv[4]),
                float(sys.argv[5]) if len(sys.argv) > 5 else None)
        elif cmd == "manage":
            manage()
        elif cmd == "status":
            status()
        elif cmd == "flatten":
            flatten()
        else:
            sys.exit(__doc__)
    except (Breaker, RuntimeError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
