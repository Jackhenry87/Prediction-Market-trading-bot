"""Commodities model: Kalshi commodity price-threshold markets priced from
the underlying futures price + realized volatility.

Identical method to the crypto model — we do NOT predict where oil/gold/gas
go (those markets are ferociously efficient). We price the *probability* a
threshold is crossed from the current futures price and its recent
volatility, and trade only where Kalshi disagrees by more than fees.

Futures prices come from Yahoo Finance's free chart endpoint (no key).
Daily realized vol is annualized (sqrt 252) and inflated by VOL_MULT for
fat tails. Calendar-time tau is a mild approximation (markets settle on
trading days); the vol buffer absorbs the slack.

    python strategy_commodities.py     # read-only scan, no orders
"""

import csv
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from kalshi_client import KalshiClient
from strategy_crypto import bucket_probability, hours_to_close
from strategy_weather import price_cents, score_pending_paper_trades, taker_fee_cents
from trade_logger import get_logger, setup_logging

log = get_logger("strategy_commodities")

# Kalshi series ticker + Yahoo futures symbol. KXWTI (daily WTI) is
# confirmed; the others are best guesses and fail closed (no events ->
# skipped) until verified from a live run's log.
ASSETS = [
    dict(series="KXWTI", yahoo="CL=F", name="WTI crude oil"),
    dict(series="KXNGAS", yahoo="NG=F", name="Natural gas"),
    dict(series="KXGOLD", yahoo="GC=F", name="Gold"),
]
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
VOL_MULT = 1.2
MIN_TAU_H = 0.5
MAX_TAU_H = 30.0
MIN_EDGE_CENTS = 5.0
TRADING_DAYS = 252
PAPER_LOG = Path(__file__).resolve().parent / "paper_trades_commodities.csv"


def realized_vol_annual(closes: list) -> float:
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 20:
        raise ValueError(f"not enough closes ({len(rets)} returns)")
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS)


def get_spot_and_vol(yahoo_symbol: str) -> tuple:
    resp = requests.get(
        YAHOO_URL.format(sym=yahoo_symbol),
        params={"interval": "1d", "range": "3mo"},
        headers={"User-Agent": "Mozilla/5.0 (prediction-market-bot)"},
        timeout=20,
    )
    resp.raise_for_status()
    result = resp.json()["chart"]["result"][0]
    closes = [c for c in result["indicators"]["quote"][0]["close"] if c]
    spot = closes[-1]
    return spot, realized_vol_annual(closes) * VOL_MULT


def evaluate_market(market: dict, spot: float, sigma: float) -> list:
    tau_h = hours_to_close(market)
    if tau_h is None or not MIN_TAU_H <= tau_h <= MAX_TAU_H:
        return []
    tau_years = tau_h / (24 * 365)
    p = bucket_probability(spot, sigma, tau_years,
                           market.get("floor_strike"), market.get("cap_strike"))
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
    client = KalshiClient(env="prod")
    results = []
    for asset in ASSETS:
        try:
            spot, sigma = get_spot_and_vol(asset["yahoo"])
        except Exception as exc:
            log.warning("Skipping %s (price data failed: %s)", asset["name"], exc)
            continue
        log.info("%s: futures $%s, realized vol %.0f%%/yr (x%.1f tail buffer)",
                 asset["name"], f"{spot:,.2f}",
                 100 * sigma / VOL_MULT, VOL_MULT)
        try:
            data = client._request(
                "GET", "/events",
                params={"series_ticker": asset["series"], "status": "open",
                        "with_nested_markets": "true", "limit": 30},
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
            results.append(dict(date=event_ticker, mu=spot, city=asset["name"],
                                title=event.get("title", ""), signals=signals))
    return results


def main() -> int:
    setup_logging()
    log.info("Commodities scanner: futures vol model vs Kalshi threshold "
             "markets (READ-ONLY, no orders)")
    try:
        score_pending_paper_trades(PAPER_LOG)
    except Exception as exc:
        log.warning("Scoring skipped (%s)", exc)

    results = scan()
    total = 0
    for r in results:
        log.info("%s (%s):", r["title"], r["date"])
        for s in r["signals"]:
            log.info("  SIGNAL: buy %s %s @ %.0fc | model %.0f%% | EV +%.1fc | %s",
                     s["side"].upper(), s["ticker"], s["price_cents"],
                     100 * s["model_prob"], s["ev_cents"], s["subtitle"])
        total += len(r["signals"])
    log.info("%s signal(s). NO ORDERS PLACED by this script.", total or "No")
    return 0


if __name__ == "__main__":
    sys.exit(main())
