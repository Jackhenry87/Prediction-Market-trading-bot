# Prediction-Market Trading Bot (Kalshi)

A research harness for finding a real edge on **Kalshi** (CFTC-regulated, legal
for US users), running on GitHub Actions. It runs **one model, live on Kalshi with a $50 bankroll**, sized by quarter
Kelly off each signal's own edge. Its job is to produce closing-line-value and
real fill-rate data that either proves the model or kills it.

## Where this stands

The first generation of this bot traded eight models with real money and lost
**-$33.42 of $50** (39W / 86L). The full record is in `HISTORY.md`,
`kalshi_report.csv` and `executed_trades.csv`, which are kept deliberately.
The post-mortem, in short:

| Evidence | Reading |
|---|---|
| Brier 0.308 (weather), 0.278 (sports) vs 0.25 for a coin flip | The models were worse than guessing |
| Avg CLV -11.6¢ (weather), -6.0¢ (sports) | We were consistently on the wrong side of the price |
| -$27.10 of the -$33.42 from one city, mostly one trade | Position limits arrived after the accident |
| 0 usable CLV samples in a month | The instrument measuring "do we have an edge" was broken |
| Sports odds API returning 401 for weeks, every run still green | `\|\| true` on every step hid it |

So the models were retired, the measurement was fixed first, and one
research-backed model replaced them.

## The one model: favourite-bias, maker-only

`strategy_favorite.py`. It does not try to out-forecast anything. It bets on
two documented structural facts about the venue:

1. **Favourite-longshot bias.** Low-priced contracts are systematically
   overpriced and high-priced contracts underpriced — the most replicated
   anomaly in prediction and betting markets. On Kalshi transaction data
   (300k+ contracts, Bürgi/Deng/Whelan), contracts under 10¢ lose over 60% of
   stake while high-priced contracts return a small positive amount.
2. **Makers beat takers.** The same study splits by execution style: on
   longshots takers lose ~32%, makers ~10%. Kalshi charges makers **no fee**;
   takers pay `0.07·p·(1-p)` per contract. Resting instead of crossing is
   worth more than most models' entire claimed edge.

So: buy the **favourite** side, only ever as a **maker**, and measure with CLV.

```
edge = (mid - entry)                      <- mechanical: we rest below the
                                             market's own midpoint
     + FAV_BIAS_CENTS * BIAS_CONFIDENCE   <- the assumption under test
```

Edge is measured against **fair value (the mid)**, not against the ask. An
earlier version counted `ask - entry` — the saving a taker forgoes by crossing
the spread — as edge. That is not what we gain over fair value, and it inflated
every signal roughly threefold and every position size derived from it.

Only the second term is a claim; the first is arithmetic. If mean CLV over 100+
**filled** samples is positive, the claim survived. If not, this model dies like
the others.

The scan needs **no API keys** — public Kalshi market data only, so the kind of
silent credential expiry that blinded the sports model for weeks cannot happen
here. Placing orders does use the Kalshi key.

## Why this model and not the other eight

| Strategy | Verdict |
|---|---|
| weather | Brier 0.308 vs 0.25 coin flip, CLV −11.6¢, −$27 of the −$33. Retired. |
| sports | Brier 0.278, CLV −6.0¢, and needs a paid odds API whose free tier covers ~3 days a month. Retired. |
| crypto / commodities | Lognormal pricing of BTC/oil thresholds — competing with actual options desks. Never produced a signal. Retired. |
| macro resolution-lag | Sound idea, wrong infrastructure: the edge decays in minutes and GitHub cron lags by tens of minutes. Structurally infeasible here. |
| smart-money copy | Premise is that big Polymarket wallets are sharp. The largest study of both venues finds the most capitalised traders systematically *underperform* smaller ones. Premise inverted. |
| NRFI Martingale | 1-2-4 doubling. Martingale converts a small edge into eventual ruin with certainty. Should never be re-enabled. |
| risk-free arb | Genuinely real, but capacity-constrained and latency-competitive; on a $50 book the absolute return is pennies. |
| **favourite-bias maker** | **Structural rather than predictive, externally replicated, no paid dependency, 23 live signals on first real scan.** |

On "a new strategy profitable for a short time": that instinct is right, and this
model already captures it. Newly listed series have wide, thin books, and the
edge here *is* the spread — resting at bid+1 in a 85/95 book buys 4¢ below fair
value versus 0¢ in an 89/91 book. No special-casing needed; wide books
automatically size larger.

## Position sizing

Units are **not** a flat percentage. `sizing.py` sizes each signal off its own
edge, using Kelly for a binary contract:

```
f* = edge_cents / (100 - entry_cents)      # edge divided by what you can win
```

