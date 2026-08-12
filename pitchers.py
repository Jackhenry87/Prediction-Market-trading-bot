"""Probable starting pitchers and what their matchup implies. Free, no key.

WHY THIS IS A CHECK AND NOT A PRICE
-----------------------------------
Be precise about what pitcher data can and cannot do for the pregame model.

Our pregame fair value is a devigged consensus of the sportsbooks. Those books
ALREADY know who is starting — it is the single largest input they price. So
adding ERA to that estimate cannot make it sharper; it would be double-counting
the same information and calling the result an edge.

What pitcher data CAN do is tell us when our number is not about this game at
all. Both bad trades this month were mismatches that produced a large, confident
edge:

  2026-08-07  a WNBA championship future priced off one game's win probability
  2026-08-12  "Los Angeles A" priced off the DODGERS, because the Angels game
              had started and the fallback matched the only other LA team

In the second, the market had the Angels at 43-44c and our model said 67%. The
starters were George Klassen (7.27 ERA) for the Angels against Cal Quantrill
(3.56) for Texas. A model claiming the team with the far worse starter is a
two-thirds favourite is not finding an edge; it is describing a different game.
That contradiction is visible for free, in a payload we already fetch.

So this module answers one question — DOES THE PITCHING MATCHUP SUPPORT THE
SIDE WE ARE BACKING? — and the answer is used to corroborate a large claimed
edge, never to price one.

HONEST LIMITS
-------------
ERA is a blunt instrument: it is noisy over a partial season, it credits the
defence behind the pitcher, and it says nothing about the bullpen that will
throw the other half of the game. It is used here only for its SIGN and only
when the gap is wide, which is the one thing a crude number is reliable for.
It is not a substitute for the books' pitcher pricing and is not treated as one.
"""

import os
import sys

import requests

from trade_logger import get_logger, setup_logging

log = get_logger("pitchers")

SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/"
              "baseball/mlb/scoreboard")

# How much worse a starter's ERA must be before the matchup is called lopsided.
# Deliberately wide: ERA is noisy, so only a gap this large is worth acting on.
ERA_GAP = float(os.getenv("PITCHER_ERA_GAP", "1.50"))
# How far apart the two sources' start times may be and still be one game.
# Wide enough for a timezone or a rounded listing, far tighter than the ~24h
# between consecutive games of a series.
SAME_GAME_HOURS = float(os.getenv("PITCHER_SAME_GAME_H", "6"))


def _era(competitor: dict):
    """Season ERA of this side's probable starter, or None."""
    for prob in competitor.get("probables") or []:
        for stat in prob.get("statistics") or []:
            if (stat.get("abbreviation") or stat.get("name")) == "ERA":
                try:
                    return float(stat.get("displayValue"))
                except (TypeError, ValueError):
                    return None
    return None


def _name(competitor: dict):
    for prob in competitor.get("probables") or []:
        a = prob.get("athlete") or {}
        if a.get("displayName"):
            return a["displayName"]
    return None


def _slate(dates: str = None, timeout: int = 15) -> list:
    params = {"dates": dates} if dates else None
    resp = requests.get(SCOREBOARD, params=params, timeout=timeout)
    resp.raise_for_status()
    out = []
    for event in (resp.json() or {}).get("events") or []:
        try:
            comp = (event.get("competitions") or [{}])[0]
            sides = {c.get("homeAway"): c for c in comp.get("competitors") or []}
            home, away = sides.get("home"), sides.get("away")
            if not home or not away:
                continue
            out.append(dict(
                home_team=(home.get("team") or {}).get("displayName", ""),
                away_team=(away.get("team") or {}).get("displayName", ""),
                commence=event.get("date"),
                home_pitcher=_name(home), away_pitcher=_name(away),
                home_era=_era(home), away_era=_era(away),
            ))
        except Exception as exc:
            log.warning("Unparseable scoreboard event: %s", exc)
    return out


