"""National Weather Service forecast for Central Park, NYC — the station
Kalshi's KXHIGHNY (NYC daily high temperature) markets settle against.

Free public API, no key needed. NWS asks for an identifying User-Agent.
"""

import requests

from trade_logger import get_logger

log = get_logger("nws")

BASE = "https://api.weather.gov"
LAT, LON = 40.7794, -73.9692  # Central Park
HEADERS = {"User-Agent": "prediction-market-trading-bot (personal project)"}


def get_daily_high_forecasts() -> dict:
    """Return {'YYYY-MM-DD': forecast_high_F, ...} for the coming days."""
    points = requests.get(
        f"{BASE}/points/{LAT},{LON}", headers=HEADERS, timeout=20
    )
    points.raise_for_status()
    forecast_url = points.json()["properties"]["forecast"]

    forecast = requests.get(forecast_url, headers=HEADERS, timeout=20)
    forecast.raise_for_status()

    highs = {}
    for period in forecast.json()["properties"]["periods"]:
        # Daytime periods carry the day's high; overnight ones carry lows.
        if period.get("isDaytime") and period.get("temperatureUnit") == "F":
            date = period["startTime"][:10]
            highs[date] = float(period["temperature"])
    log.info("NWS Central Park daily highs: %s", highs)
    return highs
