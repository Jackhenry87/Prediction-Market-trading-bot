"""The paper-to-live gate.

This scoreboard is the only thing standing between "his profile looks
amazing" and real orders, so its arithmetic has to be honest — especially
about unsettled positions, which must never be counted as anything.
"""

import follow_scoreboard as fs


def _row(**kw):
    base = dict(decision="paper", reason="", model="sports", our_p="0.6",
                market_price_cents="", his_price_cents="", contracts="",
                outcome="")
    base.update(kw)
    return base


def test_settled_pnl_counts_only_settled_rows():
    rows = [
        _row(contracts="10", market_price_cents="40", outcome="win (+60c)"),
        _row(contracts="10", market_price_cents="40", outcome="loss (-40c)"),
        _row(contracts="10", market_price_cents="40", outcome=""),   # open
    ]
    pnl = fs.settled_pnl(rows)
    assert pnl["wins"] == 1 and pnl["losses"] == 1
    # +10x60c  -10x40c = +$2.00; the open position contributes nothing
    assert round(pnl["net_usd"], 2) == 2.00


def test_open_positions_are_not_counted_as_flat():
    """Counting an unsettled copy as zero would flatter a losing book."""
    rows = [_row(contracts="10", market_price_cents="40", outcome="")]
    assert fs.settled_pnl(rows) == dict(wins=0, losses=0, net_usd=0.0)


def test_slippage_is_measured_against_his_entry():
    rows = [
        _row(his_price_cents="42", market_price_cents="45"),   # +3
        _row(his_price_cents="50", market_price_cents="59"),   # +9
        _row(his_price_cents="", market_price_cents="60"),     # ignored
    ]
    slip = fs.slippage_stats(rows)
    assert slip["n"] == 2
    assert round(slip["mean"], 1) == 6.0
    assert slip["worst"] == 9


def test_empty_ledger_renders_without_crashing():
    out = fs.build([])
    assert "No copies have settled yet" in out
    assert "0 notification(s)" in out


def test_report_shows_the_fire_rate():
    """Two thirds of his bets are unpriceable by us — the report has to make
    that visible rather than hide it behind the trades that did fire."""
    rows = [_row(decision="paper")] + [
        _row(decision="skip", reason="no model of ours prices this market")
        for _ in range(9)]
    out = fs.build(rows)
    assert "10 notification(s) · 1 would-trade (10%)" in out
    assert "no model of ours prices this market" in out