def matchups(dates: str = None, days: int = 2, timeout: int = 15) -> list:
    """Probable starters for the next `days` slates.

    TODAY IS NOT ENOUGH. The model trades games up to 36 hours out, and a
    single scoreboard call returns one day — so a game tomorrow night had no
    matchup, found nothing, and failed open with no corroboration at all. Two
    calls cover the whole window, and both are free.
    """
    if dates:
        return _slate(dates, timeout)
    from datetime import datetime, timedelta, timezone
    seen, out = set(), []
    today = datetime.now(timezone.utc).date()
    for offset in range(max(1, days)):
        try:
            rows = _slate((today + timedelta(days=offset)).strftime("%Y%m%d"),
                          timeout)
        except Exception as exc:
            log.warning("Scoreboard for offset %d unavailable: %s", offset, exc)
            continue
        for m in rows:
            key = (m["home_team"], m["away_team"], m.get("commence"))
            if key not in seen:
                seen.add(key)
                out.append(m)
    return out


def favours(matchup: dict):
    """'home', 'away', or None — which side the starting pitching favours.

    None means "no opinion", which is the honest answer whenever a starter is
    unannounced or the gap is inside the noise. A None must never be read as
    disagreement; see supports_side.
    """
    if not matchup:
        return None
    h, a = matchup.get("home_era"), matchup.get("away_era")
    if h is None or a is None:
        return None
    if a - h >= ERA_GAP:          # away starter is the worse one
        return "home"
    if h - a >= ERA_GAP:
        return "away"
    return None


def supports_side(matchup: dict, side: str) -> bool:
    """Does the pitching matchup CONTRADICT backing `side`?  True = no veto.

    Fails OPEN on missing data on purpose. A veto is only justified by positive
    evidence that the matchup points the other way; refusing every game with an
    unannounced starter would silently disable the model on the mornings when
    lineups are not yet posted, which is a bigger failure than the one it
    prevents.
    """
    lean = favours(matchup)
    return lean is None or lean == side


def _hours_apart(a, b):
    from datetime import datetime, timezone
    def parse(v):
        if not v:
            return None
        try:
            t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return None
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    ta, tb = parse(a), parse(b)
    if ta is None or tb is None:
        return None
    return abs((ta - tb).total_seconds()) / 3600.0


def find(matchups_list: list, home_team: str, away_team: str,
         matches, commence=None) -> dict:
    """The matchup for THIS game, by teams AND start time.

    `matches(label, team)` is injected rather than imported so this module
    stays independent of the sports model's naming rules — and so the matcher
    that crossed the Angels with the Dodgers cannot be re-implemented here in a
    second, subtly different way.

    THE START TIME IS NOT OPTIONAL when a series is playing. Texas and the
    Angels played three consecutive nights with a different starter each time;
    matching on team names alone returned whichever appeared first in the
    slate, so the veto would have corroborated a bet against the wrong game's
    pitchers — the exact failure it was built to catch. A caller that passes no
    commence time gets team-only matching and must accept that risk.
    """
    best = None
    for m in matchups_list or []:
        if not (matches(home_team, m["home_team"])
                and matches(away_team, m["away_team"])):
            continue
        if commence is None:
            return m
        gap = _hours_apart(commence, m.get("commence"))
        if gap is None or gap > SAME_GAME_HOURS:
            continue
        if best is None or gap < best[0]:
            best = (gap, m)
    return best[1] if best else None


def main() -> int:
    setup_logging()
    rows = matchups(dates=(sys.argv[1] if len(sys.argv) > 1 else None))
    log.info("%d scheduled game(s)", len(rows))
    for m in rows:
        lean = favours(m)
        log.info("  %-24s %-18s (%s) vs %-18s (%s) -> %s",
                 f"{m['away_team']} @ {m['home_team']}",
                 m["away_pitcher"] or "TBA",
                 "?" if m["away_era"] is None else f"{m['away_era']:.2f}",
                 m["home_pitcher"] or "TBA",
                 "?" if m["home_era"] is None else f"{m['home_era']:.2f}",
                 lean or "no lean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
