"""Tests for equity valuation of open positions (the honest P&L number)."""

import auto_trade


class _FakeClient:
    def __init__(self, positions, markets):
        self._p, self._m = positions, markets

    def get_positions(self):
        return {"market_positions": self._p}

    def get_market(self, t):
        m = self._m[t]
        if m is None:
            raise RuntimeError("unreachable market")
        return m


def test_positions_marked_at_live_value_not_cost():
    positions = [
        # long YES 2 @ open market -> mid (60+64)/2 = 62c -> $1.24
        {"ticker": "A", "position": 2, "market_exposure": 88},
        # long NO 1 @ open market yes_mid 30c -> NO worth 70c -> $0.70
        {"ticker": "B", "position": -1, "market_exposure": 30},
        # YES settled YES -> $1.00
        {"ticker": "C", "position": 1},
        # NO settled YES (lost) -> $0.00  (cost basis would wrongly say $1.35)
        {"ticker": "D", "position": -3, "market_exposure": 135},
        # unpriceable -> cost basis fallback $1.40
        {"ticker": "E", "position": 2, "market_exposure": 140},
        {"ticker": "Z", "position": 0},          # flat -> ignored
    ]
    markets = {
        "A": {"yes_bid": 60, "yes_ask": 64},
        "B": {"yes_bid": 28, "yes_ask": 32},
        "C": {"result": "yes"},
        "D": {"result": "yes"},
        "E": None,
    }
    val = auto_trade.positions_market_value(_FakeClient(positions, markets))
    # 1.24 + 0.70 + 1.00 + 0.00 + 1.40 = 4.34 (NOT the ~5.69 cost basis)
    assert abs(val - 4.34) < 1e-6


def test_unquoted_open_market_falls_back_not_overvalued():
    # a NO position in a market with no bid/ask must NOT be marked at $1
    positions = [{"ticker": "N", "position": -2, "market_exposure": 40}]
    markets = {"N": {"last_price": 0, "yes_bid": 0, "yes_ask": 0}}
    val = auto_trade.positions_market_value(_FakeClient(positions, markets))
    assert abs(val - 0.40) < 1e-6          # cost-basis fallback, not $2.00
