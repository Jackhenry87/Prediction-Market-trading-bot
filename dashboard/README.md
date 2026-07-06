# Dashboard — live "business page" for the bot

A single-page, read-only web dashboard: live-animated fill feed, ticker
tape, bankroll & P&L tiles, cumulative-P&L chart, per-model scoreboard,
and open positions. **It can never place, change, or cancel an order** —
it only reads.

## Run it

```bash
pip install -r requirements.txt -r dashboard/requirements.txt
uvicorn dashboard.app:app --port 8000     # then open http://localhost:8000
```

Two modes, picked automatically:

- **LIVE** — if `.env` has `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH`
  (and `KALSHI_ENV=prod`), it polls your account read-only every
  `DASHBOARD_POLL_SECONDS` (default 20) and animates every new fill,
  settlement, balance change, and price move on held markets.
- **REPLAY** — with no credentials it loops your real `trade_history.csv`
  as an animated stream (clearly badged REPLAY, simulated balance).

## Options (via `.env` / environment)

| Variable | Default | Meaning |
|---|---|---|
| `DASHBOARD_PASSWORD` | _(empty)_ | If set, the page and API require a login. Set it whenever the dashboard is reachable by anyone but you. |
| `DASHBOARD_POLL_SECONDS` | `20` | Live-mode poll cadence (min 5 — be kind to rate limits). |

## Hosting (optional)

`render.yaml` deploys it on Render the same way as `paperbook/`. If you
host it, **set `DASHBOARD_PASSWORD`** and add the Kalshi credentials as
Render secret env vars (`KALSHI_PRIVATE_KEY_PATH` pointing at a secret
file). Hosting is optional — local is the default.
