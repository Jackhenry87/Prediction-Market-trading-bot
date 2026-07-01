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

## Files

- `config.py` — loads/validates `.env`; the only module that touches env vars.
- `trade_logger.py` — timestamped file + console logging.
- `fetch_orderbook.py` — Phase 1 script (read-only order book fetch).
- `find_market.py` — helper to look up a market's token IDs from its URL.
