"""Macro resolution-lag strategy (Option 2: crowd-lag on news).

The edge is NOT predicting the number — it's the window AFTER a macro
figure is released (CPI, jobs, unemployment, Fed) but BEFORE Kalshi's
thinner books fully reprice. Once the actual value is known, the correct
side of a threshold market is *determined* (probability ~1), so if it's
still offered below fair value we buy it with a limit order.

Ground truth comes from FRED (free API, FRED_API_KEY). Because a wrong
metric mapping would produce confident WRONG trades, this model:
  - is OFF by default (add 'macro' to ENABLED_MODELS to arm it),
  - only fires when we have a FRESH actual value (released within
    FRESH_HOURS) so we're acting on genuine news, not stale data,
  - buys only the KNOWN-correct side, with a limit, capturing the lag.

Every ticker/series mapping below is a best guess until confirmed against
a real settled market — verify from a live run's log before trusting it.
"""

import os
import sys
from datetime import datetime, timezone

import requests

from strategy_weather import price_cents, score_pending_paper_trades, taker_fee_cents
from trade_logger import get_logger, setup_logging
from pathlib import Path

log = get_logger("strategy_macro")

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_META_URL = "https://api.stlouisfed.org/fred/series"
PAPER_LOG = Path(__file__).resolve().parent / "paper_trades_macro.csv"

# Only act on a release this fresh (hours) — that's the crowd-lag window.
FRESH_HOURS = 12.0
# A known-correct side is worth ~100c; only bother if the lag leaves at
# least this much edge after fees.
MIN_EDGE_CENTS = 3.0

# FRED series -> the Kalshi market series it settles. 'transform' says how
# to turn the raw FRED observation into the number the market is about.
# VERIFY every row against a real settled Kalshi market before arming.
MACRO_SERIES = [
    # jobless claims: weekly (Thu), the frequent testbed. ICSA is the raw
    # claim count; Kalshi thresholds are raw counts too -> 'level'.
    dict(fred="ICSA", kalshi="KXJOBLESSCLAIMS", transform="level",
         name="Initial jobless claims (count)"),
    dict(fred="UNRATE", kalshi="KXU3", transform="level",
         name="US unemployment rate (%)"),
    dict(fred="CPIAUCSL", kalshi="KXCPIYOY", transform="yoy_pct",
         name="CPI year-over-year (%)"),
    # PAYEMS is in THOUSANDS; Kalshi payroll strikes are raw jobs -> x1000
    dict(fred="PAYEMS", kalshi="KXPAYROLLS", transform="mom_change_jobs",
         name="Nonfarm payrolls (MoM change, jobs)"),
]


def fetch_fred(series_id: str, api_key: str) -> list:
    """Recent observations (date, value), newest last."""
    resp = requests.get(FRED_URL, params={
        "series_id": series_id, "api_key": api_key, "file_type": "json",
        "sort_order": "asc", "limit": 25}, timeout=20)
    resp.raise_for_status()
    obs = []
    for o in resp.json().get("observations", []):
        if o.get("value") not in (".", "", None):
            obs.append((o["date"], float(o["value"])))
    return obs


def latest_actual(obs: list, transform: str):
    """(release_date, actual_value) for the given transform, or None."""
    if not obs:
        return None
    if transform == "level":
        return obs[-1][0], obs[-1][1]
    if transform == "yoy_pct" and len(obs) >= 13:
        return obs[-1][0], (obs[-1][1] / obs[-13][1] - 1.0) * 100.0
    if transform == "mom_change_jobs" and len(obs) >= 2:
        # PAYEMS is in thousands; ×1000 to match Kalshi's raw-jobs strikes
        return obs[-1][0], (obs[-1][1] - obs[-2][1]) * 1000.0
    return None


def series_last_updated(series_id: str, api_key: str):
    """When FRED last refreshed this series — i.e. roughly when the number
    was released. This is the correct freshness signal (NOT the observation
    date, which is the reference period)."""
    resp = requests.get(FRED_META_URL, params={
        "series_id": series_id, "api_key": api_key, "file_type": "json"},
        timeout=20)
    resp.raise_for_status()
    raw = resp.json()["seriess"][0]["last_updated"]  # e.g. "2026-07-02 07:31:02-05"
    # normalize the timezone offset (FRED gives -05, Python wants -0500)
    raw = raw.strip()
    if len(raw) >= 3 and raw[-3] in "+-" and raw[-2:].isdigit():
        raw = raw + "00"
    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S%z")


