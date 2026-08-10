"""Win expectancy from game state.

The exact numbers cannot be verified here against a reference table, so these
tests pin the PROPERTIES a win-probability model must have. A model that
satisfies all of them can still be miscalibrated; a model that violates any of
them is broken in a way that would size a position wrongly.
"""

import win_expectancy as we

SEED = 7
FAST = 1500          # enough for property assertions, fast enough for CI


def wp(inning, half, outs, bases, home, away):
    return we.win_probability(inning, half, outs, bases, home, away,
                              sims=FAST, seed=SEED)


# --- bounds and basic sanity -----------------------------------------------

def test_probability_is_always_a_probability():
    for st in [(1, "top", 0, (0, 0, 0), 0, 0), (9, "bottom", 2, (1, 1, 1), 0, 9),
               (7, "top", 1, (0, 1, 0), 12, 1)]:
        p = wp(*st)
        assert 0.0 <= p <= 1.0


def test_even_game_is_near_a_coin_flip_at_first_pitch():
    p = wp(1, "top", 0, (0, 0, 0), 0, 0)
    # Slightly above 0.5: the home team bats last. No home-field advantage is
    # modelled beyond that, so this must not drift far from even.
    assert 0.48 <= p <= 0.60


# --- monotonicity: the directions that must never invert --------------------

def test_more_runs_ahead_is_always_better():
    ps = [wp(5, "top", 0, (0, 0, 0), h, 3) for h in (1, 2, 3, 4, 5)]
    assert ps == sorted(ps), f"win prob must rise with the lead, got {ps}"


def test_a_lead_is_worth_more_later_in_the_game():
    early = wp(2, "top", 0, (0, 0, 0), 4, 2)
    late = wp(8, "top", 0, (0, 0, 0), 4, 2)
    assert late > early


def test_runners_on_help_the_team_that_is_batting():
    # Home batting in the 8th: bases loaded must beat bases empty.
    empty = wp(8, "bottom", 0, (0, 0, 0), 2, 3)
    loaded = wp(8, "bottom", 0, (1, 1, 1), 2, 3)
    assert loaded > empty


def test_more_outs_hurt_the_team_that_is_batting():
    ps = [wp(9, "bottom", o, (1, 1, 0), 2, 3) for o in (0, 1, 2)]
    assert ps == sorted(ps, reverse=True), f"outs must hurt the batter: {ps}"


def test_runners_on_hurt_the_home_team_when_the_away_team_is_batting():
    empty = wp(8, "top", 0, (0, 0, 0), 3, 2)
    loaded = wp(8, "top", 0, (1, 1, 1), 3, 2)
    assert loaded < empty


# --- the endgame ------------------------------------------------------------

def test_a_big_late_lead_is_nearly_certain():
    assert wp(9, "top", 2, (0, 0, 0), 10, 1) > 0.98


def test_a_big_late_deficit_is_nearly_hopeless():
    assert wp(9, "bottom", 2, (0, 0, 0), 1, 10) < 0.02


def test_finished_games_are_certain_not_simulated():
    # A model that reports 0.97 for a game already won is a bug waiting to
    # size a position.
    assert we.win_probability_for(
        dict(state="post", home_score=5, away_score=2)) == 1.0
    assert we.win_probability_for(
        dict(state="post", home_score=2, away_score=5)) == 0.0


def test_home_does_not_bat_when_already_ahead_in_the_ninth():
    # If the home team could keep scoring in a game it has already won, its
    # win probability would be inflated everywhere in the late innings.
    assert wp(9, "top", 2, (0, 0, 0), 4, 1) > 0.95


# --- run expectancy table ---------------------------------------------------

def test_run_expectancy_rises_with_runners_and_falls_with_outs():
    assert we.run_expectancy((0, 0, 0), 0) < we.run_expectancy((1, 1, 1), 0)
    assert we.run_expectancy((1, 0, 0), 0) > we.run_expectancy((1, 0, 0), 2)


def test_three_outs_is_worth_nothing():
    assert we.run_expectancy((1, 1, 1), 3) == 0.0


def test_every_base_out_state_is_covered():
    for bases in [(a, b, c) for a in (0, 1) for b in (0, 1) for c in (0, 1)]:
        for outs in (0, 1, 2):
            assert we.run_expectancy(bases, outs) >= 0


# --- determinism ------------------------------------------------------------

def test_the_same_seed_gives_the_same_answer():
    a = wp(4, "top", 1, (1, 0, 0), 2, 2)
    b = wp(4, "top", 1, (1, 0, 0), 2, 2)
    assert a == b
