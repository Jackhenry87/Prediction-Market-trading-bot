"""Measure real 1-day-ahead high-temperature forecast error per station.

SIGMA_F in strategy_weather is the std dev we assume for NWS high-temp
forecast error — every weather probability flows from it. It was widened
3.0 -> 4.5 on judgement after early losses; this script replaces judgement
with measurement so it can be tuned per city.

Forecast:  Open-Meteo Historical Forecast API (archived best_match model
           forecasts — a close proxy for the NWS point forecast we trade).
Actual:    Open-Meteo Archive API (ERA5 reanalysis at the same point).

Prints per-station bias / std / MAE / fat-tail rates over the last year and
a suggested sigma (std inflated ~10% for station-vs-grid representativeness,
rounded up to 0.5F). Read-only; run from the calibrate-weather workflow.
"""

import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from strategy_weather import CITIES
from trade_logger import get_logger, setup_logging

log = get_logger("calibrate_weather")

FC_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
AR_URL = "https://archive-api.open-meteo.com/v1/archive"
CAL_PATH = Path(__file__).resolve().parent / "weather_calibration.json"

# Self-calibration knobs. RECENT captures the CURRENT season; the year
# baseline is the humble floor. A station whose recent forecast error is so
# large that even correct pricing has ~no edge (sigma above BENCH_SIGMA —
# error wider than the buckets) gets benched until it recovers.
RECENT_DAYS = int(os.getenv("WEATHER_CAL_RECENT_DAYS", "45"))
MIN_RECENT = int(os.getenv("WEATHER_CAL_MIN_RECENT", "20"))
BENCH_SIGMA = float(os.getenv("WEATHER_CAL_BENCH_SIGMA", "5.0"))


def calibrate(recent: dict, baseline: dict) -> dict:
    """Effective per-station calibration: prefer the RECENT window (current
    season) once it has enough days, else the year baseline. Sigma is never
    tighter than the long-run measurement (humble in the tails). Bench the
    station when even a correctly-priced forecast has little edge because
    the recent error dwarfs the bucket width."""
    use_recent = bool(recent and recent["n"] >= MIN_RECENT)
    src = recent if use_recent else (baseline or recent)
    if not src:
        return None
    sigma = src["suggested_sigma"]
    if baseline:
        sigma = max(sigma, baseline["suggested_sigma"])
    return dict(bias=src["bias"], sigma=sigma, trade=sigma <= BENCH_SIGMA,
                recent_std=(recent or {}).get("std"),
                recent_bias=(recent or {}).get("bias"),
                n=src["n"], window="recent" if use_recent else "baseline")


def daily_map(url: str, lat: float, lon: float, start: str, end: str) -> dict:
    """One year of daily highs. The first run showed Open-Meteo can be slow
    on later requests (LAX/Austin timed out at 45s) — use a generous timeout
    and one retry with a pause."""
    last_exc = None
    for attempt in range(3):
        if attempt:
            time.sleep(20 * attempt)
        try:
            resp = requests.get(url, params={
                "latitude": lat, "longitude": lon,
                "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit", "timezone": "auto",
                "start_date": start, "end_date": end}, timeout=120)
            resp.raise_for_status()
            d = resp.json().get("daily", {})
            return {t: v for t, v in zip(d.get("time", []),
                                         d.get("temperature_2m_max", []))
                    if v is not None}
        except Exception as exc:
            last_exc = exc
    raise last_exc


def error_stats(errs: list) -> dict:
    n = len(errs)
    mean = sum(errs) / n
    std = math.sqrt(sum((e - mean) ** 2 for e in errs) / (n - 1))
    return dict(
        n=n, bias=round(mean, 2), std=round(std, 2),
        mae=round(sum(abs(e) for e in errs) / n, 2),
        p_gt3=round(sum(1 for e in errs if abs(e) > 3) / n, 3),
        p_gt5=round(sum(1 for e in errs if abs(e) > 5) / n, 3),
        # settle is a point station, our 'actual' a grid cell: inflate ~10%
        # then round UP to 0.5F — humble beats overconfident in the tails
        suggested_sigma=math.ceil(std * 1.10 * 2) / 2,
    )


def main() -> int:
    setup_logging()
    end = date.today() - timedelta(days=5)      # ERA5 lags a few days
    start = end - timedelta(days=365)
    recent_start = (end - timedelta(days=RECENT_DAYS)).isoformat()
    start_s, end_s = start.isoformat(), end.isoformat()
    log.info("Calibrating high-temp error: baseline %s..%s, recent last %dd",
             start_s, end_s, RECENT_DAYS)

    out = {}
    for city in CITIES:
        try:
            fc = daily_map(FC_URL, city["lat"], city["lon"], start_s, end_s)
            ar = daily_map(AR_URL, city["lat"], city["lon"], start_s, end_s)
        except Exception as exc:
            log.warning("%s: fetch failed (%s)", city["name"], exc)
            continue
        base_errs = [fc[t] - ar[t] for t in fc if t in ar]
        recent_errs = [fc[t] - ar[t] for t in fc
                       if t in ar and t >= recent_start]
        if len(base_errs) < 60:
            log.warning("%s: only %d overlapping days — skipping",
                        city["name"], len(base_errs))
            continue
        baseline = error_stats(base_errs)
        recent = error_stats(recent_errs) if len(recent_errs) >= 2 else None
        cal = calibrate(recent, baseline)
        if cal is None:
            continue
        cal["updated"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        out[city["series"]] = cal
        log.info("%-22s [%s n=%d] bias=%+.2fF sigma=%.1fF %s (baseline "
                 "bias %+.2f sigma %.1f)", city["name"], cal["window"],
                 cal["n"], cal["bias"], cal["sigma"],
                 "TRADE" if cal["trade"] else "BENCHED",
                 baseline["bias"], baseline["suggested_sigma"])

    if out:
        CAL_PATH.write_text(json.dumps(out, indent=2))
        log.info("Wrote %s (%d stations, %d benched)", CAL_PATH.name,
                 len(out), sum(1 for c in out.values() if not c["trade"]))
    print(json.dumps(out, indent=2))
    return 0 if out else 1


if __name__ == "__main__":
    sys.exit(main())
