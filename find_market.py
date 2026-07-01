"""Helper: look up the CLOB token IDs for a Polymarket market.

Paste a market URL from polymarket.com (or just its slug) and this prints
each outcome with its token ID, ready to copy into MARKET_TOKEN_ID in .env.

Usage:
    python find_market.py "https://polymarket.com/event/some-market-question"
    python find_market.py some-market-question

Read-only: only queries the public Gamma API. No wallet, no orders.
"""

import json
import sys
import urllib.parse

import requests

GAMMA_API = "https://gamma-api.polymarket.com"


def fetch_json(path: str):
    # requests bundles its own CA certificates, which avoids the macOS
    # python.org-install SSL verification failure that urllib hits.
    resp = requests.get(f"{GAMMA_API}{path}", timeout=15)
    resp.raise_for_status()
    return resp.json()


def slug_from_arg(arg: str) -> str:
    """Accept a full polymarket.com URL or a bare slug.

    Handles /event/<slug>, sports pages like /sports/mlb/<slug>, and strips
    the curly "smart quotes" some editors substitute for straight quotes.
    """
    arg = arg.strip().strip("\"'“”‘’")
    if "polymarket.com" in arg:
        if "//" not in arg:
            arg = "https://" + arg
        path = urllib.parse.urlparse(arg).path
        parts = [p for p in path.split("/") if p]
        if not parts:
            sys.exit(
                "That URL has no market in it. On polymarket.com, click into "
                "ONE specific game or question, then paste that page's URL."
            )
        return parts[-1]
    return arg.strip("/")


def as_list(value):
    """Gamma returns some list fields as JSON-encoded strings."""
    if isinstance(value, str):
        return json.loads(value)
    return value or []


def markets_for_slug(slug: str) -> list:
    quoted = urllib.parse.quote(slug)
    markets = fetch_json(f"/markets?slug={quoted}")
    if markets:
        return markets
    # Maybe it's an event slug (a page grouping several markets)
    events = fetch_json(f"/events?slug={quoted}")
    if events:
        return events[0].get("markets", [])
    return []


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1

    slug = slug_from_arg(sys.argv[1])

    if slug.isdigit() and len(slug) > 20:
        # That's already a CLOB token ID, not a slug.
        print("That long number is already a CLOB token ID — no lookup needed.")
        print("Checking it has a live order book ...")
        try:
            resp = requests.get(
                "https://clob.polymarket.com/book",
                params={"token_id": slug},
                timeout=15,
            )
            resp.raise_for_status()
            book = resp.json()
            print(
                f"Valid: order book found with {len(book.get('bids', []))} bid "
                f"and {len(book.get('asks', []))} ask levels."
            )
        except Exception as exc:
            print(f"Could not confirm it ({exc}) — it may be wrong or inactive.")
        print(f"\nPut this line in your .env:\n  MARKET_TOKEN_ID={slug}")
        return 0

    print(f"Looking up slug: {slug!r} ...")
    try:
        markets = markets_for_slug(slug)
    except Exception as exc:
        print(f"Gamma API request failed: {exc}")
        return 1

    if not markets:
        print(
            "No market found for that slug.\n"
            "Make sure you copied the URL of a specific market question "
            "(it should contain /event/), not a category or homepage."
        )
        return 1

    for market in markets:
        outcomes = as_list(market.get("outcomes"))
        token_ids = as_list(market.get("clobTokenIds"))
        status = "CLOSED" if market.get("closed") else "active"
        print(f"\nMarket: {market.get('question', '<no question>')} [{status}]")
        if not token_ids:
            print("  (no CLOB token IDs — this market may not be tradeable)")
            continue
        for outcome, token_id in zip(outcomes, token_ids):
            print(f"  {outcome}:")
            print(f"    MARKET_TOKEN_ID={token_id}")

    print(
        "\nCopy ONE of the MARKET_TOKEN_ID lines above (from an active market) "
        "into your .env file."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
