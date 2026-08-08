#!/usr/bin/env python3
"""Local trading dashboard — stdlib http.server, no extra deps.

http://localhost:8787 · auto-refreshes every 10s.
Sections: stat tiles, intraday equity line (crosshair+tooltip), shadow
variant comparison (cumulative R), positions/orders/fills/signals tables,
decision journal timeline, log tail.
Palette: dataviz reference instance (validated), light+dark selected.
"""

import csv
import json
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import truststore

truststore.inject_into_ssl()

import requests

import journal
import rh

ET = ZoneInfo("America/New_York")

ROOT = Path(__file__).parent
PORT = 8787


def load_env():
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def alpaca(path, **params):
    base = os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
    r = requests.get(f"{base}{path}", params=params, timeout=15, headers={
        "APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"],
        "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"]})
    r.raise_for_status()
    return r.json()


def today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def portfolio_history():
    try:
        h = alpaca("/v2/account/portfolio/history",
                   period="1D", timeframe="5Min")
        return [{"t": t, "e": e} for t, e in
                zip(h.get("timestamp") or [], h.get("equity") or []) if e]
    except Exception:
        return []


def variants_summary():
    path = ROOT / "variants.csv"
    if not path.exists():
        return []
    agg = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            a = agg.setdefault(r["variant"],
                               {"variant": r["variant"], "trades": 0,
                                "wins": 0, "total_r": 0.0, "total_pnl": 0.0})
            a["trades"] += 1
            rr = float(r["r"] or 0)
            a["wins"] += rr > 0
            a["total_r"] += rr
            a["total_pnl"] += float(r["pnl"] or 0)
    out = sorted(agg.values(), key=lambda a: -a["total_r"])
    for a in out:
        a["total_r"] = round(a["total_r"], 2)
        a["total_pnl"] = round(a["total_pnl"], 2)
    return out


STRATEGIES = [
    {"name": "orb5-volx2", "live": True,
     "desc": "5-min ORB · breakout minute must print 2× volume · half @1R, "
             "half @2R, breakeven stop (promoted 07/22: backtest 80% win "
             "vs 58% unfiltered)"},
    {"name": "orb5-plain", "live": False,
     "desc": "same entries WITHOUT the volume filter — old live rule, "
             "kept as control"},
    {"name": "orb5-full2R", "live": False,
     "desc": "5-min ORB · full position rides to 2R or stop"},
    {"name": "orb15", "live": False,
     "desc": "15-min opening range · half @1R, half @2R"},
    {"name": "orb5-dip", "live": False,
     "desc": "Ross-style: break, then buy the first dip back to range high"},
    {"name": "hod-dip", "live": True, "tag": "hod",
     "desc": "second radar, REAL paper orders: intraday HOD-momo scan "
             "alert (+10% day, +3% in 5min) → buy the first pullback "
             "(backtest: 74% win intraday) · max 2/day"},
]


def strategies_today(events, variants):
    totals = {v["variant"]: v for v in variants}
    out = []
    for s in STRATEGIES:
        n, st = s["name"], dict(s)
        armed, entries, exits = [], [], []
        for e in events:
            d = e.get("data") or {}
            if s["live"]:
                tag = s.get("tag", "orb")
                arm_ev = "hod.alert" if tag == "hod" else "arm"
                if e["type"] == arm_ev and d.get("symbol"):
                    armed.append(d["symbol"])
                elif (e["type"] == "trade.sizing" and d.get("symbol")
                        and d.get("tag", "orb") == tag):
                    entries.append(d["symbol"])
            elif d.get("variant") == n:
                if e["type"] == "variant.arm":
                    armed = sorted((d.get("ranges") or {}).keys())
                elif e["type"] == "variant.entry":
                    entries.append(d.get("symbol"))
                elif e["type"] == "variant.exit":
                    exits.append({"symbol": d.get("symbol"),
                                  "kind": d.get("exit_kind"),
                                  "r": d.get("r")})
        st.update(armed=list(dict.fromkeys(armed)),
                  entries=list(dict.fromkeys(entries)),
                  exits=exits, total=totals.get(n))
        out.append(st)
    return out


# --- funnel: every symbol's journey through today's filters ------------
# stage rank: the further right, the further the symbol got
STAGE_RANK = ["filtered", "expired", "dropped", "scan-pass", "alerted",
              "picked", "armed", "pending", "vetoed", "triggered", "bought"]

FUNNEL_MAP = {  # event type -> (stage, source override)
    "scan.reject": ("filtered", None), "scan.pass": ("scan-pass", None),
    "watchlist.skip": ("filtered", None), "watchlist.add": ("picked", None),
    "arm.drop": ("dropped", None), "arm": ("armed", None),
    "trigger.pending": ("pending", None), "trigger.veto": ("vetoed", None),
    "trigger": ("triggered", None), "trade.sizing": ("bought", None),
    "hod.alert": ("alerted", "hod"), "hod.veto": ("vetoed", "hod"),
    "hod.skip": ("vetoed", "hod"), "hod.expire": ("expired", "hod"),
    "hod.trigger": ("triggered", "hod"),
}


