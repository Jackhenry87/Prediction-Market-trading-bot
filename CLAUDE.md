# CLAUDE.md — Prediction-Market Trading Bot (Kalshi)

Project memory for Claude Code. Global defaults live in `~/.claude/CLAUDE.md`
(a copy of that file is kept at `docs/global-CLAUDE.md`); this file adds the
rules specific to this repo. **This bot trades real money on Kalshi via
GitHub Actions cron — treat every change to an order path as a production
change.**

## Non-negotiable safety rails (never weaken these)

- Every order must pass `safety.check_order()` before anything is signed or
  sent. Never add an order path that skips it, and never relax its checks
  without the owner explicitly asking.
- `DRY_RUN` defaults to **true** (`config.py`). Never flip that default.
- `KILL_SWITCH=true` must always block all order placement.
- Limit orders only — every order names its price. No market orders.
- Sizing caps (`MAX_ORDER_SIZE`, `MAX_TOTAL_EXPOSURE`, `MAX_ORDER_PCT`,
  `MAX_THEME_PCT`, one bet per event) are enforced on every order; a
  breaching order is rejected and logged, never trimmed-and-placed silently.
- Exposure computation must **fail closed**: if current exposure can't be
  determined, no order is placed.
- Ask the owner before: changing order-placement or sizing logic, enabling a
  model in `ENABLED_MODELS`, touching `KALSHI_ENV`, or anything else that
  changes what real orders get placed.

## Secrets

- Keys live in `.env` locally (gitignored) and in GitHub repo Secrets on
  Actions (`KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PEM`, `ODDS_API_KEY`,
  `FRED_API_KEY`). Never commit, print, or log them.
- All configuration is read through `config.py` — nothing else reads
  `os.environ`. Keep it that way; add new settings there with validation.
- Runtime knobs (`DRY_RUN`, `KILL_SWITCH`, caps, `ENABLED_MODELS`, …) are
  GitHub repo **Variables**, changeable without code edits. Prefer adding a
  knob over hardcoding a number.

## Layout

- Strategies: `strategy_weather.py`, `strategy_sports.py`,
  `strategy_macro.py`, `strategy_crypto.py`, `strategy_commodities.py` —
  each is independently runnable read-only (prints signals, places nothing).
- Pipeline: `auto_trade.py` (scan → risk caps → limit orders → commit
  ledgers). Exchange access: `kalshi_client.py`. Gate: `safety.py`.
- Automation: `.github/workflows/` (hourly `autotrade.yml` is the live one).
- Side components: `paperbook/` (FastAPI paper sportsbook),
  `dfs_analyzer.py`, `deploy/` (optional VPS runner).

## Machine-owned files — do not hand-edit

`HISTORY.md`, `SCOREBOARD.md`, `trade_history.csv`, `executed_trades.csv`,
`paper_trades_macro.csv`, `dfs_picks.csv`, and anything under `logs/` are
written by the workflows/scripts. Fix the generator, not the output.
Workflow commits marked `[skip ci]` are the bot's own ledger commits.

## Conventions & validation

- Python 3.12, deps pinned in `requirements.txt` (keep it minimal).
- Every strategy and safety change needs a test in `tests/` — run
  `pytest tests/` before finishing; `tests/test_safety.py` must always pass.
- Docstrings explain the *why* (edge hypothesis, safety intent) — keep that
  style. Update `README.md` tables when adding/changing models or workflows.
- Historical note: the project started on Polymarket; that code was removed.
  Don't reintroduce Polymarket paths — everything targets Kalshi Trade API v2.
