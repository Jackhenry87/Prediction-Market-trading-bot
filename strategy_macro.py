"""Macro resolution-lag strategy (Option 2: crowd-lag on news).

The edge is NOT predicting the number — it's the window AFTER a macro
figure is released (CPI, jobs, unemployment, Fed) but BEFORE Kalshi's
thinner books fully reprice. Once the actual value is known, the correct
side of a threshold market is *determined* (probability ~1), so if it's
still offered below fair value we buy it with a limit order.

Ground truth: BLS's own API first (publishes AT the 8:30:00 release, free,
optional BLS_API_KEY for higher limits), FRED as fallback and freshness
reference (FRED_API_KEY). When BLS shows a newer reference period than
FRED's mirror, we are inside the post-release lag window by definition.
Because a wrong metric mapping would produce confident WRONG trades, this
model:
  - is OFF by default (add 'macro' to ENABLED_MODELS to arm it),
  - only fires when we have a FRESH actual value (released within
    FRESH_HOURS) so we're acting on genuine news, not stale data,
  - only trades the event whose PERIOD matches the observation we hold
    (event_matches_observation) — a June print never prices a November
    market; if the period can't be parsed we refuse to claim certainty,
  - buys only the KNOWN-correct side, with a limit, capturing the lag.

Every ticker/series mapping below is a best guess until confirmed against
a real settled market — verify from a live run's log before trusting it.
"""

import os
import re
import sys
from datetime import date, datetime, timezone

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

# Data series -> the Kalshi market series it settles. 'transform' says how
# to turn the raw observation into the number the market is about. 'bls'
# is the BLS API series id: on release mornings BLS publishes AT 8:30:00
# while FRED's mirror lags ~an hour — exactly the lag window we trade — so
# BLS is tried first and FRED is both fallback and freshness reference.
# Claims are DOL (not BLS) and FRED mirrors them within minutes, so the
# claims row has no 'bls'.
# VERIFY every row against a real settled Kalshi market before arming.
MACRO_SERIES = [
    # jobless claims: weekly (Thu), the frequent testbed. ICSA is the raw
    # claim count; Kalshi thresholds are raw counts too -> 'level'.
    dict(fred="ICSA", kalshi="KXJOBLESSCLAIMS", transform="level",
         name="Initial jobless claims (count)"),
    dict(fred="UNRATE", bls="LNS14000000", kalshi="KXU3", transform="level",
         name="US unemployment rate (%)"),
    # headline "CPI rose X% over 12 months" is the NOT-seasonally-adjusted
    # series (BLS CUUR0000SA0 / FRED CPIAUCNS) — the SA series we used
    # before differs by ~0.1pp, enough to flip a threshold market.
    dict(fred="CPIAUCNS", bls="CUUR0000SA0", kalshi="KXCPIYOY",
         transform="yoy_pct", name="CPI year-over-year (%)"),
    # PAYEMS/CES0000000001 are in THOUSANDS; Kalshi strikes are raw jobs
    dict(fred="PAYEMS", bls="CES0000000001", kalshi="KXPAYROLLS",
         transform="mom_change_jobs",
         name="Nonfarm payrolls (MoM change, jobs)"),
]

BLS_V1_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
BLS_V2_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"


def fetch_fred(series_id: str, api_key: str) -> list:
    """MOST RECENT observations (date, value), newest last. sort_order must
    be 'desc' here: FRED applies 'limit' AFTER sorting, so asc+limit returns
    the oldest rows of the whole series (1940s!), not the latest — a bug the
    first paper run caught when 'current' unemployment came back as Jan 1950."""
    resp = requests.get(FRED_URL, params={
        "series_id": series_id, "api_key": api_key, "file_type": "json",
        "sort_order": "desc", "limit": 25}, timeout=20)
    resp.raise_for_status()
    obs = []
    for o in resp.json().get("observations", []):
        if o.get("value") not in (".", "", None):
            obs.append((o["date"], float(o["value"])))
    obs.reverse()   # newest last, matching everything downstream
    return obs


