#!/usr/bin/env python3
"""Offline session replay — run the REAL autotrader session logic against
a virtual clock and a fake broker, in seconds instead of market days.

Every code path the live system has (server-side stop-limit entry, OCO
bracket fills, breakeven move, TP2, cutoff flatten, circuit breaker, all
four shadow variants including the dip state machine) gets exercised by
synthetic price scenarios. Any NameError/AttributeError/logic bug in the
session code surfaces here instead of at 9:36 ET with money on the line.

Usage:  .venv/bin/python sim.py            # run all scenarios
        .venv/bin/python sim.py moonshot   # run one
"""

import sys
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import autotrader
import executor
import journal
import rh

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).parent
TODAY = datetime.now(ET).date()


def at(h, m, s=0):
    return datetime(TODAY.year, TODAY.month, TODAY.day, h, m, s, tzinfo=ET)


# --- fake broker: bracket + OCO mechanics ------------------------------

class Broker:
    def __init__(self):
        self.orders = []
        self.positions = {}          # sym -> {qty, avg}
        self.realized = 0.0
        self.prices = {}
        self.seq = 0
        self.clock = at(9, 0)

    def _oid(self):
        self.seq += 1
        return f"sim-{self.seq:04d}"

    def equity(self):
        unreal = sum((self.prices.get(s, p["avg"]) - p["avg"]) * p["qty"]
                     for s, p in self.positions.items())
        return 100_000 + self.realized + unreal

    def api(self, method, path, **payload):
        now = self.clock.astimezone(timezone.utc).isoformat()
        if method == "GET" and path == "/v2/account":
            return {"equity": str(self.equity()),
                    "last_equity": "100000", "buying_power": "400000"}
        if method == "GET" and path == "/v2/positions":
            return [{"symbol": s, "qty": str(p["qty"]),
                     "avg_entry_price": str(p["avg"]),
                     "current_price": str(self.prices.get(s, p["avg"])),
                     "unrealized_pl": str(round(
                         (self.prices.get(s, p["avg"]) - p["avg"])
                         * p["qty"], 2))}
                    for s, p in self.positions.items() if p["qty"] > 0]
        if method == "GET" and path == "/v2/orders":
            want = payload.get("status", "open")
            out = []
            for o in self.orders:
                live = o["status"] in ("new", "held")
                if (want == "all" or (want == "open" and live)
                        or (want == "closed" and not live)):
                    out.append(dict(o))  # copies: real API returns fresh JSON
            return out
        if method == "POST" and path == "/v2/orders":
            cid = payload.get("client_order_id")
            if cid and any(o.get("client_order_id") == cid
                           for o in self.orders):
                raise RuntimeError(f"API 422: duplicate client_order_id {cid}")
            gid = self._oid()
            parent = {"id": gid, "group": gid, "role": "parent",
                      "symbol": payload["symbol"], "side": "buy",
                      "type": payload["type"], "qty": payload["qty"],
                      "limit_price": payload.get("limit_price"),
                      "stop_price": payload.get("stop_price"),
                      "status": "new", "filled_qty": "0",
                      "filled_avg_price": None, "filled_at": None,
                      "submitted_at": now,
                      "client_order_id": cid}
            self.orders.append(parent)
            tp = payload.get("take_profit") or {}
            sl = payload.get("stop_loss") or {}
            self.orders.append({"id": self._oid(), "group": gid,
                                "role": "tp", "symbol": payload["symbol"],
                                "side": "sell", "type": "limit",
                                "qty": payload["qty"],
                                "limit_price": tp.get("limit_price"),
                                "stop_price": None, "status": "held",
                                "filled_qty": "0", "filled_avg_price": None,
                                "filled_at": None, "submitted_at": now})
            self.orders.append({"id": self._oid(), "group": gid,
                                "role": "sl", "symbol": payload["symbol"],
                                "side": "sell", "type": "stop",
                                "qty": payload["qty"], "limit_price": None,
                                "stop_price": sl.get("stop_price"),
                                "status": "held", "filled_qty": "0",
                                "filled_avg_price": None, "filled_at": None,
                                "submitted_at": now})
            return dict(parent)
        if method == "PATCH" and path.startswith("/v2/orders/"):
            oid = path.rsplit("/", 1)[1]
            for o in self.orders:
                if o["id"] == oid and o["status"] in ("new", "held"):
                    o.update({k: str(v) for k, v in payload.items()})
                    return dict(o)
            raise RuntimeError("API 404: order not found or not live")
        if method == "DELETE" and path == "/v2/orders":
            for o in self.orders:
                if o["status"] in ("new", "held"):
                    o["status"] = "canceled"
            return {}
        if method == "DELETE" and path.startswith("/v2/positions/"):
            sym = path.rsplit("/", 1)[1]
            self._close(sym, self.prices.get(sym), "market-flatten")
            return {}
        raise RuntimeError(f"sim broker: unhandled {method} {path}")

    def _fill(self, o, price):
        now = self.clock.astimezone(timezone.utc).isoformat()
        o.update(status="filled", filled_qty=o["qty"],
                 filled_avg_price=str(price), filled_at=now)
        qty = int(o["qty"])
        sym = o["symbol"]
        if o["side"] == "buy":
            p = self.positions.setdefault(sym, {"qty": 0, "avg": 0.0})
            p["avg"] = (p["avg"] * p["qty"] + price * qty) / (p["qty"] + qty)
            p["qty"] += qty
            for leg in self.orders:  # activate this bracket's exits
                if leg.get("group") == o["group"] and leg["role"] != "parent":
                    leg["status"] = "new" if leg["role"] == "tp" else "held"
        else:
            p = self.positions.get(sym)
            if p and p["qty"] > 0:
                take = min(qty, p["qty"])
                self.realized += (price - p["avg"]) * take
                p["qty"] -= take
            for leg in self.orders:  # OCO: cancel the sibling
                if (leg.get("group") == o.get("group")
                        and leg["id"] != o["id"]
                        and leg["role"] != "parent"
                        and leg["status"] in ("new", "held")):
                    leg["status"] = "canceled"

    def _close(self, sym, price, why):
        p = self.positions.get(sym)
        if p and p["qty"] > 0 and price:
            self.realized += (price - p["avg"]) * p["qty"]
            p["qty"] = 0

    def step(self):
        """Process fills against current prices."""
        for o in list(self.orders):
            px = self.prices.get(o["symbol"])
            if px is None or o["status"] not in ("new", "held"):
                continue
            if o["role"] == "parent" and o["side"] == "buy":
                if o["type"] == "stop_limit" and o["status"] == "new":
                    trig, lim = float(o["stop_price"]), float(o["limit_price"])
                    if px >= trig and px <= lim:
                        self._fill(o, max(trig, min(px, lim)))
                elif o["type"] == "limit" and px <= float(o["limit_price"]):
                    self._fill(o, px)
            elif o["role"] == "tp" and o["status"] == "new":
                if px >= float(o["limit_price"]):
                    self._fill(o, float(o["limit_price"]))
            elif o["role"] == "sl" and o["status"] in ("new", "held"):
                # OCO stop is armed once the parent has filled
                parent = next(x for x in self.orders
                              if x["group"] == o["group"]
                              and x["role"] == "parent")
                if parent["status"] == "filled" and px <= float(o["stop_price"]):
                    self._fill(o, float(o["stop_price"]))