def funnel(events):
    rows = {}
    for e in events:  # chronological: later stages overwrite earlier
        t, d = e["type"], e.get("data") or {}
        sym = d.get("symbol")
        if not sym or t not in FUNNEL_MAP:
            continue
        stage, src = FUNNEL_MAP[t]
        r = rows.setdefault(sym, {"symbol": sym, "source": "gap",
                                  "stage": stage, "reason": "", "ts": ""})
        if src:
            r["source"] = src
        # keep the furthest stage; vetoes/drops overwrite waiting states
        if (STAGE_RANK.index(stage) >= STAGE_RANK.index(r["stage"])
                or stage in ("vetoed", "dropped", "expired")):
            r.update(stage=stage, reason=e["msg"], ts=e["ts"][11:16])
        if t == "watchlist.add":
            r["metrics"] = {k: d.get(k) for k in
                            ("price", "gap_pct", "rvol", "spread", "float")
                            if d.get(k) not in (None, "")}
    out = sorted(rows.values(),
                 key=lambda r: -STAGE_RANK.index(r["stage"]))
    return out


def trades_summary(fills):
    """Per-symbol execution recap from today's actual fills."""
    by = {}
    for o in fills:
        try:
            q = float(o["filled_qty"])
            p = float(o["filled_avg_price"] or 0)
        except (TypeError, ValueError):
            continue
        s = by.setdefault(o["symbol"], {
            "symbol": o["symbol"], "tag": "orb",
            "bought": 0.0, "bavg": 0.0, "sold": 0.0, "savg": 0.0})
        if (o.get("client_order_id") or "").startswith("hod-"):
            s["tag"] = "hod"
        if o["side"] == "buy":
            s["bavg"] = (s["bavg"] * s["bought"] + p * q) / (s["bought"] + q)
            s["bought"] += q
        else:
            s["savg"] = (s["savg"] * s["sold"] + p * q) / (s["sold"] + q)
            s["sold"] += q
    out = []
    for s in by.values():
        closed = min(s["bought"], s["sold"])
        s["realized"] = round((s["savg"] - s["bavg"]) * closed, 2)
        s["open_qty"] = int(s["bought"] - s["sold"])
        for k in ("bavg", "savg"):
            s[k] = round(s[k], 3)
        out.append(s)
    return sorted(out, key=lambda s: -abs(s["realized"]))


def state():
    a = alpaca("/v2/account")
    closed = alpaca("/v2/orders", status="closed", limit=50)
    fills = [o for o in closed
             if (o.get("filled_at") or "")[:10] == today_utc()]
    signals = []
    if (ROOT / "signals.csv").exists():
        with open(ROOT / "signals.csv") as f:
            signals = [r for r in csv.DictReader(f)
                       if r["scanned_at"][:10] == today_utc()][-20:]
    log_tail = ""
    if (ROOT / "autotrader.log").exists():
        log_tail = "\n".join(
            (ROOT / "autotrader.log").read_text().splitlines()[-40:])
    return {
        "asof": datetime.now().strftime("%H:%M:%S"),
        "equity": float(a["equity"]),
        "pnl": float(a["equity"]) - float(a["last_equity"]),
        "total": float(a["equity"]) - 100_000.0,
        "bp": float(a["buying_power"]),
        "positions": alpaca("/v2/positions"),
        "orders": alpaca("/v2/orders", status="open", limit=50),
        "fills": fills,
        "signals": signals,
        "events": journal.today(120),
        "history": portfolio_history(),
        "variants": variants_summary(),
        "log": log_tail,
    }


def state_with_strategies():
    s = state()
    s["strategies"] = strategies_today(s["events"], s["variants"])
    full = journal.today(3000)  # funnel needs the whole morning, not a tail
    s["funnel"] = funnel(full)
    s["trades"] = trades_summary(s["fills"])
    syms = []
    for e in full:
        d = e.get("data") or {}
        if (e["type"] in ("watchlist.add", "trade.sizing", "hod.alert")
                and d.get("symbol")):
            syms.append(d["symbol"])
    syms += [p["symbol"] for p in s["positions"]]
    s["chart_symbols"] = list(dict.fromkeys(syms))
    return s


def chart_data(symbol):
    """1-min candles since 9:30 ET + today's decision levels for overlay."""
    if not rh.available():
        return {"bars": [], "levels": {}, "error": "robinhood not connected"}
    now = datetime.now(ET)
    start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now < start:
        start -= timedelta(days=3 if now.weekday() == 0 else 1)
    bars = []
    try:
        for b in rh.bars(symbol, start.isoformat(), now.isoformat()):
            try:
                bars.append({"t": b.get("begins_at") or b.get("timestamp"),
                             "o": float(b["open"]), "h": float(b["high"]),
                             "l": float(b["low"]), "c": float(b["close"]),
                             "v": float(b.get("volume") or 0)})
            except (KeyError, TypeError, ValueError):
                continue
    except Exception as e:
        return {"bars": [], "levels": {}, "error": str(e)[:200]}
    levels = {}
    story = []
    STORY_TYPES = ("watchlist.add", "watchlist.skip", "arm", "arm.drop",
                   "trigger.pending", "trigger.veto", "trigger",
                   "hod.alert", "hod.veto", "hod.skip", "hod.expire",
                   "hod.trigger", "trade.sizing", "order.submit",
                   "order.reject", "stop.breakeven", "scan.pass",
                   "scan.reject")
    for e in journal.today(3000):
        d = e.get("data") or {}
        if d.get("symbol") != symbol:
            continue
        if e["type"] == "arm":
            levels["OR high"] = d.get("or_high")
            levels["OR low"] = d.get("or_low")
        elif e["type"] == "trade.sizing":
            levels["entry"] = d.get("entry")
            levels["stop"] = d.get("stop")
        elif e["type"] == "stop.breakeven":
            levels["BE stop"] = d.get("new_stop")
        if e["type"] in STORY_TYPES:
            story.append({"ts": e["ts"][11:19], "type": e["type"],
                          "msg": e["msg"]})
    return {"bars": bars, "story": story,
            "levels": {k: v for k, v in levels.items() if v}}


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Cameron — paper trading</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink-2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --baseline:#c3c2b7;
  --border:rgba(11,11,11,.10); --series-1:#2a78d6;
  --good:#006300; --good-mark:#0ca30c; --critical:#d03b3b;
}
@media (prefers-color-scheme: dark) { :root {
  --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink-2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --baseline:#383835;
  --border:rgba(255,255,255,.10); --series-1:#3987e5;
  --good:#0ca30c; --good-mark:#0ca30c; --critical:#d03b3b;
}}
* { box-sizing:border-box; margin:0 }
body { background:var(--page); color:var(--ink); padding:24px;
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
  max-width:1200px; margin:0 auto }
