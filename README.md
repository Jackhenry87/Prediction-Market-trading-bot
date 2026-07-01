# Polymarket Trading Bot

Incremental build of a Polymarket (Polygon) trading bot using the official
[`py-clob-client`](https://github.com/Polymarket/py-clob-client).

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
| 1 | **current** | Project setup + authenticate to the CLOB + fetch and print the live order book for one market. Read-only. |
| 2 | not started | Place a single manually-triggered order, respecting DRY_RUN and both limits. |
| 3+ | not started | Strategy logic / automation. Edge to be defined first. |

## Setup (Phase 1)

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
number. Easiest way:

1. Open the market on polymarket.com and copy the slug from the URL, e.g.
   `https://polymarket.com/event/.../will-x-happen` → slug `will-x-happen`.
2. Query the Gamma API in your browser:
   `https://gamma-api.polymarket.com/markets?slug=will-x-happen`
3. In the JSON, find `clobTokenIds` — it holds two IDs, in the same order as
   the `outcomes` field (usually `["Yes","No"]`). Copy the one you want into
   `MARKET_TOKEN_ID`.

### Run

```bash
python fetch_orderbook.py
```

Prints the top of the order book (bids/asks, spread, mid) and writes a log
to `logs/bot_<timestamp>.log`. Places no orders.

## Files

- `config.py` — loads/validates `.env`; the only module that touches env vars.
- `trade_logger.py` — timestamped file + console logging.
- `fetch_orderbook.py` — Phase 1 script (read-only order book fetch).
