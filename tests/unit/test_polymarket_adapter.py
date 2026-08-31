"""Tests for the Polymarket adapter scaffold (task #203).

The adapter is the trading half of the master flywheel goal
(95dfbf02 — generate $1000 real Polymarket revenue). These tests
pin the load contract:

  * Read-only market-data methods work without a wallet key
    (they hit the public Gamma API).
  * Every signing action raises PolymarketKeyMissingError when
    POLYMARKET_PRIVATE_KEY is absent — NEVER a silent mock.
    The "no mocks" rule is non-negotiable per user directive.
  * The adapter never imports py-clob-client at module load
    time; the import is lazy so the read-only path works on
    machines without the SDK.

The full happy path (real signed order) cannot be tested without
real Polygon funds; CI runs the unkeyed branches only.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


# ─── Import contract — module loads cleanly with no key set ────────

def test_module_imports_without_key():
    """Importing the adapter must NOT touch AIKeyVault or py-clob-client.

    A fresh import on a key-less machine should succeed.  If a future
    refactor moves the AIKeyVault import to the top of the module,
    this test fails before that refactor ships.
    """
    import importlib
    import sys
    sys.modules.pop('integrations.trading', None)
    sys.modules.pop('integrations.trading.polymarket_adapter', None)
    mod = importlib.import_module('integrations.trading.polymarket_adapter')
    assert hasattr(mod, 'PolymarketAdapter')
    assert hasattr(mod, 'PolymarketKeyMissingError')
    assert hasattr(mod, 'PolymarketClientUnavailableError')


def test_adapter_construct_does_not_load_key():
    """Constructing the adapter is a no-op for AIKeyVault.

    The wallet key is loaded LAZILY on the first signing call.
    This means an agent can hold an adapter handle even before
    the user supplies the key — the call site decides when to
    fail loudly.
    """
    from integrations.trading import PolymarketAdapter
    adapter = PolymarketAdapter()
    assert adapter._client is None
    assert adapter._key_loaded is False


# ─── Public-data path — works without key (mocked HTTP) ────────────

def test_get_markets_does_not_require_key():
    """get_markets() hits the public Gamma API; no key needed."""
    from integrations.trading import PolymarketAdapter
    adapter = PolymarketAdapter()
    fake_response = MagicMock()
    fake_response.json.return_value = [
        {'condition_id': 'abc', 'question': 'Will X happen?',
         'active': True, 'closed': False},
    ]
    fake_response.raise_for_status = MagicMock()
    with patch('requests.get', return_value=fake_response) as mock_get:
        markets = adapter.get_markets(limit=10)
    assert mock_get.called
    call_args = mock_get.call_args
    assert 'gamma-api.polymarket.com' in call_args[0][0]
    assert call_args[1]['params']['limit'] == 10
    assert markets[0]['question'] == 'Will X happen?'


def test_get_market_raises_on_unknown_id():
    """If Gamma returns empty list, raise ValueError with the id."""
    from integrations.trading import PolymarketAdapter
    adapter = PolymarketAdapter()
    fake_response = MagicMock()
    fake_response.json.return_value = []
    fake_response.raise_for_status = MagicMock()
    with patch('requests.get', return_value=fake_response):
        with pytest.raises(ValueError, match='no market with condition_id='):
            adapter.get_market('bogus_cid')


# ─── Signing path — must raise PolymarketKeyMissingError ───────────

def test_get_balance_raises_without_key():
    """get_balance triggers _client_or_die → _load_wallet_key.

    With no POLYMARKET_PRIVATE_KEY in AIKeyVault, the call must raise
    PolymarketKeyMissingError — NOT return a mock value, NOT silently
    return 0.0. The 'no mocks' rule is non-negotiable.
    """
    from integrations.trading import (
        PolymarketAdapter, PolymarketKeyMissingError,
    )
    adapter = PolymarketAdapter()

    fake_vault = MagicMock()
    fake_vault.get_tool_key.return_value = ''  # explicitly empty
    fake_ai_key_vault = MagicMock()
    fake_ai_key_vault.get_instance.return_value = fake_vault

    with patch.dict('sys.modules',
                    {'hartos.ai_key_vault': MagicMock(
                        AIKeyVault=fake_ai_key_vault)}):
        with pytest.raises(PolymarketKeyMissingError,
                           match='POLYMARKET_PRIVATE_KEY'):
            adapter.get_balance()


def test_place_order_raises_without_key():
    from integrations.trading import (
        PolymarketAdapter, PolymarketKeyMissingError,
    )
    adapter = PolymarketAdapter()

    fake_vault = MagicMock()
    fake_vault.get_tool_key.return_value = None
    fake_ai_key_vault = MagicMock()
    fake_ai_key_vault.get_instance.return_value = fake_vault

    with patch.dict('sys.modules',
                    {'hartos.ai_key_vault': MagicMock(
                        AIKeyVault=fake_ai_key_vault)}):
        with pytest.raises(PolymarketKeyMissingError):
            adapter.place_order(
                side='BUY', price=0.55, size=10.0,
                market_id='0xabc...')


def test_get_open_positions_raises_without_key():
    from integrations.trading import (
        PolymarketAdapter, PolymarketKeyMissingError,
    )
    adapter = PolymarketAdapter()
    fake_vault = MagicMock()
    fake_vault.get_tool_key.return_value = ''
    fake_ai_key_vault = MagicMock()
    fake_ai_key_vault.get_instance.return_value = fake_vault
    with patch.dict('sys.modules',
                    {'hartos.ai_key_vault': MagicMock(
                        AIKeyVault=fake_ai_key_vault)}):
        with pytest.raises(PolymarketKeyMissingError):
            adapter.get_open_positions()


# ─── place_order input validation (no key/network needed) ──────────

def test_place_order_rejects_invalid_side():
    from integrations.trading import PolymarketAdapter
    adapter = PolymarketAdapter()
    with pytest.raises(ValueError, match='side must be BUY or SELL'):
        adapter.place_order(side='LONG', price=0.5,
                            size=1.0, market_id='x')


def test_place_order_rejects_price_outside_probability_range():
    from integrations.trading import PolymarketAdapter
    adapter = PolymarketAdapter()
    for bad_price in [0.0, 1.0, -0.1, 1.5, 100]:
        with pytest.raises(ValueError, match='price must be'):
            adapter.place_order(side='BUY', price=bad_price,
                                size=1.0, market_id='x')


def test_place_order_rejects_nonpositive_size():
    from integrations.trading import PolymarketAdapter
    adapter = PolymarketAdapter()
    for bad_size in [0, -1, -0.5]:
        with pytest.raises(ValueError, match='size must be positive'):
            adapter.place_order(side='BUY', price=0.5,
                                size=bad_size, market_id='x')


# ─── Singleton ─────────────────────────────────────────────────────

def test_get_polymarket_adapter_caches_per_user():
    from integrations.trading.polymarket_adapter import (
        get_polymarket_adapter,
    )
    a = get_polymarket_adapter(user_id='sathish')
    b = get_polymarket_adapter(user_id='sathish')
    assert a is b
    # Different user_id forces a fresh adapter (different key namespace)
    c = get_polymarket_adapter(user_id='other')
    assert c is not a


# ─── Structural: no module-level import of py-clob-client ──────────

def test_module_does_not_import_py_clob_client_at_load():
    """py-clob-client must NOT be imported at module load — that's
    what allows the read-only path to work on machines without the SDK.
    """
    import ast
    import pathlib
    src = pathlib.Path(
        'integrations/trading/polymarket_adapter.py').read_text()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = (
                node.module if isinstance(node, ast.ImportFrom)
                else node.names[0].name
            )
            assert mod and 'py_clob_client' not in mod, (
                f'top-level import of py_clob_client at line {node.lineno} '
                f'— must be lazy inside _client_or_die to keep the '
                f'read-only path working without the SDK')
