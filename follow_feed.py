"""Watch a followed trader's activity by polling Kalshi directly.

    python follow_feed.py --probe     # answer the auth question, print shapes
    python follow_feed.py             # one read-only poll, print new trades

WHY THIS REPLACED THE PHONE
---------------------------
The first design forwarded a Kalshi push notification off the owner's phone
via an iOS Shortcut. That was solving the wrong problem. A notification only
says *that* he traded; it took hand-typed input to say *what*, and stock iOS
has no notification trigger to automate it with anyway.

Probing Kalshi turned up routes that answer the real question:

    /v1/users/{username}/trades        401  (exists)
    /v1/users/{username}/positions     401  (exists)
    /v1/users/{username}/orders        401  (exists)
    /v1/users/{username}/nonsense      404  (the router knows the real ones)
    /v1/does_not_exist                 404

Auth is checked before the handler — `/v1/users/{garbage}` also 401s — so the
401s prove the ROUTES exist, not the user. These are what the Kalshi app calls
to render a profile page.

Polling them gives the exact ticker, side, price and contract count, with
detection bounded by the poll interval rather than by push latency plus a
human. No phone is involved at any point.

THESE ARE UNDOCUMENTED ENDPOINTS
--------------------------------
They can change or close without notice, so this module's rule is: FAIL
LOUDLY. A feed that silently returns nothing is indistinguishable from "he
stopped trading", and that is the failure mode that would quietly kill the
whole strategy while looking fine. Every auth failure and every shape we
cannot read gets logged at warning or error, and `poll` raises `FeedError`
rather than returning an empty list when the feed itself is broken.

AUTH, IN ORDER
--------------
1. The RSA API key this repo already signs with (`KalshiClient._headers`
   signs an arbitrary path, so /v1 is reachable with the existing key).
2. `FOLLOW_SESSION_TOKEN`, a bearer token copied out of a logged-in browser.
   Works, but expires — hence the alarm when a token that WAS working stops.

The key is tried first, and evidence says it is the right order. Signing a
/v1 request with a THROWAWAY key changes the error from
`token_authentication_failure` (unsigned) to `authentication_error:
NOT_FOUND` (key id looked up, not found) — the exact same transition as
`/trade-api/v2/portfolio/balance`, which is known to accept key auth. So /v1
processes the KALSHI-ACCESS-* scheme rather than ignoring it.

What that does NOT settle is AUTHORISATION: whether a valid key is allowed to
read ANOTHER user's positions, or only its own. Authentication and permission
are different gates, and only a real key answers the second. `--probe` is
what answers it.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

from trade_logger import get_logger

log = get_logger("follow_feed")

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "follow_seen.json"

USERNAME = os.getenv("FOLLOW_USERNAME", "blushing.wildebeest7119").strip()
SESSION_TOKEN = os.getenv("FOLLOW_SESSION_TOKEN", "").strip()
# How many trade ids to remember. Bounded so the state file cannot grow
# without limit; far larger than any plausible burst.
MAX_SEEN = int(os.getenv("FOLLOW_MAX_SEEN", "500"))

TRADES_PATH = "/v1/users/{u}/trades"
POSITIONS_PATH = "/v1/users/{u}/positions"


class FeedError(Exception):
    """The feed itself is broken — as distinct from 'he has not traded'.

    The caller must treat this as an outage and say so, never as silence.
    """


# ----------------------------- transport -----------------------------

def _signed_get(client, path: str, params=None):
    """GET an arbitrary path with the repo's existing RSA signing.

    `KalshiClient._request` hardcodes the /trade-api/v2 prefix, but
    `_headers` signs whatever path it is handed — so the /v1 surface is
    reachable with the key we already have, without duplicating the
    RSA-PSS signing code.
    """
    return requests.get(client.root + path, params=params,
                        headers=client._headers("GET", path), timeout=15)


def _token_get(client, path: str, params=None, scheme: str = "Bearer"):
    """GET with a browser session token."""
    value = f"{scheme} {SESSION_TOKEN}".strip() if scheme else SESSION_TOKEN
    return requests.get(client.root + path, params=params,
                        headers={"Authorization": value}, timeout=15)


def fetch(client, path: str, params=None) -> tuple:
    """(json, auth_mode) for a /v1 path, or raise FeedError.

    Tries the API key first because it never expires, then the session token.
    The scheme for the token is tried two ways since the header format is
    not documented anywhere we can see.
    """
    attempts = [("api_key", lambda: _signed_get(client, path, params))]
    if SESSION_TOKEN:
        attempts += [
            ("session_bearer", lambda: _token_get(client, path, params,
                                                  "Bearer")),
            ("session_raw", lambda: _token_get(client, path, params, "")),
        ]

    last = None
    for mode, call in attempts:
        try:
            resp = call()
        except Exception as exc:
            last = f"{mode}: {exc}"
            continue
        if resp.status_code == 200:
            try:
                return resp.json(), mode
            except ValueError:
                last = f"{mode}: 200 but body was not JSON"
                continue
        last = f"{mode}: HTTP {resp.status_code} {resp.text[:160]}"
        log.debug("auth attempt %s -> %s", mode, resp.status_code)

    raise FeedError(f"no auth mode could read {path} (last: {last}). "
                    f"{diagnose(last)} The copier is BLIND until this is "
                    f"fixed.")


def diagnose(detail: str) -> str:
    """Turn Kalshi's auth errors into the actual next step.

    The taxonomy was mapped by signing a request with a throwaway key and
    comparing responses, so each of these means something specific rather
    than just 'auth failed':
    """
    d = (detail or "").lower()
    if "not_found" in d:
        return ("The key ID was looked up and does not exist — check "
                "KALSHI_API_KEY_ID matches the key you created, and that "
                "KALSHI_ENV points at the right environment (a prod key is "
                "not valid on demo, or vice versa).")
    if "token_authentication_failure" in d:
        return ("No credentials reached the endpoint at all — the request "
                "went out unsigned. Check KALSHI_PRIVATE_KEY_PATH points at "
                "a readable .pem.")
    if "403" in d or "forbidden" in d or "permission" in d:
        return ("Authenticated but NOT AUTHORISED. This is the one failure "
                "that kills the approach: the key is valid, so Kalshi is "
                "refusing to show one account another account's positions. "
                "No amount of credential fixing changes that.")
    if "session" in d:
        # The last mode tried was a browser token, and those expire. Say so
        # here, because a token that worked yesterday and 401s today looks
        # identical to a broken endpoint from the log alone.
        return ("A session token was tried and rejected — if it was working "
                "before, it has EXPIRED. Refresh FOLLOW_SESSION_TOKEN from a "
                "logged-in browser, or set up an API key so this stops "
                "recurring.")
    return ("Unrecognised auth failure — run `python follow_feed.py --probe` "
            "and send the output.")


# ---------------------------- normalising ----------------------------

def _first(raw: dict, *keys):
    """First present, non-empty value among `keys`."""
    for k in keys:
        v = raw.get(k)
        if v not in (None, "", []):
            return v
    return None


def price_to_cents(value):
    """Kalshi renders prices as cents ints ('42') and as dollar strings
    ('0.4200') depending on the endpoint. Normalise both to cents."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if 0 < f < 1:                 # 0.4200 dollars
        return round(f * 100)
    if 1 <= f <= 100:             # already cents
        return round(f)
    return None


