from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_DIR = Path(__file__).resolve().parent.parent


def build_trading_client(host: str, chain_id: int):
    """
    Authenticated CLOB client (Level 2), matching upstream usage:

        client = ClobClient(host, key=..., chain_id=..., signature_type=..., funder=...)
        client.set_api_creds(client.create_or_derive_api_key())

    See https://docs.polymarket.com/trading/clients/l1 and /l2.
    """
    load_dotenv(_PROJECT_DIR / ".env", override=False)
    from py_clob_client_v2 import ClobClient

    key = os.environ.get("POLYMARKET_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY")
    if not key:
        raise RuntimeError("Set PRIVATE_KEY or POLYMARKET_PRIVATE_KEY in .env or environment.")

    funder = os.environ.get("POLYMARKET_FUNDER")
    if not funder:
        raise RuntimeError("Set POLYMARKET_FUNDER in .env or environment.")

    sig = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
    client = ClobClient(
        host,
        key=key,
        chain_id=chain_id,
        signature_type=sig,
        funder=funder,
    )
    creds = client.create_or_derive_api_key()
    if creds is None:
        raise RuntimeError(
            "Failed to create or derive CLOB API credentials; check private key and chain_id."
        )
    client.set_api_creds(creds)
    return client


def funder_address() -> str:
    load_dotenv(_PROJECT_DIR / ".env", override=False)
    a = os.environ.get("POLYMARKET_FUNDER")
    if not a:
        raise RuntimeError("POLYMARKET_FUNDER is not set.")
    return a
