"""The http_disabled latch must lift once HEVOLVEAI_API_URL appears.

Live incident 2026-08-25 (build 16 desktop): hevolve_social's init
constructed the WorldModelBridge singleton at 19:15:46 with no
HEVOLVEAI_API_URL in the env, latching _http_disabled=True.  The
hevolveai_supervisor exported that env var at 19:15:49 — three seconds
later — so the latch outlived the promise for the whole boot, every
exported delta carried no intelligence_index, and the hive census showed
null for a node whose own :8000 child was answering
/v1/hivemind/intelligence with a real number.  hartos_bootstrap's
"spawn hevolveai BEFORE the agent-engine subsystem" ordering comment is
vacuous under a first-caller-wins singleton.

The fix: get_learning_stats() — reached every federation tick — calls
_maybe_reenable_http(), which adopts the env URL and clears the latch
the first tick after the supervisor's export.  Proven RED before the
fix (the latch stayed True and the method did not exist).
"""
import sys
import types
import pytest


def _quiet_pooled_get(monkeypatch):
    """No real HTTP from the test: every pooled_get raises like a dead server."""
    import requests

    def _refuse(*a, **k):
        raise requests.RequestException('test: no server')

    import integrations.agent_engine.world_model_bridge as wmb
    monkeypatch.setattr(wmb, 'pooled_get', _refuse, raising=False)


def _fresh_bridge(monkeypatch):
    """A bridge constructed with NO env promise, like the 19:15:46 boot."""
    monkeypatch.delenv('HEVOLVEAI_API_URL', raising=False)
    # Keep hart_intelligence out of sys.modules so in-process mode is
    # structurally impossible, matching the live boot's retry outcome.
    monkeypatch.delitem(sys.modules, 'hart_intelligence', raising=False)
    from integrations.agent_engine.world_model_bridge import WorldModelBridge
    bridge = WorldModelBridge()
    # The latch this whole test exists for:
    assert bridge._in_process is False
    assert bridge._http_disabled is True
    return bridge


def test_env_appearing_after_construction_reenables_http(monkeypatch):
    _quiet_pooled_get(monkeypatch)
    bridge = _fresh_bridge(monkeypatch)

    started = []
    monkeypatch.setattr(
        bridge, '_start_inbound_skill_poller', lambda: started.append(1))

    # Supervisor's export lands AFTER construction (the live race).
    monkeypatch.setenv('HEVOLVEAI_API_URL', 'http://localhost:9137')

    bridge.get_learning_stats()

    assert bridge._http_disabled is False
    assert bridge._api_url == 'http://localhost:9137'
    assert started == [1], 'inbound poller must start exactly once on heal'


def test_heal_is_self_extinguishing(monkeypatch):
    _quiet_pooled_get(monkeypatch)
    bridge = _fresh_bridge(monkeypatch)

    started = []
    monkeypatch.setattr(
        bridge, '_start_inbound_skill_poller', lambda: started.append(1))
    monkeypatch.setenv('HEVOLVEAI_API_URL', 'http://localhost:9137')

    bridge.get_learning_stats()
    bridge.get_learning_stats()

    assert started == [1], 'second tick must not re-run the heal'


def test_no_env_stays_disabled(monkeypatch):
    _quiet_pooled_get(monkeypatch)
    bridge = _fresh_bridge(monkeypatch)

    result = bridge.get_learning_stats()

    assert bridge._http_disabled is True
    assert result['hivemind'] == {}