header { display:flex; align-items:baseline; gap:12px; margin-bottom:18px }
h1 { font-size:17px; font-weight:650; letter-spacing:-.01em }
#asof { color:var(--muted); font-size:12px }
.pulse { display:inline-block; width:7px; height:7px; border-radius:50%;
  background:var(--good-mark); margin-right:5px;
  animation:pulse 2.4s ease-out infinite }
@keyframes pulse { 0%{opacity:1} 70%{opacity:.25} 100%{opacity:1} }

.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
  gap:12px; margin-bottom:14px }
.tile { background:var(--surface); border:1px solid var(--border);
  border-radius:10px; padding:14px 16px }
.tile .k { color:var(--ink-2); font-size:12px; font-weight:500 }
.tile .v { font-size:27px; font-weight:650; letter-spacing:-.02em;
  margin-top:3px }
.tile .d { font-size:12px; margin-top:3px; color:var(--muted) }
.up { color:var(--good) } .down { color:var(--critical) }

.row2 { display:grid; grid-template-columns:3fr 2fr; gap:12px;
  margin-bottom:14px }
@media (max-width:900px){ .row2 { grid-template-columns:1fr } }
section { background:var(--surface); border:1px solid var(--border);
  border-radius:10px; padding:14px 16px; margin-bottom:12px; min-width:0 }
section h2 { font-size:11.5px; font-weight:600; color:var(--ink-2);
  text-transform:uppercase; letter-spacing:.05em; margin-bottom:10px }
.chart { position:relative }
.grid { stroke:var(--grid); stroke-width:1 }
.axis { fill:var(--muted); font-size:10.5px }
.tip { position:absolute; background:var(--surface); color:var(--ink);
  border:1px solid var(--border); border-radius:6px; padding:4px 9px;
  font-size:12px; font-variant-numeric:tabular-nums; pointer-events:none;
  box-shadow:0 2px 8px rgba(0,0,0,.12); white-space:nowrap }

.srow { padding:9px 0; border-bottom:1px solid var(--grid) }
.srow:last-child { border-bottom:none }
.shead { display:flex; align-items:baseline; gap:8px; flex-wrap:wrap }
.sname { font:600 12.5px ui-monospace,Menlo,monospace }
.live-badge { font-size:9.5px; font-weight:700; letter-spacing:.06em;
  color:var(--surface); background:var(--series-1); border-radius:4px;
  padding:1.5px 6px; vertical-align:middle }
.sdesc { color:var(--muted); font-size:11.5px }
.sstate { font-size:12px; color:var(--ink-2); margin-top:3px }
.sbar { display:grid; grid-template-columns:1fr 64px 120px; gap:10px;
  align-items:center; margin-top:5px }
.vname { font:12px ui-monospace,Menlo,monospace; color:var(--ink-2);
  overflow:hidden; text-overflow:ellipsis }
.vbar { height:14px; position:relative; background:transparent }
.vbar::before { content:""; position:absolute; left:0; top:0; bottom:0;
  width:2px; background:var(--baseline) }
.vfill { height:100%; border-radius:0 4px 4px 0; min-width:2px }
.vfill.pos { background:var(--series-1) }
.vfill.neg { background:var(--critical) }
.vval { text-align:right; font-weight:600;
  font-variant-numeric:tabular-nums; font-size:13px }
.vmeta { color:var(--muted); font-size:11.5px; text-align:right }

table { width:100%; border-collapse:collapse;
  font-variant-numeric:tabular-nums }
th { text-align:left; color:var(--muted); font-weight:500; font-size:11.5px;
  border-bottom:1px solid var(--grid); padding:3px 10px 6px 0 }
td { padding:6px 10px 6px 0; border-bottom:1px solid var(--grid);
  font-size:13px; vertical-align:top }
tr:last-child td { border-bottom:none }
td.num, th.num { text-align:right }
.empty { color:var(--muted); padding:6px 0; font-size:13px }
.sym { font-weight:600 }
.side-buy { color:var(--good) } .side-sell { color:var(--critical) }

.chip { display:inline-block; font:10.5px ui-monospace,Menlo,monospace;
  color:var(--ink-2); background:var(--page);
  border:1px solid var(--border); border-radius:5px; padding:1px 7px;
  white-space:nowrap }
.chip-warn { color:var(--critical); border-color:var(--critical) }
.chip-trade { color:var(--series-1); border-color:var(--series-1) }
.jrow { display:grid; grid-template-columns:64px 128px 1fr; gap:10px;
  padding:5px 0; border-bottom:1px solid var(--grid); font-size:12.5px }
