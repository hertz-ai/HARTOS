"""Polymarket CLOB adapter — read-only market data is unkeyed,
signed actions (place_order, cancel_order) require a wallet key
from AIKeyVault.

Architecture:

    Market data (public CLOB GET endpoints)
        get_markets()       — list active prediction markets
        get_market(cid)     — single-market detail + current price book

    Wallet-bound (require POLYMARKET_PRIVATE_KEY in AIKeyVault)
        get_balance()       — USDC available on the wallet
        get_open_positions()
        place_order(side, price, size, market_id)
        cancel_order(order_id)

The wallet key is loaded lazily via ``AIKeyVault.get_tool_key`` so
nothing happens at import time.  If py-clob-client is not installed,
methods raise a clear ImportError early — we never silently fall back
to mocks (the goal explicitly bans them).

Per-trade safety gates (the agent layer enforces, not this adapter):
    1. hive_consensus.py vote (3-of-5 minimum)
    2. ConsentOverlayService push to the user's phone
    3. Budget-gate check (no order >5% of wallet)
    4. ImmutableAuditLog entry written BEFORE order POST

This module deliberately stops at "I can sign and submit if asked";
the trader agent is responsible for the four gates above.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger('hevolve.trading.polymarket')

# Polymarket CLOB endpoints — public, read-only data
CLOB_HOST = 'https://clob.polymarket.com'
GAMMA_HOST = 'https://gamma-api.polymarket.com'

# Chain ID for Polygon mainnet (Polymarket runs here)
POLYGON_CHAIN_ID = 137


class PolymarketKeyMissingError(RuntimeError):
    """Raised when a signing action is attempted without a wallet key.

    The agent should catch this, push a consent request to the user
    asking for the key (via the AIKeyVault credential request flow),
    and retry once the key lands.  Never silently downgrade to a mock
    trade — the flywheel goal explicitly forbids mocks.
    """


class PolymarketClientUnavailableError(RuntimeError):
    """Raised when py-clob-client isn't installed.

    Read-only methods still work over plain HTTP, but order
    signing requires the client.  Install with:
        pip install py-clob-client
    """


class PolymarketAdapter:
    """Thin wrapper around py-clob-client + the public Gamma API.

    Construct without arguments for read-only use; the first signing
    call lazily loads the wallet key via AIKeyVault.
    """

    def __init__(self, user_id: str = 'sathish', timeout: float = 10.0):
        self.user_id = user_id
        self.timeout = timeout
        self._client = None  # py-clob-client.ClobClient instance, lazy-loaded
        self._key_loaded = False

    # ─── Public market data (no key required) ──────────────────────

    def get_markets(self, limit: int = 50, active_only: bool = True) -> list:
        """Return active prediction markets from the Gamma API.

        No key required.  This is the discovery surface the trader
        agent uses to pick which markets to research.
        """
        import requests
        params = {'limit': limit}
        if active_only:
            params['active'] = 'true'
            params['closed'] = 'false'
        resp = requests.get(
            f'{GAMMA_HOST}/markets', params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_market(self, condition_id: str) -> dict:
        """Return one market's detail by its condition_id.

        Includes current orderbook (best bid/ask) — what the trader
        agent quotes against when sizing an order.
        """
        import requests
        resp = requests.get(
            f'{GAMMA_HOST}/markets',
            params={'condition_ids': condition_id, 'limit': 1},
            timeout=self.timeout)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            raise ValueError(f'no market with condition_id={condition_id}')
        return rows[0]

    # ─── Wallet-bound (key required) ───────────────────────────────

    def _load_wallet_key(self) -> str:
        """Pull POLYMARKET_PRIVATE_KEY from AIKeyVault.

        Raises PolymarketKeyMissingError if the key is absent or empty.
        The agent's consent flow should catch this and request the key
        from the user via the AIKeyVault credential UI.
        """
        if self._key_loaded:
            # _client is already configured for this session
            return ''
        try:
            from hartos.ai_key_vault import AIKeyVault
            vault = AIKeyVault.get_instance()
            key = vault.get_tool_key('POLYMARKET_PRIVATE_KEY')
        except Exception as e:
            raise PolymarketKeyMissingError(
                f'AIKeyVault unavailable: {e!r}. '
                f'Set POLYMARKET_PRIVATE_KEY via the credential vault.'
            ) from e
        if not key:
            raise PolymarketKeyMissingError(
                'POLYMARKET_PRIVATE_KEY not in AIKeyVault. '
                'Add it via the credential request flow before '
                'any signing action.')
        self._key_loaded = True
        return key

    def _client_or_die(self):
        """Lazy-construct the py-clob-client ClobClient.

        Order matters: check the wallet key FIRST (raises
        PolymarketKeyMissingError), then try to import the SDK
        (raises PolymarketClientUnavailableError if missing).

        The key gate runs first because a missing key is the more
        common failure mode in production — agents hit this before
        the user has supplied their wallet seed.  A missing SDK is
        a deploy / packaging issue that the operator fixes once.
        """
        if self._client is not None:
            return self._client
        key = self._load_wallet_key()
        try:
            from py_clob_client.client import ClobClient
        except ImportError as e:
            raise PolymarketClientUnavailableError(
                'py-clob-client not installed; '
                'pip install py-clob-client'
            ) from e
        self._client = ClobClient(
            host=CLOB_HOST, key=key, chain_id=POLYGON_CHAIN_ID)
        # Bootstrap API creds (ClobClient handles caching internally)
        self._client.set_api_creds(self._client.create_or_derive_api_creds())
        return self._client

    def get_balance(self) -> dict:
        """USDC balance on the Polymarket wallet.

        Returns {'usdc_available': float, 'usdc_locked': float}.
        """
        client = self._client_or_die()
        return client.get_balance_allowance(asset_type='COLLATERAL')

    def get_open_positions(self) -> list:
        """Open orders + outstanding positions."""
        client = self._client_or_die()
        return client.get_orders()

    def place_order(self, *, side: str, price: float, size: float,
                    market_id: str, order_type: str = 'GTC') -> dict:
        """Submit a signed limit order.

        side: 'BUY' or 'SELL'
        price: 0.0–1.0 (probability quote)
        size: shares (whole units of 1 USDC payout)
        market_id: the token_id (NOT condition_id) of the YES or NO side
        order_type: 'GTC' (good-til-cancel, default) or 'FOK'

        Returns the CLOB response dict.  Does NOT enforce consent gates;
        the caller (trader agent) is responsible for the 4 gates listed
        in the module docstring.
        """
        if side not in {'BUY', 'SELL'}:
            raise ValueError(f"side must be BUY or SELL, got {side!r}")
        if not (0.0 < price < 1.0):
            raise ValueError(
                f"price must be 0 < p < 1 (probability), got {price}")
        if size <= 0:
            raise ValueError(f"size must be positive, got {size}")
        client = self._client_or_die()
        from py_clob_client.order_builder.constants import BUY, SELL
        from py_clob_client.clob_types import OrderArgs
        args = OrderArgs(
            price=price, size=size,
            side=(BUY if side == 'BUY' else SELL),
            token_id=market_id)
        signed = client.create_order(args)
        return client.post_order(signed, order_type)

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a previously-placed order by id."""
        client = self._client_or_die()
        return client.cancel(order_id=order_id)


# ─── Module-level singleton (lazy) ─────────────────────────────────

_default_adapter: Optional[PolymarketAdapter] = None


def get_polymarket_adapter(user_id: str = 'sathish') -> PolymarketAdapter:
    """Return the process-wide default PolymarketAdapter.

    Singleton so the wallet key is only loaded from AIKeyVault once
    per process even if many tool calls reach this module.
    """
    global _default_adapter
    if _default_adapter is None or _default_adapter.user_id != user_id:
        _default_adapter = PolymarketAdapter(user_id=user_id)
    return _default_adapter
