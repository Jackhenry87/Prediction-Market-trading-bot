"""Kalshi crypto price-threshold model. READ-ONLY scanner — no orders.

Hypothesis: Kalshi's BTC/ETH "price above X at time T" markets drift from
what current volatility justifies, especially on strikes far from spot.

Model: zero-drift lognormal. Realized volatility is computed from ~12 days
of hourly closes (free public Coinbase candles) and inflated by VOL_MULT
because real crypto tails are fatter than lognormal — humility reduces
fake edges. Only near-dated markets are considered (0.5h-30h to close):
long-dated crypto is dominated by news we can't model.

Defined risk like everything else here: buying a contract can lose at most
its price. This is NOT perpetuals/leverage.

    python strategy_crypto.py
"""

import csv
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from kalshi_client import KalshiClient
from strategy_weather import (price_cents, score_pending_paper_trades,
                              taker_fee_cents)
from trade_logger import get_logger, setup_logging

log = get_logger("strategy_crypto")

ASSETS = [
    dict(series="KXBTCD", pair="BTC-USD", name="Bitcoin"),
    dict(series="KXETHD", pair="ETH-USD", name="Ethereum"),
]
VOL_MULT = 1.2          # lognormal tails are too thin for crypto; widen them
MIN_TAU_H = 0.5         # skip markets closing within 30 min (execution risk)
MAX_TAU_H = 30.0        # and beyond ~a day (news risk dwarfs the vol model)
MIN_EDGE_CENTS = 5.0
PAPER_LOG = Path(__file__).resolve().parent / "paper_trades_crypto.csv"

CANDLES_URL = "https://api.exchange.coinbase.com/products/{pair}/candles"
HOURS_PER_YEAR = 24 * 365


def realized_vol_annual(closes: list) -> float:
    """Annualized volatility from consecutive hourly closes."""
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0]
    if len(rets) < 24:
        raise ValueError(f"not enough candles ({len(rets)} returns)")
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(HOURS_PER_YEAR)


def get_spot_and_vol(pair: str) -> tuple:
    """Current price and annualized realized vol from public hourly candles."""
    resp = requests.get(
        CANDLES_URL.format(pair=pair), params={"granularity": 3600},
        headers={"User-Agent": "prediction-market-trading-bot"}, timeout=20,
    )
    resp.raise_for_status()
    candles = resp.json()  # [[time, low, high, open, close, volume], ...] newest first
    closes = [c[4] for c in reversed(candles)]
    spot = closes[-1]
    return spot, realized_vol_annual(closes) * VOL_MULT


def prob_above(spot: float, sigma: float, tau_years: float, strike: float) -> float:
    """P(price > strike at close) under zero-drift lognormal."""
    if tau_years <= 0 or sigma <= 0:
        return 1.0 if spot > strike else 0.0
    z = (math.log(spot / strike) - 0.5 * sigma * sigma * tau_years) / (
        sigma * math.sqrt(tau_years))
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def bucket_probability(spot, sigma, tau_years, floor_strike, cap_strike) -> float:
    above_floor = (prob_above(spot, sigma, tau_years, float(floor_strike))
                   if floor_strike is not None else 1.0)
    above_cap = (prob_above(spot, sigma, tau_years, float(cap_strike))
                 if cap_strike is not None else 0.0)
    return max(above_floor - above_cap, 0.0)


def hours_to_close(market: dict):
    raw = market.get("close_time") or market.get("expiration_time")
    if not raw:
        return None
    try:
        close = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (close - datetime.now(timezone.utc)).total_seconds() / 3600.0


def evaluate_market(market: dict, spot: float, sigma: float) -> list:
    tau_h = hours_to_close(market)
    if tau_h is None or not MIN_TAU_H <= tau_h <= MAX_TAU_H:
        return []
    tau_years = tau_h / HOURS_PER_YEAR
    p = bucket_probability(spot, sigma, tau_years,
                           market.get("floor_strike"),
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
        no_price = 100.0 - yes_bid
        ev = 100.0 * (1.0 - p) - no_price - taker_fee_cents(no_price)
        if ev >= MIN_EDGE_CENTS:
            signals.append(dict(side="no", price_cents=no_price,
                                model_prob=1.0 - p, ev_cents=ev))
    for s in signals:
        s.update(ticker=market.get("ticker"),
                 subtitle=market.get("subtitle")
                 or market.get("yes_sub_title") or "")
    return signals


def scan() -> list:
    """Same result shape as strategy_weather.scan(): one entry per event,
    'date' holding the event ticker so the executor's one-signal-per-event
    rule groups correctly."""
    client = KalshiClient(env="prod")
    results = []
    for asset in ASSETS:
        try:
            spot, sigma = get_spot_and_vol(asset["pair"])
        except Exception as exc:
            log.warning("Skipping %s (price data failed: %s)", asset["name"], exc)
            continue
        log.info("%s: spot $%,.0f, realized vol %.0f%%/yr (x%.1f tail buffer)",
                 asset["name"], spot, 100 * sigma / VOL_MULT, VOL_MULT)
        try:
            data = client._request(
                "GET", "/events",
                params={"series_ticker": asset["series"], "status": "open",
                        "with_nested_markets": "true", "limit": 20},
            )
        except Exception as exc:
            log.warning("Skipping %s markets: %s", asset["name"], exc)
            continue

        for event in data.get("events", []):
            event_ticker = event.get("event_ticker") or event.get("ticker") or ""
            signals = []
            for market in event.get("markets") or []:
                if market.get("status") not in (None, "active", "open"):
                    continue
                signals.extend(evaluate_market(market, spot, sigma))
            if not signals:
                continue
            signals.sort(key=lambda s: -s["ev_cents"])
            results.append(dict(date=event_ticker, mu=spot,
                                city=asset["name"],
                                title=event.get("title", ""),
                                signals=signals))
    return results


def append_paper_trades(signals: list, spot: float, event: str) -> None:
    new_file = not PAPER_LOG.exists()
    with open(PAPER_LOG, "a", newline="") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(["scanned_at_utc", "event", "ticker", "side",
                             "price_cents", "model_prob", "ev_cents",
                             "spot", "outcome"])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for s in signals:
            writer.writerow([now, event, s["ticker"], s["side"],
                             f"{s['price_cents']:.0f}",
                             f"{s['model_prob']:.3f}",
                             f"{s['ev_cents']:.1f}", f"{spot:.0f}", ""])


def main() -> int:
    setup_logging()
    log.info("Crypto edge scanner: realized-vol model vs Kalshi BTC/ETH "
             "threshold markets (READ-ONLY, no orders)")
    try:
        score_pending_paper_trades(PAPER_LOG)
    except Exception as exc:
        log.warning("Scoring skipped (%s)", exc)

    try:
        results = scan()
    except Exception as exc:
        log.error("Scan failed: %s", exc)
        return 1

    total = 0
    for r in results:
        log.info("%s (%s):", r["title"], r["date"])
        for s in r["signals"]:
            log.info("  SIGNAL: buy %s %s @ %.0fc | model %.0f%% | EV +%.1fc | %s",
                     s["side"].upper(), s["ticker"], s["price_cents"],
                     100 * s["model_prob"], s["ev_cents"], s["subtitle"])
        append_paper_trades(r["signals"], r["mu"], r["date"])
        total += len(r["signals"])

    log.info("%s signal(s). NO ORDERS PLACED — outcomes auto-score on later "
             "runs.", total or "No")
    return 0


if __name__ == "__main__":
    sys.exit(main())
