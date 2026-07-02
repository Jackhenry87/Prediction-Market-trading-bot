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
from pathlib import Path

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


def save_token_id(token_id: str) -> None:
    """Write MARKET_TOKEN_ID into the local .env, replacing the existing line."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        sys.exit(".env not found — run:  cp .env.example .env  and fill it in first.")
    lines = env_path.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("MARKET_TOKEN_ID"):
            lines[i] = f"MARKET_TOKEN_ID={token_id}"
            break
    else:
        lines.append(f"MARKET_TOKEN_ID={token_id}")
    env_path.write_text("\n".join(lines) + "\n")


def offer_to_save(options) -> None:
    """Numbered menu: pick an outcome and it's written straight into .env."""
    if not sys.stdin.isatty():
        print("\nCopy ONE token ID above into MARKET_TOKEN_ID= in your .env file.")
        return
    choice = input(
        "\nType a number and press Enter to save that outcome into .env "
        "(or just Enter to skip): "
    ).strip()
    if not choice:
        print("Nothing saved.")
        return
    if not (choice.isdigit() and 1 <= int(choice) <= len(options)):
        print(f"No option {choice!r}. Nothing saved.")
        return
    label, token_id = options[int(choice) - 1]
    save_token_id(token_id)
    print(f"\nSaved to .env: {label}")
    print("Next step:  python place_order.py   (or python fetch_orderbook.py)")


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
        offer_to_save([(f"token {slug[:12]}…", slug)])
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

    options = []  # (label, token_id) for active outcomes, numbered in the menu
    for market in markets:
        outcomes = as_list(market.get("outcomes"))
        token_ids = as_list(market.get("clobTokenIds"))
        question = market.get("question", "<no question>")
        closed = market.get("closed")
        print(f"\nMarket: {question} [{'CLOSED' if closed else 'active'}]")
        if not token_ids:
            print("  (no CLOB token IDs — this market may not be tradeable)")
            continue
        for outcome, token_id in zip(outcomes, token_ids):
            if closed:
                print(f"       {outcome}: {token_id}  (closed — not selectable)")
            else:
                options.append((f"{question} — {outcome}", str(token_id)))
                print(f"  [{len(options)}] {outcome}: {token_id}")

    if not options:
        print("\nAll markets here are closed. Pick a market that hasn't ended yet.")
        return 1

    offer_to_save(options)
    return 0


if __name__ == "__main__":
    sys.exit(main())