Buying an 89¢ favourite risks 89¢ to win 11¢, so even a 2¢ edge is 18% of
bankroll at full Kelly. Full Kelly also carries an expected peak-to-trough
drawdown near 50% *even when the edge is real*. We use **quarter Kelly**:
variance scales with the square of the fraction, so it cuts variance ~94% while
keeping ~44% of the growth rate and capping expected drawdown near 12%. That is
the standard response to an uncertain edge, a small bankroll and correlated
positions — this strategy has all three.

The edge itself is split by how much we trust it:

- **mechanical** (`mid − entry`) — certain given a fill; we rest below the
  market's own midpoint.
- **assumed** (the favourite-longshot correction) — the research claim under
  test, multiplied by `BIAS_CONFIDENCE` (starts at 0.5). Raise it only when the
  CLV scoreboard earns it.

Two signals with the same headline edge size differently if one leans harder on
the unproven part. On a $50 bankroll this yields 1–2 contracts at $1.14–$2.50:

| entry | mid | edge | full Kelly | used | stake | units |
|---|---|---|---|---|---|---|
| 89¢ | 89.5 | 1.00¢ | 9.1% | 2.3% | $1.14 | 1 |
| 89¢ | 90 | 1.50¢ | 13.6% | 3.4% | $1.70 | 1 |
| 89¢ | 91 | 2.50¢ | 22.7% | 5.0% | $2.50 | 2 |
| 86¢ | 90 | 4.50¢ | 32.1% | 5.0% | $2.50 | 2 |

A stake below one contract is **zero** contracts, never one — rounding a
sub-minimum stake up is how a sized model quietly becomes a flat-stake punter
on its weakest signals. `MAX_BANKROLL_PCT` (5%) is a hard backstop above Kelly.

## Honest limits of the thesis

- The favourite-side excess return in the literature is **small** (low single
  digit percent). It can be eaten by adverse selection.
- A resting bid fills *precisely when someone wants to sell to you*, which is
  correlated with the price about to move against you. The CLV tracker only
  counts a signal once the book actually reached our price, and reports
  unfilled orders separately, but this is the risk most likely to kill the edge.
- Sample independence is imperfect: `FAV_MAX_PER_EVENT` caps correlated
  markets, but 100 samples across a few event types is not 100 independent draws.

## Hard rules

- **One order path.** `favorite_runner.py` is the only code that can place an
  order, and every order passes `safety.check_order`. `strategy_favorite.py`
  itself still imports no order-placing code.
- **Maker only, re-checked live.** The book moves between scanning and placing,
  so the price is re-verified against the current ask before every order and
  re-priced or skipped if it would cross.
- **Stop from a phone.** Repo Variable `KILL_SWITCH=true` blocks every order at
  the gate. `DRY_RUN=true` logs orders without sending them.
- **Merges are gated.** `.github/workflows/tests.yml` runs the full suite on
  every PR. Make it a required check in Settings → Branches.
- **State is not on main.** Live state lives on the `bot-state` branch.

The order-safety machinery (`safety.py`, `kalshi_exposure.py`, the caps in
`auto_trade.py`) is intact and tested, for whenever a model earns real money
again.

## Workflows

| Workflow | Schedule | What it does |
|----------|----------|--------------|
| `tests.yml` | every PR + push to main | Full pytest suite. The merge gate. |
| `favorite.yml` | every 20 min (see note) | Scan → size → place maker orders → confirm fills against the exchange → refresh `CLV_SCOREBOARD.md`. |
| `env-check.yml` | manual | Verifies the odds key and compares Kalshi venues. |

> Cron note: a `*/15` schedule previously produced ~14 runs/day, not 96 —
> GitHub drops scheduled runs under load. The model only signals on markets
> with 2h+ to close so a sparse cadence still captures a closing line.

Everything else was deleted (`live-bots`, `autotrade`, `refresh-account`,
`calibrate-weather`, `release-capture`, the arb and demo runners, the probes).
They are all recoverable from git history if a model ever earns its way back.

## Reading the result

`CLV_SCOREBOARD.md` on the `bot-state` branch is the only number that matters:

```
- Scored samples: N / 100
- Mean CLV: +X.Xc per bet
### Sample accounting
- resting / filled-and-open / expired unfilled / unscored
```

`unscored` is a **measurement failure**, not a result — it means a market
closed before the tracker ever saw it open. The old tracker hid those; this one
reports them, because four silently-dropped rows are how "0 settled bets" was
mistaken for "no data yet" for a month.

The model is live now, at deliberately small size, because one question can
only be answered with real orders: **would a resting maker bid actually be
filled?** The paper proxy ("the ask reached our price") ignores queue position —
other orders sit ahead of yours at the same price. `mark_real_fills()` resolves
every resting order against Kalshi's own fills endpoint, so `fill_status` is
the exchange's answer, not an assumption.

Scale up only at **100+ scored samples with a positive mean CLV**.

