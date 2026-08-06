"""Kelly sizing for copied trades.

The owner specified Kelly as K% = (p·b - q)/b with b the net decimal odds.
For a binary contract bought at c cents, b = (100-c)/c, and that expression
reduces exactly to sizing.kelly_fraction's edge/(100-c). The first test pins
that identity, because the whole sizing story depends on the two formulas
being the same formula.
"""

import follow_prob
import follow_runner as fr
import sizing


def kelly_from_odds(p: float, b: float) -> float:
    """The owner's formula, written out literally."""
    q = 1.0 - p
    return (p * b - q) / b


def test_owner_formula_equals_the_repo_formula():
    """(p·b - q)/b  ==  (p - c/100)/(1 - c/100)  ==  edge/(100-c)."""
    for c in (20, 42, 55, 60, 89):
        for p in (0.35, 0.5, 0.62, 0.9):
            b = (100 - c) / c                  # net decimal odds at c cents
            theirs = kelly_from_odds(p, b)
            edge_cents = 100.0 * p - c         # gross edge, no fee
            ours = sizing.kelly_fraction(c, edge_cents)
            if theirs <= 0:
                assert ours == 0.0             # no edge -> no bet
            else:
                assert round(theirs, 9) == round(ours, 9)


def test_worked_example_from_the_spec():
    """Their example: even odds (b=1, i.e. 50c), p=0.60 -> 20% of bankroll."""
    assert round(kelly_from_odds(0.60, 1.0), 9) == 0.20
    assert round(sizing.kelly_fraction(50, 100 * 0.60 - 50), 9) == 0.20


def test_quarter_kelly_is_a_quarter_of_full():
    """Fractional Kelly, as specified: a quarter of the recommendation."""
    full = sizing.kelly_fraction(50, 10.0)
    out = sizing.size_position(1000.0, 50, 10.0, kelly_fraction_used=0.25)
    assert round(out["kelly_full"], 6) == round(full, 6)
    # capped by MAX_BANKROLL_PCT, so compare the uncapped intent
    assert out["kelly_used"] <= full * 0.25 + 1e-9


def test_fee_makes_a_thin_edge_unprofitable():
    """EV is computed net of Kalshi's taker fee, so a signal has to beat the
    cost of trading it, not merely the mid."""
    calc = fr.evaluate(our_p=0.52, price=50.0)
    gross = 100 * 0.52 - 50.0
    assert calc["edge_cents"] < gross          # fee has been taken out
    assert calc["edge_cents"] > 0

    thin = fr.evaluate(our_p=0.505, price=50.0)
    assert thin["edge_cents"] < 0              # 0.5c of edge does not pay 1.75c
    assert thin["kelly_full"] == 0.0


def test_sub_contract_stake_is_zero_contracts_not_one():
    """No hail marys: a stake that cannot buy one contract buys none."""
    out = sizing.size_position(1.00, 90, 5.0, kelly_fraction_used=0.25)
    assert out["contracts"] == 0
    assert "no bet" in out["reason"]


def test_his_stake_never_scales_our_size():
    """He bets round $100,000 tiers off a bankroll we cannot see; our size
    comes from OUR bankroll and OUR probability, and nothing else."""
    small = sizing.size_position(100.0, 44, 20.0, kelly_fraction_used=0.25)
    big = sizing.size_position(10_000.0, 44, 20.0, kelly_fraction_used=0.25)
    assert big["contracts"] > small["contracts"]
    # the fraction is identical — only the bankroll differs
    assert round(small["kelly_used"], 9) == round(big["kelly_used"], 9)


# --- coverage routing ----------------------------------------------------

def test_unsupported_markets_get_no_probability():
    """Spreads, combos, outrights and goalscorers are markets he trades often
    and we cannot price. They must return no view rather than a guess."""
    for title in ("Arizona vs Pittsburgh: Spread",
                  "The Open Championship Winner",
                  "Portugal vs Croatia: Goalscorer",
                  "Combo",
                  "England vs Argentina",     # 'advance' style markets
                  "Spain vs Austria: Regulation Time BTTS"):
        assert follow_prob.is_unsupported(title, {}) or \
            follow_prob.model_prob("KXSOMETHING-1", {}, event_title=title) \
            == (None, None)


def test_sport_routing_by_ticker():
    assert follow_prob.sport_for("KXMLBGAME-26AUG06LADCHC-CHC") == "baseball_mlb"
    assert follow_prob.sport_for("KXNBA-26AUG06-NYK") == "basketball_nba"
    assert follow_prob.sport_for("KXATPMATCH-26JUL07-DJO") is None


def test_no_odds_key_means_no_view():
    """Without the odds feed the sports model has nothing to say, and
    silence must not be mistaken for agreement."""
    assert follow_prob.model_prob("KXMLBGAME-26AUG06LADCHC-CHC", {},
                                  event_title="Los Angeles D vs Chicago C",
                                  odds_api_key="") == (None, None)
