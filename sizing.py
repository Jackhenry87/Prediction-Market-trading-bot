"""Position sizing: how many contracts, per signal, from edge and confidence.

Not a flat percentage. A flat percentage bets the same on a 0.5c edge and a
4c edge, which either overbets the weak signals or underbets the strong ones.
Size follows the edge.

THE FORMULA
-----------
For a binary contract bought at price `c` cents when your probability estimate
is `p`, the Kelly-optimal fraction of bankroll is

    f* = (p - c/100) / (1 - c/100)    ==    edge_cents / (100 - c)

i.e. **edge divided by what you stand to win**. Buying a 89c favourite risks
89c to win 11c, so even a 2c edge is a large Kelly fraction (2/11 = 18%) —
which is exactly why full Kelly is not usable here.

WHY QUARTER KELLY
-----------------
Full Kelly has an expected peak-to-trough drawdown near 50% of bankroll even
when the edge is real and known. Our edge is neither: `p` is an assumption
under test. Variance scales with the square of the fraction, so quarter Kelly
cuts variance by ~94% versus full while keeping roughly 44% of the growth
rate, and caps expected drawdown near 12%. It is the standard response to an
uncertain edge, a small bankroll, and correlated positions — this strategy has
all three.

CONFIDENCE-WEIGHTED EDGE
------------------------
The edge has two parts and they are NOT equally trustworthy:

  * MECHANICAL (mid - entry): we rest below the market's own mid, so if we
    fill, we bought below fair value by construction. Certain, given a fill.
  * ASSUMED (the favourite-longshot correction): the market's mid is itself
    biased low on favourites. This is the research claim under test, so it is
    multiplied by BIAS_CONFIDENCE — start it low and raise it only when the
    CLV scoreboard earns it.

A signal whose edge is all assumption gets sized smaller than one whose edge
is mostly mechanical, at the same headline edge. That is the point.

NO HAIL MARYS
-------------
Sizing is floored, not rounded up: if quarter Kelly does not pay for a single
contract, the answer is zero contracts, not one. Rounding a sub-minimum stake
up to one contract is how a disciplined model quietly becomes a flat-stake
punter on its weakest signals.
"""

import math
import os

# Fraction of full Kelly. 0.25 = quarter Kelly.
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))

# How much of the assumed favourite-bias term to believe, 0..1. Starts at half
# because the claim is unvalidated on this account. Raise it only against a
# positive CLV scoreboard.
BIAS_CONFIDENCE = float(os.getenv("BIAS_CONFIDENCE", "0.5"))

# Hard ceiling per position regardless of what Kelly says — a backstop against
# a bad probability estimate producing an enormous "optimal" bet.
MAX_BANKROLL_PCT = float(os.getenv("MAX_BANKROLL_PCT", "5"))

# Never bet an edge this thin; it is inside the noise of our own estimate.
MIN_EDGE_CENTS = float(os.getenv("SIZING_MIN_EDGE_CENTS", "0.75"))

# Absolute contract ceiling per order.
MAX_CONTRACTS = int(os.getenv("MAX_CONTRACTS_PER_ORDER", "50"))


def effective_edge_cents(mid: float, entry: float, bias_cents: float,
                         bias_confidence: float = None) -> float:
    """Edge in cents versus fair value, discounting the unproven part.

    mid   - the market's own midpoint, our best available fair value
    entry - the price we would actually pay (a resting maker bid)
    bias  - the assumed favourite-longshot correction, in cents
    """
    conf = BIAS_CONFIDENCE if bias_confidence is None else bias_confidence
    mechanical = mid - entry          # certain given a fill
    assumed = bias_cents * conf       # the research claim, discounted
    return mechanical + assumed


def kelly_fraction(entry_cents: float, edge_cents: float) -> float:
    """f* = edge / (100 - entry). Zero when there is no edge or no upside."""
    upside = 100.0 - entry_cents
    if upside <= 0 or edge_cents <= 0:
        return 0.0
    return edge_cents / upside


def size_position(bankroll_usd: float, entry_cents: float, edge_cents: float,
                  kelly_fraction_used: float = None,
                  max_bankroll_pct: float = None) -> dict:
    """Contracts to buy, plus the reasoning behind the number.

    Returns a dict so the decision is auditable in the ledger and the logs —
    'why was this 3 contracts and that one 1?' should never need a debugger.
    """
    frac_k = KELLY_FRACTION if kelly_fraction_used is None else kelly_fraction_used
    cap_pct = MAX_BANKROLL_PCT if max_bankroll_pct is None else max_bankroll_pct

    out = dict(contracts=0, edge_cents=round(edge_cents, 2),
               kelly_full=0.0, kelly_used=0.0, stake_usd=0.0, reason="")

    if edge_cents < MIN_EDGE_CENTS:
        out["reason"] = (f"edge {edge_cents:.2f}c below the "
                         f"{MIN_EDGE_CENTS:.2f}c floor")
        return out
    if not 0 < entry_cents < 100:
        out["reason"] = f"invalid entry price {entry_cents}"
        return out
    if bankroll_usd <= 0:
        out["reason"] = "no bankroll"
        return out

    full = kelly_fraction(entry_cents, edge_cents)
    used = min(full * frac_k, cap_pct / 100.0)
    stake = bankroll_usd * used
    price_usd = entry_cents / 100.0
    contracts = int(math.floor(stake / price_usd))

    out.update(kelly_full=round(full, 4), kelly_used=round(used, 4),
               stake_usd=round(stake, 2))

    if contracts < 1:
        out["reason"] = (f"quarter-Kelly stake ${stake:.2f} < one contract "
                         f"(${price_usd:.2f}) — no bet")
        return out

    contracts = min(contracts, MAX_CONTRACTS)
    out["contracts"] = contracts
    out["reason"] = (f"edge {edge_cents:.2f}c / upside {100 - entry_cents:.0f}c "
                     f"= {full:.1%} full Kelly, x{frac_k:g} -> {used:.2%} of "
                     f"${bankroll_usd:.2f} = ${stake:.2f} = {contracts} @ "
                     f"{entry_cents:.0f}c")
    return out
