"""Shared construction of the authenticated Polymarket CLOB client."""

from py_clob_client.client import ClobClient

from trade_logger import get_logger

log = get_logger("clob")


def build_client(settings) -> ClobClient:
    """Create an authenticated CLOB client (L1 wallet auth + derived L2 API creds)."""
    kwargs = {
        "key": settings.private_key,
        "chain_id": settings.chain_id,
    }
    if settings.signature_type in (1, 2):
        kwargs["signature_type"] = settings.signature_type
        kwargs["funder"] = settings.funder_address

    client = ClobClient(settings.clob_api_url, **kwargs)

    # Derives (or creates on first run) L2 API credentials by signing with the
    # wallet key.
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    log.info(
        "Authenticated to CLOB at %s as wallet %s (signature_type=%s)",
        settings.clob_api_url,
        client.get_address(),
        settings.signature_type,
    )
    return client
