"""NRFI / YRFI model — bet whether a run scores in the 1st inning.

Edge source (the only part that matters): sharp FIRST-INNING odds from the-odds-
api's `totals_1st_1_innings` (Over/Under 0.5 runs), devigged to a fair
P(run in 1st) and compared to Kalshi's `KXMLBRFI` price. YRFI = Over = buy YES;
NRFI = Under = buy NO.

Each day the model commits to ONE direction (the side with more edge vs Kalshi
across the slate) and picks up to 3 +EV games, STAGGERED so each game's first
inning resolves before the next starts. A runner then plays them as a 1-2-4
Martingale (stop at the first win). The Martingale is pure staking — it adds no
edge; the daily sharp-vs-Kalshi selection is the whole edge.

This module is the pure, unit-tested logic. nrfi_runner.py does the I/O.
"""

import os

from strategy_sports import shin_two_way
from strategy_weather import taker_fee_cents

NRFI_MIN_EDGE_CENTS = float(os.getenv("NRFI_MIN_EDGE_CENTS", "3"))
NRFI_MIN_BOOKS = int(os.getenv("NRFI_MIN_BOOKS", "2"))
NRFI_MAX_LEGS = int(os.getenv("NRFI_MAX_LEGS", "3"))
NRFI_STAGGER_MIN = float(os.getenv("NRFI_STAGGER_MIN", "45"))   # minutes
NRFI_STAKE_MULTS = [1, 2, 4]              # martingale progression (stop on win)


def fair_yrfi(over_dec: float, under_dec: float):
    """Devigged P(a run is scored in the 1st inning) from a book's two-way
    first-inning total-0.5 odds (Over = YRFI). None if unusable."""
    if not over_dec or not under_dec or over_dec <= 1 or under_dec <= 1:
        return None
    return shin_two_way(over_dec, under_dec)          # P(over) = P(YRFI)


def consensus_yrfi(book_probs: list):
    """Average P(YRFI) across the books that quoted it. None if too few."""
    ps = [p for p in book_probs if p is not None]
    if len(ps) < NRFI_MIN_BOOKS:
        return None
    return sum(ps) / len(ps)


def edge_cents(fair_yrfi_p: float, price_cents: float, is_yrfi_side: bool):
    """EV in cents of buying this side at price_cents. fair_yrfi_p = P(YRFI)."""
    p_win = fair_yrfi_p if is_yrfi_side else (1.0 - fair_yrfi_p)
    return 100.0 * p_win - price_cents - taker_fee_cents(price_cents)


def stake_mult(step: int):
    """Martingale multiplier for this 0-based step, or None past the sequence."""
    return NRFI_STAKE_MULTS[step] if 0 <= step < len(NRFI_STAKE_MULTS) else None


def _stagger(legs: list, min_gap_min: float, max_legs: int) -> list:
    """Keep games far enough apart (by commence, a unix ts) that each first
    inning finishes before the next game starts — required for a SEQUENTIAL
    Martingale. Greedy over commence-sorted legs."""
    kept = []
    for leg in sorted(legs, key=lambda x: x["commence"]):
        if not kept or leg["commence"] - kept[-1]["commence"] >= min_gap_min * 60:
            kept.append(leg)
        if len(kept) >= max_legs:
            break
    return kept


def decide(games: list, min_edge: float = None, max_legs: int = None,
           stagger_min: float = None):
    """Pick the day's direction + ordered Martingale legs.

    games: list of dicts {ticker, commence(unix), fair_yrfi, yes_ask, no_ask}
      (yes_ask = buy-YES price for YRFI; no_ask = 100 - yes_bid for NRFI).
    Returns {'direction': 'yes'|'no', 'legs': [{ticker, commence, side_price}]}
    ordered by start time, or None if neither side has a +EV, staggered slate.
    """
    min_edge = NRFI_MIN_EDGE_CENTS if min_edge is None else min_edge
    max_legs = NRFI_MAX_LEGS if max_legs is None else max_legs
    stagger_min = NRFI_STAGGER_MIN if stagger_min is None else stagger_min

    yrfi, nrfi = [], []
    for g in games:
        f = g.get("fair_yrfi")
        if f is None:
            continue
        if g.get("yes_ask") and 0 < g["yes_ask"] < 100:
            e = edge_cents(f, g["yes_ask"], True)
            if e >= min_edge:
                yrfi.append(dict(ticker=g["ticker"], commence=g["commence"],
                                 side_price=g["yes_ask"], edge=e))
        if g.get("no_ask") and 0 < g["no_ask"] < 100:
            e = edge_cents(f, g["no_ask"], False)
            if e >= min_edge:
                nrfi.append(dict(ticker=g["ticker"], commence=g["commence"],
                                 side_price=g["no_ask"], edge=e))

    yrfi_tot = sum(x["edge"] for x in yrfi)
    nrfi_tot = sum(x["edge"] for x in nrfi)
    if not yrfi and not nrfi:
        return None
    direction = "yes" if yrfi_tot >= nrfi_tot and yrfi else "no"
    pool = yrfi if direction == "yes" else nrfi
    # best edges first, then keep a staggered subset ordered by start time
    top = sorted(pool, key=lambda x: -x["edge"])[:max(max_legs * 3, max_legs)]
    legs = _stagger(top, stagger_min, max_legs)
    if not legs:
        return None
    return {"direction": direction,
            "legs": [{"ticker": l["ticker"], "commence": l["commence"],
                      "side_price": round(l["side_price"], 0)} for l in legs]}
