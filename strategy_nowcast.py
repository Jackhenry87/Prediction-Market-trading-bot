"""Weather NOWCAST: intraday KNOWN outcomes on today's high-temp markets.

The macro model's thesis — once the number is known, buy the certain side
while the book lags — repeats inside every weather day. NWS stations
publish observations in near-real-time; once today's running maximum has
already exceeded a bucket's cap, every "high in/below that bucket" market
is DETERMINED (a daily max cannot go back down), yet thin books often
still price the dead side above zero. We buy the certain side.

Only exceedance is ever certain intraday: the high can still rise, so
"temperature will stay below X" is never known before the day ends and is
never traded here. A safety margin guards rounding at the boundary.

Time-critical (the lag closes as others notice), so this model is in
TAKER_MODELS — it crosses the spread — and, like macro, is exempt from
the 60-90c band: certainties near 99c are exactly the trade.

    python strategy_nowcast.py    # read-only scan, no orders
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from strategy_weather import (CITIES, date_from_event_ticker, price_cents,
                              score_pending_paper_trades, taker_fee_cents)
from trade_logger import get_logger, setup_logging

log = get_logger("strategy_nowcast")

OBS_URL = "https://api.weather.gov/stations/{station}/observations"
HEADERS = {"User-Agent": "prediction-market-trading-bot (personal project)"}
PAPER_LOG = Path(__file__).resolve().parent / "paper_trades_nowcast.csv"

# The METAR station behind each Kalshi settlement (same stations as CITIES)
STATIONS = {
    "KXHIGHNY": "KNYC", "KXHIGHCHI": "KMDW", "KXHIGHMIA": "KMIA",
    "KXHIGHDEN": "KDEN", "KXHIGHLAX": "KLAX", "KXHIGHAUS": "KATT",
}

MIN_EDGE_CENTS = 3.0
# require the observed max to clear the strike by this much: guards
# station-rounding and sensor jitter at the exact boundary
MARGIN_F = 0.5


def observed_max_f(station: str, tz: str, now=None) -> float:
    """Today's running maximum at the station, in Fahrenheit, from the
    NWS observations feed (local-midnight onward). None if unavailable."""
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    resp = requests.get(
        OBS_URL.format(station=station),
        params={"start": midnight.isoformat(), "limit": 200},
        headers=HEADERS, timeout=20)
    resp.raise_for_status()
    temps_c = [
        f["properties"]["temperature"]["value"]
        for f in resp.json().get("features", [])
        if (f.get("properties", {}).get("temperature") or {}).get("value")
        is not None
    ]
    if not temps_c:
        return None
    return max(temps_c) * 9.0 / 5.0 + 32.0


def determined_signals(markets: list, obs_max: float) -> list:
    """Signals whose outcome is already DETERMINED by the running max.
    Certain only by exceedance: obs_max past a bucket's cap -> NO;
    obs_max past an above-threshold floor (no cap) -> YES."""
    signals = []
    for market in markets or []:
        if market.get("status") not in (None, "active", "open"):
            continue
        floor_s, cap_s = market.get("floor_strike"), market.get("cap_strike")
        cap = float(cap_s) if cap_s is not None else None
        floor = float(floor_s) if floor_s is not None else None

        side = price = None
        if cap is not None and obs_max >= cap + MARGIN_F:
            bid = price_cents(market, "yes_bid")   # buying NO takes the bid
            price = (100.0 - bid) if bid else None
            side = "no"
        elif cap is None and floor is not None \
                and obs_max >= floor + MARGIN_F:
            price = price_cents(market, "yes_ask")
            side = "yes"
        if side is None or not price or not 0 < price < 100:
            continue
        ev = 100.0 - price - taker_fee_cents(price)
        if ev < MIN_EDGE_CENTS:
            continue
        signals.append(dict(
            side=side, price_cents=price, model_prob=1.0,  # outcome KNOWN
            ev_cents=ev, ticker=market.get("ticker"),
            subtitle=f"{market.get('yes_sub_title') or ''} "
                     f"(obs max {obs_max:.1f}F)"))
    return signals


def scan() -> list:
    from kalshi_client import KalshiClient
    client = KalshiClient(env="prod")
    results = []
    for city in CITIES:
        station = STATIONS.get(city["series"])
        if not station:
            continue
        try:
            obs_max = observed_max_f(station, city["tz"])
        except Exception as exc:
            log.warning("Skipping %s (obs fetch failed: %s)",
                        city["name"], exc)
            continue
        if obs_max is None:
            continue
        today = datetime.now(timezone.utc).astimezone(
            ZoneInfo(city["tz"])).strftime("%Y-%m-%d")
        try:
            data = client._request(
                "GET", "/events",
                params={"series_ticker": city["series"], "status": "open",
                        "with_nested_markets": "true", "limit": 10})
        except Exception as exc:
            log.warning("Skipping %s markets: %s", city["name"], exc)
            continue
        for event in data.get("events", []):
            event_ticker = event.get("event_ticker") or event.get(
                "ticker") or ""
            if date_from_event_ticker(event_ticker) != today:
                continue   # only TODAY's market can be determined intraday
            signals = determined_signals(event.get("markets"), obs_max)
            if signals:
                signals.sort(key=lambda s: -s["ev_cents"])
                log.info("%s: obs max %.1fF DETERMINES %d market(s)",
                         city["name"], obs_max, len(signals))
                results.append(dict(date=event_ticker, mu=obs_max,
                                    city=city["name"],
                                    title=event.get("title", ""),
                                    signals=signals))
    return results


def main() -> int:
    setup_logging()
    try:
        score_pending_paper_trades(PAPER_LOG)
    except Exception as exc:
        log.warning("Scoring skipped (%s)", exc)
    results = scan()
    total = sum(len(r["signals"]) for r in results)
    for r in results:
        for s in r["signals"]:
            log.info("  KNOWN: buy %s %s @ %.0fc | EV +%.1fc | %s",
                     s["side"].upper(), s["ticker"], s["price_cents"],
                     s["ev_cents"], s["subtitle"])
    log.info("%s nowcast signal(s). NO ORDERS placed by this script.",
             total or "No")
    return 0


if __name__ == "__main__":
    sys.exit(main())
