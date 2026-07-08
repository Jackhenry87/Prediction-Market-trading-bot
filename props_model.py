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
def find_value(dfs_lines: list, fair: dict, min_edge_pct: float = None) -> list:
    """Signals where the sharp fair probability beats the DFS payout. For each
    DFS line we look up the sharp P(over) at the SAME number and take the side
    whose EV (fair_prob * dfs_decimal - 1) clears the margin. EV is ROI per $1;
    a 6% edge means +6c expected per $1 staked, after the DFS juice."""
    min_edge = (min_edge_pct if min_edge_pct is not None
                else MIN_EDGE_PCT) / 100.0
    out = []
    for d in dfs_lines:
        s = fair.get((d["player_norm"], d["market"], d["line"]))
        if not s or s["books"] < MIN_BOOKS:
            continue
        p_over = s["p"]
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
            books=s["books"], title=d["title"], source=d["source"]))
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
    log.info("Sharp consensus from %d book(s): %d priced (player, market, line)",
             len(payloads), len(fair))
    return find_value(dfs, fair)[:MAX_PICKS_PER_DAY]


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
