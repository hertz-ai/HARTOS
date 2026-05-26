"""HARTOS trading integrations — real-money prediction-market adapters.

Currently exports:
    PolymarketAdapter — py-clob-client wrapper for Polymarket CLOB.
    PolymarketKeyMissingError — raised when POLYMARKET_PRIVATE_KEY is
                                absent from AIKeyVault.

All real-money actions gate on hive_consensus.py voting + a per-trade
consent push to the user (ConsentOverlayService).  Never auto-trade
without an explicit human ACK on each order.
"""
from .polymarket_adapter import (
    PolymarketAdapter,
    PolymarketKeyMissingError,
)

__all__ = ['PolymarketAdapter', 'PolymarketKeyMissingError']
