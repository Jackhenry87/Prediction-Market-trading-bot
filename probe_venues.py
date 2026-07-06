"""Probe: can advance-style World Cup plays and tennis matches be placed?

A. KXFIFAGAME knockout events: dump markets WITH rules text and check for
   TIE markets. Knockout games without a TIE market (or with advance
   wording in the rules) settle on WHO ADVANCES -> the sharps' favorite
   bet type maps cleanly and can be placed.
B. Kalshi tennis venue: inventory series/events that look like tennis
   (Wimbledon is on) + direct series-ticker guesses.
C. Polymarket tennis slug taxonomy from the live tape.

Read-only, no keys needed. Results committed back by CI. Delete after.
"""

import json
import re
import time
from collections import Counter
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent / "probe_results"
HEADERS = {"User-Agent": "Mozilla/5.0"}
BASE = "https://api.elections.kalshi.com/trade-api/v2"


def kget(path, **params):
    time.sleep(1.5)
    r = requests.get(f"{BASE}{path}", params=params, timeout=30,
                     headers=HEADERS)
    return r.json() if r.status_code == 200 else {"http": r.status_code}


def main() -> int:
    out = {}

    # A. newest KXFIFAGAME events with market rules (paginate to the end —
    # earlier probe showed the list starts at March; today's knockouts are
    # at the tail)
    events, cursor = [], ""
    for _ in range(20):
        params = dict(series_ticker="KXFIFAGAME", limit=100,
                      with_nested_markets="true")
        if cursor:
            params["cursor"] = cursor
        data = kget("/events", **params)
        if "events" not in data:
            break
        events.extend(data["events"])
        cursor = data.get("cursor") or ""
        if not cursor:
            break
    out["fifagame_total"] = len(events)
    tail = events[-6:] if events else []
    keep = ("ticker", "yes_sub_title", "title", "status", "yes_ask",
            "yes_bid", "rules_primary")
    out["fifagame_tail"] = [{
        "event_ticker": ev.get("event_ticker"),
        "title": ev.get("title"),
        "markets": [{k: (str(m.get(k))[:300] if k == "rules_primary"
                         else m.get(k)) for k in keep}
                    for m in (ev.get("markets") or [])[:4]],
    } for ev in tail]
    # do any July events exist / do knockout events carry a TIE market?
    out["july_fifagame"] = [ev.get("event_ticker") for ev in events
                            if "26JUL" in (ev.get("event_ticker") or "")]

    # B. tennis on Kalshi: series guesses + open-event title scan
    guesses = {}
    for s in ("KXATPMATCH", "KXWTAMATCH", "KXTENNISMATCH", "KXWIMBLEDON",
              "KXATP", "KXWIMBLEDONMENS", "KXWIMBLEDONWOMENS"):
        d = kget("/events", series_ticker=s, limit=2,
                 with_nested_markets="true")
        n = len(d.get("events", [])) if isinstance(d, dict) else -1
        sample = None
        if n:
            evs = d.get("events") or [{}]
            sample = {"event": evs[0].get("event_ticker"),
                      "title": evs[0].get("title"),
                      "markets": [m.get("ticker") for m in
                                  (evs[0].get("markets") or [])[:4]]}
        guesses[s] = {"n": n, "sample": sample}
    out["tennis_guesses"] = guesses

    tennisy = []
    cursor = ""
    for _ in range(15):
        params = dict(status="open", limit=200)
        if cursor:
            params["cursor"] = cursor
        data = kget("/events", **params)
        if "events" not in data:
            break
        for ev in data["events"]:
            blob = f"{ev.get('title', '')} {ev.get('sub_title', '')}"
            if re.search(r"wimbledon|tennis|atp|wta", blob, re.I):
                tennisy.append({"series": ev.get("series_ticker"),
                                "event": ev.get("event_ticker"),
                                "title": ev.get("title")})
        cursor = data.get("cursor") or ""
        if not cursor:
            break
    out["tennis_open_events"] = tennisy[:30]

    # C. Polymarket tennis slugs from the tape
    slugs = Counter()
    samples = {}
    for page in range(4):
        try:
            trades = requests.get(
                "https://data-api.polymarket.com/trades",
                params={"limit": 500, "offset": page * 500},
                timeout=30, headers=HEADERS).json()
        except Exception as exc:
            out.setdefault("tape_errors", []).append(str(exc))
            continue
        for tr in trades:
            title = tr.get("title") or ""
            if re.search(r"wimbledon|atp|wta|tennis", title, re.I):
                slug = tr.get("slug", "")
                slugs[slug] += 1
                samples.setdefault(slug, {"title": title,
                                          "outcome": tr.get("outcome"),
                                          "price": tr.get("price")})
    out["poly_tennis"] = [{"slug": s, "n": n, **samples[s]}
                          for s, n in slugs.most_common(20)]

    OUT.mkdir(exist_ok=True)
    (OUT / "venues_probe.json").write_text(json.dumps(out, indent=2)[:70000])
    print("fifagame:", out["fifagame_total"], "| july:",
          len(out["july_fifagame"]), "| tennis events:", len(tennisy),
          "| poly tennis slugs:", len(slugs))
    return 0


if __name__ == "__main__":
    main()
