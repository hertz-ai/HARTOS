"""#71 + omni-channel inbound — the live local HARTOS backend URL resolver.

dispatch's Tier-2 fallback AND the channel inbound bridge (flask_integration)
used to hardcode their target to get_port('backend')=6777. In a BUNDLED desktop
HARTOS is served in-process on the Flask port (5000) and the standalone 6777
subprocess isn't running, so both hit a dead port. They now share ONE resolver,
core.port_registry.get_local_backend_url, which probes the live local port.

Behavioral: mock the port-probe boundary, call the real resolver, assert the URL.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def pr():
    import core.port_registry as m
    return m


def test_env_base_url_wins_verbatim(pr, monkeypatch):
    """HEVOLVE_BASE_URL (remote/cloud + shared discovery/federation base) is
    used verbatim, even if a local port would probe live."""
    monkeypatch.setenv('HEVOLVE_BASE_URL', 'http://remote-host:6777')
    monkeypatch.setattr(pr, '_is_port_listening', lambda *a, **k: True)
    assert pr.get_local_backend_url() == 'http://remote-host:6777'


def test_bundled_picks_flask_when_backend_dead(pr, monkeypatch):
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)
    flask_port = pr.get_port('flask')
    monkeypatch.setattr(
        pr, '_is_port_listening',
        lambda port, *a, **k: int(port) == flask_port)
    assert pr.get_local_backend_url() == f'http://localhost:{flask_port}'


def test_standalone_picks_backend_when_live(pr, monkeypatch):
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)
    backend_port = pr.get_port('backend')
    monkeypatch.setattr(
        pr, '_is_port_listening',
        lambda port, *a, **k: int(port) == backend_port)
    assert pr.get_local_backend_url() == f'http://localhost:{backend_port}'


def test_coldboot_falls_back_to_backend(pr, monkeypatch):
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)
    monkeypatch.setattr(pr, '_is_port_listening', lambda *a, **k: False)
    assert pr.get_local_backend_url() == f'http://localhost:{pr.get_port("backend")}'


def test_dispatch_delegates_to_canonical_resolver(monkeypatch):
    """dispatch._local_dispatch_base_url is now a thin delegate — no parallel
    6777-hardcoding path."""
    try:
        import core.port_registry as pr
        import integrations.agent_engine.dispatch as d
    except Exception as e:
        pytest.skip(f"dispatch unavailable: {e}")
    monkeypatch.setattr(pr, 'get_local_backend_url', lambda: 'http://sentinel:1234')
    assert d._local_dispatch_base_url() == 'http://sentinel:1234'
