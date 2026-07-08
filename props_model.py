"""Player-props model: DFS pick'em lines vs the sharp sportsbook consensus,
both pulled from OddsBlaze. NO orders — this feeds the PAPER book only.

The edge, plainly: DraftKings/FanDuel prop lines are sharpened by the same
syndicates you'd be fighting, so they're hard to beat. Daily-fantasy
pick'em operators — PrizePicks, Underdog — post the SOFTEST lines in the
business because their moat is the payout structure, not line accuracy. So
we treat the DFS number as the board and the Shin-devigged sportsbook
consensus as truth, and back over/under wherever the sharp probability beats
the DFS payout by more than a margin.

Why OddsBlaze: one call returns a whole league's player props for a book
(no per-event credits), AND it carries PrizePicks — which we can't scrape
directly (Cloudflare-blocks datacenter IPs). Soft board + sharp books come
through the same schema and the same key. The join key is (player, market,
line): only lines a sharp book quotes at the SAME number are priced.

Trial note: a trial key is scoped to a subset of books (Pinnacle/FanDuel may
404). PROPS_SHARP_BOOKS lists the sharp consensus books to use; Pinnacle is
auto-upweighted if your plan includes it.

    python props_model.py     # read-only scan, prints value picks
"""

import os
import sys
import unicodedata
from pathlib import Path

import requests

from strategy_sports import shin_two_way
from trade_logger import get_logger, setup_logging

log = get_logger("props_model")

PAPER_LOG = Path(__file__).resolve().parent / "paper_trades_props.csv"

ODDSBLAZE_URL = "https://odds.oddsblaze.com/"
LEAGUE = os.getenv("PROPS_LEAGUE", "mlb")
# soft DFS board (the line we bet into) and the sharp consensus books
PROPS_SOURCE = os.getenv("PROPS_SOURCE", "prizepicks").strip().lower()
SHARP_BOOKS = [b.strip().lower() for b in os.getenv(
    "PROPS_SHARP_BOOKS", "draftkings,betmgm,caesars,fanatics").split(",")
    if b.strip()]
# trust the sharpest book more when it's in the plan; others equal
BOOK_WEIGHTS = {"pinnacle": 3.0, "circa": 2.0}

MIN_BOOKS = 2             # need >=2 sharp books' agreement before we trust fair
MIN_EDGE_PCT = float(os.getenv("PROPS_MIN_EDGE_PCT", "6"))   # ROI %, after juice
MAX_PICKS_PER_DAY = int(os.getenv("PROPS_MAX_PER_DAY", "4"))
USER_AGENT = "Mozilla/5.0 (compatible; props-model/1.0)"

# --- line-difference model ---
# The real DFS edge is a STALE LINE: PrizePicks posts a number the sharp market
# has moved off. Requiring the sharp book to quote PrizePicks' exact line threw
# all of those away (and left only efficient lines, ~-8% after DFS juice). So we
# also model each stat's sharp distribution as Normal(mean, sigma): from a
# book's line + devigged P(over) we back out the implied mean, then price
# PrizePicks' line off it. Prefer an EXACT sharp quote when one exists (no
# modeling error); interpolate only otherwise, and only within MAX_OFFSET_SIGMA
# of the mean (where the Normal approximation is least unreliable).
PROPS_INTERPOLATE = os.getenv(
    "PROPS_INTERPOLATE", "true").strip().lower() not in ("false", "0", "no")
MAX_OFFSET_SIGMA = float(os.getenv("PROPS_MAX_OFFSET_SIGMA", "2.0"))
# Per-stat spread (contract units). Deliberately on the generous side — a wider
# sigma UNDERstates the edge, so it errs toward NOT betting rather than toward a
# false positive. Tune once we have settled results.
DEFAULT_SIGMA = 1.5
STAT_SIGMA = {
    "Player Strikeouts": 2.2,
    "Player Total Bases": 1.4,
    "Player Hits": 0.8,
    "Player Home Runs": 0.6,
    "Player RBIs": 1.1,
    "Player Runs": 0.9,
    "Player Singles": 0.8,
    "Player Batter Walks": 0.8,
    "Player Hits + Runs + RBIs": 1.6,
    "Player Hits Allowed": 2.4,
    "Player Earned Runs": 1.9,
    "Player Walks Allowed": 1.4,
    "Player Outs": 3.5,
}


def norm_name(name: str) -> str:
    """Normalize a player name for cross-book matching: strip accents,
    punctuation and suffixes, collapse whitespace, lowercase."""
    n = unicodedata.normalize("NFKD", name or "")
    n = "".join(ch for ch in n if not unicodedata.combining(ch)).lower()
    out = [ch if ch.isalnum() or ch.isspace() else " " for ch in n]
    toks = [t for t in "".join(out).split()
            if t not in ("jr", "sr", "ii", "iii", "iv")]
    return " ".join(toks)