def is_fresh(last_updated: datetime) -> bool:
    """Released within FRESH_HOURS — the crowd-lag window. MACRO_FORCE_FRESH
    bypasses it for verification runs (safe: paper-only)."""
    if os.getenv("MACRO_FORCE_FRESH"):
        return True
    if not last_updated:
        return False
    age_h = (datetime.now(timezone.utc) - last_updated).total_seconds() / 3600.0
    return age_h <= FRESH_HOURS


def known_outcome_signal(market: dict, actual: float) -> dict:
    """If the market's outcome is DETERMINED by the actual value, return a
    signal to buy the known-correct side (if still cheap). The 'model prob'
    is 1.0 because the outcome is known, not forecast."""
    floor_s = market.get("floor_strike")
    cap_s = market.get("cap_strike")
    lo = float(floor_s) if floor_s is not None else float("-inf")
    hi = float(cap_s) if cap_s is not None else float("inf")
    yes_true = lo <= actual < hi  # actual lands in this bucket -> YES certain

    if yes_true:
        price = price_cents(market, "yes_ask")
        side = "yes"
    else:
        bid = price_cents(market, "yes_bid")
        price = (100.0 - bid) if bid else None  # buying NO takes the YES bid
        side = "no"
    if not price or not 0 < price < 100:
        return None
    ev = 100.0 - price - taker_fee_cents(price)  # known outcome pays 100c
    if ev < MIN_EDGE_CENTS:
        return None
    return dict(side=side, price_cents=price, model_prob=1.0, ev_cents=ev,
                ticker=market.get("ticker"),
                subtitle=market.get("subtitle")
                or market.get("yes_sub_title") or "")


def scan(fred_key: str) -> list:
    from kalshi_client import KalshiClient
    client = KalshiClient(env="prod")
    results = []
    for cfg in MACRO_SERIES:
        try:
            obs = fetch_fred(cfg["fred"], fred_key)
            actual = latest_actual(obs, cfg["transform"])
            updated = series_last_updated(cfg["fred"], fred_key)
        except Exception as exc:
            log.warning("Skipping %s (FRED failed: %s)", cfg["name"], exc)
            continue
        if not actual:
            continue
        release_date, value = actual
        if not is_fresh(updated):
            log.info("%s: latest %s=%.2f (updated %s) not fresh — no lag to "
                     "capture", cfg["name"], cfg["fred"], value, updated)
            continue
        log.info("%s: FRESH actual %.2f (FRED updated %s) — checking %s markets",
                 cfg["name"], value, updated, cfg["kalshi"])
        try:
            data = client._request(
                "GET", "/events",
                params={"series_ticker": cfg["kalshi"], "status": "open",
                        "with_nested_markets": "true", "limit": 40})
        except Exception as exc:
            log.warning("Skipping %s markets: %s", cfg["kalshi"], exc)
            continue
        for event in data.get("events", []):
            signals = []
            for market in event.get("markets") or []:
                if market.get("status") not in (None, "active", "open"):
                    continue
                sig = known_outcome_signal(market, value)
                if sig:
                    signals.append(sig)
            if signals:
                signals.sort(key=lambda s: -s["ev_cents"])
                results.append(dict(
                    date=event.get("event_ticker") or event.get("ticker") or "",
                    mu=value, city=cfg["name"],
                    title=event.get("title", ""), signals=signals))
    return results


def main() -> int:
    import os
    setup_logging()
    key = os.getenv("FRED_API_KEY", "").strip()
    if not key:
        log.error("FRED_API_KEY not set. Free key at "
                  "https://fred.stlouisfed.org/docs/api/api_key.html")
        return 1
    try:
        score_pending_paper_trades(PAPER_LOG)
    except Exception as exc:
        log.warning("Scoring skipped (%s)", exc)
    results = scan(key)
    total = sum(len(r["signals"]) for r in results)
    for r in results:
        log.info("%s (%s):", r["title"], r["date"])
        for s in r["signals"]:
            log.info("  RESOLUTION-LAG: buy %s %s @ %.0fc (outcome known) | "
                     "EV +%.1fc | %s", s["side"].upper(), s["ticker"],
                     s["price_cents"], s["ev_cents"], s["subtitle"])
    log.info("%s resolution-lag signal(s). NO ORDERS placed by this script.",
             total or "No")
    return 0


if __name__ == "__main__":
    sys.exit(main())