## Local setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r paperbook/requirements.txt -r requirements-dev.txt
python strategy_favorite.py   # read-only scan, places nothing
python clv_tracker.py         # snapshot + scoreboard
python -m pytest -q           # 283 tests
```

## Copy-trading a leaderboard trader

`follow_*.py` mirrors a Kalshi leaderboard account. It is **off by default**
(`FOLLOW_ENABLED=false`) and **paper by default** (`FOLLOW_DRY_RUN=true`).

### How it sees his trades

Kalshi's *public* API cannot attribute a trade to anyone — `GET
/trade-api/v2/markets/trades` carries no user field, and `/leaderboard` and
`/social/*` are 404. But the routes its own app uses do exist:

| Path | Response |
|---|---|
| `/v1/users/{username}/trades` | 401 — exists, needs auth |
| `/v1/users/{username}/positions` | 401 — exists, needs auth |
| `/v1/users/{username}/orders` | 401 — exists, needs auth |
| `/v1/users/{username}/nonsense` | 404 — the router knows the real ones |

`follow_feed.py` polls `/trades` every `FOLLOW_POLL_SECONDS` (20s default) and
diffs the result, so detection is bounded by the poll interval and carries the
exact ticker, side, price and size.

**These are undocumented endpoints.** They can change or close without notice,
so the feed **fails loudly**: a `FeedError` stops the runner rather than
returning an empty list, because a silent feed is indistinguishable from "he
stopped trading" — the failure mode that would leave the copier looking
healthy and permanently blind.

### Setup

1. Fill in your Kalshi API key and `.pem` path in `.env` (needed for trading
   regardless).
2. Find out which auth Kalshi accepts on `/v1`:

   ```bash
   python follow_feed.py --probe
   ```

   It tries the RSA API key first, then `FOLLOW_SESSION_TOKEN`, and prints the
   response shape and field names. If the API key works you never need to
   refresh anything. If it does not, paste a session token from a logged-in
   browser into `FOLLOW_SESSION_TOKEN` — those **expire**, and the feed alarms
   when one that was working stops.
3. Run `follow_runner.py` (see `deploy/follow-runner.service`).

**The first run copies nothing.** `follow_feed` seeds every trade already on
his record as "already seen" and logs `COLD START: seeded N existing
trade(s)`. Without that, arming the copier would replay his ~123 historical
trades as ~123 simultaneous orders into markets that mostly settled weeks ago.
The cursor persists in `follow_seen.json`, so a restart is equally safe.

### How a copy is decided

His trade is the **trigger**; our own model is the **reason**; our bankroll is
the **size**.

1. `follow_feed` polls and diffs; new trades only, entries only (sales are
   skipped — copying an exit as a buy would be backwards).
2. The feed usually names a ticker, so no prose matching is needed.
   `follow_resolve` remains the fallback when only a title and outcome come
   back; ambiguity there resolves to nothing rather than a guess.
3. `follow_prob` asks **our** models for `p`. **No model view → no order.**
   Coverage is roughly a third of his activity (MLB/NBA moneylines, totals,
   first-inning runs); soccer, golf, tennis, UFC, spreads and combos are
   skipped by design.
4. Sizing is quarter Kelly on `p` at the price **we** would pay now:
   `f* = edge/(100-c)`, which is `(p·b - q)/b` with `b = (100-c)/c`.
5. `safety.check_order` and every existing hard rail still apply.

### Why it is this suspicious

His profile reads +$442,763 on $2.6M volume, Top 1% P&L. Reconstructed from
his 115 settled positions:

- 61W–54L (53.0%) — 0.7σ from a coin flip on that sample;
- his top 5 trades are $586,308, **131% of his net profit**. Excluding them
  the record is **−$137,632**;
- 52% of his losses land on round $1,000 tiers ($100,000.00, $50,000.00,
  $20,000.00 …), which is hand-typed sizing, not Kelly output — there is no
  sizing model of his to copy;
- he moves 100k–390k contracts per position, so **his own order is the price
  move**. `FOLLOW_MAX_SLIP_CENTS` (default 3¢) skips any signal where the line
  has already run past his entry. Polling shortens our lag; it does not make
  us early.

Run `python follow_scoreboard.py` to write `FOLLOW_SCOREBOARD.md`. Flip
`FOLLOW_DRY_RUN=false` only when that shows a positive settled net after fees
— his record is not evidence that *we* profit.

## Other components

- `paperbook/` — a free-to-play paper sportsbook web app (FastAPI + SQLite).
- `dfs_analyzer.py` — devig-based +EV picker for DFS slates (manual).
- The retired strategy modules are still present and still tested; they are
  simply not scheduled. `strategy_weather.py` also holds the shared
  `price_cents` / `taker_fee_cents` helpers that the live model imports.