def normalise_side(value):
    """-> 'yes' | 'no' | None. Field names differ across Kalshi endpoints."""
    s = str(value or "").strip().lower()
    if s in ("yes", "y", "long"):
        return "yes"
    if s in ("no", "n", "short"):
        return "no"
    return None


def normalise_trade(raw: dict) -> dict:
    """One API record -> the signal shape follow_runner consumes.

    Deliberately tolerant about field names: the response shape of these
    undocumented routes is unknown until `--probe` is run against a real
    account, and guessing one exact schema would make the module brittle for
    no benefit. Anything we cannot read comes back None and the caller skips.
    """
    if not isinstance(raw, dict):
        return {}
    ticker = _first(raw, "ticker", "market_ticker", "market_id", "market")
    return dict(
        trade_id=_first(raw, "trade_id", "id", "fill_id", "order_id"),
        ticker=ticker,
        # Fallbacks for follow_resolve when no ticker comes back.
        market_title=_first(raw, "event_title", "market_title", "title"),
        outcome_text=_first(raw, "yes_sub_title", "subtitle", "outcome",
                            "side_title"),
        side=normalise_side(_first(raw, "side", "taker_side",
                                   "outcome_side", "taker_outcome_side")),
        price_cents=price_to_cents(_first(
            raw, "price_cents", "price", "yes_price", "yes_price_dollars",
            "avg_price")),
        contracts=_first(raw, "count", "contracts", "quantity", "size",
                         "count_fp"),
        action=str(_first(raw, "action", "taker_action") or "buy").lower(),
        ts=_first(raw, "created_time", "traded_at", "ts", "timestamp"),
        username=USERNAME,
        raw=raw,
    )


