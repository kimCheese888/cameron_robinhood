#!/usr/bin/env python3
"""Daily post-session report across all paper instances -> Telegram.

Run after the 11:00 ET cutoff (server cron ~15:20 UTC, Mon-Fri). Reads the
primary instance's journal to narrate each watchlist stock (bought or not,
and why, with a TradingView link), then lists per-account P&L for the three
live-rule variants.

Sends to Telegram (HTML) if TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are in
.env; otherwise prints (safe to run/test anytime).
"""

import json
import os
from datetime import datetime, timezone

import requests

import executor

ROOT = executor.ROOT
BASE = "https://paper-api.alpaca.markets"
TV = "https://www.tradingview.com/chart/?symbol="

# (label, event-file suffix, account key suffix, starting equity)
INSTANCES = [
    ("volx2 现役 (2x)", "",         "",         100_000),
    ("volx2-1.5x",      "_15x",     "_15X",     1_000_000),
    ("volx2-nochase",   "_nochase", "_NOCHASE", 1_000_000),
]
WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def account(suf):
    kid = os.environ.get("APCA_API_KEY_ID" + suf)
    sec = os.environ.get("APCA_API_SECRET_KEY" + suf)
    if not kid or not sec:
        return None
    try:
        r = requests.get(BASE + "/v2/account", timeout=20, headers={
            "APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec})
        return r.json() if r.status_code < 400 else None
    except requests.RequestException:
        return None


def fills_realized(suf, day):
    """Realized $ per symbol today from real fills (matched buy vs sell)."""
    kid = os.environ.get("APCA_API_KEY_ID" + suf)
    sec = os.environ.get("APCA_API_SECRET_KEY" + suf)
    if not kid:
        return {}
    try:
        acts = requests.get(BASE + "/v2/account/activities", timeout=20,
                            params={"activity_types": "FILL", "date": day},
                            headers={"APCA-API-KEY-ID": kid,
                                     "APCA-API-SECRET-KEY": sec}).json()
    except requests.RequestException:
        return {}
    agg = {}
    for a in acts if isinstance(acts, list) else []:
        s = a["symbol"]
        q, p = float(a["qty"]), float(a["price"])
        d = agg.setdefault(s, [0.0, 0.0, 0.0, 0.0])  # bq,bv,sq,sv
        if a["side"] == "buy":
            d[0] += q; d[1] += q * p
        else:
            d[2] += q; d[3] += q * p
    out = {}
    for s, (bq, bv, sq, sv) in agg.items():
        if bq and sq:
            m = min(bq, sq)
            out[s] = (sv / sq - bv / bq) * m
    return out


def today_events(suf, day):
    p = ROOT / ("events" + suf + ".jsonl")
    out = []
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("ts", "")[:10] == day:
                out.append(e)
    return out


def decide(sym, evs, realized):
    """Human one-liner: did we buy this stock, and why / why not."""
    by = [e for e in evs if (e.get("data") or {}).get("symbol") == sym]
    typ = {e["type"] for e in by}
    d = lambda t: next(((e.get("data") or {}) for e in by
                        if e["type"] == t), {})
    if "trigger" in typ:
        t = d("trigger")
        r = realized.get(sym)
        res = ""
        if r is not None:
            rr = r / 100.0
            res = "　结果 <b>%+.0f$（%+.1fR）</b>" % (r, rr)
        return ("✅", "<b>买了</b> — 放量突破箱顶 %s（%.2f× 均量，过 2× 确认），"
                "%s 进、止损 %s。%s" % (t.get("or_high", "?"),
                t.get("ratio", 0) or 0, t.get("entry", "?"),
                t.get("stop", "?"), res))
    if "trigger.veto" in typ:
        t = d("trigger.veto")
        if t.get("reason") == "chase_too_far":
            return ("⛔", "没买 — 量能确认了(%.2f×)，但确认时价格 %s 已经"
                    "追出箱顶 %s 太多（超过箱体一半），追高幅度封顶，不追。"
                    % (t.get("ratio", 0) or 0, t.get("last", "?"),
                       t.get("or_high", "?")))
        return ("⛔", "没买 — 破了箱顶但突破量只有 %.2f×（需 2×），"
                "当假突破否掉。" % (t.get("ratio", 0) or 0))
    if "arm.drop" in typ:
        r = d("arm.drop").get("reason", "")
        why = {"spread_at_open": "开盘价差太宽（>$0.20），盘口太薄",
               "range_too_wide": "开盘箱体太宽（>10%），止损会离太远",
               "no_bars": "开盘没有K线数据"}.get(r, r)
        return ("⛔", "没做 — %s。" % why)
    if "arm" in typ:
        return ("😴", "没买 — 布防了但整天没突破箱顶。")
    return ("·", "没进入候选（未布防）。")


def side_entries(esuf, day):
    """A side instance's (1.5x/nochase/...) own real trigger events for
    `day` — each variant has its own volume threshold / entry mechanism
    and can fire on a day/symbol the primary vetoed or never armed. Only
    checking the primary's narrative silently drops these (real bug,
    2026-08-28: 1.5x traded DAIC+WSHP on a day primary vetoed both and
    the daily report said "0 trades across all 3 accounts")."""
    evs = today_events(esuf, day)
    out = []
    for e in evs:
        if e["type"] != "trigger":
            continue
        d = e.get("data") or {}
        out.append(d)
    return out


def build(day):
    dt = datetime.strptime(day, "%Y-%m-%d")
    evs = today_events("", day)          # primary drives the narrative
    watch = [(e.get("data") or {}) for e in evs if e["type"] == "watchlist.add"]
    realized = fills_realized("", day)
    n_buy = sum(1 for w in watch
                if decide(w.get("symbol", ""), evs, realized)[0] == "✅")

    L = ["\U0001F4CA <b>Cameron 收盘复盘 · %s %s</b>" % (day, WD[dt.weekday()])]
    if watch:
        L.append("今天扫到 %d 只、做了 %d 笔。" % (len(watch), n_buy))
    else:
        L.append("今天没有符合条件的跳空股,空仓。")
    for w in watch:
        sym = w.get("symbol", "")
        emoji, text = decide(sym, evs, realized)
        stat = "+%s%% gap · RVOL %s · $%s" % (
            w.get("gap_pct", "?"), w.get("rvol", "?"), w.get("price", "?"))
        L.append("")
        L.append('%s <a href="%s%s">%s</a> · %s' % (emoji, TV, sym, sym, stat))
        L.append(text)

    # side instances often have a looser/different threshold and fire on
    # a symbol/day the primary narrative above didn't buy — say so
    # explicitly instead of only reporting a P&L delta with no story
    side_lines = []
    for label, esuf, asuf, start in INSTANCES[1:]:
        entries = side_entries(esuf, day)
        r = fills_realized(asuf, day)
        for d in entries:
            sym = d.get("symbol", "")
            pnl = r.get(sym)
            res = ("　结果 <b>%+.0f$</b>" % pnl) if pnl is not None else ""
            side_lines.append(
                "· <b>%s</b> 单独下单 <a href=\"%s%s\">%s</a> "
                "@ %s（%.2fx 量能确认，现役的门槛是 2x）%s"
                % (label, TV, sym, sym, d.get("entry", "?"),
                   d.get("ratio", 0) or 0, res))
    if side_lines:
        L.append("")
        L.append("━━━━━━━━")
        L.append("<b>侧账户单独动作</b>（跟现役门槛不同，可能现役没买这里买了）")
        L.extend(side_lines)

    L.append("")
    L.append("━━━━━━━━")
    L.append("<b>账户（今日 / 累计）</b>")
    for label, esuf, asuf, start in INSTANCES:
        a = account(asuf)
        if not a:
            L.append("· %s — 未配置" % label)
            continue
        eq = float(a["equity"])
        # equity's own last_equity is only meaningful for the actual
        # current trading day; for any `day` (including backfills) the
        # correct "today" P&L is realized fills for that specific day
        dp = sum(fills_realized(asuf, day).values())
        L.append("· %s　<b>%+.0f$</b> / %+.0f$" % (label, dp, eq - start))
    return "\n".join(L)


def main():
    executor.load_env()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = build(day)
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            r = requests.post(
                "https://api.telegram.org/bot%s/sendMessage" % tok, timeout=20,
                json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": True})
            print("telegram:", r.status_code,
                  "" if r.status_code < 400 else r.text[:160])
        except requests.RequestException as e:
            print("telegram failed:", str(e)[:120])
    else:
        print("(TELEGRAM not set — print only)\n")
        print(text)


if __name__ == "__main__":
    main()
