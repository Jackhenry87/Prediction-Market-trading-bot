"""World Cup probe round 5: KXFIFAGAME market-level shape + today's games.

Confirmed so far: event tickers are KXFIFAGAME-{YY}{MON}{DD}{AAA}{BBB}
(FIFA trigrams, same codes Polymarket uses in fifwc slugs). Still needed:
the per-market tickers/labels inside one event (is there a -TIE market?
what do yes_sub_titles look like?) and whether today's knockout games are
listed yet. Read-only, no keys.
"""

import json
import time
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent / "probe_results"
HEADERS = {"User-Agent": "Mozilla/5.0"}
BASE = "https://api.elections.kalshi.com/trade-api/v2"


def get(path, **params):
    time.sleep(2)
    resp = requests.get(f"{BASE}{path}", params=params, timeout=30,
                        headers=HEADERS)
    return resp.json() if resp.status_code == 200 else {
        "http": resp.status_code, "text": resp.text[:200]}


def main() -> int:
    out = {}

    # 1. market-level shape from one known (settled) event
    out["markets_sample"] = get("/markets",
                                event_ticker="KXFIFAGAME-26MAR31IRQBOL",
                                limit=10)
    if isinstance(out["markets_sample"], dict):
        ms = out["markets_sample"].get("markets") or []
        out["markets_sample"] = [
            {k: m.get(k) for k in ("ticker", "yes_sub_title", "subtitle",
                                   "title", "status", "result")}
            for m in ms]

    # 2. hunt for today's games: page the series without a status filter
    found = []
    cursor = ""
    for _ in range(12):
        params = dict(series_ticker="KXFIFAGAME", limit=100)
        if cursor:
            params["cursor"] = cursor
        data = get("/events", **params)
        if not isinstance(data, dict) or "events" not in data:
            out.setdefault("page_errors", []).append(data)
            break
        for ev in data["events"]:
            t = ev.get("event_ticker", "")
            if "26JUL" in t:
                found.append({"event_ticker": t, "title": ev.get("title")})
        cursor = data.get("cursor") or ""
        if not cursor:
            break
    out["july_events"] = found

    OUT.mkdir(exist_ok=True)
    (OUT / "wc_probe.json").write_text(json.dumps(out, indent=2)[:60000])
    print("july events:", len(found))
    return 0


if __name__ == "__main__":
    main()
