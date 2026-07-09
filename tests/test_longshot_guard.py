"""Guards against the Chicago-88° blunder: a resolution-lag model bought 98 NO
contracts on a near-certain-LOSER bucket at a 1-8c longshot price. Two rails
must now make that impossible:
  1. lag models (macro/nowcast) get a HIGH price band, so a longshot buy is
     dropped before it can ever be sized/placed, and
  2. size_order can never balloon the contract count on a cheap price."""

import auto_trade as at
from config import Settings
from ledger import apply_price_band


def _res(prices):
    """One event's results with a signal at each given buy price (cents)."""
    return [{"event_ticker": "E", "date": "d", "mu": 0.0,
             "signals": [{"side": "no", "price_cents": p, "ev_cents": 5,
                          "model_prob": 0.9} for p in prices]}]


def test_lag_band_drops_longshot_prices():
    # the exact failure: nowcast NO at 1/6/8c must be dropped; a genuine lag
    # capture at 95c is kept.
    banded = apply_price_band(_res([1, 6, 8, 95]),
                              at.LAG_MIN_PRICE, at.LAG_MAX_PRICE)
    kept = [s["price_cents"] for r in banded for s in r["signals"]]
    assert kept == [95]
    assert 1 not in kept and 8 not in kept


def test_lag_band_has_a_floor_at_all():
    # a fully-longshot slate leaves NOTHING to trade (the old code returned it
    # untouched, which is how 98 NO got placed at 1-8c).
    assert apply_price_band(_res([1, 3, 8]),
                            at.LAG_MIN_PRICE, at.LAG_MAX_PRICE) == []


def _settings(**kw):
    d = dict(max_order_size=2.0, max_total_exposure=200.0)
    d.update(kw)
    return Settings(**d)


def test_size_order_caps_units_on_cheap_price(monkeypatch):
    monkeypatch.setattr(at, "MAX_CONTRACTS_PER_ORDER", 50)
    # $2 budget at 1c would be 200 contracts -> capped at 50
    assert at.size_order(1, 0.0, _settings(), max_usd=2.0) == 50
    # $0.73 at 1c (the real fill) would be 73 -> capped at 50
    assert at.size_order(1, 0.0, _settings(), max_usd=0.73) == 50


def test_size_order_normal_prices_unaffected(monkeypatch):
    monkeypatch.setattr(at, "MAX_CONTRACTS_PER_ORDER", 50)
    # $2 budget at 62c -> 3 contracts, well under the cap
    assert at.size_order(62, 0.0, _settings(), max_usd=2.0) == 3