def fetch_bls(series_id: str, api_key: str = "") -> list:
    """Monthly observations (date, value) straight from the BLS API, newest
    last — same shape as fetch_fred. BLS publishes at the release moment
    (8:30:00 ET) while FRED mirrors ~an hour later. v2 with a free
    registration key (BLS_API_KEY) or v1 keyless (25 requests/day, plenty
    for release bursts)."""
    now = datetime.now(timezone.utc)
    payload = {"seriesid": [series_id],
               "startyear": str(now.year - 2), "endyear": str(now.year)}
    url = BLS_V1_URL
    if api_key:
        payload["registrationkey"] = api_key
        url = BLS_V2_URL
    resp = requests.post(url, json=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS: {data.get('message') or data.get('status')}")
    obs = []
    for o in data["Results"]["series"][0].get("data", []):
        period = o.get("period", "")
        if not period.startswith("M") or period == "M13":  # M13 = annual avg
            continue
        obs.append((f"{o['year']}-{int(period[1:]):02d}-01",
                    float(o["value"])))
    obs.sort()   # ISO dates: lexicographic == chronological, newest last
    return obs


def bls_is_ahead(fred_obs: list, bls_obs: list) -> bool:
    """True when BLS has a NEWER reference period than FRED's mirror —
    which happens precisely during the post-release lag window we trade.
    That gap IS the freshness signal for the BLS path."""
    return bool(bls_obs) and (not fred_obs or bls_obs[-1][0] > fred_obs[-1][0])


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


MONTH_ABBR = dict(JAN=1, FEB=2, MAR=3, APR=4, MAY=5, JUN=6,
                  JUL=7, AUG=8, SEP=9, OCT=10, NOV=11, DEC=12)


def event_period(event_ticker: str):
    """(year, month, day-or-None) embedded in a Kalshi macro event ticker:
    KXU3-26NOV -> (2026, 11, None); KXJOBLESSCLAIMS-26JUL09 -> (2026, 7, 9).
    None when no recognizable period — then we refuse to claim certainty."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})?$", event_ticker or "")
    if not m or m.group(2) not in MONTH_ABBR:
        return None
    return 2000 + int(m.group(1)), MONTH_ABBR[m.group(2)], \
        int(m.group(3)) if m.group(3) else None


def event_matches_observation(event_ticker: str, obs_date: str) -> bool:
    """True only when this event settles on the observation we hold — the
    guard against 'certain' trades on the WRONG period (the other bug the
    paper run caught: pricing November's market off June's print at '100%').
    Monthly tickers must match the observation's reference month; weekly
    claims tickers carry the RELEASE date, which lands within a week after
    the week-ending observation date."""
    period = event_period(event_ticker)
    if not period:
        return False
    year, month, day = period
    try:
        obs = datetime.strptime(obs_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    if day is None:                       # monthly: same reference month
        return (obs.year, obs.month) == (year, month)
    try:
        release = date(year, month, day)  # weekly claims: Thu after week end
    except ValueError:
        return False
    return 0 < (release - obs).days <= 7


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
            updated = series_last_updated(cfg["fred"], fred_key)
        except Exception as exc:
            log.warning("Skipping %s (FRED failed: %s)", cfg["name"], exc)
            continue
        fresh, source = is_fresh(updated), f"FRED updated {updated}"
        if cfg.get("bls"):
            try:
                bls_obs = fetch_bls(cfg["bls"],
                                    os.getenv("BLS_API_KEY", "").strip())
                if bls_is_ahead(obs, bls_obs):
                    # BLS has the print, mirrors don't yet: THE lag window
                    obs, fresh = bls_obs, True
                    source = f"BLS {cfg['bls']} ahead of FRED (release window)"
            except Exception as exc:
                log.warning("BLS fetch failed for %s (%s); using FRED",
                            cfg["name"], exc)
        actual = latest_actual(obs, cfg["transform"])
        if not actual:
            continue
        release_date, value = actual
        if not fresh:
            log.info("%s: latest %s=%.2f (%s) not fresh — no lag to "
                     "capture", cfg["name"], cfg["fred"], value, source)
            continue
        log.info("%s: FRESH actual %.2f (%s) — checking %s markets",
                 cfg["name"], value, source, cfg["kalshi"])
        try:
            data = client._request(
                "GET", "/events",
                params={"series_ticker": cfg["kalshi"], "status": "open",
                        "with_nested_markets": "true", "limit": 40})
        except Exception as exc:
            log.warning("Skipping %s markets: %s", cfg["kalshi"], exc)
            continue
        for event in data.get("events", []):
            event_ticker = (event.get("event_ticker")
                            or event.get("ticker") or "")
            if not event_matches_observation(event_ticker, release_date):
                log.debug("%s: period != observation %s — outcome NOT known, "
                          "skipping", event_ticker, release_date)
                continue
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
                    date=event_ticker,
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
