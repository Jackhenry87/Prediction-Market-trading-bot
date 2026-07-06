"""Probe: does Kalshi list combo/multi-leg markets as tradeable tickers?

If a combo exists as its own market (own ticker, own order book), the bot
can trade it like any binary. Checks:
  A. series-ticker guesses (KXMULTI/KXCOMBO/KXPARLAY/...)
  B. a full scan of OPEN events+markets for 'AND'-style combo titles
  C. Polymarket's combo products on the tape (titles with ' AND '), for
     the consensus-detection side of any future mapping.
Read-only, no keys. Results committed back by CI. Delete after.
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
COMBO_RE = re.compile(r"\bAND\b|&|\bparlay\b|\bcombo\b|\bmulti\b", re.I)


def kget(path, **params):
    time.sleep(1.2)
    r = requests.get(f"{BASE}{path}", params=params, timeout=30,
                     headers=HEADERS)
    return r.json() if r.status_code == 200 else {"http": r.status_code}


def main() -> int:
    out = {}

    # A. direct series guesses
    guesses = {}
    for s in ("KXMULTI", "KXCOMBO", "KXPARLAY", "KXMULTIS", "KXCOMBOS",
              "KXMULTIGAME", "KXPARLAYS"):
        d = kget("/events", series_ticker=s, limit=2,
                 with_nested_markets="true")
        n = len(d.get("events", [])) if isinstance(d, dict) else -1
        guesses[s] = {"n": n,
                      "sample": (d.get("events") or [{}])[0].get("title")
                      if isinstance(d, dict) and d.get("events") else None}
    out["series_guesses"] = guesses

    # B. open-event scan for combo-looking titles (events AND market rows)
    combo_events, combo_markets = [], []
    series_seen = Counter()
    cursor = ""
    for _ in range(15):
        params = dict(status="open", limit=200, with_nested_markets="true")
        if cursor:
            params["cursor"] = cursor
        data = kget("/events", **params)
        if "events" not in data:
            out.setdefault("page_errors", []).append(data)
            break
        for ev in data["events"]:
            series_seen[ev.get("series_ticker") or ""] += 1
            title = ev.get("title") or ""
            if " AND " in title.upper() or "PARLAY" in title.upper():
                combo_events.append({"series": ev.get("series_ticker"),
                                     "event": ev.get("event_ticker"),
                                     "title": title})
            for mk in ev.get("markets") or []:
                mt = f"{mk.get('title', '')} {mk.get('yes_sub_title', '')}"
                if " AND " in mt.upper() or "PARLAY" in mt.upper():
                    combo_markets.append({"ticker": mk.get("ticker"),
                                          "title": mt[:120]})
        cursor = data.get("cursor") or ""
        if not cursor:
            break
    out["combo_events"] = combo_events[:25]
    out["combo_markets"] = combo_markets[:25]
    out["open_series_count"] = len(series_seen)

    # C. Polymarket combo products currently trading
    poly = Counter()
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
            if " AND " in title.upper():
                slug = tr.get("slug") or tr.get("conditionId", "")[:20]
                poly[slug] += 1
                samples.setdefault(slug, {"title": title[:110],
                                          "outcome": tr.get("outcome"),
                                          "price": tr.get("price")})
    out["poly_combos"] = [{"slug": s, "n": n, **samples[s]}
                          for s, n in poly.most_common(15)]

    OUT.mkdir(exist_ok=True)
    (OUT / "combos_probe.json").write_text(json.dumps(out, indent=2)[:60000])
    print("kalshi combo events:", len(combo_events),
          "| combo markets:", len(combo_markets),
          "| poly combos:", len(poly))
    return 0


if __name__ == "__main__":
    main()
