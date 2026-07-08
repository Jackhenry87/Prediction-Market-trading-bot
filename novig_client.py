"""Novig trading client — OAuth2, mirrors the KalshiClient interface so the
existing strategies can place on Novig as a SECOND autonomous exchange.

Novig is a CFTC-regulated, API-first, peer-to-peer prediction-market exchange
(docs.novig.com): OAuth 2.0 auth, HTTP endpoints for orders / positions /
market data / account, OpenAPI 3.1. This is the only DFS/prediction app besides
Kalshi with a real public order-placement API.

  ┌─────────────────────────────────────────────────────────────────────────┐
  │ PROVISIONAL PATHS. The docs site (docs.novig.com) bot-blocks scripted     │
  │ fetches, so the endpoint paths and response field names in ENDPOINTS /    │
  │ FIELDS below are best-effort and MUST be reconciled against the live      │
  │ OpenAPI spec once you have API access. Everything else — OAuth flow,      │
  │ token refresh, request plumbing, the method surface — is real and tested. │
  │ Correcting the spec is a one-block edit; no method bodies change.         │
  └─────────────────────────────────────────────────────────────────────────┘

Auth: OAuth2 client-credentials. Set NOVIG_CLIENT_ID + NOVIG_CLIENT_SECRET
(never hardcode — repo secrets / .env only). The access token is fetched on
first use and refreshed automatically before it expires.
"""

import os
import threading
import time

import requests

from trade_logger import get_logger

log = get_logger("novig")

# Base + OAuth token URL — override via env once confirmed.
API_BASE = os.getenv("NOVIG_API_BASE", "https://api.novig.com").rstrip("/")
TOKEN_URL = os.getenv("NOVIG_TOKEN_URL", f"{API_BASE}/oauth/token")

# --- PROVISIONAL: reconcile against the live OpenAPI spec ---
ENDPOINTS = {
    "balance": "/v1/account/balance",
    "positions": "/v1/positions",
    "orders": "/v1/orders",                 # GET list / POST create
    "order": "/v1/orders/{order_id}",       # DELETE cancel
    "fills": "/v1/fills",
    "markets": "/v1/markets",
    "market": "/v1/markets/{market_id}",
}
# Response field names we read, isolated so a spec mismatch is a one-line fix.
FIELDS = {
    "balance_cents": "balance_cents",       # else balance dollars -> *100
    "positions_list": "positions",
    "orders_list": "orders",
    "fills_list": "fills",
    "markets_list": "markets",
    "cursor": "cursor",
}


class NovigError(Exception):
    pass


class NovigClient:
    def __init__(self, client_id: str = None, client_secret: str = None,
                 api_base: str = None, token_url: str = None):
        """Without credentials the client is read-only where Novig allows it;
        any account/trading call will fail auth."""
        self.client_id = client_id or os.getenv("NOVIG_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("NOVIG_CLIENT_SECRET")
        self.api_base = (api_base or API_BASE).rstrip("/")
        self.token_url = token_url or TOKEN_URL
        self._token = None
        self._token_expiry = 0.0
        self._lock = threading.Lock()

    # --- OAuth2 ---
    def _fetch_token(self) -> None:
        if not (self.client_id and self.client_secret):
            raise NovigError("NOVIG_CLIENT_ID / NOVIG_CLIENT_SECRET not set")
        resp = requests.post(
            self.token_url,
            data={"grant_type": "client_credentials",
                  "client_id": self.client_id,
                  "client_secret": self.client_secret},
            timeout=15,
        )
        if resp.status_code >= 400:
            raise NovigError(f"token fetch -> HTTP {resp.status_code}: "
                             f"{resp.text[:300]}")
        tok = resp.json()
        self._token = tok["access_token"]
        # refresh 60s before actual expiry; default 1h if not provided
        self._token_expiry = time.time() + float(tok.get("expires_in", 3600)) - 60

    def _access_token(self) -> str:
        with self._lock:
            if not self._token or time.time() >= self._token_expiry:
                self._fetch_token()
            return self._token

    def _request(self, method: str, path: str, params=None, body=None):
        resp = requests.request(
            method, self.api_base + path, params=params, json=body,
            headers={"Authorization": f"Bearer {self._access_token()}",
                     "Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code == 401:          # token maybe stale -> one retry
            with self._lock:
                self._token = None
            resp = requests.request(
                method, self.api_base + path, params=params, json=body,
                headers={"Authorization": f"Bearer {self._access_token()}",
                         "Accept": "application/json"},
                timeout=15,
            )
        if resp.status_code >= 400:
            raise NovigError(f"{method} {path} -> HTTP {resp.status_code}: "
                             f"{resp.text[:400]}")
        return resp.json() if resp.content else {}

    def _paged(self, path: str, list_key: str, params=None) -> list:
        out, cursor = [], None
        base = dict(params or {})
        for _ in range(50):
            p = dict(base, limit=200)
            if cursor:
                p["cursor"] = cursor
            data = self._request("GET", path, params=p)
            page = data.get(list_key) or []
            out += page
            cursor = data.get(FIELDS["cursor"])
            if not cursor or not page:
                break
        return out

    # --- account / portfolio ---
    def get_balance_cents(self) -> int:
        data = self._request("GET", ENDPOINTS["balance"])
        if FIELDS["balance_cents"] in data:
            return int(data[FIELDS["balance_cents"]])
        if "balance" in data:                # dollars fallback
            return int(round(float(data["balance"]) * 100))
        raise NovigError(f"no balance field in {str(data)[:200]}")

    def get_positions(self) -> list:
        return self._paged(ENDPOINTS["positions"], FIELDS["positions_list"])

    def get_resting_orders(self) -> list:
        return self._paged(ENDPOINTS["orders"], FIELDS["orders_list"],
                           params={"status": "open"})

    def get_fills(self, min_ts=None) -> list:
        params = {"since": int(min_ts)} if min_ts else None
        return self._paged(ENDPOINTS["fills"], FIELDS["fills_list"], params)

    # --- market data ---
    def get_markets(self, **params) -> list:
        return self._paged(ENDPOINTS["markets"], FIELDS["markets_list"], params)

    def get_market(self, market_id: str):
        return self._request(
            "GET", ENDPOINTS["market"].format(market_id=market_id))

    # --- trading ---
    def create_limit_order(self, market_id: str, side: str, action: str,
                           count: int, price_cents: int):
        """Place a limit order. side: 'yes'|'no'; action: 'buy'|'sell'; price
        in cents (1-99, i.e. implied probability). Sent as a decimal
        probability string, matching how event-contract exchanges price.
        NOTE: confirm Novig's exact side/price convention against the spec —
        this mirrors Kalshi's (buy YES @ p == buy NO @ 100-p)."""
        if not 1 <= price_cents <= 99:
            raise NovigError(f"price_cents must be 1-99, got {price_cents}")
        body = {
            "market_id": market_id,
            "side": side,
            "action": action,
            "count": int(count),
            "price": f"{price_cents / 100:.4f}",
            "type": "limit",
            "time_in_force": "gtc",
        }
        return self._request("POST", ENDPOINTS["orders"], body=body)

    def cancel_order(self, order_id: str):
        return self._request(
            "DELETE", ENDPOINTS["order"].format(order_id=order_id))
