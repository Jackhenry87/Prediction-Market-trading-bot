"""Current USDC exposure: value of held positions plus USDC reserved by
open BUY orders. Used to enforce MAX_TOTAL_EXPOSURE.

If exposure cannot be determined (API down, unexpected response), raises
ExposureError — callers must treat that as "reject the order" (fail closed),
never as "assume zero".
"""

import requests
from py_clob_client.clob_types import OpenOrderParams

from trade_logger import get_logger

log = get_logger("exposure")

DATA_API = "https://data-api.polymarket.com"


class ExposureError(Exception):
    """Exposure could not be determined; the caller must fail closed."""


def _open_buy_order_notional(client) -> float:
    orders = client.get_orders(OpenOrderParams())
    total = 0.0
    for order in orders or []:
        if str(order.get("side", "")).upper() != "BUY":
            continue
        original = float(order.get("original_size", 0) or 0)
        matched = float(order.get("size_matched", 0) or 0)
        price = float(order.get("price", 0) or 0)
        total += max(original - matched, 0.0) * price
    return total


def _position_value(address: str) -> float:
    resp = requests.get(
        f"{DATA_API}/positions", params={"user": address}, timeout=15
    )
    resp.raise_for_status()
    positions = resp.json()
    total = 0.0
    for pos in positions or []:
        value = pos.get("currentValue")
        if value is None:
            value = float(pos.get("size", 0) or 0) * float(pos.get("curPrice", 0) or 0)
        total += float(value or 0)
    return total


def current_exposure_usdc(client, settings) -> float:
    """Positions are held by the funder (proxy) address for signature types
    1/2, and by the wallet itself for type 0."""
    address = settings.funder_address or client.get_address()
    try:
        open_orders = _open_buy_order_notional(client)
        positions = _position_value(address)
    except Exception as exc:
        raise ExposureError(f"could not determine current exposure: {exc}") from exc

    total = open_orders + positions
    log.info(
        "Current exposure: %.2f USDC (open BUY orders %.2f + positions %.2f) "
        "for address %s",
        total, open_orders, positions, address,
    )
    return total
