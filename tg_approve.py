#!/usr/bin/env python3
"""Propose a code change via Telegram, deploy only on an explicit tap.

Design (deliberately narrow — see docs/APPROVAL.md):
  - Claude drafts + commits a fix to a `pending/<slug>` branch, pushes it,
    then calls propose() with a human-readable summary. That sends one
    Telegram message with two inline buttons (✅ 确认 / ❌ 拒绝) and
    records its message_id + a random token bound to those buttons.
  - check_approvals() polls getUpdates for `callback_query` events (a
    button tap) whose callback_data token matches a pending proposal.
    Tapping is the primary path; a native Telegram *reply* to the
    proposal message with an exact approve/reject keyword still works
    as a fallback for clients where buttons are awkward. Free-form text
    elsewhere in the chat is never treated as an instruction — this is
    a confirm-or-reject gate on a specific already-drafted diff, not a
    remote command channel.
  - Approving does NOT itself run git/deploy commands — it just reports
    which branch(es) were approved/rejected. The Claude session that
    polls is the one that does `git merge / push / ssh pull / restart`,
    same as every other change in this project's history.
"""

import json
import os
import secrets
from pathlib import Path

import requests

import executor

ROOT = executor.ROOT
STATE_PATH = ROOT / ".pending_proposals.json"
OFFSET_PATH = ROOT / ".tg_offset.json"
API = "https://api.telegram.org/bot%s"

APPROVE_WORDS = {"确认", "同意", "go", "yes", "ok", "okay", "上", "可以"}
REJECT_WORDS = {"不", "拒绝", "no", "算了", "先不", "撤回"}


def _load(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (ValueError, OSError):
            pass
    return default


def _save(path, data):
    path.write_text(json.dumps(data, indent=2))


def _tok():
    return os.environ["TELEGRAM_BOT_TOKEN"]


def _chat():
    return os.environ["TELEGRAM_CHAT_ID"]


def propose(branch, summary):
    """Send a proposal message with 确认/拒绝 buttons, record it pending.
    `summary` should already explain what/why/evidence."""
    token = secrets.token_hex(4)
    text = ("\U0001F527 <b>待确认改动</b> — 分支 <code>%s</code>\n\n%s"
            % (branch, summary))
    keyboard = {"inline_keyboard": [[
        {"text": "✅ 确认", "callback_data": "appr:%s" % token},
        {"text": "❌ 拒绝", "callback_data": "rej:%s" % token},
    ]]}
    r = requests.post(API % _tok() + "/sendMessage", timeout=20,
                      json={"chat_id": _chat(), "text": text,
                            "parse_mode": "HTML", "reply_markup": keyboard})
    r.raise_for_status()
    mid = r.json()["result"]["message_id"]
    pending = _load(STATE_PATH, [])
    pending.append({"branch": branch, "message_id": mid, "token": token,
                    "summary": summary})
    _save(STATE_PATH, pending)
    return mid


def _clear_buttons(message_id, toast):
    """Best-effort: drop the inline keyboard so a decided proposal can't
    be tapped twice, and pop a small confirmation on the user's screen."""
    try:
        requests.post(API % _tok() + "/editMessageReplyMarkup", timeout=10,
                     json={"chat_id": _chat(), "message_id": message_id,
                           "reply_markup": {"inline_keyboard": []}})
    except requests.RequestException:
        pass


def check_approvals():
    """Poll for button taps (primary) or replies (fallback) on pending
    proposals. Returns (approved_branches, rejected_branches) — both
    lists, either may be empty. Always advances the update offset so
    updates aren't reprocessed, even ones that don't match anything."""
    pending = _load(STATE_PATH, [])
    if not pending:
        return [], []
    by_token = {p["token"]: p for p in pending if p.get("token")}
    by_mid = {p["message_id"]: p for p in pending}
    offset = _load(OFFSET_PATH, {}).get("offset", 0)
    r = requests.get(API % _tok() + "/getUpdates", timeout=20,
                     params={"offset": offset, "timeout": 0})
    r.raise_for_status()
    updates = r.json().get("result", [])
    approved, rejected = [], []
    max_update_id = offset - 1
    for u in updates:
        max_update_id = max(max_update_id, u["update_id"])

        cq = u.get("callback_query")
        if cq:
            action, _, token = (cq.get("data") or "").partition(":")
            p = by_token.get(token)
            if p and action in ("appr", "rej"):
                (approved if action == "appr" else rejected).append(
                    p["branch"])
                try:
                    requests.post(API % _tok() + "/answerCallbackQuery",
                                 timeout=10, json={
                                     "callback_query_id": cq["id"],
                                     "text": "已确认，准备部署" if action == "appr"
                                             else "已撤回，不部署"})
                except requests.RequestException:
                    pass
                _clear_buttons(p["message_id"], action)
            continue

        msg = u.get("message") or {}
        reply = msg.get("reply_to_message")
        if not reply or reply.get("message_id") not in by_mid:
            continue
        text = (msg.get("text") or "").strip().casefold()
        p = by_mid[reply["message_id"]]
        if text in APPROVE_WORDS:
            approved.append(p["branch"])
            _clear_buttons(p["message_id"], "appr")
        elif text in REJECT_WORDS:
            rejected.append(p["branch"])
            _clear_buttons(p["message_id"], "rej")

    if updates:
        _save(OFFSET_PATH, {"offset": max_update_id + 1})
    if approved or rejected:
        remaining = [p for p in pending
                    if p["branch"] not in approved
                    and p["branch"] not in rejected]
        _save(STATE_PATH, remaining)
    return approved, rejected


def notify_done(branch, deployed):
    text = ("✅ <code>%s</code> 已上线" % branch if deployed else
            "\U0001F5D1 <code>%s</code> 已撤回，未部署" % branch)
    requests.post(API % _tok() + "/sendMessage", timeout=20,
                 json={"chat_id": _chat(), "text": text,
                       "parse_mode": "HTML"})


if __name__ == "__main__":
    import sys
    executor.load_env()
    if len(sys.argv) >= 3 and sys.argv[1] == "propose":
        branch, summary = sys.argv[2], sys.argv[3]
        print("sent message_id:", propose(branch, summary))
    elif len(sys.argv) >= 2 and sys.argv[1] == "check":
        print(check_approvals())
    else:
        print(__doc__)
