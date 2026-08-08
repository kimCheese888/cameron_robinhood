#!/usr/bin/env python3
"""Premarket gap scanner — Ross Cameron style momentum filter.

Pipeline:
  1. Universe: Alpaca screener (most-actives + movers)
  2. Snapshot each symbol -> gap% vs previous close, last price, volume
  3. Filters: price $2-20, gap >= 10%, min volume
  4. News lookup for survivors (catalyst check)
  5. Append results to signals.csv for later validation

Uses only stdlib + requests. Free-tier caveat: volume is IEX-only
(single exchange, ~2% of tape) so absolute volume is understated;
RVOL is computed on the same feed for numerator and denominator,
which keeps the ratio directionally useful but imprecise.
"""

import csv
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import truststore

truststore.inject_into_ssl()  # corporate TLS proxy: trust system keychain CAs

import requests

import journal
import rh

ROOT = Path(__file__).parent

# --- config -----------------------------------------------------------
PRICE_MIN = 2.0
PRICE_MAX = 20.0
GAP_MIN_PCT = 10.0
RVOL_MIN = 5.0
FLOAT_MAX = 20_000_000   # Ross wants <10M; 20M cap still kills mid-caps
MIN_TODAY_VOLUME = 30_000        # IEX feed only; ~2% of tape
UNIVERSE_SIZE = 100
CSV_PATH = ROOT / "signals.csv"

DATA = "https://data.alpaca.markets"


def load_env():
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def api(path, **params):
    for attempt in range(3):  # timeouts/disconnects: retry with backoff
        try:
            r = requests.get(
                f"{DATA}{path}",
                params=params,
                headers={
                    "APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"],
                    "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"],
                },
                timeout=30,
            )
            r.raise_for_status()
            return r.json()
        except requests.HTTPError:
            raise  # 4xx/5xx: not transient, caller decides
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))


rh_vetted = set()  # symbols pre-screened server-side on consolidated data


def get_universe():
    global rh_vetted
    rh_vetted = set()
    symbols = set()
    movers = api("/v1beta1/screener/stocks/movers", top=50)
    symbols.update(m["symbol"] for m in movers.get("gainers", []))
    actives = api("/v1beta1/screener/stocks/most-actives",
                  by="volume", top=UNIVERSE_SIZE)
    symbols.update(a["symbol"] for a in actives.get("most_actives", []))
    if rh.available():
        try:  # server-side consolidated gap scan (float+rvol pre-filtered)
            hits = rh.scan_tickers()
            journal.event("scan.rh_universe", "robinhood gap scanner "
                          f"contributed {len(hits)}: {hits[:25]}",
                          symbols=hits)
            symbols.update(hits)
            rh_vetted = set(hits)
        except Exception as e:
            journal.event("rh.error",
                          f"rh scanner failed: {str(e)[:120]}")
    return sorted(symbols)


def get_snapshots(symbols):
    out = {}
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i + 100]
        data = api("/v2/stocks/snapshots",
                   symbols=",".join(chunk), feed="iex")
        out.update(data)
    return out


def avg_daily_volume(symbol, days=20):
    """20-day average volume on the same IEX feed, for RVOL."""
    start = (datetime.now(timezone.utc) - timedelta(days=days * 2)).date()
    try:
        data = api(f"/v2/stocks/{symbol}/bars", timeframe="1Day",
                   start=str(start), limit=days, feed="iex",
                   adjustment="split")
        bars = data.get("bars") or []
        vols = [b["v"] for b in bars]
        return sum(vols) / len(vols) if vols else None
    except requests.HTTPError:
        return None


def latest_headline(symbol):
    try:
        items = api("/v1beta1/news", symbols=symbol, limit=1).get("news", [])
        if items:
            return items[0]["created_at"][:10] + " " + items[0]["headline"][:120]
    except requests.HTTPError:
        pass
    return ""


