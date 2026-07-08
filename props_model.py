"""Player-props model: DFS pick'em lines (Underdog) vs the sharp sportsbook
consensus (The Odds API). NO orders — this feeds the PAPER book only.

The edge, plainly: DraftKings/FanDuel prop lines are sharpened by the same
syndicates you'd be fighting, so they're hard to beat. Daily-fantasy
pick'em operators — Underdog, PrizePicks — post the SOFTEST lines in the
business because their moat is the payout structure, not line accuracy.
So we treat the DFS number as the board and the Pinnacle-weighted, Shin-
devigged sportsbook consensus as truth, and back "higher"/"lower" wherever
the sharp probability beats the DFS payout by more than a margin.

Source note: Underdog's public over_under_lines endpoint returns the whole
board as JSON and is reachable from cloud IPs. PrizePicks' projections
endpoint is Cloudflare-blocked from datacenter IPs (403), so it needs a
residential proxy — the fetcher is structured to slot it in behind
PROPS_SOURCE=prizepicks + PROPS_PROXY once you have one.

The join key between the two worlds is (player, stat, line): no game
matching, no team-id translation. Only lines where a sharp book quotes the
SAME number are priced — an exact, conservative comparison.

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

# DFS board source. Underdog works from the cloud; PrizePicks is blocked.
PROPS_SOURCE = os.getenv("PROPS_SOURCE", "underdog").strip().lower()
UNDERDOG_URL = "https://api.underdogfantasy.com/beta/v5/over_under_lines"
PRIZEPICKS_URL = "https://api.prizepicks.com/projections"
PROPS_PROXY = os.getenv("PROPS_PROXY", "").strip()   # residential proxy for PP

# The Odds API MLB player-prop markets (the props add-on — premium credits).
ODDS_EVENTS_URL = "https://api.the-odds-api.com/v4/sports/{sport}/events"
ODDS_EVENT_ODDS_URL = ("https://api.the-odds-api.com/v4/sports/{sport}/events/"
                       "{event_id}/odds")
SPORT_KEY = "baseball_mlb"

# Underdog display_stat -> Odds API market key. Only mapped stats are priced;
# anything else (esports, golf, combos we don't model) is skipped.
STAT_MAP = {
    "home runs": "batter_home_runs",
    "hits": "batter_hits",
    "total bases": "batter_total_bases",
    "rbis": "batter_rbis",
    "runs": "batter_runs_scored",
    "runs scored": "batter_runs_scored",
    "stolen bases": "batter_stolen_bases",
    "batter walks": "batter_walks",
    "walks": "batter_walks",
    "singles": "batter_singles",
    "doubles": "batter_doubles",
    "triples": "batter_triples",
    "batter strikeouts": "batter_strikeouts",
    "hits + runs + rbis": "batter_hits_runs_rbis",
    "strikeouts": "pitcher_strikeouts",
    "pitcher strikeouts": "pitcher_strikeouts",
    "hits allowed": "pitcher_hits_allowed",
    "walks allowed": "pitcher_walks",
    "earned runs allowed": "pitcher_earned_runs",
    "outs": "pitcher_outs",
    "pitching outs": "pitcher_outs",
}

PINNACLE_WEIGHT = 3.0     # trust the sharpest book ~3x a soft book
MIN_BOOKS = 2             # need >=2 books' agreement before we trust the fair
MIN_EDGE_PCT = float(os.getenv("PROPS_MIN_EDGE_PCT", "6"))   # ROI %, after juice
MAX_PICKS_PER_DAY = int(os.getenv("PROPS_MAX_PER_DAY", "4"))
USER_AGENT = "Mozilla/5.0 (compatible; props-model/1.0)"


def norm_name(name: str) -> str:
    """Normalize a player name for cross-source matching: strip accents,
    punctuation and suffixes, collapse whitespace, lowercase."""
    n = unicodedata.normalize("NFKD", name or "")
    n = "".join(ch for ch in n if not unicodedata.combining(ch)).lower()
    out = []
    for ch in n:
        out.append(ch if ch.isalnum() or ch.isspace() else " ")
    toks = [t for t in "".join(out).split() if t not in ("jr", "sr", "ii", "iii", "iv")]
    return " ".join(toks)


# ---------- DFS board (Underdog) ----------
def _decimal(opt: dict):
    try:
        d = float(opt.get("decimal_price"))
        return d if d > 1 else None
    except (TypeError, ValueError):
        return None


def parse_underdog(payload: dict, sport: str = "MLB") -> list:
    """Normalize Underdog's over_under_lines payload into DFS prop lines for
    one sport. Each: player, display_stat, market key, line, over/under decimal
    prices. Pure — takes the already-fetched JSON so it's unit-testable."""
    players = {p["id"]: p for p in payload.get("players", [])}
    appear = {a["id"]: a for a in payload.get("appearances", [])}
    want = sport.upper()
    lines = []
    for ln in payload.get("over_under_lines", []):
        if ln.get("status") != "active":
            continue
        ou = ln.get("over_under") or {}
        if ou.get("category") != "player_prop":
            continue
        stat = ou.get("appearance_stat") or {}
        appearance = appear.get(stat.get("appearance_id"))
        if not appearance:
            continue
        player = players.get(appearance.get("player_id"))
        if not player or (player.get("sport_id") or "").upper() != want:
            continue
        market = STAT_MAP.get((stat.get("display_stat") or "").strip().lower())
        if not market:
            continue
        try:
            line = float(ln.get("stat_value"))
        except (TypeError, ValueError):
            continue
        over = under = None
        for opt in ln.get("options", []):
            if opt.get("status") != "active":
                continue
            if opt.get("choice") == "higher":
                over = _decimal(opt)
            elif opt.get("choice") == "lower":
                under = _decimal(opt)
        if over is None or under is None:
            continue
        name = f"{player.get('first_name', '')} {player.get('last_name', '')}".strip()
        lines.append(dict(
            player=name, player_norm=norm_name(name),
            display_stat=stat.get("display_stat"), market=market, line=line,
            over_decimal=over, under_decimal=under,
            title=ou.get("title", ""), source="underdog"))
    return lines


