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

## Hosting on Render (a real URL, live account data)

The app accepts the private key as text (`KALSHI_PRIVATE_KEY_PEM`) —
the same value already stored in GitHub Secrets — so no key file is
needed on the host. Setup:

1. Sign up at render.com with your GitHub account, then
   **New → Web Service** and pick this repo + branch.
2. Settings: Runtime **Python 3**;
   build command `pip install -r requirements.txt -r dashboard/requirements.txt`;
   start command `uvicorn dashboard.app:app --host 0.0.0.0 --port $PORT`.
3. Environment variables:
   - `DASHBOARD_PASSWORD` — pick one; **required on a public URL**
   - `KALSHI_API_KEY_ID` — from Kalshi API settings
   - `KALSHI_PRIVATE_KEY_PEM` — paste the full `.pem` contents,
     `-----BEGIN...` through `...END-----`
   - `KALSHI_ENV` — `prod`
4. Deploy. The page at `https://<name>.onrender.com` shows a green
   **LIVE** pill; the log prints `dashboard LIVE (prod)`.

Free-tier note: the service sleeps after ~15 idle minutes, so the first
visit takes ~30–60 s to wake; the paid Starter tier keeps it always-on.
Security note: the key on the host can trade, even though this app never
does — treat the Render account like you treat GitHub Secrets, and set
that password.
