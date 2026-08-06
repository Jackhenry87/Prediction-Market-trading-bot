"""Diagnostic: is the odds key alive, and does the Kalshi DEMO venue carry
usable market data?

Answers two questions that can't be answered from a laptop:

  1. ODDS API — does ODDS_API_KEY work, and how many of the 500 monthly
     credits are left? The /v4/sports listing is free, so this costs nothing
     unless --spend is passed.

  2. KALSHI DEMO vs PROD — the favourite-bias model measures closing-line
     value, which is only meaningful against the book that real money trades.
     Demo is a sandbox with its own liquidity. This prints, for both venues,
     how many markets are open, how many carry a two-sided quote, and how many
     the model would actually signal on — so the choice of venue is made on
     evidence rather than assumption.

    python env_check.py            # free: no odds credits spent
    python env_check.py --spend    # also make ONE metered odds call (2 credits)
"""

import os
import sys

import requests

from kalshi_client import KalshiClient
from trade_logger import get_logger, setup_logging

log = get_logger("env_check")

SPORTS_LIST_URL = "https://api.the-odds-api.com/v4/sports/"
ODDS_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds/"


def _quota(resp) -> str:
    h = resp.headers
    return (f"used={h.get('x-requests-used', '?')} "
            f"remaining={h.get('x-requests-remaining', '?')} "
            f"last_call_cost={h.get('x-requests-last', '?')}")


def check_odds(spend: bool) -> int:
    key = os.getenv("ODDS_API_KEY", "").strip()
    print("\n=== THE ODDS API ===")
    if not key:
        print("  ODDS_API_KEY not set")
        return 1
    print(f"  key present: {len(key)} chars, ends {key[-4:]}")

    try:
        resp = requests.get(SPORTS_LIST_URL, params={"apiKey": key}, timeout=20)
    except Exception as exc:
        print(f"  NETWORK ERROR: {exc}")
        return 1

    print(f"  GET /v4/sports -> HTTP {resp.status_code}   (free endpoint)")
    print(f"  quota: {_quota(resp)}")
    if resp.status_code != 200:
        print(f"  BODY: {resp.text[:300]}")
        print("  >> KEY IS NOT WORKING")
        return 1

    sports = resp.json()
    active = [s for s in sports if s.get("active") and not s.get("has_outrights")]
    print(f"  KEY WORKS. {len(sports)} sports listed, {len(active)} active:")
    for s in active[:20]:
        print(f"    - {s['key']}")

    if spend:
        # One real metered call so we can see the true per-call cost.
        sport = next((s["key"] for s in active if "baseball" in s["key"]),
                     active[0]["key"] if active else None)
        if sport:
            r2 = requests.get(ODDS_URL.format(sport=sport),
                              params={"apiKey": key, "regions": "us",
                                      "markets": "h2h,totals",
                                      "oddsFormat": "decimal"}, timeout=20)
            print(f"\n  METERED CALL {sport} -> HTTP {r2.status_code}")
            print(f"  quota after: {_quota(r2)}")
            if r2.status_code == 200:
                print(f"  games returned: {len(r2.json())}")
    return 0


def _summarise(label: str, markets: list) -> dict:
    import strategy_favorite as fav
    quoted = [m for m in markets if fav.favourite_side(m)]
    signals = [s for s in (fav.evaluate_market(m) for m in markets) if s]
    volumes = sorted((float(m.get("volume") or 0) for m in markets),
                     reverse=True)
    print(f"  [{label}] markets={len(markets)} two_sided={len(quoted)} "
          f"volume>0={sum(1 for v in volumes if v > 0)} "
          f"signals={len(signals)} top_vol={[int(v) for v in volumes[:5]]}")
    for s in signals[:5]:
        print(f"      {s['side']:3} {s['ticker']:34} {s['price_cents']:.0f}c "
              f"book {s['bid']:.0f}/{s['ask']:.0f}")
    return dict(markets=len(markets), quoted=len(quoted),
                signals=len(signals),
                traded=sum(1 for v in volumes if v > 0))


def check_kalshi(env: str) -> dict:
    """Compare naive paging against a close-time-windowed query.

    The naive `/markets?status=open` page walk returned 4000 markets with one
    two-sided quote and zero volume — it was paging through far-dated markets
    that have never traded. Kalshi's own liquidity lives in the contracts
    closing soon, so the windowed query is the one that matters.
    """
    import time
    import strategy_favorite as fav
    print(f"\n=== KALSHI {env.upper()} ===")
    try:
        client = KalshiClient(env=env)
    except Exception as exc:
        print(f"  ERROR: {exc}")
        return dict(env=env, error=str(exc))

    out = {}
    try:
        naive = fav.list_open_markets(client)
        out["naive"] = _summarise("naive page-walk", naive)
    except Exception as exc:
        print(f"  naive query ERROR: {exc}")
        naive = []

    # What does a market payload actually look like? Field names matter:
    # `volume` may simply not be present in the list response.
    if naive:
        sample = naive[0]
        print(f"  sample market keys: {sorted(sample.keys())}")
        print(f"  sample values: " + ", ".join(
            f"{k}={sample.get(k)!r}" for k in
            ("ticker", "status", "yes_bid", "yes_ask", "volume",
             "open_interest", "close_time") if k in sample))
        booked = next((m for m in naive if fav.favourite_side(m)), None)
        if booked:
            print(f"  first two-sided market: {booked.get('ticker')} "
                  f"bid={booked.get('yes_bid')} ask={booked.get('yes_ask')} "
                  f"vol={booked.get('volume')} close={booked.get('close_time')}")

    # Markets closing in the next week — where the live book is.
    now = int(time.time())
    try:
        windowed, cursor = [], None
        for _ in range(20):
            params = {"status": "open", "limit": 200,
                      "min_close_ts": now + 3600,
                      "max_close_ts": now + 7 * 86400}
            if cursor:
                params["cursor"] = cursor
            data = client._request("GET", "/markets", params=params)
            page = data.get("markets") or []
            windowed += page
            cursor = data.get("cursor")
            if not cursor or not page:
                break
        out["windowed"] = _summarise("closing within 7d", windowed)
    except Exception as exc:
        print(f"  windowed query ERROR: {exc}")

    out["env"] = env
    return out


def main() -> int:
    setup_logging()
    spend = "--spend" in sys.argv
    rc = check_odds(spend)
    demo = check_kalshi("demo")
    prod = check_kalshi("prod")

    print("\n=== VERDICT ===")
    for name, res in (("demo", demo), ("prod", prod)):
        if res.get("error"):
            print(f"  {name}: ERROR {res['error']}")
            continue
        for q in ("naive", "windowed"):
            if q in res:
                r = res[q]
                print(f"  {name:4} {q:9}: {r['markets']:5} markets, "
                      f"{r['traded']:4} traded, {r['signals']:3} signals")
    pw = prod.get("windowed", {})
    dw = demo.get("windowed", {})
    if pw.get("signals", 0) > dw.get("signals", 0):
        print("  >> PROD carries the live book. Measure CLV there (read-only).")
    if pw.get("signals", 0) == 0 and pw.get("traded", 0) == 0:
        print("  >> Still no tradeable markets found — the market listing is "
              "not returning book/volume fields; needs a different endpoint.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
