"""#71 — Tier-2 HTTP fallback resolves the LIVE local HARTOS port.

dispatch.py's Tier-2 fallback used to hardcode its default to
get_port('backend')=6777. In a BUNDLED desktop HARTOS is served
in-process on the Flask port (5000) and the standalone 6777 subprocess
isn't running, so the fallback always hit a dead port. The resolver now
probes the local candidates and uses the first that's listening, without
touching the shared HEVOLVE_BASE_URL (discovery/federation/peer keep it).

Behavioral: mock the port-probe boundary, call the real resolver, assert
the chosen URL.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def dispatch_mod():
    try:
        import integrations.agent_engine.dispatch as d
    except Exception as e:
        pytest.skip(f"dispatch unavailable: {e}")
    return d


def test_env_base_url_wins_verbatim(dispatch_mod, monkeypatch):
    """When HEVOLVE_BASE_URL is set (remote/cloud), it is used verbatim —
    the shared discovery/federation/peer base must not be second-guessed."""
    monkeypatch.setenv('HEVOLVE_BASE_URL', 'http://remote-host:6777')
    # Even if probing would say a local port is live, env still wins.
    monkeypatch.setattr(dispatch_mod, '_is_local_port_listening', lambda *a, **k: True)
    assert dispatch_mod._local_dispatch_base_url() == 'http://remote-host:6777'


def test_bundled_picks_flask_when_backend_dead(dispatch_mod, monkeypatch):
    """Bundled desktop: backend(6777) refused, flask(5000) live → flask."""
    from core.port_registry import get_port
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)
    flask_port = get_port('flask')
    monkeypatch.setattr(
        dispatch_mod, '_is_local_port_listening',
        lambda port, timeout=0.25: int(port) == flask_port)
    assert dispatch_mod._local_dispatch_base_url() == f'http://localhost:{flask_port}'


def test_standalone_picks_backend_when_live(dispatch_mod, monkeypatch):
    """Standalone: backend(6777) is live → backend (probed first, returned)."""
    from core.port_registry import get_port
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)
    backend_port = get_port('backend')
    monkeypatch.setattr(
        dispatch_mod, '_is_local_port_listening',
        lambda port, timeout=0.25: int(port) == backend_port)
    assert dispatch_mod._local_dispatch_base_url() == f'http://localhost:{backend_port}'


def test_coldboot_falls_back_to_backend_when_nothing_listens(dispatch_mod, monkeypatch):
    """Cold boot: neither port answers yet → stable backend default (the
    caller's connection-error path + circuit breaker handle the retry)."""
    from core.port_registry import get_port
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)
    monkeypatch.setattr(
        dispatch_mod, '_is_local_port_listening', lambda *a, **k: False)
    assert dispatch_mod._local_dispatch_base_url() == f'http://localhost:{get_port("backend")}'