def fetch_dfs_lines(sport: str = "MLB") -> list:
    """Live DFS board. Underdog by default (works from the cloud); PrizePicks
    only if PROPS_SOURCE=prizepicks AND a residential PROPS_PROXY is set (it's
    Cloudflare-blocked from datacenter IPs otherwise)."""
    if PROPS_SOURCE == "prizepicks":
        if not PROPS_PROXY:
            log.error("PrizePicks needs a residential PROPS_PROXY (datacenter "
                      "IPs get 403). Falling back to Underdog.")
        else:
            # structured for when a proxy is available; PrizePicks' schema
            # differs, so parsing is left for when the source is reachable.
            log.warning("PrizePicks parsing not wired yet; using Underdog.")
    resp = requests.get(UNDERDOG_URL, timeout=25,
                        headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return parse_underdog(resp.json(), sport)


# ---------- sharp consensus (The Odds API) ----------
def sharp_over_probs(event_odds: dict) -> dict:
    """From one event's player-prop odds, build a Pinnacle-weighted fair
    P(over) for every (player_norm, market, line) the books quote two-way.
    Returns {(player_norm, market, line): {'p': prob, 'books': n}}."""
    acc = {}
    for book in event_odds.get("bookmakers", []):
        w = PINNACLE_WEIGHT if book.get("key") == "pinnacle" else 1.0
        for market in book.get("markets", []):
            mkey = market.get("key")
            # group this book's outcomes by (player, line): need over AND under
            pair = {}
            for o in market.get("outcomes", []):
                who = norm_name(o.get("description") or "")
                pt = o.get("point")
                side = (o.get("name") or "").lower()
                price = o.get("price")
                if not who or pt is None or side not in ("over", "under"):
                    continue
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    continue
                if price <= 1:
                    continue
                pair.setdefault((who, float(pt)), {})[side] = price
            for (who, pt), sides in pair.items():
                if "over" not in sides or "under" not in sides:
                    continue
                p_over = shin_two_way(sides["over"], sides["under"])
                key = (who, mkey, pt)
                a = acc.setdefault(key, {"wsum": 0.0, "wtot": 0.0, "books": 0})
                a["wsum"] += w * p_over
                a["wtot"] += w
                a["books"] += 1
    return {k: {"p": a["wsum"] / a["wtot"], "books": a["books"]}
            for k, a in acc.items() if a["wtot"] > 0}


def fetch_sharp_props(api_key: str, markets: set, sport: str = SPORT_KEY,
                      regions: str = "us") -> dict:
    """Sharp fair P(over) for every (player_norm, market, line) across all of
    today's events, for the given Odds API market keys. One events list (cheap)
    + one per-event odds call (premium credits) per event."""
    if not markets:
        return {}
    ev = requests.get(ODDS_EVENTS_URL.format(sport=sport),
                      params={"apiKey": api_key}, timeout=20)
    ev.raise_for_status()
    fair = {}
    mkt_param = ",".join(sorted(markets))
    for event in ev.json():
        eid = event.get("id")
        if not eid:
            continue
        try:
            r = requests.get(
                ODDS_EVENT_ODDS_URL.format(sport=sport, event_id=eid),
                params={"apiKey": api_key, "regions": regions,
                        "markets": mkt_param, "oddsFormat": "decimal"},
                timeout=20)
            r.raise_for_status()
        except Exception as exc:
            log.warning("Props odds fetch failed for event %s: %s", eid, exc)
            continue
        for key, val in sharp_over_probs(r.json()).items():
            # keep the observation with the most book agreement
            if key not in fair or val["books"] > fair[key]["books"]:
                fair[key] = val
    return fair


# ---------- value ----------
def find_value(dfs_lines: list, fair: dict, min_edge_pct: float = None) -> list:
    """Signals where the sharp fair probability beats the DFS payout. For each
    DFS line we look up the sharp P(over) at the SAME number and take the side
    whose EV (fair_prob * dfs_decimal - 1) clears the margin. EV is ROI per $1;
    a 6% edge means +6c expected per $1 staked, after the DFS juice."""
    min_edge = (min_edge_pct if min_edge_pct is not None else MIN_EDGE_PCT) / 100.0
    out = []
    for d in dfs_lines:
        s = fair.get((d["player_norm"], d["market"], d["line"]))
        if not s or s["books"] < MIN_BOOKS:
            continue
        p_over = s["p"]
        ev_over = p_over * d["over_decimal"] - 1.0
        ev_under = (1.0 - p_over) * d["under_decimal"] - 1.0
        if ev_over >= ev_under:
            side, ev, decimal, prob = "over", ev_over, d["over_decimal"], p_over
        else:
            side, ev, decimal, prob = "under", ev_under, d["under_decimal"], 1 - p_over
        if ev < min_edge:
            continue
        out.append(dict(
            player=d["player"], display_stat=d["display_stat"],
            market=d["market"], line=d["line"], side=side,
            decimal=decimal, sharp_prob=prob, edge_pct=ev * 100.0,
            books=s["books"], title=d["title"], source=d["source"]))
    out.sort(key=lambda x: -x["edge_pct"])
    return out


def scan(api_key: str, sport: str = "MLB") -> list:
    """Full read-only scan: DFS board -> needed sharp markets -> value picks,
    best-edge first, capped at MAX_PICKS_PER_DAY."""
    dfs = fetch_dfs_lines(sport)
    log.info("DFS board: %d %s player-prop lines", len(dfs), sport)
    if not dfs:
        return []
    markets = {d["market"] for d in dfs}
    fair = fetch_sharp_props(api_key, markets)
    log.info("Sharp consensus: %d priced (player, market, line) points", len(fair))
    picks = find_value(dfs, fair)
    return picks[:MAX_PICKS_PER_DAY]


def main() -> int:
    setup_logging()
    api_key = os.getenv("ODDS_API_KEY", "").strip()
    if not api_key:
        log.error("ODDS_API_KEY not set (needs the player-props add-on).")
        return 1
    picks = scan(api_key)
    for p in picks:
        log.info("VALUE: %s %s %.1f %s @ %.2f | sharp %.0f%% | +%.1f%% edge (%d books) [%s]",
                 p["player"], p["display_stat"], p["line"], p["side"].upper(),
                 p["decimal"], 100 * p["sharp_prob"], p["edge_pct"], p["books"],
                 p["source"])
    log.info("%s value pick(s). NO ORDERS — paper only.", len(picks) or "No")
    return 0


if __name__ == "__main__":
    sys.exit(main())
