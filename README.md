# Prediction-Market Trading Bot (Kalshi)

Incremental build of a prediction-market trading bot, now targeting
**Kalshi** (CFTC-regulated, legal for US users) via its Trade API v2.

> **Why Kalshi?** The project started against Polymarket's crypto CLOB,
> but that exchange geoblocks US trading (orders are rejected server-side
> with a 403). The Polymarket scripts remain in the repo for read-only
> market data; all live trading targets Kalshi. Kalshi also provides a
> full fake-money demo environment (demo.kalshi.co), which we use before
> any real-money step.

## Hard rules (every phase)

- **DRY_RUN by default** — with `DRY_RUN=true` in `.env` the bot logs what it
  *would* do and places zero real orders.
- **Limits** — `MAX_ORDER_SIZE` (USDC per order) and `MAX_TOTAL_EXPOSURE`
  (USDC across all open positions) are enforced on every order path; a
  breaching order is rejected and logged, never placed.
- **Kill switch** — `KILL_SWITCH=true` makes the bot refuse to place any order.
- **Logging** — every action (price read, order attempt, result, rejection)
  is written to a timestamped file in `logs/`.
- **No loops** — everything runs once and exits until explicitly changed.
- **No secrets in code** — everything sensitive lives in `.env` (gitignored).
  The private key is never printed or logged.

## Phase plan

| Phase | Status | Scope |
|-------|--------|-------|
| 1 | done (Polymarket), **current (Kalshi)** | Authenticate + fetch and print a live order book. Read-only. |
| 2 | done (Polymarket, blocked by US geoblock), **current (Kalshi)** | Place a single manually-triggered order, respecting DRY_RUN and both limits. Demo env first, then one tiny real order. |
| 3+ | not started | Strategy logic / automation. Edge to be defined first. |

## Kalshi setup

1. Create a **demo** account at demo.kalshi.co (fake money, instant).
2. In the account settings, create an **API key**: you get a Key ID (UUID)
   and a one-time download of an RSA private key `.pem` file. Save the file
   into this project folder (e.g. `kalshi_demo.pem` — `*.pem` is gitignored).
3. `cp .env.example .env` and fill in `KALSHI_API_KEY_ID`,
   `KALSHI_PRIVATE_KEY_PATH`, `KALSHI_ENV=demo`, `MARKET_TICKER`, and the
   safety rails.
4. The `MARKET_TICKER` is shown on every Kalshi market page (e.g.
   `KXHIGHNY-26JUL03-B87.5`).

```bash
python kalshi_fetch_orderbook.py   # auth + balance + live order book
python kalshi_place_order.py       # one order through the safety gate
python kalshi_cancel_orders.py     # list/cancel all resting orders
```

Prices are cents per contract (1-99¢); a contract pays $1 if you're right.
Order parameters are the `ORDER PARAMETERS` block at the top of
`kalshi_place_order.py`. When demo works end to end, switching to real
money is: prod account + prod API key + `KALSHI_ENV=prod`.

## Polymarket setup (legacy, read-only from the US)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env — see below
```

### Filling in .env

| Variable | What to put there |
|----------|-------------------|
| `POLYGON_WALLET_PRIVATE_KEY` | Your Polygon wallet's private key (export from MetaMask: account menu → Account details → Show private key). Never share or commit it. |
| `POLY_SIGNATURE_TYPE` | `0` if your USDC sits directly in the wallet whose key you exported. `1` if your Polymarket account was created with email/magic link. `2` if you sign in to polymarket.com with a browser wallet (funds live in a Polymarket proxy address). |
| `POLY_FUNDER_ADDRESS` | Only for signature type 1 or 2: your Polymarket proxy wallet address (on polymarket.com: profile → the deposit address shown there). Leave empty for type 0. |
| `CLOB_API_URL` / `CHAIN_ID` | Leave the defaults (`https://clob.polymarket.com`, `137`). |
| `MARKET_TOKEN_ID` | The CLOB token ID of one outcome of one market — see below. |
| `DRY_RUN` | Keep `true`. |
| `KILL_SWITCH` | `false` (set `true` to block all orders in later phases). |
| `MAX_ORDER_SIZE` | Max USDC per single order, e.g. `5`. |
| `MAX_TOTAL_EXPOSURE` | Max USDC across all positions, e.g. `20`. |

