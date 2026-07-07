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
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from kalshi_client import KalshiClient
from trade_logger import get_logger, setup_logging

log = get_logger("strategy_weather")

SERIES_TICKER = "KXHIGHNY"  # kept for log labels / backwards compatibility

# Kalshi settles each city's market on the NWS Daily Climate Report of ONE
# specific station. Coordinates below are those stations — not "the city".
# 'sigma' is the MEASURED 1-day-ahead forecast-error std dev and 'bias' the
# measured mean error (forecast - actual, deg F) for that station
# (calibrate_weather.py, 366 days of Open-Meteo archived forecasts vs ERA5
# actuals, run 2026-07-06; sigma inflated ~10% for station-vs-grid
# representativeness). Bias is a proxy measurement (Open-Meteo best_match,
# not NWS) — small, so applied, but revisit once live settle data
# accumulates. Cities without a measurement use SIGMA_F and bias 0.
CITIES = [
    dict(series="KXHIGHNY", name="NYC (Central Park)",
         lat=40.7794, lon=-73.9692, sigma=3.0, bias=0.7,
         tz="America/New_York"),
    dict(series="KXHIGHCHI", name="Chicago (Midway)",
         lat=41.7861, lon=-87.7522, sigma=2.0, bias=0.4,
         tz="America/Chicago"),
    dict(series="KXHIGHMIA", name="Miami (Intl Airport)",
         lat=25.7906, lon=-80.3164, sigma=2.0, bias=0.7,
         tz="America/New_York"),
    dict(series="KXHIGHDEN", name="Denver (Intl Airport)",
         lat=39.8467, lon=-104.6562, sigma=2.5, bias=-0.5,
         tz="America/Denver"),
    # LAX has the fattest tails of the six (marine-layer burn-off: |err|>5F
    # on 7% of days) — treat its sigma as a floor, not a promise.
    dict(series="KXHIGHLAX", name="Los Angeles (LAX)",
         lat=33.9382, lon=-118.3866, sigma=3.0, bias=0.7,
         tz="America/Los_Angeles"),
    # Austin forecasts ran nearly 2F WARM — the largest bias measured.
    dict(series="KXHIGHAUS", name="Austin (Camp Mabry)",
         lat=30.3208, lon=-97.7660, sigma=2.5, bias=2.0,
         tz="America/Chicago"),
]
# Which cities the model may actually TRADE. New York is cut by default:
# scored against real Kalshi settlements it was -$6.31 (the model's whole
# net loss), losing under-forecast bets four days straight AND oversized.
# One repo Variable brings a city back — no code change. Names match the
# short code in each series ticker (NY/CHI/MIA/DEN/LAX/AUS).
ENABLED_CITIES = {s.strip().upper() for s in os.getenv(
    "WEATHER_CITIES", "CHI,MIA,DEN,LAX,AUS").split(",") if s.strip()}


def city_enabled(city: dict) -> bool:
    """A city trades only if its series code is in ENABLED_CITIES."""
    code = city["series"].replace("KXHIGH", "").replace("KXLOW", "")
    return code.upper() in ENABLED_CITIES


# SELF-CALIBRATION: calibrate_weather.py writes a RECENT-window measurement
# of each station's forecast error here; the model prefers it over the
# static constants above, so bias/sigma track the current SEASON and a
# station whose forecasts have broken is auto-benched. Missing file -> fall
# back to the hardcoded constants (nothing breaks).
CALIBRATION_PATH = Path(__file__).resolve().parent / "weather_calibration.json"


