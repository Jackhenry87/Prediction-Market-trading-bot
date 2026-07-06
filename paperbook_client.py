"""Client that connects the trading bot to the Paper Sportsbook API.

Set PAPERBOOK_URL (e.g. https://your-app.onrender.com) and PAPERBOOK_KEY
(the API key from your /mybets page). The bot then places PAPER bets on
your own site the same way it trades Kalshi — clean API, no scraping.

    from paperbook_client import PaperbookClient
    pb = PaperbookClient()
    for g in pb.games():
        ...
    pb.place_bet(game_id, "home", stake_cents=500)
"""

import os

import requests


class PaperbookClient:
    def __init__(self, base_url: str = None, api_key: str = None):
        self.base = (base_url or os.getenv("PAPERBOOK_URL", "")).rstrip("/")
        self.key = api_key or os.getenv("PAPERBOOK_KEY", "")
        if not self.base or not self.key:
            raise ValueError("Set PAPERBOOK_URL and PAPERBOOK_KEY")
        self.headers = {"X-API-Key": self.key}

    def games(self) -> list:
        r = requests.get(f"{self.base}/api/games", headers=self.headers, timeout=15)
        r.raise_for_status()
        return r.json()["games"]

    def me(self) -> dict:
        r = requests.get(f"{self.base}/api/me", headers=self.headers, timeout=15)
        r.raise_for_status()
        return r.json()

    def place_bet(self, game_id: str, side: str, stake_cents: int) -> dict:
        r = requests.post(f"{self.base}/api/bets", headers=self.headers,
                          json={"game_id": game_id, "side": side,
                                "stake_cents": stake_cents}, timeout=15)
        r.raise_for_status()
        return r.json()
