#!/usr/bin/env python3
"""Best-effort realtime Telegram alerts for the live session.

Only the PRIMARY instance sends (gated on CAMERON_LIVE_ONLY != "1"), so the
15x / nochase paper services stay silent and the user gets one clean
stream. Every send swallows errors and uses a short timeout, so alerting
can never block or crash a trading session.
"""

import os

import requests

TV = "https://www.tradingview.com/chart/?symbol="


def _on():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN")
               and os.environ.get("TELEGRAM_CHAT_ID")
               and os.environ.get("CAMERON_LIVE_ONLY") != "1")


def send(text):
    if not _on():
        return
    try:
        requests.post("https://api.telegram.org/bot%s/sendMessage"
                      % os.environ["TELEGRAM_BOT_TOKEN"], timeout=8,
                      json={"chat_id": os.environ["TELEGRAM_CHAT_ID"],
                            "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True})
    except Exception:
        pass


def link(sym):
    return '<a href="%s%s">%s</a>' % (TV, sym, sym)


def watchlist(rows):
    out = ["\U0001F4CB <b>今日盯 %d 只</b>" % len(rows)]
    for r in rows:
        out.append("· %s · +%s%% gap · RVOL %s · $%s" % (
            link(r["symbol"]), r.get("gap_pct", "?"),
            r.get("rvol", "?"), r.get("price", "?")))
    return "\n".join(out)


def entry(sym, hi, ratio, e, stop):
    return ("✅ <b>买入 %s</b> @ %s\n放量突破箱顶 %s（%.2f× 均量，过 2× 确认），"
            "止损 %s" % (link(sym), e, hi, ratio or 0, stop))


def veto(sym, ratio):
    return ("⛔ <b>%s 否决</b> — 突破量 %.2f× < 2×，判为假突破,不买"
            % (link(sym), ratio or 0))


def exit_(sym, pnl):
    r = "" if pnl is None else "　<b>%+.0f$（%+.1fR）</b>" % (pnl, pnl / 100.0)
    return "\U0001F534 <b>%s 平仓</b>%s" % (link(sym), r)


def hod_watch(sym, last):
    px = "?" if last is None else last
    return ("\U0001F440 <b>%s 盘中动能</b> @ %s\n新高附近放量触发第二雷达，"
            "盯回踩确认入场" % (link(sym), px))


def hod_entry(sym, entry, stop):
    return ("✅ <b>买入 %s</b>（盘中动能回踩）@ %s\n"
            "回踩后反包确认，止损 %s" % (link(sym), entry, stop))