# --- virtual market: minute bars -> price path -------------------------

def make_bars(specs):
    """specs: [(min_from_930, o, h, l, c, v), ...] -> normalized bars."""
    out = []
    for m, o, h, lo, c, v in specs:
        out.append({"open": o, "high": h, "low": lo, "close": c,
                    "volume": v,
                    "begins_at": at(9, 30).replace(tzinfo=None).isoformat()
                    and (at(9, 30) + timedelta(minutes=m))
                    .astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "_t": at(9, 30) + timedelta(minutes=m)})
    return out


class Market:
    def __init__(self, broker, bars_by_sym):
        self.broker = broker
        self.bars = bars_by_sym

    def price_at(self, sym, t):
        path_px = None
        for b in self.bars.get(sym, []):
            dt = (t - b["_t"]).total_seconds()
            if dt < 0:
                break
            if dt >= 60:
                path_px = b["close"]
                continue
            seq = ([b["open"], b["high"], b["low"], b["close"]]
                   if b["close"] >= b["open"] else
                   [b["open"], b["low"], b["high"], b["close"]])
            path_px = seq[min(3, int(dt // 15))]
        return path_px

    def advance_to(self, target):
        t = self.broker.clock
        while t < target:
            t = min(t + timedelta(seconds=15), target)
            self.broker.clock = t
            for sym in self.bars:
                px = self.price_at(sym, t)
                if px is not None:
                    self.broker.prices[sym] = px
            self.broker.step()


# --- wire the real code to the fake world ------------------------------

def run_scenario(name, bars_by_sym, watch_rows, hod_syms=()):
    print(f"\n{'='*62}\n SCENARIO: {name}\n{'='*62}")
    broker = Broker()
    market = Market(broker, {s: make_bars(b) for s, b in bars_by_sym.items()})

    journal.PATH = ROOT / "sim_events.jsonl"
    journal.PATH.write_text("")  # fresh event log per scenario
    autotrader.VARIANTS_CSV = ROOT / "sim_variants.csv"
    autotrader.now_et = lambda: broker.clock
    autotrader.sleep_until = market.advance_to
    _time.sleep = lambda s: market.advance_to(
        broker.clock + timedelta(seconds=s))
    autotrader.last_price = lambda sym: broker.prices.get(sym)
    autotrader.build_watchlist = lambda: watch_rows
    executor.api = broker.api
    rh.available = lambda: True
    rh.sync_watchlist = lambda *a, **k: {"added": [], "removed": []}
    # HOD-momo scan: scenario symbols appear once the spike is underway
    rh.hod_tickers = lambda: (list(hod_syms)
                              if broker.clock >= at(9, 45) else [])
    rh.quotes = lambda syms: {
        s: {"bid": (broker.prices.get(s) or 1) - 0.01,
            "ask": (broker.prices.get(s) or 1) + 0.01,
            "last": broker.prices.get(s), "spread": 0.02} for s in syms}

    def _bars(sym, start_iso, end_iso, interval="minute"):
        s = datetime.fromisoformat(start_iso)
        e = min(datetime.fromisoformat(end_iso), broker.clock)
        return [b for b in market.bars.get(sym, [])
                if s <= b["_t"] and b["_t"] + timedelta(seconds=60) <= e]
    rh.bars = _bars

    try:
        autotrader.run_session(TODAY)
        ok = True
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"*** SCENARIO CRASHED: {type(exc).__name__}: {exc}")
        ok = False
    live = [o for o in broker.orders if o["status"] in ("new", "held")]
    open_pos = {s: p for s, p in broker.positions.items() if p["qty"] > 0}
    print(f"-> equity ${broker.equity():,.2f}  realized ${broker.realized:+.2f}"
          f"  open orders {len(live)}  open positions {len(open_pos)}")
    assert not open_pos, f"POSITIONS LEFT OPEN AFTER SESSION: {open_pos}"
    assert not live, f"LIVE ORDERS LEFT AFTER SESSION: {live}"
    if hod_syms:  # hod-dip must have placed REAL (fake-broker) orders
        import json
        evs = [json.loads(x) for x in
               journal.PATH.read_text().splitlines()]
        trig = [e for e in evs if e["type"] == "hod.trigger"]
        sized = [e for e in evs if e["type"] == "trade.sizing"
                 and (e.get("data") or {}).get("tag") == "hod"]
        fills = [o for o in broker.orders if o["status"] == "filled"
                 and o["symbol"] in hod_syms]
        assert trig, "HOD alert never produced a pullback trigger"
        assert sized, "hod trigger placed no order"
        assert fills, "hod orders never filled in the sim market"
        print(f"-> hod-dip: {trig[0]['msg']}")
        print(f"-> hod-dip fills: "
              + ", ".join(f"{o['role']} {o['qty']}@{o['filled_avg_price']}"
                          for o in fills))
    return ok


def w(sym, price):
    return [{"symbol": sym, "price": price, "gap_pct": 40.0, "rvol": 20.0,
             "volume": 500000, "spread": 0.03, "float": 5_000_000,
             "news": "sim"}]


BASE = [(0, 2.90, 2.94, 2.88, 2.92, 9000), (1, 2.92, 2.93, 2.89, 2.90, 6000),
        (2, 2.90, 2.94, 2.90, 2.93, 7000), (3, 2.93, 2.94, 2.91, 2.92, 5000),
        (4, 2.92, 2.94, 2.90, 2.91, 6000)]  # OR = 2.88-2.94

FLAT = [(m, 2.90, 2.92, 2.88, 2.90, 3000) for m in range(5, 90)]

SCENARIOS = {
    # break out, then collapse through the stop
    "stopout": {"SIMA": BASE + [
        (6, 2.93, 2.99, 2.92, 2.97, 30000), (8, 2.97, 2.98, 2.80, 2.82, 40000),
        (10, 2.82, 2.84, 2.35, 2.40, 60000)] +
        [(m, 2.40, 2.42, 2.38, 2.40, 5000) for m in range(12, 90)]},
    # break out and run through TP1 (breakeven move) and TP2
    "moonshot": {"SIMA": BASE + [
        (6, 2.93, 2.99, 2.92, 2.98, 30000), (8, 2.98, 3.10, 2.97, 3.08, 40000),
        (10, 3.08, 3.30, 3.05, 3.28, 50000),
        (12, 3.28, 3.60, 3.25, 3.55, 60000)] +
        [(m, 3.55, 3.58, 3.52, 3.55, 8000) for m in range(14, 90)]},
    # break, dip back to the range high (orb5-dip entry), then rally
    "dip-rally": {"SIMA": BASE + [
        (6, 2.93, 3.02, 2.92, 3.01, 30000), (8, 3.01, 3.02, 2.94, 2.95, 15000),
        (10, 2.95, 3.05, 2.94, 3.04, 25000),
        (12, 3.04, 3.25, 3.03, 3.22, 40000)] +
        [(m, 3.22, 3.25, 3.18, 3.20, 9000) for m in range(14, 90)]},
    # break then chop sideways into the 11:00 cutoff flatten
    "chop-cutoff": {"SIMA": BASE + [
        (6, 2.93, 2.99, 2.92, 2.97, 30000)] +
        [(m, 2.96, 2.99, 2.93, 2.96, 4000) for m in range(7, 90)]},
    # never breaks the range: nothing should trade, cutoff clean
    "no-trigger": {"SIMA": BASE + FLAT},
    # breaks the range on WEAK volume: live must veto (shadows may trade)
    "weak-volume": {"SIMA": BASE + [
        (6, 2.93, 2.99, 2.92, 2.97, 4000)] +
        [(m, 2.96, 2.99, 2.93, 2.96, 3000) for m in range(7, 90)]},
    # watchlist symbol never triggers, but an off-watchlist name spikes
    # intraday: HOD-momo alert -> pullback -> reclaim -> hod-dip shadow
    # rides it to TP2 (exercises the whole second-radar path)
    "hod-alert": {"SIMA": BASE + FLAT, "HODX":
        [(m, 5.00, 5.02, 4.98, 5.00, 3000) for m in range(0, 15)] + [
        (15, 5.00, 5.60, 5.00, 5.55, 50000),   # burst (scan hit at 9:45)
        (16, 5.55, 5.70, 5.50, 5.65, 40000),
        (17, 5.65, 5.66, 5.40, 5.45, 20000),   # first red bar: dip starts
        (18, 5.45, 5.50, 5.35, 5.40, 15000),   # pullback low 5.35
        (19, 5.40, 5.60, 5.38, 5.58, 30000),   # green reclaim: entry
        (20, 5.58, 5.75, 5.55, 5.72, 35000),   # through TP1
        (21, 5.72, 5.80, 5.70, 5.78, 25000),
        (22, 5.78, 5.95, 5.75, 5.90, 30000)] + # through TP2
        [(m, 5.88, 5.92, 5.85, 5.88, 8000) for m in range(23, 90)]},
}

HOD_SCENARIOS = {"hod-alert": ["HODX"]}


if __name__ == "__main__":
    picks = sys.argv[1:] or list(SCENARIOS)
    results = {}
    for name in picks:
        bars = dict(SCENARIOS[name])
        sym = next(iter(bars))
        results[name] = run_scenario(name, bars, w(sym, 2.92),
                                     hod_syms=HOD_SCENARIOS.get(name, ()))
    print("\n" + "=" * 62)
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not all(results.values()):
        sys.exit(1)
