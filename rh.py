#!/usr/bin/env python3
"""Robinhood agentic-trading MCP data client — the bot's own OAuth identity.

One-time setup (run it yourself; opens a browser for Robinhood approval):
    .venv/bin/python rh.py auth

After that, scanner/autotrader import this module and call quotes() /
fundamentals() / enrich(); tokens live in .rh_tokens.json (gitignored)
and refresh automatically. available() gates every caller so everything
degrades to Alpaca IEX when Robinhood isn't set up.

CLI:  rh.py auth | quote SYM [SYM...] | fund SYM [SYM...]
"""

import base64
import hashlib
import http.server
import json
import secrets
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import truststore

truststore.inject_into_ssl()

import requests

ROOT = Path(__file__).parent
ORIGIN = "https://agent.robinhood.com"
BASE = f"{ORIGIN}/mcp/trading"
TOKENS = ROOT / ".rh_tokens.json"
REDIRECT_PORT = 8899
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"


def available():
    return TOKENS.exists()


def _load():
    return json.loads(TOKENS.read_text())


def _save(d):
    TOKENS.write_text(json.dumps(d, indent=2))


# --- one-time OAuth: discovery -> dynamic registration -> PKCE flow ----

def _discover():
    as_url = ORIGIN
    for path in ("/.well-known/oauth-protected-resource/mcp/trading",
                 "/.well-known/oauth-protected-resource"):
        r = requests.get(ORIGIN + path, timeout=15)
        if r.ok:
            servers = r.json().get("authorization_servers") or []
            if servers:
                as_url = servers[0].rstrip("/")
                break
    # RFC 8414: for an issuer WITH a path, .well-known goes between host
    # and path; also try the naive suffix form and the bare origin.
    p = urlparse(as_url)
    origin = f"{p.scheme}://{p.netloc}"
    tried = []
    for wk in ("oauth-authorization-server", "openid-configuration"):
        urls = []
        if p.path and p.path != "/":
            urls.append(f"{origin}/.well-known/{wk}{p.path}")
        urls += [f"{as_url}/.well-known/{wk}", f"{origin}/.well-known/{wk}"]
        for url in urls:
            tried.append(url)
            try:
                r = requests.get(url, timeout=15)
            except requests.RequestException:
                continue
            if r.ok:
                try:
                    m = r.json()
                except ValueError:
                    continue
                if m.get("authorization_endpoint") and m.get("token_endpoint"):
                    return m
    raise RuntimeError("no OAuth metadata; tried:\n  " + "\n  ".join(tried))


