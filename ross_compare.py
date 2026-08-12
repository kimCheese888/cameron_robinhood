#!/usr/bin/env python3
"""Daily Ross Cameron comparison.

Compares our bot's watchlist for a day against the stocks Ross Cameron
names in his daily YouTube video (watchlist / recap). Ross's transcript
is fetched externally (browser, residential IP — YouTube blocks the
server's datacenter IP) and handed to this script as a text file.

Usage:
  ross_compare.py TRANSCRIPT.txt [--day YYYY-MM-DD]
                                 [--video-id ID] [--title "..."]

Pipeline:
  1. our watchlist for the day  <- events.jsonl (watchlist.add)
  2. overlap  = our symbols found verbatim in Ross's transcript (reliable)
  3. ross_all = best-effort ticker extraction from the transcript
  4. write ross.compare event + append ross_compare.csv
"""

import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
EVENTS = ROOT / "events.jsonl"
COMPARE_CSV = ROOT / "ross_compare.csv"

# Uppercase acronyms / words that look like tickers but are trading jargon,
# not stocks Ross is trading. Extraction is best-effort; the human reviews.
STOPWORDS = {
    "A", "I", "AI", "OK", "US", "USA", "IRA", "ETF", "ETFS", "FDA", "CEO",
    "CFO", "IPO", "SEC", "NYSE", "AH", "PM", "AM", "ET", "EST", "EDT", "TV",
    "ID", "AWS", "GCP", "HOD", "LOD", "ORB", "VWAP", "RVOL", "ATH", "EPS",
    "T12", "SSR", "P", "T", "S", "PR", "OTC", "DD", "YOLO", "FOMO", "TL",
    "DR", "FYI", "IMO", "AKA", "USD", "GDP", "CPI", "FOMC", "QE", "YT",
}
TICKER_RE = re.compile(r"\b[A-Z]{1,5}\b")


def our_watchlist(day):
    """Symbols our bot put on the watchlist for `day` (watchlist.add)."""
    syms = []
    if not EVENTS.exists():
        return syms
    for line in EVENTS.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("ts", "")[:10] == day and e.get("type") == "watchlist.add":
            s = (e.get("data") or {}).get("symbol")
            if s and s not in syms:
                syms.append(s)
    return syms


def match_symbol(sym, text):
    """How Ross names `sym`, if at all: None | 'exact' | 'fuzzy'.

    YouTube auto-captions mangle tickers — JWEL -> "Jwell", ZJYL ->
    "Zjy L", SCKT -> "Seckt". `exact` is a whole-word case-insensitive
    hit; `fuzzy` tolerates spaces between the letters and trailing extra
    letters, anchored at a word start so we don't match a ticker buried
    inside an English word (NAMI must not hit "dyNAMIc")."""
    if re.search(rf"\b{re.escape(sym)}\b", text, re.IGNORECASE):
        return "exact"
    # letters separated by optional whitespace, anchored at a word start;
    # no trailing boundary so "Jwell" still matches JWEL. Skip <3-char
    # symbols — too short to fuzzy-match without false positives.
    if len(sym) >= 3:
        pat = r"\b" + r"\s*".join(re.escape(c) for c in sym)
        if re.search(pat, text, re.IGNORECASE):
            return "fuzzy"
    return None


def find_overlap(transcript, symbols):
    """Which of `symbols` Ross names (exact or caption-fuzzy)."""
    return [s for s in symbols if match_symbol(s, transcript)]


def extract_ross_tickers(transcript):
    """Best-effort: uppercase 1-5 letter tokens minus jargon. Auto-caption
    spacing can split symbols (e.g. ZJYL -> 'ZJ Y L'), so this misses some
    and catches some noise — a review aid, not ground truth."""
    seen = {}
    for m in TICKER_RE.findall(transcript):
        if m in STOPWORDS:
            continue
        seen[m] = seen.get(m, 0) + 1
    # keep tokens that appear like tickers (>=1) sorted by frequency
    return sorted(seen, key=lambda k: (-seen[k], k))


def compare(transcript, day, video_id="", title="", symbols=None):
    ours = list(symbols) if symbols is not None else our_watchlist(day)
    overlap = find_overlap(transcript, ours)
    fuzzy = [s for s in ours if match_symbol(s, transcript) == "fuzzy"]
    ross = extract_ross_tickers(transcript)
    our_only = [s for s in ours if s not in overlap]
    ross_only = [s for s in ross if s not in ours]
    return {
        "date": day, "video_id": video_id, "title": title,
        "our_watchlist": ours,
        "overlap": overlap,
        "overlap_fuzzy": fuzzy,  # matched only via caption-tolerant match
        "our_only": our_only,
        "ross_candidates": ross,
        "ross_only_topN": ross_only[:15],
    }


def write_report(result):
    # append a compact row to ross_compare.csv
    new = not COMPARE_CSV.exists()
    with open(COMPARE_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "date", "video_id", "title", "our_watchlist", "overlap",
            "our_only", "ross_candidates"])
        if new:
            w.writeheader()
        w.writerow({
            "date": result["date"], "video_id": result["video_id"],
            "title": result["title"],
            "our_watchlist": " ".join(result["our_watchlist"]),
            "overlap": " ".join(result["overlap"]),
            "our_only": " ".join(result["our_only"]),
            "ross_candidates": " ".join(result["ross_candidates"]),
        })
    # structured event so the dashboard / journal can surface it
    line = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "type": "ross.compare",
        "msg": (f"Ross vs bot {result['date']}: overlap "
                f"{result['overlap'] or 'none'}; we flagged "
                f"{result['our_watchlist'] or 'none'}"),
        "data": result,
    }
    with open(EVENTS, "a") as f:
        f.write(json.dumps(line) + "\n")


def main():
    args = [a for a in sys.argv[1:]]
    if not args or args[0] in ("-h", "--help"):
        sys.exit(__doc__)
    tpath = Path(args[0])
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    video_id = title = ""
    i = 1
    while i < len(args):
        if args[i] == "--day":
            day = args[i + 1]; i += 2
        elif args[i] == "--video-id":
            video_id = args[i + 1]; i += 2
        elif args[i] == "--title":
            title = args[i + 1]; i += 2
        else:
            i += 1
    transcript = tpath.read_text(errors="ignore")
    result = compare(transcript, day, video_id, title)
    write_report(result)
    print(f"=== Ross vs bot · {result['date']} ===")
    print(f"video : {result['title']} ({result['video_id']})")
    print(f"our watchlist   : {result['our_watchlist'] or '(none)'}")
    fuzzy = result.get("overlap_fuzzy") or []
    marked = [f"{s}~" if s in fuzzy else s for s in result["overlap"]]
    print(f"OVERLAP         : {marked or '(none)'}  (~ = caption-fuzzy)")
    print(f"our-only        : {result['our_only'] or '(none)'}")
    print(f"ross candidates : {result['ross_candidates'][:15]}")
    print(f"-> appended {COMPARE_CSV.name} + ross.compare event")


if __name__ == "__main__":
    main()
