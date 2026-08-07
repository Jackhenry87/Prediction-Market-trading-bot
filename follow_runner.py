"""Always-on copier for a followed Kalshi leaderboard trader.

    python follow_runner.py            # loop for FOLLOW_RUN_MINUTES
    python follow_runner.py --once     # a single drain+decide pass

WHAT THIS DOES
--------------
Polls his trade history straight from Kalshi (see follow_feed), asks OUR OWN
models what each market is worth, and — only if our model agrees there is an
edge — places a quarter-Kelly order sized against OUR bankroll.

No phone is involved. An earlier design forwarded push notifications off the
owner's iPhone, which needed a market, side and price typed in by hand and
was solving the wrong problem: a notification says only THAT he traded.
Polling `/v1/users/{u}/trades` says WHAT he traded, with the exact ticker,
side, price and size, bounded by the poll interval rather than by push
latency plus a human.

His trade is the trigger. Our model is the reason. Our bankroll is the size.

WHY IT IS BUILT THIS SUSPICIOUSLY
---------------------------------
His public record looks spectacular (+$448,676 over 115 settled positions,
Top 1% P&L) and is much thinner than it looks:

  * his five best trades are $586,308 — 131% of his net profit. Remove them
    and the record is -$137,632;
  * 61W-54L is 53.0%, which on 115 trials is 0.7 standard deviations from a
    coin flip;
  * 52% of his losses land on round $1,000 tiers ($100,000.00, $50,000.00,
    $20,000.00 …). That is hand-typed sizing, not Kelly output, so there is
    no sizing model of his to copy;
  * he moves 100,000-390,000 contracts a position. An order that size IS the
    price move, so a copier arriving two minutes later is buying his own
    market impact.

Hence: our own `p` or no trade (follow_prob), a hard slippage guard, quarter
Kelly, and FOLLOW_DRY_RUN defaulting to on until this copier's own ledger
shows a profit. Every decision — placed or skipped, and why — is written to
follow_trades.csv, because the point of the paper phase is measurement.

SAFETY
------
Nothing here bypasses the hard rails. `safety.check_order` runs on every
order, and KILL_SWITCH / MAX_ORDER_SIZE / MAX_TOTAL_EXPOSURE / the longshot
floor remain the final backstop. FOLLOW_DRY_RUN is separate from the global
DRY_RUN so this copier can be armed (or disarmed) on its own.
"""

import csv
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import follow_feed
import follow_prob
import follow_resolve
import ledger
import sizing
from auto_trade import event_of
from config import ConfigError, load_kalshi_settings
from kalshi_client import KalshiClient
from kalshi_exposure import ExposureError, current_exposure_usd
from safety import check_order, scaled_exposure_cap, scaled_order_cap
from strategy_weather import price_cents, taker_fee_cents
from trade_logger import get_logger, setup_logging

log = get_logger("follow_runner")

ROOT = Path(__file__).resolve().parent
PAPER_LOG = ROOT / "follow_trades.csv"
# The shared execution ledgers, held as module constants and passed to the
# ledger calls EXPLICITLY. `ledger.log_execution(path=EXEC_LOG)` binds its
# default at import time, so a test that patches `ledger.EXEC_LOG` would
# still append to the repo's live CSVs — which is exactly what happened
# while this module was being written. Naming the path at the call site is
# what makes the redirect in tests/conftest.py actually bite.
EXEC_LOG = ledger.EXEC_LOG
COPY_LOG = ledger.COPY_LOG

