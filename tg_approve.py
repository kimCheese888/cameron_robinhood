#!/usr/bin/env python3
"""Propose a code change via Telegram, deploy only on an explicit reply.

Design (deliberately narrow — see docs/APPROVAL.md):
  - Claude drafts + commits a fix to a `pending/<slug>` branch, pushes it,
    then calls propose() with a human-readable summary. That sends one
    Telegram message and records its message_id.
  - check_approvals() polls getUpdates and only ever matches a message
    that is a native Telegram *reply* to one of our pending message_ids,
    whose text (stripped/casefolded) is an exact approve/reject keyword.
    Free-form text in the chat is never treated as an instruction —
    unanchored text, wrong keyword, or a reply to some other message
    is ignored. This is a confirm-or-reject gate on a specific
    already-drafted diff, not a remote command channel.
  - Approving does NOT itself run git/deploy commands — it just reports
    which branch(es) were approved/rejected. The Claude session that
    polls is the one that does `git merge / push / ssh pull / restart`,
    same as every other change in this project's history.
"""

import json
import os
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
    """Send a proposal message, record it pending. `summary` should
    already explain what/why/evidence — this just adds the approval
    instructions and tracks the branch."""
    text = ("\U0001F527 <b>待确认改动</b> — 分支 <code>%s</code>\n\n%s\n\n"
            "回复本条消息「确认」上线，或「不」撤回。" % (branch, summary))
    r = requests.post(API % _tok() + "/sendMessage", timeout=20,
                      json={"chat_id": _chat(), "text": text,
                            "parse_mode": "HTML"})
    r.raise_for_status()
    mid = r.json()["result"]["message_id"]
    pending = _load(STATE_PATH, [])
    pending.append({"branch": branch, "message_id": mid, "summary": summary})
    _save(STATE_PATH, pending)
    return mid


def check_approvals():
    """Poll for replies to pending proposals. Returns
    (approved_branches, rejected_branches) — both lists, either may be
    empty. Always advances the update offset so messages aren't
    reprocessed, even ones that don't match anything."""
    pending = _load(STATE_PATH, [])
    if not pending:
        return [], []
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
        msg = u.get("message") or {}
        reply = msg.get("reply_to_message")
        if not reply or reply.get("message_id") not in by_mid:
            continue
        text = (msg.get("text") or "").strip().casefold()
        p = by_mid[reply["message_id"]]
        if text in APPROVE_WORDS:
            approved.append(p["branch"])
        elif text in REJECT_WORDS:
            rejected.append(p["branch"])
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
