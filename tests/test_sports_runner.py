"""Sports executor: the daily dollar brake.

Sizing moved to full Kelly with a 20%-of-bankroll ceiling (owner call). That
made the existing caps insufficient on their own: MAX_ORDERS_PER_DAY bounds how
MANY bets a day, never how much MONEY, and four picks at up to 20% each is
~80% of the bankroll between two sunrises. These tests pin the cap that
actually answers "can it lose the account today?".
"""

import csv
from dataclasses import dataclass
from datetime import datetime, timezone

import ledger
import sports_runner as sr
import strategy_sports


@dataclass
class FakeSettings:
    kill_switch: bool = False
    dry_run: bool = False
    max_order_size: float = 50.0
    max_total_exposure: float = 100.0
    odds_api_key: str = "k"


class _Client:
    def __init__(self, balance=4000):
        self.balance = balance
        self.orders = []

    def get_balance_cents(self):
        return self.balance

    def get_positions(self):
        return {"market_positions": []}

    def get_resting_orders(self):
        return []

    def create_limit_order(self, ticker, side, action, count, price):
        self.orders.append(dict(ticker=ticker, count=count, price=price))
        return {"order_id": f"o{len(self.orders)}"}


def _signal(ticker, price, edge=8.0):
    return dict(ticker=ticker, side="no", price_cents=price, ev_cents=edge,
                model_prob=0.6, subtitle="x", steam=None, is_home=None,
                is_underdog=price < 50)


def _scan_returning(*signals):
    return lambda key: [dict(date="d", mu=0.0, city="MLB", title="t",
                             signals=list(signals))]


def _run(monkeypatch, signals, deployed=0.0, cap=18.0, dry=False):
    monkeypatch.setattr(sr, "MAX_DAILY_USD", cap)
    monkeypatch.setattr(strategy_sports, "sports_deployed_today",
                        lambda: deployed)
    monkeypatch.setattr(strategy_sports, "scan", _scan_returning(*signals))
    monkeypatch.setattr(strategy_sports, "append_paper_trades",
                        lambda *a, **k: None)
    client = _Client()
    sr.sports_pass(client, FakeSettings(dry_run=dry),
                   dict(placed=set(), events=set()))
    return client


def test_cap_stops_placing_once_the_day_is_spent(monkeypatch):
    c = _run(monkeypatch, [_signal("KXA-1-A", 50)], deployed=18.0, cap=18.0)
    assert c.orders == []


def test_cap_trims_the_order_instead_of_skipping_it(monkeypatch):
    # Sizing wants 3 contracts ($1.50) here; only $1.00 of the day is left, so
    # the order is cut to 2 rather than dropped. A big pick must not silently
    # forfeit the remainder of the day's budget.
    c = _run(monkeypatch, [_signal("KXA-1-A", 50)], deployed=17.0, cap=18.0)
    assert len(c.orders) == 1 and c.orders[0]["count"] == 2


def test_an_order_within_budget_is_not_trimmed(monkeypatch):
    # The complement: the trim must only fire when it has to.
    c = _run(monkeypatch, [_signal("KXA-1-A", 50)], deployed=0.0, cap=18.0)
    assert len(c.orders) == 1 and c.orders[0]["count"] == 3


def test_a_trim_that_buys_nothing_is_skipped_not_rounded_up(monkeypatch):
    # 30c left, 50c contracts. Zero, never a token bet.
    c = _run(monkeypatch, [_signal("KXA-1-A", 50)], deployed=17.7, cap=18.0)
    assert c.orders == []


def test_spend_accumulates_within_a_single_pass(monkeypatch):
    # Two picks in one pass must both count against the day, or the cap only
    # ever bounds the first order of each run.
    c = _run(monkeypatch,
             [_signal("KXA-1-A", 50, edge=9.0), _signal("KXB-1-B", 50, edge=8.0)],
             deployed=0.0, cap=6.0)
    spent = sum(o["count"] * o["price"] / 100.0 for o in c.orders)
    assert spent <= 6.0 + 1e-9


def test_dry_run_places_nothing_and_still_respects_the_cap(monkeypatch):
    c = _run(monkeypatch, [_signal("KXA-1-A", 50)], cap=18.0, dry=True)
    assert c.orders == []


# --- the ledger read behind the cap ----------------------------------------

def _ledger(tmp_path, rows):
    p = tmp_path / "exec.csv"
    with open(p, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["placed_at_utc", "model", "ticker", "side", "count",
                    "price_cents", "cost_usd", "order_id", "outcome"])
        for r in rows:
            w.writerow(r)
    return p


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_deployed_today_counts_only_todays_sports_orders(tmp_path, monkeypatch):
    t = _today()
    p = _ledger(tmp_path, [
        [f"{t}T01:00:00Z", "sports", "A", "no", 3, 66, "1.98", "o1", ""],
        [f"{t}T02:00:00Z", "sports", "B", "no", 3, 71, "2.13", "o2", ""],
        [f"{t}T03:00:00Z", "favorite", "C", "yes", 2, 90, "1.80", "o3", ""],
        ["2020-01-01T00:00:00Z", "sports", "D", "no", 3, 50, "1.50", "o4", ""],
    ])
    monkeypatch.setattr(ledger, "EXEC_LOG", p)
    assert abs(strategy_sports.sports_deployed_today() - 4.11) < 1e-6


def test_deployed_today_is_zero_without_a_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "EXEC_LOG", tmp_path / "nope.csv")
    assert strategy_sports.sports_deployed_today() == 0.0


def test_deployed_today_survives_a_junk_cost(tmp_path, monkeypatch):
    t = _today()
    p = _ledger(tmp_path, [
        [f"{t}T01:00:00Z", "sports", "A", "no", 3, 66, "oops", "o1", ""],
        [f"{t}T02:00:00Z", "sports", "B", "no", 3, 71, "2.13", "o2", ""],
    ])
    monkeypatch.setattr(ledger, "EXEC_LOG", p)
    assert abs(strategy_sports.sports_deployed_today() - 2.13) < 1e-6