def load_calibration() -> dict:
    try:
        import json
        return json.loads(CALIBRATION_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def station_cal(city: dict, cal: dict = None) -> dict:
    """Effective {bias, sigma, trade} for a station: the freshly MEASURED
    recent-window values when available, else the static constants. `trade`
    is False when calibration has benched the station (forecasts too broken
    to have an edge this season)."""
    cal = load_calibration() if cal is None else cal
    c = cal.get(city["series"]) or {}
    return dict(
        bias=c.get("bias", city.get("bias", 0.0)),
        sigma=c.get("sigma") or city.get("sigma") or SIGMA_F,
        trade=c.get("trade", True),
    )
# Fallback forecast-error std dev (deg F) for stations without a measured
# sigma. History: guessed 3.0 -> widened to 4.5 after the first live week's
# losses -> measurement showed the real error is ~1.5-2.3F and the losses
# came from elsewhere (concentration, since fixed by the theme cap). Keep
# the humble 4.5 ONLY as the unmeasured-city fallback.
SIGMA_F = 4.5
# Only report trades with at least this much expected value per contract,
# in cents, after fees. Below this, spread/model noise eats the edge.
MIN_EDGE_CENTS = 5.0
PAPER_LOG = Path(__file__).resolve().parent / "paper_trades.csv"


# A same-day forecast made in the afternoon is far more accurate than one
# made at dawn — by evening the day's high has essentially happened. Scale
# sigma down through the LOCAL day for today's market; tomorrow's market
# always gets the full sigma. Floor keeps us from absurd overconfidence.
INTRADAY_FACTORS = ((10, 1.0), (13, 0.75), (16, 0.55), (24, 0.4))
MIN_SIGMA_F = 1.2

# Ensemble spread -> day-specific sigma. The spread-skill relationship
# (ensemble member disagreement predicts that day's forecast error) is one
# of the most replicated results in forecast verification: a calm ridge
# day deserves a tighter sigma than a frontal-passage day. Open-Meteo's
# free ensemble API gives per-member forecasts; we take the std dev of the
# members' daily maxes, inflate by SPREAD_K (ensembles run underdispersive),
# and CLAMP to 0.6-2.0x the station's calibrated sigma so one weird API
# response can never nuke the model. Any failure -> calibrated fallback.
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
ENSEMBLE_SIGMA = os.getenv("ENSEMBLE_SIGMA", "true").strip().lower() \
    not in ("false", "0", "no")
SPREAD_K = float(os.getenv("SPREAD_K", "1.3"))
MIN_MEMBERS = 8


def ensemble_daily_sigma(lat: float, lon: float, tz: str) -> dict:
    """{'YYYY-MM-DD': sigma_F} from ensemble-member daily-max spread.
    Empty dict on any failure — callers fall back to calibrated sigma."""
    import requests as _rq
    resp = _rq.get(ENSEMBLE_URL, params={
        "latitude": lat, "longitude": lon, "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit", "timezone": tz,
        "forecast_days": 3, "models": "gfs_seamless"}, timeout=25)
    resp.raise_for_status()
    hourly = resp.json().get("hourly", {})
    times = hourly.get("time", [])
    member_keys = [k for k in hourly
                   if k.startswith("temperature_2m_member")]
    if len(member_keys) < MIN_MEMBERS or not times:
        return {}
    # per member, per local date: the daily max
    by_date = {}   # date -> list of per-member maxes
    for key in member_keys:
        maxes = {}
        for t, v in zip(times, hourly[key]):
            if v is None:
                continue
            d = t[:10]
            if d not in maxes or v > maxes[d]:
                maxes[d] = v
        for d, mx in maxes.items():
            by_date.setdefault(d, []).append(mx)
    out = {}
    for d, vals in by_date.items():
        if len(vals) < MIN_MEMBERS:
            continue
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
        out[d] = math.sqrt(var) * SPREAD_K
    return out


def blended_sigma(calibrated: float, spread_sigma) -> float:
    """Day-specific sigma, clamped to 0.6-2.0x the calibrated value."""
    if spread_sigma is None:
        return calibrated
    return max(0.6 * calibrated, min(2.0 * calibrated, spread_sigma))


def intraday_sigma_factor(local_hour: int) -> float:
    """Sigma multiplier for a market settling TODAY, by local hour."""
    for cutoff, factor in INTRADAY_FACTORS:
        if local_hour < cutoff:
            return factor
    return INTRADAY_FACTORS[-1][1]


def effective_sigma(base_sigma: float, market_date: str, tz: str = None,
                    now=None) -> float:
    """base sigma, tightened if the market settles today in the city's
    timezone. Any parsing problem falls back to the untightened sigma."""
    try:
        from zoneinfo import ZoneInfo
        local = (now or datetime.now(timezone.utc)).astimezone(
            ZoneInfo(tz)) if tz else (now or datetime.now(timezone.utc))
        if local.strftime("%Y-%m-%d") == market_date:
            return max(base_sigma * intraday_sigma_factor(local.hour),
                       MIN_SIGMA_F)
    except Exception:
        pass
    return base_sigma


def normal_cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def bucket_probability(mu: float, floor_strike, cap_strike,
                       sigma: float = None) -> float:
    """P(high temp lands in this market's bucket) under Normal(mu, sigma).
    Tail markets have only one bound. sigma defaults to SIGMA_F."""
    s = sigma if sigma else SIGMA_F
    lo = float(floor_strike) if floor_strike is not None else -math.inf
    hi = float(cap_strike) if cap_strike is not None else math.inf
    lo_cdf = 0.0 if lo == -math.inf else normal_cdf(lo, mu, s)
    hi_cdf = 1.0 if hi == math.inf else normal_cdf(hi, mu, s)
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


def evaluate_market(market: dict, mu: float, sigma: float = None) -> list:
    """Return signal dicts for +EV ways to take liquidity in this market."""
    p = bucket_probability(mu, market.get("floor_strike"),
                           market.get("cap_strike"), sigma)
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


def scan() -> list:
    """Fetch forecasts + markets for every configured city and return
    per-event results: [{'date', 'mu', 'title', 'city', 'signals': [...]},
    ...]. Read-only. A city whose forecast or markets fail to load is
    skipped with a warning — one broken city must not stop the rest."""
    from nws import get_daily_high_forecasts

    # Public prod market data — no account or credentials involved.
    client = KalshiClient(env="prod")

    results = []
    for city in CITIES:
        if not city_enabled(city):
            log.info("%s cut from the weather model (owner call — lost "
                     "money at this station)", city["name"])
            continue
        cal = station_cal(city)
        if not cal["trade"]:
            log.info("%s auto-benched: recent forecast error too high to "
                     "trade this season (self-calibration)", city["name"])
            continue
        try:
            forecasts = get_daily_high_forecasts(
                city["lat"], city["lon"], city["name"]
            )
            data = client._request(
                "GET", "/events",
                params={"series_ticker": city["series"], "status": "open",
                        "with_nested_markets": "true", "limit": 10},
            )
        except Exception as exc:
            log.warning("Skipping %s: %s", city["name"], exc)
            continue

        base_sigma = cal["sigma"]           # measured recent-season sigma
        spread_by_date = {}
        if ENSEMBLE_SIGMA:
            try:
                spread_by_date = ensemble_daily_sigma(
                    city["lat"], city["lon"], city.get("tz", "auto"))
            except Exception as exc:
                log.warning("%s: ensemble sigma unavailable (%s) — using "
                            "calibrated", city["name"], exc)
        for event in data.get("events", []):
            date = date_from_event_ticker(event.get("event_ticker")
                                          or event.get("ticker") or "")
            if not date or date not in forecasts:
                continue
            # measured bias: forecast - actual, so subtract to de-bias
            mu = forecasts[date] - cal["bias"]
            day_sigma = blended_sigma(base_sigma, spread_by_date.get(date))
            if abs(day_sigma - base_sigma) > 0.05:
                log.info("%s %s: ensemble spread sets sigma %.1fF "
                         "(calibrated %.1fF)", city["name"], date,
                         day_sigma, base_sigma)
            sigma = effective_sigma(day_sigma, date, city.get("tz"))
            signals = []
            for market in event.get("markets") or []:
                if market.get("status") not in (None, "active", "open"):
                    continue
                signals.extend(evaluate_market(market, mu, sigma))
            signals.sort(key=lambda s: -s["ev_cents"])
            results.append(dict(date=date, mu=mu, city=city["name"],
                                sigma=sigma,
                                title=event.get("title", ""),
                                signals=signals))
    return results


def _close_cents(market: dict):
    """The market's last traded price in cents — our closing-line proxy."""
    last = market.get("last_price")
    if last in (None, "", 0):
        dollars = market.get("last_price_dollars")
        last = float(dollars) * 100.0 if dollars not in (None, "") else None
    return float(last) if last else None


def score_pending_paper_trades(log_path: Path = None) -> None:
    """Fill in the outcome column for settled markets: win if the
    recommended side matches Kalshi's official result. Works for any
    signal CSV that has ticker/side/price_cents/outcome columns. Paper
    ledgers (model_prob present) also get CLV: entry vs the last traded
    price — the earliest reliable predictor of long-term profitability."""
    path = log_path or PAPER_LOG
    if not path.exists():
        return
    with open(path, newline="") as fh:
        rows = list(csv.reader(fh))
    if len(rows) < 2:
        return
    header, body = rows[0], rows[1:]
    upgraded = False
    if "model_prob" in header and "clv_cents" not in header:
        header.append("clv_cents")
        upgraded = True
    for row in body:                     # pad legacy/short rows
        while len(row) < len(header):
            row.append("")
    idx = {name: i for i, name in enumerate(header)}
    client = KalshiClient(env="prod")
    scored = 0
    for row in body:
        if row[idx["outcome"]]:
            continue
        try:
            market = client.get_market(row[idx["ticker"]])
        except Exception:
            continue
        result = market.get("result")  # 'yes' / 'no' once settled
        if result not in ("yes", "no"):
            continue
        won = result == row[idx["side"]]
        price = float(row[idx["price_cents"]])
        pnl = (100.0 - price) if won else -price
        row[idx["outcome"]] = f"{'win' if won else 'loss'} ({pnl:+.0f}c)"
        close = _close_cents(market)
        # only if not already sampled earlier (e.g. the copier's mid-life
        # CLV read) — the first, pre-settlement reading is the honest one
        if "clv_cents" in idx and close is not None \
                and not row[idx["clv_cents"]]:
            clv = (close - price) if row[idx["side"]] == "yes" \
                else ((100.0 - close) - price)
            row[idx["clv_cents"]] = f"{clv:+.0f}"
        scored += 1
    if scored or upgraded:
        with open(path, "w", newline="") as fh:
            csv.writer(fh).writerows([header] + body)
    if scored:
        wins = sum("win" in r[idx["outcome"]] for r in body if r[idx["outcome"]])
        total = sum(1 for r in body if r[idx["outcome"]])
        pnl = sum(float(r[idx["outcome"]].split("(")[1].rstrip("c)"))
                  for r in body if "(" in r[idx["outcome"]])
        log.info("Scored %d settled signal(s). Running record: %d/%d wins, "
                 "paper P&L %+.0fc per 1-contract stakes.",
                 scored, wins, total, pnl)


def main() -> int:
    setup_logging()
    log.info("Edge scanner: NWS forecasts vs Kalshi daily-high markets in "
             "%d cities (READ-ONLY, no orders)", len(CITIES))

    try:
        score_pending_paper_trades()
    except Exception as exc:
        log.warning("Could not score past signals (%s) — will retry next run", exc)

    try:
        results = scan()
    except Exception as exc:
        log.error("Scan failed: %s", exc)
        return 1

    total_signals = 0
    for r in results:
        log.info("%s (%s): NWS forecast high %.0fF, sigma %.1fF",
                 r["title"], r["date"], r["mu"], r.get("sigma") or SIGMA_F)
        if not r["signals"]:
            log.info("  No edge >= %.0fc after fees. Correctly priced (or "
                     "books empty).", MIN_EDGE_CENTS)
            continue
        for s in r["signals"]:
            log.info(
                "  SIGNAL: buy %s %s @ %.0fc | model prob %.0f%% | "
                "EV +%.1fc/contract after fees | %s",
                s["side"].upper(), s["ticker"], s["price_cents"],
                100 * s["model_prob"], s["ev_cents"], s["subtitle"],
            )
        append_paper_trades(r["signals"], r["mu"], r["date"])
        total_signals += len(r["signals"])

    if total_signals:
        log.info(
            "%d signal(s) written to %s. NO ORDERS WERE PLACED — paper "
            "trading only. Outcomes fill in automatically on later runs.",
            total_signals, PAPER_LOG.name,
        )
    else:
        log.info("No signals today. That's a normal, honest result.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
