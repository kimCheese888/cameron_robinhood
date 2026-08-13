#!/usr/bin/env python3
"""Structured decision journal — every trading decision, filter verdict and
order action is appended to events.jsonl (one JSON object per line) so a
session can be audited, rendered in the dashboard, and replayed exactly.

Event shape: {"ts": ISO-8601 UTC, "type": "scan.reject", "msg": human
explanation of WHY, "data": {machine-readable values used in the decision}}
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

# extra instances (see executor.ACCOUNT) log to their own events file so
# the multi-account services never clobber each other's journal
PATH = (Path(__file__).parent
        / ("events" + os.environ.get("CAMERON_INSTANCE", "") + ".jsonl"))


def event(etype, msg="", **data):
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "type": etype,
        "msg": msg,
    }
    if data:
        rec["data"] = data
    with open(PATH, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[{rec['ts'][11:19]}Z] {etype}: {msg}", flush=True)
    return rec


def today(limit=200):
    """Today's events (UTC), oldest first — used by the dashboard."""
    if not PATH.exists():
        return []
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = []
    for line in PATH.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("ts", "")[:10] == day:
            out.append(rec)
    return out[-limit:]
