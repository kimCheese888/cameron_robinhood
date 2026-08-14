#!/usr/bin/env python3
"""Daily post-session report across all paper instances -> Telegram.

Run after the 11:00 ET cutoff (server cron ~15:20 UTC, Mon-Fri). For each
instance it reads that instance's event file + its own Alpaca account and
builds a compact summary comparing the live rule variants side by side.

Sends to Telegram if TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set in
.env; otherwise just prints (so it's safe to run/test anytime).
"""

import json
import os
from datetime import datetime, timezone

import requests

import executor

ROOT = executor.ROOT
BASE = "https://paper-api.alpaca.markets"

# (label, event-file suffix, account key suffix, starting equity)
INSTANCES = [
    ("volx2 (2x, 现役)", "",         "",         100_000),
    ("volx2-1.5x",        "_15x",     "_15X",     1_000_000),
    ("volx2-nochase",     "_nochase", "_NOCHASE", 1_000_000),
]


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


def build(day):
    lines = ["\U0001F4CA Cameron 日报 · " + day]
    for label, esuf, asuf, start in INSTANCES:
        evs = today_events(esuf, day)
        trig = [e for e in evs if e["type"] == "trigger"]
        veto = [e for e in evs if e["type"] == "trigger.veto"]
        drop = [e for e in evs if e["type"] == "arm.drop"]
        watch = [(e.get("data") or {}).get("symbol") for e in evs
                 if e["type"] == "watchlist.add"]
        watch = [w for w in watch if w]
        a = account(asuf)
        lines.append("")
        lines.append("─ " + label)
        if a:
            eq = float(a["equity"])
            last = float(a.get("last_equity") or eq)
            dp = eq - last if 0.5 * eq <= last <= 2 * eq else 0.0
            lines.append("  ***%s | equity $%s" %
                         (a["account_number"][-4:], format(round(eq), ",")))
            lines.append("  今日 $%+.2f | 累计 $%+d" % (dp, round(eq - start)))
        else:
            lines.append("  (账户未配置)")
        lines.append("  watchlist: " + (" ".join(watch) or "—"))
        for t in trig:
            lines.append("  ✅ " + t["msg"][:64])
        lines.append("  下单 %d · 量能否决 %d · 丢弃 %d"
                     % (len(trig), len(veto), len(drop)))
    return "\n".join(lines)


def main():
    executor.load_env()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = build(day)
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            r = requests.post(
                "https://api.telegram.org/bot%s/sendMessage" % tok,
                json={"chat_id": chat, "text": text}, timeout=20)
            print("telegram sendMessage:", r.status_code,
                  "" if r.status_code < 400 else r.text[:120])
        except requests.RequestException as e:
            print("telegram failed:", str(e)[:120])
    else:
        print("(TELEGRAM_BOT_TOKEN/CHAT_ID not set — print only)\n")
        print(text)


if __name__ == "__main__":
    main()