.jrow:last-child { border-bottom:none }
.jtime { color:var(--muted); font-variant-numeric:tabular-nums }
.jmsg { color:var(--ink); overflow-wrap:anywhere }
.jdata { color:var(--muted); font:11px ui-monospace,Menlo,monospace;
  overflow-wrap:anywhere }
details > summary { cursor:pointer; color:var(--ink-2); font-size:12px;
  user-select:none }
section h3 { font-size:11px; font-weight:600; color:var(--muted);
  text-transform:uppercase; letter-spacing:.05em; margin:14px 0 8px }
.scrolly { max-height:260px; overflow-y:auto }
.stage { display:inline-block; font-size:10.5px; font-weight:700;
  letter-spacing:.04em; border-radius:4px; padding:1.5px 8px;
  white-space:nowrap }
.st-bought,.st-triggered { color:var(--surface); background:var(--good-mark) }
.st-armed,.st-pending,.st-picked,.st-alerted { color:var(--series-1);
  border:1px solid var(--series-1) }
.st-vetoed,.st-dropped { color:var(--critical);
  border:1px solid var(--critical) }
.st-filtered,.st-expired,.st-scan-pass { color:var(--muted);
  border:1px solid var(--border) }
.story-row { display:grid; grid-template-columns:64px 1fr; gap:10px;
  padding:6px 0; border-bottom:1px solid var(--grid); font-size:13px }
.story-row:last-child { border-bottom:none }
.story-good { border-left:3px solid var(--good-mark); padding-left:9px }
.story-bad { border-left:3px solid var(--critical); padding-left:9px }
.story-info { border-left:3px solid var(--baseline); padding-left:9px }
.reason { color:var(--ink-2); font-size:12.5px; overflow-wrap:anywhere }
.tagchip { font:10px ui-monospace,Menlo,monospace; color:var(--muted);
  border:1px solid var(--border); border-radius:4px; padding:0 5px }
a { color:var(--series-1); text-decoration:none }
a:hover { text-decoration:underline }
.symchip { display:inline-block; font:600 12px ui-monospace,Menlo,monospace;
  color:var(--ink-2); background:var(--page); border:1px solid var(--border);
  border-radius:6px; padding:3px 11px; margin-right:6px; cursor:pointer }
.symchip.sel { color:var(--surface); background:var(--series-1);
  border-color:var(--series-1) }
.klink { font-size:12px; color:var(--muted); margin-right:14px }
pre { font:12px/1.55 ui-monospace,Menlo,monospace; color:var(--ink-2);
  white-space:pre-wrap; max-height:280px; overflow-y:auto; margin-top:8px }
#journal { max-height:420px; overflow-y:auto }
</style></head><body>
<header><h1>Cameron · paper trading</h1>
  <span id="asof"><span class="pulse"></span>connecting…</span></header>
<div class="tiles" id="tiles"></div>

<section><h2>① 今日实际操作 · executions &amp; P/L</h2>
  <div id="trades" style="margin-bottom:12px"></div>
  <h3>操作时间轴 — 每一次下单/改单/成交的原因</h3>
  <div id="ops" class="scrolly"></div></section>

<section><h2>② 选股漏斗 · 每只票走到哪一步、为什么</h2>
  <div id="funnel"></div></section>

<section><h2>③ K线 + 决策过程 · 为什么买 / 为什么没买</h2>
  <div id="chips" style="margin-bottom:10px"></div>
  <div class="chart" id="kchart"><div class="empty">waiting for today's
    watchlist…</div></div>
  <div id="klinks" style="margin-top:8px"></div>
  <h3 id="storyh" hidden>这只票的完整决策记录</h3>
  <div id="story"></div></section>

<div class="row2">
  <section><h2>Equity — today (5-min)</h2>
    <div class="chart" id="equity"></div></section>
  <section><h2>Strategies — live + shadow</h2>
    <div id="variants"></div></section>
</div>
<div class="row2">
  <section><h2>Positions</h2><div id="positions"></div></section>
  <section><h2>Open orders</h2><div id="orders"></div></section>
</div>
<section><h2>Today's fills</h2><div id="fills"></div></section>
<section><h2>Scanner signals — today</h2><div id="signals"></div></section>
<section><h2>Decision journal — full firehose</h2>
  <div id="journal"></div></section>
<section><details><summary>Raw process log</summary><pre id="log"></pre>
  </details></section>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const usd = x => (x<0?"\\u2212":"") + "$" + Math.abs(x).toLocaleString(
  undefined, {minimumFractionDigits:2, maximumFractionDigits:2});
const usd0 = x => (x<0?"\\u2212":"") + "$" + Math.abs(x).toLocaleString(
  undefined, {maximumFractionDigits:0});

function table(rows, cols) {
  if (!rows.length) return '<div class="empty">none</div>';
  return '<table><tr>' + cols.map(c =>
      `<th class="${c.num?"num":""}">${c.h}</th>`).join("") + '</tr>' +
    rows.map(r => '<tr>' + cols.map(c =>
      `<td class="${c.num?"num":""}">${c.f(r)}</td>`).join("") +
      '</tr>').join("") + '</table>';
}

