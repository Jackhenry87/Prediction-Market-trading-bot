"""Minimal authenticated Kalshi Trade API v2 client.

Auth: every request carries three headers — the API key ID, a millisecond
timestamp, and an RSA-PSS(SHA-256) signature of "<timestamp><METHOD><path>"
(path includes the /trade-api/v2 prefix, excludes the query string). The
private key never leaves this machine.
"""

import base64
import time
import uuid

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from trade_logger import get_logger

log = get_logger("kalshi")

ROOTS = {
    "demo": "https://demo-api.kalshi.co",
    "prod": "https://api.elections.kalshi.com",
}
PREFIX = "/trade-api/v2"


class KalshiError(Exception):
    pass


class KalshiClient:
    def __init__(self, key_id: str = None, private_key_path: str = None,
                 env: str = "demo"):
        """Without credentials the client is public/read-only: market-data
        endpoints work unauthenticated, portfolio/trading calls will 401."""
        if env not in ROOTS:
            raise KalshiError(f"KALSHI_ENV must be demo or prod, got {env!r}")
        self.env = env
        self.root = ROOTS[env]
        self.key_id = key_id
        if key_id and private_key_path:
            with open(private_key_path, "rb") as fh:
                self.private_key = serialization.load_pem_private_key(
                    fh.read(), password=None
                )
        else:
            self.private_key = None

    def _headers(self, method: str, path: str) -> dict:
        if self.private_key is None:
            return {}
        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}{method}{path}".encode()
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256().digest_size,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }

    def _request(self, method: str, path: str, params=None, body=None):
        full_path = PREFIX + path
        resp = requests.request(
            method,
            self.root + full_path,
            params=params,
            json=body,
            headers=self._headers(method, full_path),
            timeout=15,
        )
        if resp.status_code >= 400:
            raise KalshiError(
                f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:500]}"
            )
        return resp.json()

    # --- market data ---
    def get_exchange_status(self):
        return self._request("GET", "/exchange/status")

    def get_market(self, ticker: str):
        data = self._request("GET", f"/markets/{ticker}")
        market = data.get("market")
        if not market:
            raise KalshiError(
                f"no market called {ticker!r} (response: {str(data)[:200]})"
            )
        return market

    def get_event(self, event_ticker: str):
        """Events group several markets; returns {'event': ..., 'markets': [...]}"""
        return self._request(
            "GET", f"/events/{event_ticker}", params={"with_nested_markets": "true"}
        )

    def get_orderbook(self, ticker: str, depth: int = 10):
        data = self._request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}
        )
        # Untraded markets return no book at all — treat as empty.
        return data.get("orderbook") or {}

    # --- portfolio ---
    def get_balance_cents(self) -> int:
        return self._request("GET", "/portfolio/balance")["balance"]

    def get_positions(self):
        return self._request("GET", "/portfolio/positions")

    def get_resting_orders(self):
        return self._request(
            "GET", "/portfolio/orders", params={"status": "resting"}
        ).get("orders", [])

    # --- trading ---
    def create_limit_order(self, ticker: str, side: str, action: str,
                           count: int, price_cents: int):
        """side: 'yes'|'no'; action: 'buy'|'sell'; price in cents (1-99).

        Converted to the V2 single-book model (POST /portfolio/events/orders):
        buying YES is a bid at p; buying NO is an ask at 100-p (selling is the
        mirror image). Prices are fixed-point dollar strings.
        """
        if side == "yes":
            book_side = "bid" if action == "buy" else "ask"
            book_cents = price_cents
        else:
            book_side = "ask" if action == "buy" else "bid"
            book_cents = 100 - price_cents
        body = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": book_side,
            "count": str(count),  # V2 wants numeric fields as strings
            "price": f"{book_cents / 100:.4f}",
            "time_in_force": "good_till_canceled",
            # if our own orders would match each other, cancel the incoming
            # one rather than pulling resting orders
            "self_trade_prevention_type": "taker_at_cross",
        }
        return self._request("POST", "/portfolio/events/orders", body=body)

    def cancel_order(self, order_id: str):
        return self._request("DELETE", f"/portfolio/orders/{order_id}")
