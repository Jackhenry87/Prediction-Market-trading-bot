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


# --- the per-order ceiling must trim, not block ----------------------------
#
# 2026-08-10: "BLOCKED KXMLBTOTAL-...-11: order notional 10.65 USDC exceeds
# MAX_ORDER_SIZE 10.00". The sizer and the safety gate compute their caps
# independently, so the one pick that qualified all day died at the gate and
# nothing was placed. The gate keeps the final say; the order is made to fit.

def test_an_order_over_the_per_order_cap_is_trimmed_not_dropped(monkeypatch):
    monkeypatch.setattr(sr, "MAX_DAILY_USD", 1000.0)
    monkeypatch.setattr(strategy_sports, "sports_deployed_today", lambda: 0.0)
    monkeypatch.setattr(strategy_sports, "scan", _scan_returning(
        _signal("KXA-1-A", 50, edge=12.0)))
    monkeypatch.setattr(strategy_sports, "append_paper_trades",
                        lambda *a, **k: None)
    monkeypatch.setenv("MAX_ORDER_BANKROLL_PCT", "1")
    monkeypatch.setenv("MAX_ORDER_ABS", "10")
    client = _Client(balance=100000)          # big book -> sizer wants a lot
    settings = FakeSettings(max_order_size=10.0, max_total_exposure=10000.0)
    sr.sports_pass(client, settings, dict(placed=set(), events=set()))
    assert client.orders, "the pick must be placed, not blocked"
    notional = client.orders[0]["count"] * client.orders[0]["price"] / 100.0
    assert notional <= 10.0 + 1e-9


def test_a_cap_too_small_for_one_contract_places_nothing(monkeypatch):
    monkeypatch.setattr(sr, "MAX_DAILY_USD", 1000.0)
    monkeypatch.setattr(strategy_sports, "sports_deployed_today", lambda: 0.0)
    monkeypatch.setattr(strategy_sports, "scan", _scan_returning(
        _signal("KXA-1-A", 50, edge=12.0)))
    monkeypatch.setattr(strategy_sports, "append_paper_trades",
                        lambda *a, **k: None)
    monkeypatch.setenv("MAX_ORDER_BANKROLL_PCT", "1")
    monkeypatch.setenv("MAX_ORDER_ABS", "0.20")
    client = _Client(balance=100000)
    settings = FakeSettings(max_order_size=0.20, max_total_exposure=10000.0)
    sr.sports_pass(client, settings, dict(placed=set(), events=set()))
    assert client.orders == []


# --- the exposure cap must trim too ----------------------------------------
#
# The third cap that blocked instead of trimming, and the one that bites
# hardest: exposure only GROWS as positions accumulate, so once the book is
# nearly full every later pick was discarded outright. With $7 of headroom an
# $8 order was lost entirely rather than cut to $7. The symptom is
# indistinguishable from "the model found nothing".

def _run_with(monkeypatch, settings, balance=100000, edge=12.0, price=50):
    monkeypatch.setattr(sr, "MAX_DAILY_USD", 1000.0)
    monkeypatch.setattr(strategy_sports, "sports_deployed_today", lambda: 0.0)
    monkeypatch.setattr(strategy_sports, "scan", _scan_returning(
        _signal("KXA-1-A", price, edge=edge)))
    monkeypatch.setattr(strategy_sports, "append_paper_trades",
                        lambda *a, **k: None)
    monkeypatch.setenv("MAX_ORDER_BANKROLL_PCT", "100")
    monkeypatch.setenv("MAX_ORDER_ABS", "1000")
    monkeypatch.setenv("MAX_EXPOSURE_BANKROLL_PCT", "100")
    monkeypatch.setenv("MAX_EXPOSURE_ABS", str(settings.max_total_exposure))
    client = _Client(balance=balance)
    sr.sports_pass(client, settings, dict(placed=set(), events=set()))
    return client


def test_an_order_over_the_exposure_headroom_is_trimmed(monkeypatch):
    # current_exposure_usd is 0 for a fake client with no positions, so the
    # headroom IS the cap here; a tiny cap must still produce a small order.
    s = FakeSettings(max_order_size=1000.0, max_total_exposure=7.0)
    c = _run_with(monkeypatch, s)
    assert c.orders, "the pick must be trimmed to fit, not discarded"
    notional = c.orders[0]["count"] * c.orders[0]["price"] / 100.0
    assert notional <= 7.0 + 1e-9


def test_no_exposure_headroom_places_nothing(monkeypatch):
    s = FakeSettings(max_order_size=1000.0, max_total_exposure=0.0)
    c = _run_with(monkeypatch, s)
    assert c.orders == []


def test_headroom_too_small_for_one_contract_places_nothing(monkeypatch):
    s = FakeSettings(max_order_size=1000.0, max_total_exposure=0.30)
    c = _run_with(monkeypatch, s, price=50)
    assert c.orders == []
