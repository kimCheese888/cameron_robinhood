# Cameron — Ross Cameron ORB strategy, automated (paper trading)

Automated small-cap momentum day trading (Opening Range Breakout, Warrior
Trading style) against an **Alpaca paper account**, with market data from
the **Robinhood agentic-trading MCP** (consolidated NBBO quotes, minute
bars, float, server-side gap scanner). One live strategy plus four shadow
variants run side-by-side for A/B comparison.

> **This is a research project.** It trades a simulated account. Nothing
> here is financial advice; the strategy is unproven — that's what the
> experiment is for.

## Architecture

```
scanner.py     premarket funnel: Alpaca movers + RH server-side gap scan
               → price/gap/volume filters → RH NBBO spread + float filters
autotrader.py  daily session loop: 9:15 ET scan → 9:36 arm 5-min ORB →
               server-side stop-limit entries (top picks) + poll fallback →
               manage → 11:00 ET flatten. Shadow variants (full-2R, 15-min
               range, volume-confirm, buy-the-dip) fill variants.csv
executor.py    split-bracket orders (half @1R, half @2R, breakeven move),
               daily loss circuit breaker, flatten kill switch
rh.py          Robinhood MCP client with its own OAuth (PKCE + dynamic
               client registration), quotes/bars/fundamentals/scanner/
               watchlist sync
journal.py     append-only decision log (events.jsonl) — every filter
               verdict, order, and exit with the numbers behind it
dashboard.py   localhost:8787 — equity curve, strategy panel, candlestick
               chart with trade levels, decision journal (auto-refresh)
```

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

cat > .env <<EOF
APCA_API_KEY_ID=PK...            # Alpaca paper keys
APCA_API_SECRET_KEY=...
APCA_API_BASE_URL=https://paper-api.alpaca.markets
# RH_SCAN_ID=...                 # optional: your own saved scan id
EOF

.venv/bin/python rh.py auth      # one-time Robinhood OAuth (opens browser)
.venv/bin/python executor.py     # verify: prints account status
.venv/bin/python autotrader.py --test   # dry-run the scanner
```

On a headless server, run the OAuth step through an SSH tunnel:
`ssh -L 8899:localhost:8899 server` then open the printed URL locally.

## Running

- **macOS**: `bash setup_launchd.sh` (launchd: start at boot, restart on
  crash, caffeinate keeps the machine awake — keep it on AC power).
- **Linux**: `sudo cp deploy/*.service /etc/systemd/system/ &&
  sudo systemctl enable --now cameron-autotrader cameron-dashboard`
- Dashboard binds 127.0.0.1 only (no auth). On a server, view it through
  an SSH tunnel: `ssh -L 8787:localhost:8787 server`.

## Safety rails

$100 fixed risk per trade · max 2 positions · max 4 entries/day · long
only · −$300 daily circuit breaker (flatten + refuse entries) · 11:00 ET
hard flatten · single-instance file lock · idempotent client order ids ·
all exits live server-side as bracket legs (survive process death).

## Timezone

All session logic is US/Eastern via `zoneinfo` — server system timezone
does not matter.