function lineChart(el, pts) {
  el.innerHTML = "";
  if (!pts || pts.length < 2) {
    el.innerHTML = '<div class="empty">no intraday equity data yet</div>';
    return;
  }
  const W = Math.max(el.clientWidth, 320), H = 190,
        P = {t:12, r:14, b:24, l:52};
  const xs = pts.map(p=>p.t), ys = pts.map(p=>p.e);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (y0 === y1) { y0 -= 50; y1 += 50; }
  const pad = (y1-y0)*0.10; y0 -= pad; y1 += pad;
  const X = t => P.l + (t-x0)/(x1-x0)*(W-P.l-P.r);
  const Y = v => P.t + (1-(v-y0)/(y1-y0))*(H-P.t-P.b);
  const fmtT = t => new Date(t*1000).toLocaleTimeString("en-US",
    {hour:"numeric", minute:"2-digit", timeZone:"America/New_York"});
  let grid = "";
  for (let i=0; i<=3; i++) {
    const v = y0+(y1-y0)*i/3, y = Y(v);
    grid += `<line class="grid" x1="${P.l}" x2="${W-P.r}" y1="${y}" y2="${y}"/>`
          + `<text class="axis" x="${P.l-7}" y="${y+3.5}" text-anchor="end">${usd0(v)}</text>`;
  }
  const d = pts.map((p,i) =>
    (i?"L":"M") + X(p.t).toFixed(1) + "," + Y(p.e).toFixed(1)).join("");
  const last = pts[pts.length-1];
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}">
    ${grid}
    <line id="xh" class="grid" style="stroke-dasharray:2 3" x1="-9" x2="-9"
      y1="${P.t}" y2="${H-P.b}"/>
    <path d="${d}" fill="none" stroke="var(--series-1)" stroke-width="2"
      stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${X(last.t)}" cy="${Y(last.e)}" r="4"
      fill="var(--series-1)" stroke="var(--surface)" stroke-width="2"/>
    <circle id="xhd" r="4.5" fill="var(--series-1)"
      stroke="var(--surface)" stroke-width="2" opacity="0"/>
    <text class="axis" x="${P.l}" y="${H-7}">${fmtT(pts[0].t)} ET</text>
    <text class="axis" x="${W-P.r}" y="${H-7}" text-anchor="end">${fmtT(last.t)} ET</text>
  </svg><div class="tip" id="eqtip" hidden></div>`;
  const svg = el.querySelector("svg"), tip = el.querySelector("#eqtip"),
        xh = el.querySelector("#xh"), xhd = el.querySelector("#xhd");
  svg.addEventListener("mousemove", ev => {
    const r = svg.getBoundingClientRect();
    const mx = (ev.clientX - r.left) * (W / r.width);
    let best = pts[0], bd = 1e18;
    for (const p of pts) {
      const dd = Math.abs(X(p.t)-mx);
      if (dd < bd) { bd = dd; best = p; }
    }
    const bx = X(best.t);
    xh.setAttribute("x1",bx); xh.setAttribute("x2",bx);
    xhd.setAttribute("cx",bx); xhd.setAttribute("cy",Y(best.e));
    xhd.setAttribute("opacity",1);
    tip.hidden = false;
    tip.textContent = fmtT(best.t) + " ET \\u00b7 " + usd(best.e);
    tip.style.left = Math.min(Math.max(bx/W*r.width - 50, 4),
                              r.width - 150) + "px";
    tip.style.top = "4px";
  });
  svg.addEventListener("mouseleave", () => {
    tip.hidden = true;
    xh.setAttribute("x1",-9); xh.setAttribute("x2",-9);
    xhd.setAttribute("opacity",0);
  });
}

function strategyPanel(el, strats) {
  const max = Math.max(...strats.map(s =>
    Math.abs(s.total ? s.total.total_r : 0)), 0.5);
  el.innerHTML = strats.map(s => {
    let today;
    if (s.exits && s.exits.length)
      today = "today: " + s.exits.map(x =>
        `${esc(x.symbol)} ${esc(x.kind)} ${x.r>=0?"+":"\\u2212"}${Math.abs(x.r).toFixed(1)}R`).join(" \\u00b7 ");
    else if (s.entries && s.entries.length)
      today = "today: in " + s.entries.map(esc).join(", ");
    else if (s.armed && s.armed.length)
      today = "today: armed " + s.armed.map(esc).join(", ") + " \\u2014 waiting for breakout";
    else
      today = "today: idle \\u2014 no signal yet";
    let bar = "";
    if (s.total) {
      const t = s.total, pos = t.total_r >= 0,
            w = Math.abs(t.total_r)/max*100;
      bar = `<div class="sbar">
        <div class="vbar"><div class="vfill ${pos?"pos":"neg"}" style="width:${w}%"></div></div>
        <div class="vval ${pos?"up":"down"}">${pos?"+":"\\u2212"}${Math.abs(t.total_r).toFixed(2)}R</div>
        <div class="vmeta">${t.trades} trades \\u00b7 ${t.wins}W \\u00b7 ${usd0(t.total_pnl)}</div></div>`;
    } else if (!s.live) {
      bar = `<div class="sbar"><div class="vmeta" style="text-align:left">no completed trades yet</div></div>`;
    }
    return `<div class="srow">
      <div class="shead"><span class="sname">${esc(s.name)}</span>
        ${s.live ? '<span class="live-badge">LIVE</span>' : ""}
        <span class="sdesc">${esc(s.desc)}</span></div>
      <div class="sstate">${today}</div>${bar}</div>`;
  }).join("");
}

// --- candlestick chart with decision-level overlays -------------------
let selSym = null, allSyms = [], tickN = 0;
const rhUrl = s => `https://robinhood.com/us/en/stocks/${s}/`;
const tvUrl = s => `https://www.tradingview.com/chart/?symbol=${s}`;
const symLink = s =>
  `<a class="sym" href="${rhUrl(s)}" target="_blank" rel="noopener"
     title="open ${s} on Robinhood">${esc(s)}</a>`;

