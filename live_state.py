"""Live MLB game state from ESPN's public scoreboard. Free, no key, no quota.

    python live_state.py        # print what is in progress right now

This is the input to win_expectancy. It exists as its own module because the
scoreboard payload is an undocumented public endpoint whose shape can change
without notice: everything fragile about it is isolated here, and every field
read has a fallback, so a missing key degrades one game rather than the run.
"""

import re
import sys

import requests

from trade_logger import get_logger, setup_logging

log = get_logger("live_state")

SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/"
              "baseball/mlb/scoreboard")

# "Bottom 2nd", "Top 10th", "Mid 3rd", "End 7th"
_HALF_RE = re.compile(r"\b(top|bottom|bot|mid|end)\b", re.I)
_INNING_RE = re.compile(r"(\d+)(?:st|nd|rd|th)")


def parse_half(detail: str) -> str:
    """'top' or 'bottom'. 'Mid' (between halves) counts as bottom-to-come;
    'End' means the inning is over, which we treat as the next top."""
    m = _HALF_RE.search(detail or "")
    word = (m.group(1).lower() if m else "top")
    if word in ("bottom", "bot", "mid"):
        return "bottom"
    return "top"


def parse_inning(detail: str, fallback: int = 1) -> int:
    m = _INNING_RE.search(detail or "")
    return int(m.group(1)) if m else fallback


def _score(competitor: dict) -> int:
    try:
        return int(float(competitor.get("score") or 0))
    except (TypeError, ValueError):
        return 0


def parse_event(event: dict):
    """One ESPN event -> a game-state dict, or None if it is unusable."""
    try:
        comp = (event.get("competitions") or [{}])[0]
        status = event.get("status") or {}
        stype = status.get("type") or {}
        teams = {c.get("homeAway"): c for c in comp.get("competitors") or []}
        home, away = teams.get("home"), teams.get("away")
        if not home or not away:
            return None
        detail = stype.get("detail") or stype.get("shortDetail") or ""
        sit = comp.get("situation") or {}
        return dict(
            id=event.get("id"),
            short=event.get("shortName", ""),
            state=stype.get("state"),            # 'pre' | 'in' | 'post'
            detail=detail,
            home_team=(home.get("team") or {}).get("displayName", ""),
            away_team=(away.get("team") or {}).get("displayName", ""),
            home_abbr=(home.get("team") or {}).get("abbreviation", ""),
            away_abbr=(away.get("team") or {}).get("abbreviation", ""),
            home_score=_score(home),
            away_score=_score(away),
            inning=parse_inning(detail, int(status.get("period") or 1)),
            half=parse_half(detail),
            outs=int(sit.get("outs") or 0),
            on_first=bool(sit.get("onFirst")),
            on_second=bool(sit.get("onSecond")),
            on_third=bool(sit.get("onThird")),
        )
    except Exception as exc:                      # one odd payload, one game
        log.warning("Unparseable scoreboard event: %s", exc)
        return None


def live_games(timeout: int = 15) -> list:
    """Games currently IN PROGRESS. Free endpoint; safe to poll often."""
    resp = requests.get(SCOREBOARD, timeout=timeout)
    resp.raise_for_status()
    out = []
    for event in (resp.json() or {}).get("events") or []:
        g = parse_event(event)
        if g and g["state"] == "in":
            out.append(g)
    return out


def main() -> int:
    setup_logging()
    games = live_games()
    log.info("%d game(s) in progress", len(games))
    for g in games:
        log.info("  %s | %s %s-%s | %s %d out, bases %s%s%s",
                 g["short"], g["detail"], g["away_score"], g["home_score"],
                 g["half"], g["outs"],
                 "1" if g["on_first"] else "-",
                 "2" if g["on_second"] else "-",
                 "3" if g["on_third"] else "-")
    return 0


if __name__ == "__main__":
    sys.exit(main())
