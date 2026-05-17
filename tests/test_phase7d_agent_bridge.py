"""Phase 7d.B — AgentVoiceBridge surface tests.

Plan reference: sunny-gliding-eich.md, Part E.12 + Part W.

Crypto + LiveKit SDK integration land in a follow-up; this file
locks the bridge LIFECYCLE contract:
  - attach_agent spins a worker idempotently per (call, agent) pair.
  - detach_agent stops the worker; idempotent on missing.
  - list_active filters by call_id.
  - shutdown_all kills every worker.
  - Worker thread is daemon (process exit is unblocked).
  - Worker survives transient _tick() exceptions without dying.
"""
from __future__ import annotations

import os
import sys
import time

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def cleanup_bridges():
    """Always kill any bridges left behind by a test."""
    yield
    from integrations.social.agent_voice_bridge import AgentVoiceBridge
    AgentVoiceBridge.shutdown_all()


def test_attach_agent_spawns_worker():
    from integrations.social.agent_voice_bridge import AgentVoiceBridge
    result = AgentVoiceBridge.attach_agent(
        db=None, call_id='c1', agent_id='a1', owner_id='owner1',
        scope={'can_voice': True})
    assert result['call_id'] == 'c1'
    assert result['agent_id'] == 'a1'
    assert result['alive'] is True
    bridges = AgentVoiceBridge.list_active(call_id='c1')
    assert len(bridges) == 1


def test_attach_agent_idempotent_on_pair():
    """Re-attaching the same (call, agent) returns the existing
    worker — no second thread spun, no duplicate row in list_active."""
    from integrations.social.agent_voice_bridge import AgentVoiceBridge
    a1 = AgentVoiceBridge.attach_agent(
        db=None, call_id='c1', agent_id='a1', owner_id='o',
        scope={'can_voice': True})
    a2 = AgentVoiceBridge.attach_agent(
        db=None, call_id='c1', agent_id='a1', owner_id='o',
        scope={'can_voice': True})
    assert a1['started_at'] == a2['started_at']
    assert len(AgentVoiceBridge.list_active(call_id='c1')) == 1


def test_attach_agent_validates_required_args():
    from integrations.social.agent_voice_bridge import (
        AgentVoiceBridge, AgentBridgeError)
    with pytest.raises(AgentBridgeError):
        AgentVoiceBridge.attach_agent(
            db=None, call_id='', agent_id='a', owner_id='o', scope={})
    with pytest.raises(AgentBridgeError):
        AgentVoiceBridge.attach_agent(
            db=None, call_id='c', agent_id='', owner_id='o', scope={})


def test_detach_agent_idempotent_on_missing():
    from integrations.social.agent_voice_bridge import AgentVoiceBridge
    # No worker → False
    assert AgentVoiceBridge.detach_agent('c1', 'a1') is False
    AgentVoiceBridge.attach_agent(
        db=None, call_id='c1', agent_id='a1', owner_id='o',
        scope={'can_voice': True})
    assert AgentVoiceBridge.detach_agent('c1', 'a1') is True
    # Already gone → False
    assert AgentVoiceBridge.detach_agent('c1', 'a1') is False


def test_list_active_filters_by_call():
    from integrations.social.agent_voice_bridge import AgentVoiceBridge
    AgentVoiceBridge.attach_agent(
        db=None, call_id='c1', agent_id='a1', owner_id='o',
        scope={'can_voice': True})
    AgentVoiceBridge.attach_agent(
        db=None, call_id='c2', agent_id='a2', owner_id='o',
        scope={'can_voice': True})
    assert len(AgentVoiceBridge.list_active(call_id='c1')) == 1
    assert len(AgentVoiceBridge.list_active(call_id='c2')) == 1
    assert len(AgentVoiceBridge.list_active()) == 2


def test_shutdown_all_kills_every_worker():
    from integrations.social.agent_voice_bridge import AgentVoiceBridge
    AgentVoiceBridge.attach_agent(
        db=None, call_id='c1', agent_id='a1', owner_id='o',
        scope={'can_voice': True})
    AgentVoiceBridge.attach_agent(
        db=None, call_id='c2', agent_id='a2', owner_id='o',
        scope={'can_voice': True})
    n = AgentVoiceBridge.shutdown_all()
    assert n == 2
    assert AgentVoiceBridge.list_active() == []


def test_worker_thread_is_daemon():
    """Process-exit safety: bridge threads MUST be daemons so a
    crashed agent doesn't keep the python process alive."""
    from integrations.social.agent_voice_bridge import AgentVoiceBridge
    AgentVoiceBridge.attach_agent(
        db=None, call_id='c1', agent_id='a1', owner_id='o',
        scope={'can_voice': True})
    bridges = AgentVoiceBridge.list_active()
    # Reach into the worker via the module dict to verify
    from integrations.social import agent_voice_bridge as avb
    worker = avb._ACTIVE_WORKERS[('c1', 'a1')]
    assert worker._thread is not None
    assert worker._thread.daemon is True


def test_worker_survives_transient_tick_exception(monkeypatch):
    """Plan W invariant: a transient bridge tick failure doesn't
    crash the worker — it's logged and the loop continues.  Verify
    by monkeypatching _tick to raise once, then checking the worker
    is still alive after a few cycles."""
    from integrations.social.agent_voice_bridge import (
        AgentVoiceBridge, AgentBridgeWorker)
    raised = {'count': 0}
    original_tick = AgentBridgeWorker._tick

    def crashing_tick(self):
        if raised['count'] < 1:
            raised['count'] += 1
            raise RuntimeError("simulated transient error")
        return original_tick(self)

    monkeypatch.setattr(AgentBridgeWorker, '_tick', crashing_tick)
    # Tighten the tick interval for this test so we don't wait
    # forever to observe survival.
    import integrations.social.agent_voice_bridge as avb
    monkeypatch.setattr(avb, '_WORKER_TICK_S', 0.01)
    AgentVoiceBridge.attach_agent(
        db=None, call_id='c1', agent_id='a1', owner_id='o',
        scope={'can_voice': True})
    time.sleep(0.1)  # let the worker tick several times
    bridges = AgentVoiceBridge.list_active()
    assert len(bridges) == 1
    assert bridges[0]['alive'] is True
    assert raised['count'] >= 1  # the crash actually fired