function renderChips() {
  $("chips").innerHTML = allSyms.map(s =>
    `<span class="symchip ${s===selSym?"sel":""}"
       onclick="loadChart('${esc(s)}')">${esc(s)}</span>`).join("")
    || '<span class="empty">no symbols yet today</span>';
  $("klinks").innerHTML = selSym ? `
    <a class="klink" href="${rhUrl(selSym)}" target="_blank" rel="noopener">Robinhood \\u2197</a>
    <a class="klink" href="${tvUrl(selSym)}" target="_blank" rel="noopener">TradingView \\u2197</a>` : "";
}

const GOOD_EV = /^(trigger|hod\\.trigger|trade\\.sizing|order\\.submit|stop\\.breakeven|watchlist\\.add|arm|scan\\.pass|hod\\.alert|trigger\\.pending)$/;
const BAD_EV = /veto|reject|drop|skip|expire/;

function renderStory(evts) {
  $("storyh").hidden = !evts || !evts.length;
  if (!evts || !evts.length) { $("story").innerHTML = ""; return; }
  $("story").innerHTML = evts.map(e => {
    const cls = BAD_EV.test(e.type) ? "story-bad"
              : GOOD_EV.test(e.type) ? "story-good" : "story-info";
    return `<div class="story-row">
      <div class="jtime">${e.ts.slice(0,5)}Z</div>
      <div class="${cls}"><span class="chip">${esc(e.type)}</span>
        <span class="reason">${esc(e.msg)}</span></div></div>`;
  }).join("");
}

async function loadChart(sym) {
  selSym = sym; renderChips();
  let d;
  try { d = await (await fetch("/api/chart?symbol="+sym)).json(); }
  catch (e) { $("kchart").innerHTML = '<div class="empty">chart fetch failed</div>'; return; }
  candleChart($("kchart"), d.bars, d.levels || {}, d.error);
  renderStory(d.story || []);
}

// --- ① operations: per-symbol executions + action timeline -------------
function renderTrades(el, trades) {
  el.innerHTML = table(trades, [
    {h:"Symbol", f:r=>symLink(r.symbol)},
    {h:"策略", f:r=>`<span class="tagchip">${r.tag==="hod"?"hod-dip":"orb5-volx2"}</span>`},
    {h:"买入", num:1, f:r=>r.bought ? `${r.bought} @ ${usd(r.bavg)}` : "\\u2014"},
    {h:"卖出", num:1, f:r=>r.sold ? `${r.sold} @ ${usd(r.savg)}` : "\\u2014"},
    {h:"still held", num:1, f:r=>r.open_qty || "\\u2014"},
    {h:"已实现盈亏", num:1, f:r=>{const v=r.realized;
      return `<span class="${v>=0?"up":"down"}">${usd(v)}</span>`}}]);
  if (!trades.length)
    el.innerHTML = '<div class="empty">今天还没有任何成交</div>';
}

const OPS_EV = /^(trigger$|trigger\\.|hod\\.trigger|trade\\.sizing|order\\.|stop\\.breakeven|flatten|breaker|session\\.(limit|cutoff|end|adopt|abandon)|call\\.failed)/;
function renderOps(el, events) {
  const ops = events.filter(e => OPS_EV.test(e.type));
  if (!ops.length) { el.innerHTML = '<div class="empty">还没有操作 — 等待信号</div>'; return; }
  el.innerHTML = ops.slice().reverse().map(e => `
    <div class="jrow">
      <div class="jtime">${e.ts.slice(11,19)}Z</div>
      <div><span class="${chipCls(e.type)}">${warnIcon(e.type)}${esc(e.type)}</span></div>
      <div class="jmsg">${esc(e.msg)}</div>
    </div>`).join("");
}

// --- ② funnel: how far each symbol got and why --------------------------
const STAGE_LABEL = {bought:"\\u2705 已买入", triggered:"触发",
  vetoed:"\\u274c 否决", pending:"确认中", armed:"\\u23f3 已布防",
  picked:"入选 watchlist", alerted:"HOD 报警", dropped:"布防放弃",
  expired:"报警过期", "scan-pass":"过初筛", filtered:"被过滤"};
function renderFunnel(el, rows) {
  if (!rows || !rows.length) {
    el.innerHTML = '<div class="empty">今天还没有扫描记录</div>'; return; }
  const main = rows.filter(r => r.stage !== "filtered" && r.stage !== "scan-pass");
  const cut = rows.filter(r => r.stage === "filtered" || r.stage === "scan-pass");
  const row = r => `<tr>
    <td class="jtime">${esc(r.ts||"")}</td>
    <td>${symLink(r.symbol)} <span class="tagchip">${r.source==="hod"?"盘中HOD":"盘前gap"}</span></td>
    <td><span class="stage st-${esc(r.stage)}">${STAGE_LABEL[r.stage]||esc(r.stage)}</span></td>
    <td><span class="reason">${esc(r.reason)}</span>${r.metrics ?
      ' <span class="jdata">' + esc(Object.entries(r.metrics).map(
        ([k,v])=>k+" "+v).join(" \\u00b7 ")) + '</span>' : ""}</td></tr>`;
  let html = "";
  if (main.length)
    html += `<table><tr><th>时间</th><th>股票</th><th>走到哪一步</th>
      <th>原因（最后一条判定）</th></tr>${main.map(row).join("")}</table>`;
  else
    html += '<div class="empty">还没有股票进入 watchlist</div>';
  if (cut.length)
    html += `<details style="margin-top:10px"><summary>被过滤掉的
      ${cut.length} 只（点开看原因）</summary>
      <table style="margin-top:8px">${cut.map(row).join("")}</table></details>`;
  el.innerHTML = html;
}