ENABLED = os.getenv("FOLLOW_ENABLED", "false").lower() == "true"
DRY_RUN = os.getenv("FOLLOW_DRY_RUN", "true").lower() == "true"
USERNAME = os.getenv("FOLLOW_USERNAME", "blushing.wildebeest7119").strip()
# Detection latency ceiling: we learn of a trade within one poll.
POLL_SECONDS = float(os.getenv("FOLLOW_POLL_SECONDS", "20"))
RUN_MINUTES = float(os.getenv("FOLLOW_RUN_MINUTES", "110"))
MAX_SLIP_CENTS = float(os.getenv("FOLLOW_MAX_SLIP_CENTS", "3"))
MIN_PRICE_CENTS = float(os.getenv("FOLLOW_MIN_PRICE_CENTS", "10"))
MAX_PRICE_CENTS = float(os.getenv("FOLLOW_MAX_PRICE_CENTS", "95"))
# Kelly fraction for copies. Defaults to sizing.KELLY_FRACTION (quarter) —
# an unproven edge does not earn a bigger multiplier than our own models get.
KELLY_FRACTION = float(os.getenv("FOLLOW_KELLY_FRACTION",
                                 str(sizing.KELLY_FRACTION)))

PAPER_COLUMNS = [
    "at_utc", "username", "ticker", "event_ticker", "side", "decision",
    "reason", "model", "our_p", "market_price_cents", "his_price_cents",
    "edge_cents", "kelly_full", "kelly_used", "contracts", "cost_usd",
    "his_stake_usd", "his_contracts", "order_id", "outcome", "clv_cents",
]


# ------------------------------- ledger --------------------------------

