# Prediction-Market Trading Bot (Kalshi)

A research harness for finding a real edge on **Kalshi** (CFTC-regulated, legal
for US users), running on GitHub Actions. It currently runs **one model, on
paper, with no credentials and no money at risk**, and its only job is to
produce enough closing-line-value samples to prove or kill that model.

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
edge = FAV_BIAS_CENTS        <- the assumption under test
     + (ask - entry)         <- spread kept by resting instead of crossing
     + taker_fee_cents(ask)  <- fee not paid because we're the maker
```

Only the first term is a claim. The other two are arithmetic. If mean CLV over
100+ **filled** samples is positive, the claim survived; if not, this model
dies like the others. That is the entire point of running it on paper.

It needs **no API keys** — public Kalshi market data only. The dependency that
silently died last time cannot break this one.

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

- **Paper-only by construction.** `strategy_favorite.py` imports no
  order-placing code. There is no flag that makes it trade.
- **No secrets.** The one running workflow uses unauthenticated public
  endpoints.
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
| `paper-favorite.yml` | every 20 min (see note) | Scan public Kalshi data → record paper signals → snapshot open prices → refresh `CLV_SCOREBOARD.md`. |

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

Real money returns only at **100+ scored samples with a positive mean**.

## Local setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r paperbook/requirements.txt pytest
python strategy_favorite.py   # read-only scan, places nothing
python clv_tracker.py         # snapshot + scoreboard
python -m pytest -q           # 283 tests
```

## Other components

- `paperbook/` — a free-to-play paper sportsbook web app (FastAPI + SQLite).
- `dfs_analyzer.py` — devig-based +EV picker for DFS slates (manual).
- The retired strategy modules are still present and still tested; they are
  simply not scheduled. `strategy_weather.py` also holds the shared
  `price_cents` / `taker_fee_cents` helpers that the live model imports.