def american_to_decimal(price):
    """American odds -> decimal (incl. stake). None if unusable."""
    try:
        a = float(price)
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    return 1.0 + (a / 100.0 if a > 0 else 100.0 / -a)


# ---------- OddsBlaze parsing ----------
def parse_book(payload: dict) -> dict:
    """One OddsBlaze book payload -> two-way player-prop quotes keyed by
    (player_norm, market, line) -> {'over': decimal, 'under': decimal,
    'player': display}. Only clean Over/Under markets with a line are kept."""
    quotes = {}
    for ev in payload.get("events", []):
        for o in ev.get("odds", []):
            market = o.get("market") or ""
            if not market.startswith("Player"):
                continue
            sel = o.get("selection") or {}
            side = (sel.get("side") or "").lower()
            line = sel.get("line")
            who = sel.get("name") or ""
            if side not in ("over", "under") or line is None or not who:
                continue
            dec = american_to_decimal(o.get("price"))
            if dec is None:
                continue
            try:
                line = float(line)
            except (TypeError, ValueError):
                continue
            key = (norm_name(who), market, line)
            q = quotes.setdefault(key, {"player": who})
            q[side] = dec
    return quotes


def board_lines(payload: dict) -> list:
    """Soft DFS board -> prop lines we can bet: needs both sides quoted."""
    lines = []
    for (who_norm, market, line), q in parse_book(payload).items():
        if "over" not in q or "under" not in q:
            continue
        lines.append(dict(
            player=q["player"], player_norm=who_norm,
            display_stat=market.replace("Player ", ""), market=market,
            line=line, over_decimal=q["over"], under_decimal=q["under"],
            title=f"{q['player']} {market}", source=PROPS_SOURCE))
    return lines


def sharp_consensus(book_payloads: dict) -> dict:
    """Weighted fair P(over) per (player_norm, market, line) across the sharp
    books. Each book's two-way price at a line is Shin-devigged, then averaged
    with the configured weights. {key: {'p': prob, 'books': n}}."""
    acc = {}
    for book, payload in book_payloads.items():
        w = BOOK_WEIGHTS.get(book, 1.0)
        for key, q in parse_book(payload).items():
            if "over" not in q or "under" not in q:
                continue
            p_over = shin_two_way(q["over"], q["under"])
            a = acc.setdefault(key, {"wsum": 0.0, "wtot": 0.0, "books": 0})
            a["wsum"] += w * p_over
            a["wtot"] += w
            a["books"] += 1
    return {k: {"p": a["wsum"] / a["wtot"], "books": a["books"]}
            for k, a in acc.items() if a["wtot"] > 0}


def stat_sigma(market: str) -> float:
    return STAT_SIGMA.get(market, DEFAULT_SIGMA)


def sharp_means(book_payloads: dict) -> dict:
    """Weighted implied MEAN per (player_norm, market) across the sharp books.
    From each book's two-way line: P(over line)=p implies, under
    Normal(mean, sigma), mean = line + sigma·Φ⁻¹(p). Averaged (Pinnacle-weighted)
    over every line each book quotes. {key: {'mean', 'sigma', 'books': n}}."""
    from statistics import NormalDist
    acc = {}
    for book, payload in book_payloads.items():
        w = BOOK_WEIGHTS.get(book, 1.0)
        for (who, market, line), q in parse_book(payload).items():
            if "over" not in q or "under" not in q:
                continue
            p_over = min(max(shin_two_way(q["over"], q["under"]), 1e-4),
                         1 - 1e-4)
            sigma = stat_sigma(market)
            mean = line + sigma * NormalDist().inv_cdf(p_over)
            a = acc.setdefault((who, market),
                               {"wsum": 0.0, "wtot": 0.0, "books": set()})
            a["wsum"] += w * mean
            a["wtot"] += w
            a["books"].add(book)
    return {k: {"mean": a["wsum"] / a["wtot"], "sigma": stat_sigma(k[1]),
                "books": len(a["books"])}
            for k, a in acc.items() if a["wtot"] > 0}


def over_prob(mean: float, line: float, sigma: float) -> float:
    """P(stat > line) under Normal(mean, sigma)."""
    from statistics import NormalDist
    return 1.0 - NormalDist(mean, sigma).cdf(line)