def log_decision(**kw) -> None:
    """One row per notification, placed or not. The skips are the point:
    'how often would this have fired, and at what price' is exactly what the
    paper phase has to answer."""
    new_file = not PAPER_LOG.exists()
    row = {c: "" for c in PAPER_COLUMNS}
    row["at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row.update({k: v for k, v in kw.items() if k in row})
    try:
        with open(PAPER_LOG, "a", newline="") as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(PAPER_COLUMNS)
            writer.writerow([row[c] for c in PAPER_COLUMNS])
    except OSError as exc:
        log.warning("paper ledger write failed: %s", exc)


# ------------------------------- pricing -------------------------------

def current_price_cents(client, ticker: str, side: str):
    """What WE would pay right now for `side` — not what he paid.

    Buying YES lifts the yes ask; buying NO is 100 minus the yes bid. Copying
    at his price is a fiction: his own order is what moved the book.
    """
    try:
        market = client.get_market(ticker)
    except Exception as exc:
        log.warning("market fetch failed for %s: %s", ticker, exc)
        return None, None
    if side == "yes":
        return price_cents(market, "yes_ask"), market
    bid = price_cents(market, "yes_bid")
    return (100.0 - bid if bid else None), market


def evaluate(our_p: float, price: float) -> dict:
    """Kelly on OUR probability at the price WE would pay.

    `edge_cents` is EV net of Kalshi's taker fee, so a signal has to beat the
    cost of trading it, not just the mid. sizing.kelly_fraction then gives
    edge/(100-price) — which is the owner's (p·b - q)/b with b = (100-c)/c.
    """
    edge = 100.0 * our_p - price - taker_fee_cents(price)
    return dict(edge_cents=edge,
                kelly_full=sizing.kelly_fraction(price, edge))


# ------------------------------ the pass -------------------------------

def resolve(client, signal: dict) -> dict:
    """Signal -> {'ticker', 'event_ticker', 'market'} or None.

    The feed usually hands us a ticker outright, which makes the whole prose
    matching problem disappear — no team-name tokenising, no ambiguity between
    a game's moneyline and totals events, no series walk. We still have to
    fetch the market for its labels and strikes, which follow_prob needs.

    `follow_resolve` stays as the fallback for records that carry only a
    title and outcome. It is already written and proven against live data, so
    keeping it costs nothing and covers a shape we cannot rule out.
    """
    ticker = signal.get("ticker")
    if ticker:
        try:
            market = client.get_market(ticker)
        except Exception as exc:
            log.warning("market fetch failed for %s: %s", ticker, exc)
            return None
        return dict(ticker=ticker, market=market,
                    event_ticker=market.get("event_ticker")
                    or event_of(ticker))
    return follow_resolve.resolve_ticker(client, signal)


def handle(client, settings, signal: dict, state: dict) -> bool:
    """Process one trade of his. Returns True if an order was placed."""
    if not signal or not signal.get("side"):
        log.info("skipping a record with no readable side: %s",
                 {k: v for k, v in (signal or {}).items() if k != "raw"})
        return False

    # Exits are a different (and unbuilt) strategy — copying an entry and
    # ignoring the exit is already half a strategy; copying a SALE as a buy
    # would be backwards.
    if signal.get("action") in ("sell", "sold"):
        log.info("skipping a sale — exits are out of scope for v1")
        return False

    log.info("TRADE by %s: %s (%s%s)", signal.get("username"),
             signal.get("ticker") or signal.get("market_title"),
             signal["side"],
             f" @ {signal['price_cents']}c"
             if signal.get("price_cents") else "")

    base = dict(username=signal.get("username"), side=signal["side"],
                his_price_cents=signal.get("price_cents") or "",
                his_contracts=signal.get("contracts") or "")

    hit = resolve(client, signal)
    if not hit:
        log_decision(decision="skip", reason="could not resolve to a ticker",
                     **base)
        return False

    ticker = hit["ticker"]
    market = hit["market"]
    base.update(ticker=ticker, event_ticker=hit["event_ticker"])

    # One bet per event, ever — never the other side of a match we hold.
    # Checked against the live book AND this session's durable memory,
    # because a just-placed order can be missing from the next snapshot.
    ev_key = hit["event_ticker"] or event_of(ticker)
    if ticker in state["seen"] or ev_key in state["events"] \
            or event_of(ticker) in state["events"]:
        log_decision(decision="skip",
                     reason=f"already traded event {ev_key}", **base)
        return False

    our_p, model = follow_prob.model_prob(
        ticker, market, event_ticker=hit["event_ticker"],
        event_title=market.get("title") or "",
        odds_api_key=settings.odds_api_key)
    if our_p is None:
        # THE central rule: his conviction is not our evidence.
        log_decision(decision="skip",
                     reason="no model of ours prices this market", **base)
        return False
    base.update(model=model, our_p=f"{our_p:.4f}")

    price, _ = current_price_cents(client, ticker, signal["side"])
    if not price or not 0 < price < 100:
        log_decision(decision="skip", reason="no live price", **base)
        return False
    base.update(market_price_cents=f"{price:.0f}")

    if not MIN_PRICE_CENTS <= price <= MAX_PRICE_CENTS:
        log_decision(decision="skip",
                     reason=f"price {price:.0f}c outside "
                            f"{MIN_PRICE_CENTS:.0f}-{MAX_PRICE_CENTS:.0f}c "
                            f"band", **base)
        return False

    # THE SLIPPAGE GUARD — the single most important line of defence here.
    # He trades six-figure contract counts; by the time we see the push, the
    # book has already absorbed his order. If the price has run past his
    # entry we would be buying the impact he created, not the edge he found.
    his = signal.get("price_cents")
    if his and price > his + MAX_SLIP_CENTS:
        log_decision(decision="skip",
                     reason=f"line ran {price - his:.0f}c past his {his}c "
                            f"entry (max {MAX_SLIP_CENTS:.0f}c)", **base)
        return False

    calc = evaluate(our_p, price)
    base.update(edge_cents=f"{calc['edge_cents']:.2f}",
                kelly_full=f"{calc['kelly_full']:.4f}")

    try:
        bankroll = client.get_balance_cents() / 100.0 + \
            current_exposure_usd(client)
        exposure = current_exposure_usd(client)
    except ExposureError as exc:
        log.error("REFUSING TO COPY: %s (failing closed)", exc)
        log_decision(decision="skip", reason=f"exposure unknown: {exc}",
                     **base)
        return False

    sized = sizing.size_position(bankroll, price, calc["edge_cents"],
                                 kelly_fraction_used=KELLY_FRACTION)
    base.update(kelly_used=f"{sized['kelly_used']:.4f}",
                contracts=sized["contracts"],
                cost_usd=f"{sized['contracts'] * price / 100:.2f}")
    if sized["contracts"] < 1:
        log_decision(decision="skip", reason=sized["reason"], **base)
        return False

    # Hard caps grow with the account; the absolute ceilings still bind.
    capped = replace(settings,
                     max_order_size=scaled_order_cap(bankroll, settings),
                     max_total_exposure=scaled_exposure_cap(bankroll,
                                                            settings))
    problems = check_order(capped, "BUY", price / 100.0, sized["contracts"],
                           exposure)
    if problems:
        for p in problems:
            log.warning("BLOCKED %s: %s", ticker, p)
        log_decision(decision="blocked", reason="; ".join(problems), **base)
        return False

    log.info("COPY %s: %d x %s @ %.0fc ($%.2f) | our p=%.3f (%s), edge "
             "%.2fc, %s", signal["side"], sized["contracts"], ticker, price,
             sized["contracts"] * price / 100, our_p, model,
             calc["edge_cents"], sized["reason"])

    if DRY_RUN or settings.dry_run:
        log.info("FOLLOW_DRY_RUN: order not sent.")
        log_decision(decision="paper", reason=sized["reason"], **base)
        state["seen"].add(ticker)
        state["events"].update({ev_key, event_of(ticker)})
        return False

    try:
        order = client.create_limit_order(ticker, signal["side"], "buy",
                                          sized["contracts"], int(round(price)))
    except Exception as exc:
        log.error("copy order failed for %s: %s", ticker, exc)
        log_decision(decision="error", reason=str(exc)[:200], **base)
        return False

    oid = str(order.get("order_id", ""))
    base.update(order_id=oid)
    log_decision(decision="placed", reason=sized["reason"], **base)
    try:
        ledger.log_execution("follow", ticker, signal["side"],
                             sized["contracts"], int(round(price)), oid,
                             path=EXEC_LOG)
        ledger.log_copy_execution(ticker, signal["side"], sized["contracts"],
                                  int(round(price)), oid, our_p, 1,
                                  sized["kelly_used"] * 100, path=COPY_LOG,
                                  model="follow")
    except Exception as exc:
        log.warning("execution-log write failed: %s", exc)

    state["seen"].add(ticker)
    state["events"].update({ev_key, event_of(ticker)})
    return True


def run_once(client, settings, state: dict) -> int:
    """One poll of his activity, then decide on anything new.

    A FeedError is an OUTAGE, not silence. It is re-raised to the caller so
    a broken feed is loud — an empty poll and a dead endpoint look identical
    from here, and mistaking the second for the first would leave the copier
    apparently healthy and permanently blind.
    """
    signals = follow_feed.poll(client)
    if not signals:
        return 0
    placed = 0
    for sig in signals:
        try:
            if handle(client, settings, sig, state):
                placed += 1
        except Exception as exc:
            log.error("handler crashed on %s: %s", sig.get("ticker"), exc)
    return placed


def main() -> int:
    setup_logging()
    if not ENABLED:
        log.info("FOLLOW_ENABLED is false — copier idle. Set it to true to "
                 "arm the trigger.")
        return 0
    try:
        settings = load_kalshi_settings(require_market=False)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 1

    client = KalshiClient(settings.kalshi_api_key_id,
                          settings.kalshi_private_key_path,
                          settings.kalshi_env)
    log.info("Following %s | %s | poll %.0fs | quarter-Kelly x%.2f | "
             "max slip %.0fc", USERNAME,
             "PAPER (FOLLOW_DRY_RUN)" if DRY_RUN or settings.dry_run
             else "LIVE ORDERS", POLL_SECONDS, KELLY_FRACTION,
             MAX_SLIP_CENTS)

    state = {"seen": set(), "events": set()}
    once = "--once" in sys.argv
    deadline = time.time() + RUN_MINUTES * 60
    total = 0
    while True:
        try:
            total += run_once(client, settings, state)
        except follow_feed.FeedError as exc:
            # Loud and fatal. A feed we cannot read is not "he is quiet".
            log.error("FEED DOWN — %s", exc)
            return 1
        if once or time.time() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    log.info("Done — %d copy order(s) placed.", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
