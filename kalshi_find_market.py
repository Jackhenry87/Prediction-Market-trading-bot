"""Search open Kalshi markets by keyword and save your pick into .env.

    python kalshi_find_market.py nyc temperature
    python kalshi_find_market.py fed rate

Lists matching markets as a numbered menu; typing a number writes that
market's ticker into MARKET_TICKER in .env. Read-only otherwise.
Searches whichever environment KALSHI_ENV points at (demo or prod).
"""

import sys
from pathlib import Path

from config import ConfigError, load_kalshi_settings
from kalshi_client import KalshiClient

MAX_PAGES = 10
MAX_EVENTS_SHOWN = 8
MAX_MARKETS_PER_EVENT = 12


def save_ticker(ticker: str) -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        sys.exit(".env not found — copy .env.example to .env first.")
    lines = env_path.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("MARKET_TICKER"):
            lines[i] = f"MARKET_TICKER={ticker}"
            break
    else:
        lines.insert(0, f"MARKET_TICKER={ticker}")
    env_path.write_text("\n".join(lines) + "\n")


def fetch_open_events(client) -> list:
    events, cursor = [], None
    for _ in range(MAX_PAGES):
        params = {"limit": 200, "status": "open", "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        data = client._request("GET", "/events", params=params)
        page = data.get("events", [])
        events.extend(page)
        cursor = data.get("cursor")
        if not cursor or not page:
            break
    return events


def main() -> int:
    query = " ".join(sys.argv[1:]).strip().lower()
    if not query:
        print(__doc__)
        return 1

    try:
        settings = load_kalshi_settings(require_market=False)
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 1

    client = KalshiClient(
        settings.kalshi_api_key_id,
        settings.kalshi_private_key_path,
        settings.kalshi_env,
    )
    print(f"Searching open markets on Kalshi ({settings.kalshi_env}) "
          f"for {query!r} ... (takes a few seconds)")
    try:
        events = fetch_open_events(client)
    except Exception as exc:
        print(f"Search failed: {exc}")
        return 1

    words = query.split()
    matches = [
        e for e in events
        if all(w in f"{e.get('title', '')} {e.get('ticker', '')}".lower()
               for w in words)
    ]
    if not matches:
        print(f"No open markets match {query!r}. Try fewer or different words.")
        return 1

    options = []
    for event in matches[:MAX_EVENTS_SHOWN]:
        print(f"\nEvent: {event.get('title')}")
        for market in (event.get("markets") or [])[:MAX_MARKETS_PER_EVENT]:
            label = (market.get("subtitle") or market.get("yes_sub_title")
                     or market.get("title") or "")
            options.append(market["ticker"])
            bid, ask = market.get("yes_bid"), market.get("yes_ask")
            price = f"  (yes {bid}-{ask}¢)" if bid is not None else ""
            print(f"  [{len(options)}] {market['ticker']}  {label}{price}")

    if len(matches) > MAX_EVENTS_SHOWN:
        print(f"\n({len(matches) - MAX_EVENTS_SHOWN} more events matched — "
              f"narrow the search to see them)")

    if not options:
        print("Matching events have no open markets.")
        return 1

    if not sys.stdin.isatty():
        print("\nCopy one ticker into MARKET_TICKER= in your .env file.")
        return 0

    choice = input(
        "\nType a number and press Enter to save that market into .env "
        "(or just Enter to skip): "
    ).strip()
    if not choice:
        print("Nothing saved.")
        return 0
    if not (choice.isdigit() and 1 <= int(choice) <= len(options)):
        print(f"No option {choice!r}. Nothing saved.")
        return 0

    ticker = options[int(choice) - 1]
    save_ticker(ticker)
    print(f"\nSaved to .env: MARKET_TICKER={ticker}")
    print("Next step:  python kalshi_fetch_orderbook.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
