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
import sys
from datetime import date, timedelta

import requests

from strategy_weather import CITIES
from trade_logger import get_logger, setup_logging

log = get_logger("calibrate_weather")

FC_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
AR_URL = "https://archive-api.open-meteo.com/v1/archive"


def daily_map(url: str, lat: float, lon: float, start: str, end: str) -> dict:
    resp = requests.get(url, params={
        "latitude": lat, "longitude": lon, "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit", "timezone": "auto",
        "start_date": start, "end_date": end}, timeout=45)
    resp.raise_for_status()
    d = resp.json().get("daily", {})
    return {t: v for t, v in zip(d.get("time", []),
                                 d.get("temperature_2m_max", []))
            if v is not None}


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
    start_s, end_s = start.isoformat(), end.isoformat()
    log.info("Calibrating 1-day-ahead high-temp error %s .. %s", start_s, end_s)

    out = {}
    for city in CITIES:
        try:
            fc = daily_map(FC_URL, city["lat"], city["lon"], start_s, end_s)
            ar = daily_map(AR_URL, city["lat"], city["lon"], start_s, end_s)
        except Exception as exc:
            log.warning("%s: fetch failed (%s)", city["name"], exc)
            continue
        errs = [fc[t] - ar[t] for t in fc if t in ar]
        if len(errs) < 60:
            log.warning("%s: only %d overlapping days — skipping",
                        city["name"], len(errs))
            continue
        s = error_stats(errs)
        out[city["series"]] = s
        log.info("%-22s n=%d bias=%+.2fF std=%.2fF mae=%.2fF |e|>3F=%.0f%% "
                 "|e|>5F=%.0f%% -> sigma %.1fF", city["name"], s["n"],
                 s["bias"], s["std"], s["mae"], 100 * s["p_gt3"],
                 100 * s["p_gt5"], s["suggested_sigma"])

    print(json.dumps(out, indent=2))
    return 0 if out else 1


if __name__ == "__main__":
    sys.exit(main())