# ---------- live fetch ----------
def fetch_book(key: str, sportsbook: str, league: str = None) -> dict:
    resp = requests.get(ODDSBLAZE_URL, timeout=25,
                        headers={"User-Agent": USER_AGENT},
                        params={"sportsbook": sportsbook,
                                "league": league or LEAGUE, "key": key})
    if resp.status_code == 404:
        log.warning("OddsBlaze: '%s' not in your plan (404) — skipping.",
                    sportsbook)
        return {}
    resp.raise_for_status()
    return resp.json()


# ---------- value ----------
def sharp_prob_at(d: dict, fair: dict, means: dict):
    """Sharp P(over) for one DFS line. Prefer an EXACT sharp quote at the same
    number (no modeling error); else interpolate off the implied mean, but only
    within MAX_OFFSET_SIGMA of it. Returns (p_over, books, priced_by) or None."""
    s = fair.get((d["player_norm"], d["market"], d["line"]))
    if s and s["books"] >= MIN_BOOKS:
        return s["p"], s["books"], "exact"
    if not (PROPS_INTERPOLATE and means):
        return None
    m = means.get((d["player_norm"], d["market"]))
    if not m or m["books"] < MIN_BOOKS:
        return None
    if abs(d["line"] - m["mean"]) > MAX_OFFSET_SIGMA * m["sigma"]:
        return None                      # too far out to trust the Normal
    return over_prob(m["mean"], d["line"], m["sigma"]), m["books"], "model"


def find_value(dfs_lines: list, fair: dict, means: dict = None,
               min_edge_pct: float = None) -> list:
    """Signals where the sharp fair probability beats the DFS payout. For each
    DFS line we get the sharp P(over) at that number — an exact sharp quote if
    one exists, otherwise interpolated off the implied mean (the stale-line
    case) — and take the side whose EV (fair_prob * dfs_decimal - 1) clears the
    margin. EV is ROI per $1; a 6% edge means +6c expected per $1, after juice."""
    min_edge = (min_edge_pct if min_edge_pct is not None
                else MIN_EDGE_PCT) / 100.0
    out = []
    for d in dfs_lines:
        got = sharp_prob_at(d, fair, means)
        if not got:
            continue
        p_over, books, priced_by = got
        ev_over = p_over * d["over_decimal"] - 1.0
        ev_under = (1.0 - p_over) * d["under_decimal"] - 1.0
        if ev_over >= ev_under:
            side, ev, dec, prob = "over", ev_over, d["over_decimal"], p_over
        else:
            side, ev, dec, prob = "under", ev_under, d["under_decimal"], 1 - p_over
        if ev < min_edge:
            continue
        out.append(dict(
            player=d["player"], display_stat=d["display_stat"],
            market=d["market"], line=d["line"], side=side,
            decimal=dec, sharp_prob=prob, edge_pct=ev * 100.0,
            books=books, title=d["title"], source=d["source"],
            priced_by=priced_by))
    out.sort(key=lambda x: -x["edge_pct"])
    return out


def scan(key: str, league: str = None) -> list:
    """Full read-only scan: DFS board + sharp books from OddsBlaze -> value
    picks, best-edge first, capped at MAX_PICKS_PER_DAY."""
    league = league or LEAGUE
    dfs = board_lines(fetch_book(key, PROPS_SOURCE, league))
    log.info("%s board: %d %s player-prop lines", PROPS_SOURCE, len(dfs),
             league.upper())
    if not dfs:
        return []
    payloads = {}
    for book in SHARP_BOOKS:
        p = fetch_book(key, book, league)
        if p:
            payloads[book] = p
    fair = sharp_consensus(payloads)
    means = sharp_means(payloads) if PROPS_INTERPOLATE else {}
    log.info("Sharp consensus from %d book(s): %d exact-line points, %d "
             "(player, market) means", len(payloads), len(fair), len(means))
    picks = find_value(dfs, fair, means)
    n_model = sum(1 for p in picks if p.get("priced_by") == "model")
    log.info("%d value pick(s) (%d from stale-line model)", len(picks), n_model)
    return picks[:MAX_PICKS_PER_DAY]


def main() -> int:
    setup_logging()
    key = os.getenv("ODDSBLAZE_KEY", "").strip()
    if not key:
        log.error("ODDSBLAZE_KEY not set. Add your OddsBlaze key to .env / "
                  "repo secrets.")
        return 1
    picks = scan(key)
    for p in picks:
        log.info("VALUE: %s %s %.1f %s @ %.2f | sharp %.0f%% | +%.1f%% edge "
                 "(%d books)", p["player"], p["display_stat"], p["line"],
                 p["side"].upper(), p["decimal"], 100 * p["sharp_prob"],
                 p["edge_pct"], p["books"])
    log.info("%s value pick(s). NO ORDERS — paper only.", len(picks) or "No")
    return 0


if __name__ == "__main__":
    sys.exit(main())