def scan():
    journal.event("scan.config", "filter parameters for this scan",
                  price_min=PRICE_MIN, price_max=PRICE_MAX,
                  gap_min_pct=GAP_MIN_PCT, rvol_min=RVOL_MIN,
                  min_volume=MIN_TODAY_VOLUME, float_max=FLOAT_MAX,
                  feed="iex", nbbo="robinhood" if rh.available() else "none")
    universe = get_universe()
    snaps = get_snapshots(universe)

    candidates = []
    reject_counts = {}

    def reject(sym, reason, interesting=False, **vals):
        reject_counts[reason] = reject_counts.get(reason, 0) + 1
        if interesting:  # gapping stocks that failed some OTHER filter
            journal.event("scan.reject", f"{sym}: {reason}", symbol=sym,
                          reason=reason, **vals)

    for sym, s in snaps.items():
        if not s or not s.get("prevDailyBar") or not s.get("latestTrade"):
            reject(sym, "no_data")
            continue
        prev_close = s["prevDailyBar"]["c"]
        price = s["latestTrade"]["p"]
        today = s.get("dailyBar") or {}
        volume = today.get("v", 0)
        if prev_close <= 0:
            reject(sym, "no_data")
            continue
        gap = (price - prev_close) / prev_close * 100
        gapping = gap >= GAP_MIN_PCT

        if gap < GAP_MIN_PCT:
            reject(sym, "gap_below_min")
            continue
        if not (PRICE_MIN <= price <= PRICE_MAX):
            reject(sym, "price_out_of_range", interesting=gapping,
                   price=price, gap_pct=round(gap, 1))
            continue
        if volume < MIN_TODAY_VOLUME and sym not in rh_vetted:
            # IEX volume is ~2% of tape; symbols the RH consolidated scan
            # already vetted skip this unreliable check
            reject(sym, "volume_too_low", interesting=gapping,
                   volume=volume, gap_pct=round(gap, 1))
            continue

        q = s.get("latestQuote") or {}
        spread = (q["ap"] - q["bp"]) if q.get("ap") and q.get("bp") else None
        candidates.append({
            "symbol": sym, "price": price, "gap_pct": round(gap, 1),
            "volume": volume,
            "spread": round(spread, 3) if spread is not None else "",
        })

    # Robinhood enrichment: NBBO spread (honest book) + float filter.
    # IEX premarket books on these names are garbage — 3-10x wide or absent.
    rh_data = {}
    if rh.available() and candidates:
        try:
            rh_data = rh.enrich([c["symbol"] for c in candidates])
            journal.event("rh.enrich", "robinhood NBBO+float for "
                          f"{len(rh_data)} candidates")
        except Exception as e:
            journal.event("rh.error", "robinhood enrich failed, IEX-only "
                          f"scan: {str(e)[:120]}")

    # RVOL + news only for filter survivors (keeps request count low)
    final = []
    for c in sorted(candidates, key=lambda x: -x["gap_pct"]):
        info = rh_data.get(c["symbol"]) or {}
        if info.get("spread") is not None:
            c["iex_spread"], c["spread"] = c["spread"], info["spread"]
        flt = info.get("float")
        c["float"] = int(flt) if flt else ""
        if flt and flt > FLOAT_MAX:
            reject(c["symbol"], "float_too_large", interesting=True,
                   float=int(flt), gap_pct=c["gap_pct"])
            continue
        adv = avg_daily_volume(c["symbol"])
        c["rvol"] = round(c["volume"] / adv, 1) if adv else ""
        if (c["rvol"] != "" and c["rvol"] < RVOL_MIN
                and c["symbol"] not in rh_vetted):
            reject(c["symbol"], "rvol_below_min", interesting=True,
                   rvol=c["rvol"], gap_pct=c["gap_pct"])
            continue
        c["news"] = latest_headline(c["symbol"])
        journal.event("scan.pass",
                      f"{c['symbol']}: ${c['price']} gap {c['gap_pct']}% "
                      f"rvol {c['rvol']} spread {c['spread']}", **c)
        final.append(c)

    journal.event("scan.summary",
                  f"universe {len(universe)} -> {len(final)} candidates",
                  universe=len(universe), passed=len(final),
                  rejected=reject_counts)
    return final


def append_csv(rows):
    fields = ["scanned_at", "symbol", "price", "gap_pct", "volume",
              "rvol", "spread", "news"]
    new_file = not CSV_PATH.exists()
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new_file:
            w.writeheader()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for r in rows:
            w.writerow({"scanned_at": now, **r})


def main():
    load_env()
    rows = scan()
    if not rows:
        print("no candidates passed the filter")
        return
    append_csv(rows)
    print(f"\n{'SYM':<6} {'PRICE':>7} {'GAP%':>7} {'VOL':>12} "
          f"{'RVOL':>6} {'SPRD':>6}  NEWS")
    for r in rows:
        print(f"{r['symbol']:<6} {r['price']:>7.2f} {r['gap_pct']:>7.1f} "
              f"{r['volume']:>12,} {str(r['rvol']):>6} {str(r['spread']):>6}"
              f"  {r['news'][:70]}")
    print(f"\n{len(rows)} candidates -> appended to {CSV_PATH.name}")


if __name__ == "__main__":
    main()
