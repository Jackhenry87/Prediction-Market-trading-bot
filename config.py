"""Loads and validates all configuration from .env.

Every module gets its settings from here — nothing else reads os.environ.
The private key is deliberately kept out of __repr__/logs.
"""

import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


class ConfigError(Exception):
    """Raised when .env is missing or contains invalid values."""


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"Missing required setting {name!r}. Copy .env.example to .env "
            f"and fill it in (see README)."
        )
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw == "":
        return default
    if raw in ("true", "1", "yes", "on"):
        return True
    if raw in ("false", "0", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _positive_float(name: str) -> float:
    raw = _require(name)
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if value <= 0:
        raise ConfigError(f"{name} must be > 0, got {value}")
    return value


@dataclass
class Settings:
    dry_run: bool = True
    kill_switch: bool = False
    max_order_size: float = 0.0
    max_total_exposure: float = 0.0
    # Kalshi
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
    kalshi_env: str = "demo"
    market_ticker: str = ""
    odds_api_key: str = ""        # the-odds-api.com key (sports model)
    fred_api_key: str = ""        # FRED key (macro resolution-lag model)
    max_order_pct: float = 4.0    # max buy as % of bankroll (cash+positions)
    min_order_pct: float = 1.0    # skip trades smaller than this % of bankroll
    take_profit_pct: float = 50.0  # auto-sell target: entry cost +50%
    trade_min_price: float = 60.0  # only buy contracts priced in this band
    trade_max_price: float = 90.0  # (cents) — avoids near-locks & longshots
    enabled_models: tuple = ("weather", "sports", "crypto", "commodities")
    max_theme_pct: float = 40.0   # cap total exposure to any one theme
    #                               (weather/crypto/sports/commodities) so a
    #                               pile of correlated bets can't become one
    #                               oversized wager


@dataclass
class DashboardSettings:
    """Config for the read-only web dashboard (dashboard/). Credentials are
    OPTIONAL — without them the dashboard replays trade_history.csv instead
    of polling the live account. It can never place orders either way."""
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
    kalshi_private_key_pem: str = ""   # PEM text alternative to the path
    kalshi_env: str = "demo"
    password: str = ""          # empty = no login gate (local use)
    poll_seconds: int = 20      # live-mode poll cadence


def _normalize_pem(pem: str) -> str:
    """Repair PEM text whose newlines got mangled by an env-var editor
    (a very common hosted-deploy failure): rebuild the standard header /
    64-char body lines / footer. Already-valid PEM passes through intact."""
    m = re.match(r"\s*-----BEGIN ([A-Z0-9 ]+)-----(.+)-----END \1-----\s*$",
                 pem, re.S)
    if not m:
        return pem
    label, body = m.group(1), re.sub(r"\s+", "", m.group(2))
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return (f"-----BEGIN {label}-----\n" + "\n".join(lines)
            + f"\n-----END {label}-----\n")


def load_dashboard_settings() -> DashboardSettings:
    env = os.getenv("KALSHI_ENV", "demo").strip().lower()
    if env not in ("demo", "prod"):
        raise ConfigError("KALSHI_ENV must be 'demo' or 'prod'")

    key_id = os.getenv("KALSHI_API_KEY_ID", "").strip()
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip()
    # Hosts like Render/Actions store the key as secret TEXT, not a file —
    # accept either. PEM text wins when both are set.
    key_pem = _normalize_pem(os.getenv("KALSHI_PRIVATE_KEY_PEM", "").strip())
    if key_path and not key_pem and not os.path.isfile(key_path):
        raise ConfigError(
            f"KALSHI_PRIVATE_KEY_PATH points to {key_path!r} but no such "
            f"file exists."
        )

    raw_poll = os.getenv("DASHBOARD_POLL_SECONDS", "20").strip()
    try:
        poll_seconds = int(raw_poll)
    except ValueError:
        raise ConfigError(
            f"DASHBOARD_POLL_SECONDS must be an integer, got {raw_poll!r}"
        ) from None
    if poll_seconds < 5:
        raise ConfigError("DASHBOARD_POLL_SECONDS must be >= 5 (rate limits)")

    return DashboardSettings(
        kalshi_api_key_id=key_id,
        kalshi_private_key_path=key_path,
        kalshi_private_key_pem=key_pem,
        kalshi_env=env,
        password=os.getenv("DASHBOARD_PASSWORD", "").strip(),
        poll_seconds=poll_seconds,
    )


def load_kalshi_settings(require_market: bool = True) -> Settings:
    """Settings for the Kalshi scripts: API credentials plus the shared
    safety rails (DRY_RUN, KILL_SWITCH, exposure caps)."""
    env = os.getenv("KALSHI_ENV", "demo").strip().lower()
    if env not in ("demo", "prod"):
        raise ConfigError("KALSHI_ENV must be 'demo' or 'prod'")

    key_path = _require("KALSHI_PRIVATE_KEY_PATH")
    if not os.path.isfile(key_path):
        raise ConfigError(
            f"KALSHI_PRIVATE_KEY_PATH points to {key_path!r} but no such file "
            f"exists. Download the .pem key file from your Kalshi API settings "
            f"and put its path here."
        )

    max_pct = float(os.getenv("MAX_ORDER_PCT", "4"))
    min_pct = float(os.getenv("MIN_ORDER_PCT", "1"))
    if not 0 < min_pct <= max_pct <= 100:
        raise ConfigError("Need 0 < MIN_ORDER_PCT <= MAX_ORDER_PCT <= 100")

    return Settings(
        kalshi_api_key_id=_require("KALSHI_API_KEY_ID"),
        kalshi_private_key_path=key_path,
        kalshi_env=env,
        odds_api_key=os.getenv("ODDS_API_KEY", "").strip(),
        fred_api_key=os.getenv("FRED_API_KEY", "").strip(),
        max_order_pct=max_pct,
        min_order_pct=min_pct,
        take_profit_pct=float(os.getenv("TAKE_PROFIT_PCT", "50")),
        trade_min_price=float(os.getenv("TRADE_MIN_PRICE", "60")),
        trade_max_price=float(os.getenv("TRADE_MAX_PRICE", "90")),
        enabled_models=tuple(
            m.strip().lower()
            for m in os.getenv(
                "ENABLED_MODELS", "weather,sports,crypto,commodities").split(",")
            if m.strip()
        ),
        max_theme_pct=float(os.getenv("MAX_THEME_PCT", "40")),
        market_ticker=_require("MARKET_TICKER") if require_market
        else os.getenv("MARKET_TICKER", "").strip(),
        dry_run=_bool("DRY_RUN", default=True),
        kill_switch=_bool("KILL_SWITCH", default=False),
        max_order_size=_positive_float("MAX_ORDER_SIZE"),
        max_total_exposure=_positive_float("MAX_TOTAL_EXPOSURE"),
    )