function candleChart(el, bars, levels, err) {
  if (!bars || bars.length < 2) {
    el.innerHTML = `<div class="empty">${esc(err || "no bars yet — market not open or symbol quiet")}</div>`;
    return;
  }
  const W = Math.max(el.clientWidth, 320), H = 320, VH = 52,
        P = {t:12, r:60, b:20, l:8};
  const ph = H - P.t - P.b - VH - 10;
  const n = bars.length;
  let lo = Math.min(...bars.map(b=>b.l)), hi = Math.max(...bars.map(b=>b.h));
  for (const v of Object.values(levels)) { lo=Math.min(lo,v); hi=Math.max(hi,v); }
  const pad = (hi-lo)*0.06 || 0.05; lo-=pad; hi+=pad;
  const X = i => P.l + (i+0.5)*(W-P.l-P.r)/n;
  const Y = v => P.t + (1-(v-lo)/(hi-lo))*ph;
  const vmax = Math.max(...bars.map(b=>b.v), 1);
  const vy0 = P.t + ph + 10 + VH;
  const cw = Math.max(1.5, Math.min(8, (W-P.l-P.r)/n - 2));
  const fmtT = iso => new Date(iso).toLocaleTimeString("en-US",
    {hour:"numeric", minute:"2-digit", timeZone:"America/New_York"});
  let out = "";
  for (let i=0; i<=3; i++) {
    const v = lo+(hi-lo)*i/3, y = Y(v);
    out += `<line class="grid" x1="${P.l}" x2="${W-P.r}" y1="${y}" y2="${y}"/>`
         + `<text class="axis" x="${W-P.r+7}" y="${y+3.5}">${v.toFixed(2)}</text>`;
  }
  bars.forEach((b,i) => {
    const x = X(i), up = b.c >= b.o,
          col = up ? "var(--good-mark)" : "var(--critical)";
    out += `<line x1="${x}" x2="${x}" y1="${Y(b.h)}" y2="${Y(b.l)}"
              stroke="${col}" stroke-width="1"/>`;
    const yT = Y(Math.max(b.o,b.c)), yB = Y(Math.min(b.o,b.c));
    out += `<rect x="${(x-cw/2).toFixed(1)}" y="${yT.toFixed(1)}"
              width="${cw}" height="${Math.max(1, yB-yT).toFixed(1)}"
              fill="${col}"/>`;
    const vh = b.v/vmax*VH;
    out += `<rect x="${(x-cw/2).toFixed(1)}" y="${(vy0-vh).toFixed(1)}"
              width="${cw}" height="${vh.toFixed(1)}"
              fill="var(--series-1)" opacity="0.4"/>`;
  });
  for (const [k, v] of Object.entries(levels)) {
    const y = Y(v);
    out += `<line x1="${P.l}" x2="${W-P.r}" y1="${y}" y2="${y}"
              stroke="var(--baseline)" stroke-width="1"
              stroke-dasharray="5 4"/>
            <text class="axis" x="${P.l+3}" y="${y-4}">${esc(k)} ${v}</text>`;
  }
  out += `<text class="axis" x="${P.l}" y="${H-6}">${fmtT(bars[0].t)} ET</text>
          <text class="axis" x="${W-P.r}" y="${H-6}" text-anchor="end">${fmtT(bars[n-1].t)} ET</text>`;
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%"
    height="${H}">${out}
    <line id="kxh" class="grid" style="stroke-dasharray:2 3" x1="-9" x2="-9"
      y1="${P.t}" y2="${P.t+ph}"/></svg>
    <div class="tip" id="ktip" hidden></div>`;
  const svg = el.querySelector("svg"), tip = el.querySelector("#ktip"),
        xh = el.querySelector("#kxh");
  svg.addEventListener("mousemove", ev => {
    const r = svg.getBoundingClientRect();
    const mx = (ev.clientX - r.left) * (W / r.width);
    let i = Math.round((mx - P.l)/((W-P.l-P.r)/n) - 0.5);
    i = Math.max(0, Math.min(n-1, i));
    const b = bars[i], bx = X(i);
    xh.setAttribute("x1",bx); xh.setAttribute("x2",bx);
    tip.hidden = false;
    tip.textContent = `${fmtT(b.t)} ET  O ${b.o} H ${b.h} L ${b.l} C ${b.c}`
      + `  V ${b.v.toLocaleString()}`;
    tip.style.left = Math.min(Math.max(bx/W*r.width - 90, 4),
                              r.width - 300) + "px";
    tip.style.top = "2px";
  });
  svg.addEventListener("mouseleave", () => {
    tip.hidden = true;
    xh.setAttribute("x1",-9); xh.setAttribute("x2",-9);
  });
}

const chipCls = t =>
  /error|crash|breaker|abandon|reject/.test(t) ? "chip chip-warn" :
  /trigger|order|trade|entry|exit|tp1|flatten/.test(t) ? "chip chip-trade"
  : "chip";
const warnIcon = t => /error|crash|breaker|abandon|reject/.test(t)
  ? "\\u26a0\\ufe0e " : "";

function renderJournal(el, events) {
  if (!events.length) { el.innerHTML = '<div class="empty">quiet</div>'; return; }
  el.innerHTML = events.slice().reverse().map(e => `
    <div class="jrow">
      <div class="jtime">${e.ts.slice(11,19)}Z</div>
      <div><span class="${chipCls(e.type)}">${warnIcon(e.type)}${esc(e.type)}</span></div>
      <div><div class="jmsg">${esc(e.msg)}</div>
        ${e.data ? `<div class="jdata">${esc(JSON.stringify(e.data))}</div>` : ""}</div>
    </div>`).join("");
}

async function refresh() {
  let s;
  try { s = await (await fetch("/api/state")).json(); }
  catch (e) { $("asof").innerHTML = "\\u26a0\\ufe0e disconnected — retrying"; return; }
  $("asof").innerHTML = `<span class="pulse"></span>live \\u00b7 as of ${s.asof} \\u00b7 10s refresh`;

  const pnlUp = s.pnl >= 0, totUp = s.total >= 0;
  const arrow = up => up ? "\\u25b2" : "\\u25bc";
  $("tiles").innerHTML = `
    <div class="tile"><div class="k">Equity</div>
      <div class="v">${usd(s.equity)}</div>
      <div class="d ${totUp?"up":"down"}">${arrow(totUp)} ${usd(Math.abs(s.total))} ${totUp?"gain":"loss"} since start</div></div>
    <div class="tile"><div class="k">Daily P&amp;L</div>
      <div class="v ${pnlUp?"up":"down"}">${usd(s.pnl)}</div>
      <div class="d">${arrow(pnlUp)} ${pnlUp?"up":"down"} today \\u00b7 breaker \\u2212$300</div></div>
    <div class="tile"><div class="k">Buying power</div>
      <div class="v">${usd0(s.bp)}</div>
      <div class="d">4\\u00d7 intraday margin</div></div>
    <div class="tile"><div class="k">Exposure</div>
      <div class="v">${s.positions.length}<span style="font-size:15px;color:var(--muted)"> pos</span></div>
      <div class="d">${s.orders.length} open orders</div></div>`;

  renderTrades($("trades"), s.trades || []);
  renderOps($("ops"), s.events || []);
  renderFunnel($("funnel"), s.funnel || []);
  lineChart($("equity"), s.history);
  strategyPanel($("variants"), s.strategies || []);
  tickN += 1;
  allSyms = s.chart_symbols || [];
  if (!selSym && allSyms.length) loadChart(allSyms[0]);
  else if (selSym && tickN % 3 === 0) loadChart(selSym);
  else renderChips();

  $("positions").innerHTML = table(s.positions, [
    {h:"Symbol", f:r=>symLink(r.symbol)},
    {h:"Qty", num:1, f:r=>r.qty},
    {h:"Avg entry", num:1, f:r=>usd(+r.avg_entry_price)},
    {h:"Now", num:1, f:r=>usd(+r.current_price)},
    {h:"Unrealized", num:1, f:r=>{const v=+r.unrealized_pl;
      return `<span class="${v>=0?"up":"down"}">${arrow(v>=0)} ${usd(v)}</span>`}}]);
  $("orders").innerHTML = table(s.orders, [
    {h:"Symbol", f:r=>symLink(r.symbol)},
    {h:"Side", f:r=>`<span class="side-${r.side}">${r.side}</span>`},
    {h:"Type", f:r=>r.type},
    {h:"Qty", num:1, f:r=>r.qty},
    {h:"Price", num:1, f:r=>r.limit_price||r.stop_price||"mkt"},
    {h:"Status", f:r=>r.status}]);
  $("fills").innerHTML = table(s.fills, [
    {h:"Time (ET)", f:r=>new Date(r.filled_at).toLocaleTimeString("en-US",
      {hour:"numeric",minute:"2-digit",second:"2-digit",
       timeZone:"America/New_York"})},
    {h:"Symbol", f:r=>symLink(r.symbol)},
    {h:"Side", f:r=>`<span class="side-${r.side}">${r.side}</span>`},
    {h:"Qty", num:1, f:r=>r.filled_qty},
    {h:"Avg price", num:1, f:r=>usd(+r.filled_avg_price)},
    {h:"Type", f:r=>r.type}]);
  $("signals").innerHTML = table(s.signals, [
    {h:"Scan (UTC)", f:r=>r.scanned_at.slice(11,19)},
    {h:"Symbol", f:r=>symLink(r.symbol)},
    {h:"Price", num:1, f:r=>r.price},
    {h:"Gap %", num:1, f:r=>r.gap_pct},
    {h:"RVOL", num:1, f:r=>r.rvol},
    {h:"Spread", num:1, f:r=>r.spread},
    {h:"Catalyst", f:r=>esc((r.news||"").slice(0,85))}]);
  renderJournal($("journal"), s.events);
  $("log").textContent = s.log || "no log yet";
}
refresh(); setInterval(refresh, 10000);
window.addEventListener("resize",
  () => fetch("/api/state").then(r=>r.json()).then(s=>lineChart($("equity"), s.history)));
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/api/state", "/api/chart"):
            try:
                if parsed.path == "/api/chart":
                    q = parse_qs(parsed.query)
                    sym = (q.get("symbol") or [""])[0].upper()[:8]
                    body = json.dumps(chart_data(sym)).encode()
                else:
                    body = json.dumps(state_with_strategies()).encode()
                ctype = "application/json"
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())
                return
        else:
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    load_env()
    print(f"dashboard: http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
