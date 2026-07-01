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
    chain_id: int = 137
    market_token_id: str = ""
    dry_run: bool = True
    kill_switch: bool = False
    max_order_size: float = 0.0
    max_total_exposure: float = 0.0


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
        chain_id=int(os.getenv("CHAIN_ID", "137")),
        market_token_id=_require("MARKET_TOKEN_ID") if require_market
        else os.getenv("MARKET_TOKEN_ID", "").strip(),
        dry_run=_bool("DRY_RUN", default=True),
        kill_switch=_bool("KILL_SWITCH", default=False),
        max_order_size=_positive_float("MAX_ORDER_SIZE"),
        max_total_exposure=_positive_float("MAX_TOTAL_EXPOSURE"),
    )
