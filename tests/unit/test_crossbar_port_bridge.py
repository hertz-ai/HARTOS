"""Omni-channel bridge: crossbar port must resolve to 8088, never 0.

The channel WAMP bridge's default crossbar URL came from get_port('crossbar'),
but 'crossbar' was missing from the port registry, so get_port returned 0 and
the bridge's default URL became ws://localhost:0/ws — it could never reach the
WAMP router (the bridge silently never connected).  'crossbar' is now a
registry entry (8088, the canonical CBURL default), so the bridge works.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_crossbar_in_port_registry_is_8088():
    from core.port_registry import get_port
    assert get_port('crossbar') == 8088, (
        "crossbar must resolve to 8088 (the canonical CBURL default); "
        "0 means it's missing from the registry and the bridge URL is :0")


def test_crossbar_env_override(monkeypatch):
    from core.port_registry import get_port
    monkeypatch.setenv('HART_CROSSBAR_PORT', '9999')
    assert get_port('crossbar') == 9999


def test_bridge_from_env_default_url_is_8088(monkeypatch):
    try:
        from integrations.channels.bridge.wamp_bridge import BridgeConfig
    except Exception as e:
        pytest.skip(f"wamp_bridge unavailable: {e}")
    monkeypatch.delenv('CBURL', raising=False)
    cfg = BridgeConfig.from_env()
    assert ':0/ws' not in cfg.crossbar_url, (
        f"bridge default URL still points at port 0: {cfg.crossbar_url}")
    assert cfg.crossbar_url == 'ws://localhost:8088/ws'


def test_bridge_cburl_override_still_wins(monkeypatch):
    try:
        from integrations.channels.bridge.wamp_bridge import BridgeConfig
    except Exception as e:
        pytest.skip(f"wamp_bridge unavailable: {e}")
    monkeypatch.setenv('CBURL', 'wss://router.example:8445/wss')
    cfg = BridgeConfig.from_env()
    assert cfg.crossbar_url == 'wss://router.example:8445/wss'