def extract_records(payload) -> list:
    """Pull the list of trades out of whatever envelope came back."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("trades", "fills", "results", "data", "positions",
                    "market_positions"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


# ------------------------------- state -------------------------------

def load_state(path: Path = None) -> dict:
    path = path or STATE_PATH
    try:
        data = json.loads(path.read_text())
        data.setdefault("seen", [])
        data.setdefault("seeded", False)
        return data
    except (OSError, ValueError):
        return {"seen": [], "seeded": False}


def save_state(state: dict, path: Path = None) -> None:
    path = path or STATE_PATH
    state["seen"] = list(state.get("seen", []))[-MAX_SEEN:]
    try:
        path.write_text(json.dumps(state))
    except OSError as exc:
        log.warning("feed state write failed: %s", exc)


def trade_key(sig: dict) -> str:
    """Stable identity for a trade. Prefers the API's own id; falls back to
    the tuple that makes two distinct fills distinguishable."""
    if sig.get("trade_id"):
        return str(sig["trade_id"])
    return "|".join(str(sig.get(k) or "") for k in
                    ("ticker", "ts", "side", "price_cents", "contracts"))


# ------------------------------- polling ------------------------------

def poll(client, state: dict = None, path: Path = None) -> list:
    """New trades since the last poll.

    THE COLD-START GUARD, which is the most dangerous thing in this file:
    on the very first run every trade already in his history is recorded as
    seen and NOTHING is returned. He has 123 trades on record; without this,
    arming the copier would fire a hundred-plus simultaneous orders into
    markets that mostly settled weeks ago. The state file makes a restart
    equally safe.
    """
    state = load_state(path) if state is None else state
    payload, mode = fetch(client, TRADES_PATH.format(u=USERNAME))
    records = extract_records(payload)
    if not records:
        log.warning("feed returned no records at all via %s — if he is "
                    "active this means the shape changed", mode)
        return []

    signals = [normalise_trade(r) for r in records]
    seen = set(state.get("seen", []))

    if not state.get("seeded"):
        state["seen"] = [trade_key(s) for s in signals]
        state["seeded"] = True
        state["seeded_at"] = time.time()
        save_state(state, path)
        log.info("COLD START via %s: seeded %d existing trade(s) as already "
                 "seen. Placing nothing this pass — only trades made from "
                 "now on are copied.", mode, len(signals))
        return []

    fresh = [s for s in signals if trade_key(s) not in seen]
    if fresh:
        state["seen"] = list(state.get("seen", [])) + [trade_key(s)
                                                       for s in fresh]
        save_state(state, path)
        log.info("%d new trade(s) from %s via %s", len(fresh), USERNAME, mode)
    return fresh


# -------------------------------- probe -------------------------------

def probe(client) -> int:
    """Answer the two questions that block everything else: which auth works,
    and what the response actually looks like."""
    print(f"Probing Kalshi /v1 routes for {USERNAME!r}\n")
    print(f"  API key configured    : {bool(client.private_key)}")
    print(f"  Session token set     : {bool(SESSION_TOKEN)}\n")

    for label, path in (("trades", TRADES_PATH), ("positions",
                                                  POSITIONS_PATH)):
        p = path.format(u=USERNAME)
        print(f"--- {label}  {p}")
        try:
            payload, mode = fetch(client, p)
        except FeedError as exc:
            print(f"    FAILED: {exc}\n")
            continue
        records = extract_records(payload)
        print(f"    OK via {mode} — {len(records)} record(s)")
        if records:
            print(f"    field names: {sorted(records[0].keys())}")
            print(f"    first record: {json.dumps(records[0])[:400]}")
            print(f"    normalised : {json.dumps({k: v for k, v in normalise_trade(records[0]).items() if k != 'raw'})}")
        print()
    return 0


def main() -> int:
    from config import ConfigError, load_kalshi_settings
    from kalshi_client import KalshiClient
    from trade_logger import setup_logging
    setup_logging()
    try:
        settings = load_kalshi_settings(require_market=False,
                                        require_trading=False)
        client = KalshiClient(settings.kalshi_api_key_id,
                              settings.kalshi_private_key_path,
                              settings.kalshi_env)
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        print("The /v1 routes need credentials even to probe them.")
        return 1

    if "--probe" in sys.argv:
        return probe(client)

    try:
        for sig in poll(client):
            print({k: v for k, v in sig.items() if k != "raw"})
    except FeedError as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
