"""Phase 3a: NYC temperature edge scanner. READ-ONLY — places NO orders.

Hypothesis: Kalshi's daily NYC high-temperature markets are slower to update
than the National Weather Service forecast they ultimately settle against.

What one run does (then exits — no loop):
  1. Pull the NWS forecast high for Central Park for the next few days.
  2. Pull Kalshi's open KXHIGHNY markets (public prod data, no account).
  3. Model the settlement temperature as Normal(forecast, SIGMA_F) and
     compute each bucket's probability.
  4. Compare with market prices; compute expected value per contract AFTER
     Kalshi's taker fee; print anything above the edge threshold.
  5. Append every qualifying signal to paper_trades.csv so the strategy's
     accuracy can be scored against reality over time.

    python strategy_weather.py
"""

import csv
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

from kalshi_client import KalshiClient
from trade_logger import get_logger, setup_logging

log = get_logger("strategy_weather")

SERIES_TICKER = "KXHIGHNY"
# Forecast error (std dev, deg F) for 1-2 day NWS high-temp forecasts.
# Deliberately conservative: wider sigma -> humbler probabilities -> fewer
# and stronger signals. Tighten only with evidence from paper trading.
SIGMA_F = 3.0
# Only report trades with at least this much expected value per contract,
# in cents, after fees. Below this, spread/model noise eats the edge.
MIN_EDGE_CENTS = 5.0
PAPER_LOG = Path(__file__).resolve().parent / "paper_trades.csv"


def normal_cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def bucket_probability(mu: float, floor_strike, cap_strike) -> float:
    """P(high temp lands in this market's bucket) under Normal(mu, SIGMA_F).
    Tail markets have only one bound."""
    lo = float(floor_strike) if floor_strike is not None else -math.inf
    hi = float(cap_strike) if cap_strike is not None else math.inf
    lo_cdf = 0.0 if lo == -math.inf else normal_cdf(lo, mu, SIGMA_F)
    hi_cdf = 1.0 if hi == math.inf else normal_cdf(hi, mu, SIGMA_F)
    return max(hi_cdf - lo_cdf, 0.0)


def taker_fee_cents(price_cents: float) -> float:
    """Kalshi's trading fee per contract: 0.07 * P * (1-P) dollars,
    i.e. 7 * p * (1-p) cents (rounded up per order; we keep it exact here
    to stay conservative in aggregate)."""
    p = price_cents / 100.0
    return 7.0 * p * (1.0 - p)


def date_from_event_ticker(ticker: str):
    """KXHIGHNY-26JUL02 -> '2026-07-02'."""
    try:
        raw = ticker.split("-")[1]
        return datetime.strptime(raw, "%y%b%d").strftime("%Y-%m-%d")
    except (IndexError, ValueError):
        return None


def price_cents(market: dict, field: str):
    """Read a price that may be int cents (yes_ask) or a dollar string
    (yes_ask_dollars), depending on API vintage."""
    value = market.get(field)
    if value not in (None, 0, ""):
        return float(value)
    dollars = market.get(f"{field}_dollars")
    if dollars not in (None, ""):
        return float(dollars) * 100.0
    return None


def evaluate_market(market: dict, mu: float) -> list:
    """Return signal dicts for +EV ways to take liquidity in this market."""
    p = bucket_probability(mu, market.get("floor_strike"),
                           market.get("cap_strike"))
    signals = []

    yes_ask = price_cents(market, "yes_ask")
    if yes_ask and 0 < yes_ask < 100:
        ev = 100.0 * p - yes_ask - taker_fee_cents(yes_ask)
        if ev >= MIN_EDGE_CENTS:
            signals.append(dict(side="yes", price_cents=yes_ask,
                                model_prob=p, ev_cents=ev))

    yes_bid = price_cents(market, "yes_bid")
    if yes_bid and 0 < yes_bid < 100:
        no_price = 100.0 - yes_bid  # buying NO takes the YES bid
        ev = 100.0 * (1.0 - p) - no_price - taker_fee_cents(no_price)
        if ev >= MIN_EDGE_CENTS:
            signals.append(dict(side="no", price_cents=no_price,
                                model_prob=1.0 - p, ev_cents=ev))

    for s in signals:
        s.update(ticker=market.get("ticker"),
                 subtitle=market.get("subtitle")
                 or market.get("yes_sub_title") or "")
    return signals


def append_paper_trades(signals: list, mu: float, date: str) -> None:
    new_file = not PAPER_LOG.exists()
    with open(PAPER_LOG, "a", newline="") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(["scanned_at_utc", "market_date", "ticker",
                             "side", "price_cents", "model_prob",
                             "ev_cents", "nws_forecast_f", "outcome"])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for s in signals:
            writer.writerow([now, date, s["ticker"], s["side"],
                             f"{s['price_cents']:.0f}",
                             f"{s['model_prob']:.3f}",
                             f"{s['ev_cents']:.1f}", mu, ""])


def main() -> int:
    setup_logging()
    log.info("Edge scanner: NWS forecast vs Kalshi %s (READ-ONLY, no orders)",
             SERIES_TICKER)

    try:
        from nws import get_daily_high_forecasts
        forecasts = get_daily_high_forecasts()
    except Exception as exc:
        log.error("Could not fetch NWS forecast: %s", exc)
        return 1

    # Public prod market data — no account or credentials involved.
    client = KalshiClient(env="prod")
    try:
        data = client._request(
            "GET", "/events",
            params={"series_ticker": SERIES_TICKER, "status": "open",
                    "with_nested_markets": "true", "limit": 10},
        )
    except Exception as exc:
        log.error("Could not fetch Kalshi markets: %s", exc)
        return 1

    total_signals = 0
    for event in data.get("events", []):
        date = date_from_event_ticker(event.get("event_ticker")
                                      or event.get("ticker") or "")
        if not date or date not in forecasts:
            continue
        mu = forecasts[date]
        log.info("%s (%s): NWS forecast high %.0fF, sigma %.1fF",
                 event.get("title"), date, mu, SIGMA_F)

        markets = event.get("markets") or []
        signals = []
        for market in markets:
            if market.get("status") not in (None, "active", "open"):
                continue
            signals.extend(evaluate_market(market, mu))

        if not signals:
            log.info("  No edge >= %.0fc after fees. Correctly priced (or "
                     "books empty).", MIN_EDGE_CENTS)
            continue

        signals.sort(key=lambda s: -s["ev_cents"])
        for s in signals:
            log.info(
                "  SIGNAL: buy %s %s @ %.0fc | model prob %.0f%% | "
                "EV +%.1fc/contract after fees | %s",
                s["side"].upper(), s["ticker"], s["price_cents"],
                100 * s["model_prob"], s["ev_cents"], s["subtitle"],
            )
        append_paper_trades(signals, mu, date)
        total_signals += len(signals)

    if total_signals:
        log.info(
            "%d signal(s) written to %s. NO ORDERS WERE PLACED — paper "
            "trading only. Score them after the markets settle.",
            total_signals, PAPER_LOG.name,
        )
    else:
        log.info("No signals today. That's a normal, honest result.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
