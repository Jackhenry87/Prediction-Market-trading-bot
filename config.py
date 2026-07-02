"""Loads and validates all configuration from .env.

Every module gets its settings from here — nothing else reads os.environ.
The private key is deliberately kept out of __repr__/logs.
"""

import os
from dataclasses import dataclass, field

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
    # repr=False so the key can never leak via print(settings) or logging
    private_key: str = field(repr=False, default="")
    signature_type: int = 0
    funder_address: str = ""
    clob_api_url: str = "https://clob.polymarket.com"
    polygon_rpc_url: str = "https://polygon-rpc.com"
    chain_id: int = 137
    market_token_id: str = ""
    dry_run: bool = True
    kill_switch: bool = False
    max_order_size: float = 0.0
    max_total_exposure: float = 0.0
    # Kalshi
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
    kalshi_env: str = "demo"
    market_ticker: str = ""
    max_order_pct: float = 4.0    # max buy as % of bankroll (cash+positions)
    min_order_pct: float = 1.0    # skip trades smaller than this % of bankroll
    take_profit_pct: float = 50.0  # auto-sell target: entry cost +50%


def load_settings(require_market: bool = True) -> Settings:
    signature_type = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
    if signature_type not in (0, 1, 2):
        raise ConfigError("POLY_SIGNATURE_TYPE must be 0, 1 or 2")

    funder_address = os.getenv("POLY_FUNDER_ADDRESS", "").strip()
    if signature_type in (1, 2) and not funder_address:
        raise ConfigError(
            "POLY_FUNDER_ADDRESS is required when POLY_SIGNATURE_TYPE is 1 or 2 "
            "(the proxy wallet address shown on your polymarket.com profile)."
        )

    return Settings(
        private_key=_require("POLYGON_WALLET_PRIVATE_KEY"),
        signature_type=signature_type,
        funder_address=funder_address,
        clob_api_url=os.getenv("CLOB_API_URL", "https://clob.polymarket.com").strip(),
        polygon_rpc_url=os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com").strip(),
        chain_id=int(os.getenv("CHAIN_ID", "137")),
        market_token_id=_require("MARKET_TOKEN_ID") if require_market
        else os.getenv("MARKET_TOKEN_ID", "").strip(),
        dry_run=_bool("DRY_RUN", default=True),
        kill_switch=_bool("KILL_SWITCH", default=False),
        max_order_size=_positive_float("MAX_ORDER_SIZE"),
        max_total_exposure=_positive_float("MAX_TOTAL_EXPOSURE"),
    )


def load_kalshi_settings(require_market: bool = True) -> Settings:
    """Settings for the Kalshi scripts. The Polymarket wallet key is NOT
    required here — only Kalshi API credentials plus the shared safety rails.
    """
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
        max_order_pct=max_pct,
        min_order_pct=min_pct,
        take_profit_pct=float(os.getenv("TAKE_PROFIT_PCT", "50")),
        market_ticker=_require("MARKET_TICKER") if require_market
        else os.getenv("MARKET_TICKER", "").strip(),
        dry_run=_bool("DRY_RUN", default=True),
        kill_switch=_bool("KILL_SWITCH", default=False),
        max_order_size=_positive_float("MAX_ORDER_SIZE"),
        max_total_exposure=_positive_float("MAX_TOTAL_EXPOSURE"),
    )
