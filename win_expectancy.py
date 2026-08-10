"""MLB win expectancy from live game state. No API key, no credits.

    P(home wins | inning, half, outs, bases, score)

WHY A MODEL AND NOT A FEED
--------------------------
An in-play edge needs a fair value computed independently of the market we are
trading against. Buying a live odds feed would cost 2 Odds API credits per call
against a 500/month free tier — one evening of polling and the month is gone.
Game state, by contrast, is free and unlimited from ESPN's public scoreboard,
and win expectancy from state is a solved problem in baseball analytics.

THE MODEL
---------
Monte Carlo over the remainder of the game:

  * The half-inning currently in progress contributes runs drawn around its
    base/out RUN EXPECTANCY (the standard RE24 table) — that is what "runner on
    second, one out" is worth on average.
  * Every remaining full half-inning contributes Poisson(LEAGUE_RUNS_PER_HALF).
  * The home team does not bat in the bottom of the ninth when already ahead,
    and a walk-off ends scoring the moment it takes the lead. Ignoring this
    over-counts home runs scored and biases P(home) upward.
  * Tie after nine goes to extras, modelled as repeated even innings, which is
    a mild approximation: the runner-on-second rule makes real extras higher
    scoring than a regular inning.

HONEST LIMITS
-------------
1. Poisson UNDERSTATES the variance of real innings. Baseball run scoring is
   overdispersed — most half-innings score zero, a few score several — so this
   model is slightly overconfident in the leader, most visibly in the late
   innings of a close game.
2. RE24 constants are league averages. It knows nothing about which pitcher is
   on the mound, the bullpen, park, weather or lineup handedness. A market does.
3. No leverage for pitcher quality means the model is worst exactly where money
   is made in-play: an ace mid-shutout versus a bullpen game are identical here.

These are the reasons this model's output is recorded and compared before it is
ever traded on. It is a hypothesis about fair value, not a fair value.
"""

import os
import random

# League-average run expectancy by (bases, outs) — the standard RE24 table,
# rounded to 1990-2010s MLB averages. Bases as (first, second, third) booleans.
# Recalibrate against a season's data before trusting these to a cent.
RE24 = {
    (False, False, False): (0.48, 0.26, 0.10),
    (True,  False, False): (0.85, 0.51, 0.22),
    (False, True,  False): (1.10, 0.66, 0.32),
    (False, False, True):  (1.35, 0.95, 0.37),
    (True,  True,  False): (1.44, 0.88, 0.42),
    (True,  False, True):  (1.75, 1.14, 0.50),
    (False, True,  True):  (1.95, 1.36, 0.57),
    (True,  True,  True):  (2.29, 1.54, 0.75),
}

# ~4.5 runs per team per 9 innings.
LEAGUE_RUNS_PER_HALF = float(os.getenv("LIVE_RUNS_PER_HALF", "0.50"))
REGULATION_INNINGS = 9
MAX_EXTRA_INNINGS = 9          # bound the simulation; ties beyond are coin flips
SIMS = int(os.getenv("LIVE_SIMS", "4000"))


def run_expectancy(bases, outs: int) -> float:
    """Expected runs from here to the end of this half-inning."""
    if outs >= 3:
        return 0.0
    return RE24[tuple(bool(b) for b in bases)][max(0, min(2, int(outs)))]


def _sample_partial(rng, bases, outs: int) -> int:
    """Runs in the half-inning already in progress."""
    return rng.poisson(run_expectancy(bases, outs))


class _Rng:
    """Poisson via Knuth, so the module needs no numpy dependency."""

    def __init__(self, seed=None):
        self._r = random.Random(seed)

    def poisson(self, lam: float) -> int:
        if lam <= 0:
            return 0
        target, k, p = pow(2.718281828459045, -lam), 0, 1.0
        while True:
            p *= self._r.random()
            if p <= target:
                return k
            k += 1
            if k > 30:            # a half-inning does not score 30 runs
                return k


def win_probability(inning: int, half: str, outs: int, bases,
                    home_score: int, away_score: int,
                    sims: int = None, seed: int = None) -> float:
    """P(home team wins). `half` is 'top' or 'bottom'.

    A finished game returns a hard 1.0 or 0.0 rather than a simulated number —
    a model that reports 0.97 for a game already won is a bug waiting to size
    a position.
    """
    sims = sims or SIMS
    half = (half or "top").lower()
    inning = max(1, int(inning))
    outs = max(0, min(3, int(outs)))
    rng = _Rng(seed)

    wins = ties = 0
    for _ in range(sims):
        h, a = int(home_score), int(away_score)

        # 1. finish the half-inning in progress
        if half == "top":
            a += _sample_partial(rng, bases, outs)
            # then the home half of this same inning
            if not (inning >= REGULATION_INNINGS and h > a):
                h += rng.poisson(LEAGUE_RUNS_PER_HALF)
            first_full = inning + 1
        else:
            # home is batting; a walk-off ends it the moment they lead
            h += _sample_partial(rng, bases, outs)
            first_full = inning + 1

        # 2. remaining regulation innings, both halves
        for i in range(first_full, REGULATION_INNINGS + 1):
            a += rng.poisson(LEAGUE_RUNS_PER_HALF)
            if i >= REGULATION_INNINGS and h > a:
                break            # home does not bat when already ahead in the 9th
            h += rng.poisson(LEAGUE_RUNS_PER_HALF)

        # 3. extras
        extra = 0
        while h == a and extra < MAX_EXTRA_INNINGS:
            a += rng.poisson(LEAGUE_RUNS_PER_HALF)
            h += rng.poisson(LEAGUE_RUNS_PER_HALF)
            extra += 1

        if h > a:
            wins += 1
        elif h == a:
            ties += 1

    # An unresolved tie after the extras bound is a coin flip, not a home win.
    return (wins + 0.5 * ties) / sims


def is_final(state: dict) -> bool:
    return (state or {}).get("state") == "post"


def win_probability_for(state: dict, sims: int = None,
                        seed: int = None) -> float:
    """Convenience wrapper over a live_state game dict."""
    if is_final(state):
        return 1.0 if state["home_score"] > state["away_score"] else (
            0.0 if state["home_score"] < state["away_score"] else 0.5)
    return win_probability(
        state.get("inning", 1), state.get("half", "top"),
        state.get("outs", 0),
        (state.get("on_first"), state.get("on_second"), state.get("on_third")),
        state.get("home_score", 0), state.get("away_score", 0),
        sims=sims, seed=seed)