def auth():
    meta = _discover()
    reg = meta.get("registration_endpoint")
    if not reg:
        raise RuntimeError(
            "server offers no dynamic client registration — check Robinhood "
            "agentic docs for how to obtain a client_id, then add it to "
            ".rh_tokens.json manually")
    r = requests.post(reg, json={
        "client_name": "cameron-orb-bot",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, timeout=15)
    r.raise_for_status()
    client_id = r.json()["client_id"]

    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    q = {"response_type": "code", "client_id": client_id,
         "redirect_uri": REDIRECT_URI, "state": state,
         "code_challenge": challenge, "code_challenge_method": "S256",
         "resource": BASE}
    if meta.get("scopes_supported"):
        q["scope"] = " ".join(meta["scopes_supported"])
    url = meta["authorization_endpoint"] + "?" + urlencode(q)

    box = {}

    class CB(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            box.update({k: v[0] for k, v in
                        parse_qs(urlparse(self.path).query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Authorized &mdash; you can close this tab.</h2>")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), CB)
    srv.timeout = 600
    print("Opening browser for Robinhood authorization...\nIf it doesn't "
          "open, paste this URL yourself:\n\n" + url + "\n")
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    while "code" not in box and "error" not in box:
        srv.handle_request()
    if "error" in box:
        raise RuntimeError(f"authorization denied: {box}")
    if box.get("state") != state:
        raise RuntimeError("state mismatch — possible CSRF, aborting")

    tok = requests.post(meta["token_endpoint"], data={
        "grant_type": "authorization_code", "code": box["code"],
        "redirect_uri": REDIRECT_URI, "client_id": client_id,
        "code_verifier": verifier, "resource": BASE}, timeout=15)
    tok.raise_for_status()
    t = tok.json()
    _save({"client_id": client_id, "token_endpoint": meta["token_endpoint"],
           "access_token": t["access_token"],
           "refresh_token": t.get("refresh_token"),
           "expires_at": time.time() + t.get("expires_in", 3600) - 60})
    print(f"✓ authorized, tokens saved to {TOKENS.name}")


def _access_token():
    d = _load()
    if time.time() >= d["expires_at"]:
        if not d.get("refresh_token"):
            raise RuntimeError("access token expired and no refresh token — "
                               "re-run: rh.py auth")
        r = requests.post(d["token_endpoint"], data={
            "grant_type": "refresh_token",
            "refresh_token": d["refresh_token"],
            "client_id": d["client_id"], "resource": BASE}, timeout=15)
        r.raise_for_status()
        t = r.json()
        d["access_token"] = t["access_token"]
        d["refresh_token"] = t.get("refresh_token", d["refresh_token"])
        d["expires_at"] = time.time() + t.get("expires_in", 3600) - 60
        _save(d)
    return d["access_token"]


# --- MCP JSON-RPC over streamable HTTP ---------------------------------

_session_id = None
_initialized = False
_reqid = 0


def _post(payload, _retry=True):
    global _session_id
    h = {"Authorization": f"Bearer {_access_token()}",
         "Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    if _session_id:
        h["Mcp-Session-Id"] = _session_id
    r = requests.post(BASE, json=payload, headers=h, timeout=30)
    if r.status_code == 404 and _session_id and _retry:
        _session_id = None  # server dropped our session: re-handshake
        _reinit()
        return _post(payload, _retry=False)
    r.raise_for_status()
    sid = r.headers.get("Mcp-Session-Id") or r.headers.get("mcp-session-id")
    if sid:
        _session_id = sid
    if "text/event-stream" in r.headers.get("Content-Type", ""):
        for line in r.text.splitlines():
            if line.startswith("data:"):
                data = json.loads(line[5:].strip())
                if "result" in data or "error" in data:
                    return data
        raise RuntimeError("empty SSE response")
    return r.json() if r.text.strip() else {}


def _reinit():
    global _initialized
    _initialized = False
    _ensure_init()


def _ensure_init():
    global _initialized
    if _initialized:
        return
    res = _post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "cameron-orb-bot",
                                           "version": "0.1"}}}, _retry=False)
    if "error" in res:
        raise RuntimeError(str(res["error"])[:200])
    try:
        _post({"jsonrpc": "2.0", "method": "notifications/initialized"},
              _retry=False)
    except requests.HTTPError:
        pass  # some servers 202/405 the notification; not fatal
    _initialized = True


def call(tool, **args):
    global _reqid
    _ensure_init()
    _reqid += 1
    res = _post({"jsonrpc": "2.0", "id": _reqid, "method": "tools/call",
                 "params": {"name": tool, "arguments": args}})
    if "error" in res:
        raise RuntimeError(str(res["error"])[:200])
    result = res.get("result") or {}
    text = next((c["text"] for c in result.get("content") or []
                 if c.get("type") == "text"), "{}")
    if result.get("isError"):
        raise RuntimeError(text[:200])
    return json.loads(text)


# --- data helpers -------------------------------------------------------

def _num(v):
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def quotes(symbols):
    out = {}
    for i in range(0, len(symbols), 20):
        data = call("get_equity_quotes", symbols=symbols[i:i + 20])
        for it in (data.get("data") or {}).get("results", []):
            q = it.get("quote") or {}
            bid, ask = _num(q.get("bid_price")), _num(q.get("ask_price"))
            regular = (q.get("venue_last_trade_time") or "",
                       _num(q.get("last_trade_price")))
            extended = (q.get("venue_last_non_reg_trade_time") or "",
                        _num(q.get("last_non_reg_trade_price")))
            last = max(regular, extended)[1] or regular[1] or extended[1]
            out[q.get("symbol")] = {
                "bid": bid, "ask": ask, "last": last,
                "spread": round(ask - bid, 3) if bid and ask else None,
            }
    return out


def last_price(symbol):
    return (quotes([symbol]).get(symbol) or {}).get("last")


def fundamentals(symbols):
    out = {}
    for i in range(0, len(symbols), 10):
        data = call("get_equity_fundamentals", symbols=symbols[i:i + 10])
        for it in (data.get("data") or {}).get("results", []):
            out[it["symbol"]] = {
                "float": _num(it.get("float")),
                "avg_volume_30d": _num(it.get("average_volume_30_days")),
                "day_volume": _num(it.get("volume")),
            }
    return out


def enrich(symbols):
    """NBBO quote + float/volume fundamentals, keyed by symbol."""
    qs, fs = quotes(symbols), fundamentals(symbols)
    return {s: {**(qs.get(s) or {}), **(fs.get(s) or {})} for s in symbols}


def _utc_z(iso):
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(iso)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bars(symbol, start_iso, end_iso, interval="minute"):
    """Minute OHLC bars (consolidated), normalized to float open/high/
    low/close/volume + begins_at. Upstream names fields open_price etc."""
    data = call("get_equity_historicals", symbols=[symbol],
                start_time=_utc_z(start_iso), end_time=_utc_z(end_iso),
                interval=interval, bounds="regular")
    out = []
    for d in _walk_dicts(data):
        if d.get("interpolated"):
            continue
        norm = {}
        for k in ("open", "high", "low", "close"):
            v = d.get(k, d.get(f"{k}_price"))
            if v is not None:
                norm[k] = float(v)
        if "high" in norm and "low" in norm:
            norm["volume"] = float(d.get("volume") or 0)
            norm["begins_at"] = d.get("begins_at") or d.get("timestamp")
            out.append(norm)
    return out


def _walk_dicts(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk_dicts(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_dicts(v)


# server-side saved scan "Cameron Gap Scanner": gap>10%, $2-20,
# float<20M, rvol>5 — evaluated on consolidated all-day data.
# account-specific: override via RH_SCAN_ID in .env
import os as _os

SCAN_ID = _os.environ.get("RH_SCAN_ID",
                          "0f1f9132-ca73-4ed5-8579-52e2bbd185dc")


def scan_tickers():
    """Run the saved gap scan; returns list of matching tickers."""
    data = call("run_scan", scan_id=SCAN_ID)
    out = []
    for d in _walk_dicts(data):
        t = d.get("ticker")
        if isinstance(t, str) and t and t not in out:
            out.append(t)
    return out


# server-side saved scan "Cameron HOD" (created 2026-07-22): $2-20,
# +10% on the day, +3% in the last 5 minutes (the "moving NOW" burst
# that makes it a HOD-momo scan rather than a gainers list), float
# <20M, rvol>5. Polled intraday for hod-dip detections.
HOD_SCAN_ID = _os.environ.get("RH_HOD_SCAN_ID",
                              "d2b1f12d-55aa-491b-980f-db46076dc964")


def hod_tickers():
    """Run the saved HOD-momo scan; returns list of matching tickers."""
    data = call("run_scan", scan_id=HOD_SCAN_ID)
    out = []
    for d in _walk_dicts(data):
        t = d.get("ticker")
        if isinstance(t, str) and t and t not in out:
            out.append(t)
    return out


def sync_watchlist(name, symbols):
    """Mirror today's picks into a Robinhood watchlist (create if absent)."""
    symbols = [s.upper() for s in symbols]
    lists = call("get_watchlists")
    lid = next((d["id"] for d in _walk_dicts(lists)
                if d.get("display_name") == name and d.get("id")), None)
    if lid is None:
        created = call("create_watchlist", display_name=name,
                       display_description="Auto-synced ORB scanner picks",
                       icon_emoji="🎯")
        lid = next((d["id"] for d in _walk_dicts(created)
                    if d.get("id")), None)
        if lid is None:
            raise RuntimeError("create_watchlist returned no id")
    items = call("get_watchlist_items", list_id=lid)
    current = {d["symbol"].upper() for d in _walk_dicts(items)
               if d.get("symbol")}
    add = sorted(set(symbols) - current)
    stale = sorted(current - set(symbols))
    if add:
        call("add_to_watchlist", list_id=lid, symbols=add)
    if stale:
        call("remove_from_watchlist", list_id=lid, symbols=stale)
    return {"added": add, "removed": stale}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "auth"
    if cmd == "auth":
        auth()
    elif cmd == "quote":
        print(json.dumps(quotes([s.upper() for s in sys.argv[2:]]), indent=2))
    elif cmd == "fund":
        print(json.dumps(fundamentals([s.upper() for s in sys.argv[2:]]),
                         indent=2))
    else:
        sys.exit(__doc__)
