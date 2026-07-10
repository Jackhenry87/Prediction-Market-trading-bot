"""NRFI/YRFI selection logic — the part where the edge lives. The Martingale is
just staking; if this picker is wrong, the whole thing loses. So it's tested
tightly: devig, edge sign, direction choice, stagger, and stake progression."""

import strategy_nrfi as nrfi


def test_fair_yrfi_devig_between_0_and_1():
    p = nrfi.fair_yrfi(1.82, 1.96)      # Over 0.5 / Under 0.5
    assert 0.45 < p < 0.6               # ~52% YRFI, league-typical
    assert nrfi.fair_yrfi(0, 2.0) is None


def test_edge_sign():
    # fair YRFI 45% -> NRFI true 55%. Buying NO at 48c is +EV; YES at 55c is -EV.
    assert nrfi.edge_cents(0.45, 48, is_yrfi_side=False) > 3
    assert nrfi.edge_cents(0.45, 55, is_yrfi_side=True) < 0


def test_stake_mult_progression():
    assert [nrfi.stake_mult(i) for i in range(4)] == [1, 2, 4, None]


def _g(ticker, commence, fair, yes_ask, yes_bid):
    return dict(ticker=ticker, commence=commence, fair_yrfi=fair,
                yes_ask=yes_ask, no_ask=100 - yes_bid)


def test_decide_picks_richer_direction_and_orders_by_time():
    # three games where NRFI (Under) is the +EV side; staggered by >45 min
    games = [
        _g("C", 7200, 0.44, 58, 55),   # start last
        _g("A", 0,    0.44, 58, 55),   # start first
        _g("B", 3600, 0.44, 58, 55),   # middle
    ]
    d = nrfi.decide(games, min_edge=3, max_legs=3, stagger_min=45)
    assert d["direction"] == "no"                       # NRFI has the edge
    assert [l["ticker"] for l in d["legs"]] == ["A", "B", "C"]   # start order


def test_decide_enforces_stagger():
    # B starts only 20 min after A -> B dropped (can't resolve A's inning first)
    games = [_g("A", 0, 0.44, 58, 55), _g("B", 1200, 0.44, 58, 55)]
    d = nrfi.decide(games, min_edge=3, max_legs=3, stagger_min=45)
    assert [l["ticker"] for l in d["legs"]] == ["A"]


def test_decide_none_when_no_edge():
    # priced at fair -> no +EV side
    games = [_g("A", 0, 0.50, 52, 48)]
    assert nrfi.decide(games, min_edge=3) is None