### Finding your MARKET_TOKEN_ID

Each market outcome (Yes/No) has its own CLOB token ID — a long decimal
number. Open a **specific market** on polymarket.com — a question page
(`/event/...`) or a single game page (`/sports/mlb/...`); listing pages like
`/sports/live` won't work — copy the URL, then run:

```bash
python find_market.py "https://polymarket.com/event/paste-your-market-url"
```

It prints each outcome with its token ID; copy one `MARKET_TOKEN_ID=...`
line into `.env`.

(Manual alternative: open
`https://gamma-api.polymarket.com/markets?slug=<market-slug>` in a browser
and read `clobTokenIds` from the JSON, ordered the same as `outcomes`.)

### Run

```bash
python fetch_orderbook.py
```

Prints the top of the order book (bids/asks, spread, mid) and writes a log
to `logs/bot_<timestamp>.log`. Places no orders.

## Phase 2 pre-flight (one-time, before the first real order)

Direct wallets (`POLY_SIGNATURE_TYPE=0`) must grant Polymarket's exchange
contracts a one-time on-chain approval before real orders can settle, and
must hold **USDC.e** (bridged), not native USDC. Check everything with:

```bash
python preflight.py
```

It reports POL/USDC balances, flags a needed swap, and lists missing
approvals. With `DRY_RUN=true` it never sends a transaction; set
`DRY_RUN=false` and re-run to grant the approvals (costs <$0.01 in POL
total). `KILL_SWITCH=true` blocks all transactions. Safe to re-run anytime.

## Phase 2: placing one manual order

1. Open `place_order.py` and edit the `ORDER PARAMETERS` block at the top:
   `SIDE`, `PRICE` (0–1, in USDC per share), `SIZE_SHARES`. Cost of a BUY is
   `PRICE × SIZE_SHARES`; Polymarket rejects orders worth less than $1.
   Leave `TOKEN_ID` empty to use `MARKET_TOKEN_ID` from `.env`.
2. With `DRY_RUN=true` (default), run:

   ```bash
   python place_order.py
   ```

   The order runs the full gauntlet — kill switch, price/size sanity,
   `MAX_ORDER_SIZE`, `MAX_TOTAL_EXPOSURE` (current positions + open orders),
   tick-size check — and then logs what WOULD have been sent. Nothing is
   placed.
3. For one real order: set `DRY_RUN=false` in `.env`, run it once, then set
   `DRY_RUN=true` again immediately.

Every attempt, rejection, and result is written to `logs/`. If current
exposure cannot be determined (API unreachable), the order is rejected —
the bot fails closed, never open.

## Files

Shared:
- `config.py` — loads/validates `.env`; the only module that touches env vars.
- `trade_logger.py` — timestamped file + console logging.
- `safety.py` — the hard-rules gate every order passes through.

Kalshi (live platform):
- `kalshi_client.py` — authenticated Trade API v2 client (RSA-PSS signing).
- `kalshi_fetch_orderbook.py` — Phase 1: balance + live order book.
- `kalshi_exposure.py` — current exposure (positions + resting buys).
- `kalshi_place_order.py` — Phase 2: one manual order per run.
- `kalshi_cancel_orders.py` — list/cancel all resting orders.

Polymarket (legacy; trading geoblocked in the US):
- `fetch_orderbook.py` — Phase 1 script (read-only order book fetch).
- `find_market.py` — helper to look up a market's token IDs from its URL.
- `clob.py` — shared authenticated CLOB client construction.
- `exposure.py` — current USDC exposure (positions + open BUY orders).
- `place_order.py` — Phase 2 script (one manual order per run).
- `preflight.py` — balance/approval checks + one-time exchange approvals.
- `cancel_orders.py` — list and cancel all open orders (KILL_SWITCH never
  blocks cancelling; direct-wallet orders don't appear on polymarket.com,
  so this is the way to pull them).

Tests (`pytest tests/`): `tests/test_safety.py` (the order gate),
`tests/test_kalshi_client.py` (request signing, order body).
