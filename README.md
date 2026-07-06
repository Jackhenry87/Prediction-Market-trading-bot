# Prediction-Market Trading Bot (Kalshi)

An automated trading system for **Kalshi** (CFTC-regulated, legal for US
users) that runs entirely on GitHub Actions — no laptop or server needed.
It scans several market families for mispricings, sizes orders off the
live bankroll, and records every signal and execution for scoring.

> The project started against Polymarket's crypto CLOB, but that exchange
> geoblocks US trading, so everything targets Kalshi's Trade API v2. The
> Polymarket-era code was removed once the pivot was complete (it lives in
> git history if ever needed).

## Hard rules (enforced on every order path)

- **DRY_RUN** — `true` logs what would happen and places zero orders.
  Live trading only because the owner explicitly enabled it.
- **Limits** — `MAX_ORDER_SIZE` and `MAX_TOTAL_EXPOSURE` are checked on
  every order; a breaching order is rejected and logged, never placed.
- **Kill switch** — `KILL_SWITCH=true` (repo Variable, settable from a
  phone) blocks all order placement.
- **Dynamic sizing** — per-order cost is also capped at `MAX_ORDER_PCT`%
  of the live bankroll; one theme (weather/sports/...) may not exceed
  `MAX_THEME_PCT`% of bankroll; one bet per event.
- **Limit orders only** — every order names its price; slippage is treated
  as the default outcome, never a surprise.
- **Logging** — every attempt/result/rejection goes to `logs/` and executed
  trades are committed to `executed_trades.csv`.
- **No secrets in code** — keys live in `.env` locally (gitignored) and in
  repo Secrets on Actions.

## The models

| Model | File | Edge hypothesis |
|-------|------|-----------------|
| weather | `strategy_weather.py` | NWS station forecasts reprice Kalshi's daily high-temp buckets slower than the forecast updates. Normal(forecast, SIGMA_F) prices each bucket. |
| sports | `strategy_sports.py` | Devigged sportsbook consensus (Shin's method per book, Pinnacle weighted 3×) vs Kalshi moneylines, with a line-movement (steam) filter: only trade sides the sharp line moved toward. |
| macro | `strategy_macro.py` | Resolution lag: after a macro print (claims, CPI, payrolls, U3) the correct side is *known*; buy it if still cheap. Fresh-release gate + strict reference-period matching. OFF by default. |
| crypto | `strategy_crypto.py` | Lognormal option-style pricing of BTC/ETH threshold markets. OFF (weak edge). |
| commodities | `strategy_commodities.py` | Same approach for oil/gas/metals thresholds. OFF (weak edge). |

`ENABLED_MODELS` (repo Variable) controls which run; default `weather,sports`.

## Automation (GitHub Actions)

| Workflow | Schedule | What it does |
|----------|----------|--------------|
| `autotrade.yml` | hourly 12:00–01:00 UTC | Full pipeline: scan enabled models → risk caps → place limit orders → commit ledgers + `SCOREBOARD.md`. |
| `release-capture.yml` | Thu/Fri release windows | Paper-only burst around 8:30am ET macro releases to measure the resolution lag before arming `macro` live. |
| `calibrate-weather.yml` | manual | Measures a year of real forecast error per station (Open-Meteo archives) to tune SIGMA_F. Read-only. |
| `analyze-dfs.yml` | manual | +EV analysis of DFS (PrizePicks/Underdog) picks in `dfs_picks.csv`. |
| `backfill.yml` | manual | One-time import of Kalshi fills/settlements into `HISTORY.md`. |

Control knobs without code changes (repo → Settings → Secrets and
variables → Actions → **Variables**): `DRY_RUN`, `KILL_SWITCH`,
`ENABLED_MODELS`, `MAX_ORDER_SIZE`, `MAX_TOTAL_EXPOSURE`, `MAX_ORDER_PCT`,
`MIN_ORDER_PCT`, `MAX_THEME_PCT`, `TAKE_PROFIT_PCT`, `TRADE_MIN_PRICE`,
`TRADE_MAX_PRICE`, `KALSHI_ENV`.

**Secrets** required: `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PEM`,
`ODDS_API_KEY` (sports), `FRED_API_KEY` (macro).

## Results

- **`SCOREBOARD.md`** — per-model paper/live signal scoring, refreshed by
  every run.
- **`executed_trades.csv`** — audit trail of every real order placed.
- **`HISTORY.md`** — account history (fills + settlements) since July 1.

## Local setup (optional — the cloud runs without it)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # fill in Kalshi key ID + .pem path
python strategy_weather.py # any strategy runs read-only, zero orders
python auto_trade.py       # full pipeline (respects DRY_RUN)
pytest tests/              # test suite
```

Manual tools: `kalshi_fetch_orderbook.py` (balance + book),
`kalshi_place_order.py` (one gated order), `kalshi_cancel_orders.py`
(list/cancel resting orders), `backfill_history.py`, `calibrate_weather.py`.

## Other components

- `dashboard/` — read-only live web dashboard (`uvicorn dashboard.app:app`):
  animated fill feed, P&L chart, model scoreboard. Live-polls the account
  with keys, replays `trade_history.csv` without. See `dashboard/README.md`.
- `paperbook/` — a free-to-play paper sportsbook web app (FastAPI +
  SQLite) with user accounts; `paperbook_client.py` lets the bot trade it.
- `dfs_analyzer.py` — devig-based +EV picker for DFS slates.
- `deploy/` — optional $5-VPS runner for tighter macro release timing if
  GitHub cron proves too slow.
- `macro_calendar.py` / `release_runner.py` — release schedule + burst
  runner used by `release-capture.yml`.
