"""Tests for maker execution and the intraday nowcast model."""

from datetime import datetime, timedelta, timezone

import auto_trade as at
import strategy_nowcast as nc


def test_maker_price_rests_inside(monkeypatch):
    monkeypatch.setattr(at, "MAKER_MODE", True)
    assert at.maker_price(62, "weather") == 61     # one cent inside
    assert at.maker_price(62.4, "sports") == 61
    assert at.maker_price(1, "weather") == 1       # floor
    # time-critical known-outcome models still cross at full price
    assert at.maker_price(96, "macro") == 96
    assert at.maker_price(96, "nowcast") == 96
    # maker mode off -> everything crosses
    monkeypatch.setattr(at, "MAKER_MODE", False)
    assert at.maker_price(62, "weather") == 62


class _CancelClient:
    def __init__(self):
        self.cancelled = []

    def cancel_order(self, oid):
        self.cancelled.append(oid)


def test_refresh_resting_cancels_only_stale_buys(monkeypatch):
    monkeypatch.setattr(at, "RESTING_TTL_H", 6.0)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=7)).isoformat()
    fresh = (now - timedelta(hours=1)).isoformat()
    resting = [
        {"order_id": "stale-buy", "action": "buy", "ticker": "A",
         "created_time": old},
        {"order_id": "fresh-buy", "action": "buy", "ticker": "B",
         "created_time": fresh},
        {"order_id": "old-sell", "action": "sell", "ticker": "C",
         "created_time": old},                       # take-profit: keep
        {"order_id": "no-time", "action": "buy", "ticker": "D",
         "created_time": "garbage"},                 # unparseable: keep
    ]
    client = _CancelClient()
    at.refresh_resting(client, resting)
    assert client.cancelled == ["stale-buy"]


def _mk(ticker, floor, cap, ask=None, bid=None, status="active"):
    return {"ticker": ticker, "floor_strike": floor, "cap_strike": cap,
            "yes_ask": ask, "yes_bid": bid, "status": status,
            "yes_sub_title": ticker}


def test_determined_signals_exceedance_only():
    markets = [
        # bucket 87-88, obs max 91.2 -> high can't be in it: NO certain
        _mk("B87.5", 87, 88, ask=9, bid=6),
        # bucket 91-92, obs INSIDE it but day not over -> NOT determined
        _mk("B91.5", 91, 92, ask=55, bid=50),
        # above-threshold 90+, obs 91.2 -> YES certain
        _mk("T90", 90, None, ask=88, bid=85),
        # above-threshold 95+, not reached -> unknown, never traded
        _mk("T95", 95, None, ask=10, bid=7),
    ]
    sigs = nc.determined_signals(markets, obs_max=91.2)
    by = {s["ticker"]: s for s in sigs}
    assert by["B87.5"]["side"] == "no"
    assert by["B87.5"]["price_cents"] == 94          # 100 - bid 6
    assert by["B87.5"]["model_prob"] == 1.0
    assert by["T90"]["side"] == "yes" and by["T90"]["price_cents"] == 88
    assert "B91.5" not in by and "T95" not in by


def test_determined_signals_margin_and_edge():
    # obs exactly at cap: NOT past it by the margin -> refuse (rounding risk)
    assert nc.determined_signals([_mk("B87.5", 87, 88, ask=9, bid=6)],
                                 obs_max=88.2) == []
    # certain but already priced to 99c -> no lag left, skip (edge < 3c)
    assert nc.determined_signals([_mk("T90", 90, None, ask=99, bid=97)],
                                 obs_max=95.0) == []
    # closed market never traded
    assert nc.determined_signals(
        [_mk("B87.5", 87, 88, ask=9, bid=6, status="closed")],
        obs_max=95.0) == []


def test_observed_max_parses_and_converts(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self):
            return {"features": [
                {"properties": {"temperature": {"value": 30.0}}},   # 86F
                {"properties": {"temperature": {"value": 33.5}}},   # 92.3F
                {"properties": {"temperature": {"value": None}}},   # gap
            ]}
    monkeypatch.setattr(nc.requests, "get", lambda *a, **k: R())
    got = nc.observed_max_f("KNYC", "America/New_York")
    assert abs(got - (33.5 * 9 / 5 + 32)) < 1e-9


def test_nowcast_stations_cover_all_cities():
    from strategy_weather import CITIES
    for c in CITIES:
        assert c["series"] in nc.STATIONS
