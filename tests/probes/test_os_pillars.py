"""HART OS pillar proofs — falsifiable probes that each load-bearing pillar HOLDS.

This is the "claim-validation ledger" as executable tests: one (or more) probe per
OS pillar, each importing the REAL code and asserting an observable truth that
must hold IFF the pillar bears load.  A pillar that's "erected" but cracked fails
its probe.  Runtime-only proofs (robot hardware, multi-node hive, booted ISO) are
marked and assert the strongest CONTRACT provable off-target.

Pillars:
  P1 generated/liquid UI (A2UI)        P5 learns-you / you-own-it
  P2 ambient co-presence (sees you)    P6 one fabric (cross-OS apps)
  P3 humans hold the wheel             P7 embodiment-native
  P4 composable (proven: test_shell_route_no_collision)
                                       P8 hive-native
"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ════════════════════════════════════════════════════════════════════
# P3 — Humans hold the wheel: the audit trail is the backbone.
# It must round-trip-verify (regression for #48) AND be tamper-evident.
# ════════════════════════════════════════════════════════════════════

def _fake_social_models():
    """Fake integrations.social.models that reproduces the REAL #48 condition:
    AuditLogEntry.created_at defaults to a FRESH datetime.utcnow() when not
    explicitly provided — exactly the model's `Column(DateTime, default=
    datetime.utcnow)` firing at flush.  If the fix didn't pass created_at, the
    stored value differs from the hashed one and verify_chain breaks."""
    store: list = []

    class _Col:           # class-level column stand-in so AuditLogEntry.id.desc() works
        def desc(self): return self
        def asc(self): return self

    class FakeEntry:
        id = _Col()
        def __init__(self, **kw):
            store.append(self)
            self.id = len(store)
            for k, v in kw.items():
                setattr(self, k, v)
            if not hasattr(self, 'created_at') or self.created_at is None:
                self.created_at = datetime.utcnow()  # the model default, at flush

    class _Q:
        def __init__(self, rows): self._rows = rows
        def order_by(self, *a, **k): return self
        def first(self): return self._rows[-1] if self._rows else None
        def limit(self, n): return self
        def all(self): return list(self._rows)

    class FakeDB:
        def query(self, model): return _Q(store)
        def add(self, e): pass          # FakeEntry self-appends
        def commit(self): pass
        def rollback(self): pass
        def close(self): pass

    m = types.ModuleType('integrations.social.models')
    m.AuditLogEntry = FakeEntry
    m.get_db = lambda: FakeDB()
    m._store = store
    return m


def _audit_db_mode(monkeypatch):
    monkeypatch.setitem(sys.modules, 'integrations.social.models', _fake_social_models())
    from security.immutable_audit_log import ImmutableAuditLog
    a = ImmutableAuditLog()
    a._use_db = True  # force the DB path — where #48 lived
    return a


def test_p3_audit_chain_roundtrips_through_db(monkeypatch):
    a = _audit_db_mode(monkeypatch)
    a.log_event('state_change', actor_id='user_1', action='completed task 5')
    a.log_event('tool_call', actor_id='agent_2', action='ran get_weather',
                detail={'city': 'Pune', 'api_key': 'sk-secret'})
    ok, reason = a.verify_chain()
    assert ok, f"P3: audit chain must verify clean on DB round-trip — got: {reason}"


def test_p3_audit_is_tamper_evident(monkeypatch):
    a = _audit_db_mode(monkeypatch)
    a.log_event('auth', actor_id='user_1', action='login')
    a.log_event('security', actor_id='user_1', action='granted admin')
    sys.modules['integrations.social.models']._store[0].action = 'login as root'
    ok, reason = a.verify_chain()
    assert not ok, "P3: tampering with an entry MUST be detected"
    assert 'broken' in reason.lower()


def test_p3_audit_redacts_secrets(monkeypatch):
    """Humans-in-control also means secrets never land in the trail."""
    a = _audit_db_mode(monkeypatch)
    a.log_event('tool_call', actor_id='a', action='x',
                detail={'api_key': 'sk-live-123', 'city': 'Pune'})
    store = sys.modules['integrations.social.models']._store
    assert 'sk-live-123' not in (store[-1].detail_json or '')
    assert 'Pune' in store[-1].detail_json
