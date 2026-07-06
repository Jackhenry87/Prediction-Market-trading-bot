"""World Cup probe round 2: does Kalshi have per-GAME World Cup markets?

Round 1: Polymarket taxonomy is clean (match-winner + team-to-advance are
the sharp magnets), but KXMENWORLDCUP is tournament-winner futures only
and the guessed game-series tickers were empty. This round pages ALL open
Kalshi events and inventories series tickers whose title/ticker smells
like soccer, so the mapper targets a series that actually exists (or we
conclude honestly that there is no venue). Read-only, no keys.
"""

import json
import re
from collections import Counter
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent / "probe_results"
HEADERS = {"User-Agent": "Mozilla/5.0"}
BASE = "https://api.elections.kalshi.com/trade-api/v2"
SOCCERY = re.compile(r"cup|fifa|soccer|футбол|match|uefa|footbal", re.I)


def main() -> int:
    series_counts = Counter()
    soccer_events = []
    cursor = ""
    for _ in range(15):                     # up to ~3000 open events
        params = {"status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(f"{BASE}/events", params=params, timeout=30,
                            headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()
        for ev in data.get("events", []):
            st = ev.get("series_ticker") or ev.get(
                "event_ticker", "").split("-")[0]
            series_counts[st] += 1
            blob = f"{st} {ev.get('title', '')} {ev.get('sub_title', '')}"
            if SOCCERY.search(blob):
                soccer_events.append({
                    "series": st,
                    "event_ticker": ev.get("event_ticker"),
                    "title": ev.get("title")})
        cursor = data.get("cursor") or ""
        if not cursor:
            break

    # direct guesses too, in case they're listed but not "open" right now
    guesses = {}
    for s in ("KXFIFAGAME", "KXWCGAME", "KXSOCCERGAME", "KXMENWORLDCUPMATCH",
              "KXWCMATCH", "KXFIFAWC"):
        try:
            r = requests.get(f"{BASE}/events",
                             params={"series_ticker": s, "limit": 3,
                                     "with_nested_markets": "true"},
                             timeout=30, headers=HEADERS)
            body = r.json() if r.status_code == 200 else r.text[:200]
            n = len(body.get("events", [])) if isinstance(body, dict) else -1
            guesses[s] = {"status": r.status_code, "events": n,
                          "sample": (body.get("events") or [{}])[0].get(
                              "title") if isinstance(body, dict) and
                          body.get("events") else None}
        except Exception as exc:
            guesses[s] = {"error": str(exc)}

    out = {
        "total_series": len(series_counts),
        "series_top50": series_counts.most_common(50),
        "soccer_matches": soccer_events[:60],
        "guesses": guesses,
    }
    OUT.mkdir(exist_ok=True)
    (OUT / "wc_probe.json").write_text(json.dumps(out, indent=2)[:60000])
    print("series:", len(series_counts), "| soccerish:", len(soccer_events))
    return 0


if __name__ == "__main__":
    main()
