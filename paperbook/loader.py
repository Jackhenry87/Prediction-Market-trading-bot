"""Admin CLI: load upcoming games (with devigged-ish odds) into the paper
book, and settle finished games against real results.

    python -m paperbook.loader load     # pull upcoming games + odds
    python -m paperbook.loader settle    # settle finished games by score

Uses The Odds API (ODDS_API_KEY). Without a key, `load` seeds demo games
so the site is usable offline.
"""

import os
import sys

import requests

from . import db

SPORTS = ["baseball_mlb", "basketball_wnba", "basketball_nba",
          "americanfootball_nfl", "icehockey_nhl"]
ODDS_URL = "https://api.the-odds-api.com/v4/sports/{s}/odds/"
SCORES_URL = "https://api.the-odds-api.com/v4/sports/{s}/scores/"


def _consensus(game):
    """Median home/away decimal odds across books (h2h)."""
    home, away = [], []
    for bk in game.get("bookmakers", []):
        for m in bk.get("markets", []):
            if m.get("key") != "h2h":
                continue
            prices = {o["name"]: o["price"] for o in m.get("outcomes", [])}
            if game["home_team"] in prices:
                home.append(prices[game["home_team"]])
            if game["away_team"] in prices:
                away.append(prices[game["away_team"]])
    med = lambda xs: sorted(xs)[len(xs)//2] if xs else None
    return med(home), med(away)


def load(api_key: str) -> int:
    if not api_key:
        _seed_demo()
        return 0
    n = 0
    for sport in SPORTS:
        try:
            resp = requests.get(ODDS_URL.format(s=sport),
                                params={"apiKey": api_key, "regions": "us",
                                        "markets": "h2h", "oddsFormat": "decimal"},
                                timeout=20)
            if resp.status_code != 200:
                continue
            for g in resp.json():
                ho, ao = _consensus(g)
                if not (ho and ao):
                    continue
                db.upsert_game(dict(id=g["id"], sport=sport,
                                    home=g["home_team"], away=g["away_team"],
                                    commence_time=g.get("commence_time", ""),
                                    home_odds=ho, away_odds=ao))
                n += 1
        except Exception as exc:
            print(f"skip {sport}: {exc}")
    print(f"loaded {n} games")
    return n


def settle(api_key: str) -> int:
    if not api_key:
        print("no ODDS_API_KEY; cannot fetch scores")
        return 0
    settled = 0
    open_ids = {g["id"] for g in db.open_games()}
    for sport in SPORTS:
        try:
            resp = requests.get(SCORES_URL.format(s=sport),
                                params={"apiKey": api_key, "daysFrom": 3},
                                timeout=20)
            if resp.status_code != 200:
                continue
            for g in resp.json():
                if g["id"] not in open_ids or not g.get("completed"):
                    continue
                scores = {s["name"]: float(s["score"]) for s in g.get("scores") or []
                          if s.get("score") is not None}
                home, away = g["home_team"], g["away_team"]
                if home not in scores or away not in scores:
                    continue
                result = "home" if scores[home] > scores[away] else "away"
                settled += db.settle_game(g["id"], result)
        except Exception as exc:
            print(f"skip {sport}: {exc}")
    print(f"settled {settled} bets")
    return settled


def _seed_demo() -> None:
    demo = [
        dict(id="demo-1", sport="baseball_mlb", home="NY Yankees",
             away="Boston Red Sox", commence_time="2026-07-06T23:00:00Z",
             home_odds=1.71, away_odds=2.20),
        dict(id="demo-2", sport="basketball_wnba", home="Las Vegas Aces",
             away="NY Liberty", commence_time="2026-07-07T00:00:00Z",
             home_odds=1.55, away_odds=2.55),
    ]
    for g in demo:
        db.upsert_game(g)
    print(f"seeded {len(demo)} demo games")


if __name__ == "__main__":
    db.init_db()
    key = os.getenv("ODDS_API_KEY", "").strip()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "load"
    sys.exit(0 if (load(key) if cmd == "load" else settle(key)) >= 0 else 1)
