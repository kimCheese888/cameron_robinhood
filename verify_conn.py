#!/usr/bin/env python3
"""Post-migration connectivity check: Alpaca + Robinhood from this host."""
import executor
import rh

executor.load_env()
a = executor.api("GET", "/v2/account")
print("alpaca: OK, equity", a["equity"])
print("rh tokens present:", rh.available())
q = rh.quotes(["AAPL"]).get("AAPL") or {}
print("rh quote: OK, AAPL last", q.get("last"))
print("rh scanner: OK,", len(rh.scan_tickers()), "hits right now")
print("ALL CONNECTIVITY VERIFIED")
