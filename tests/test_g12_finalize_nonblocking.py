"""PERF-3 (audit #565): _g12_finalize must NOT block the reply path.

It used to join the SSM student future with ``.result(timeout=0.5)`` — up to
0.5s of dead latency per LLM iteration for distillation training data that is
never needed to answer the user.  It now attaches a done-callback instead.

These tests pin: (a) it returns immediately for a slow/unsettled future,
(b) it records via the completion callback (not a blocking join) through the
canonical world_model_bridge sink, (c) it no-ops on a missing future / empty
teacher text.
"""
import importlib
import time

import pytest

hie = importlib.import_module('hart_intelligence_entry')


class _FakeFuture:
    """Minimal Future stand-in: records callbacks, fires them on set_result.
    result() is non-blocking (the real callback only runs once settled)."""

    def __init__(self):
        self._cbs = []
        self._result = None
        self._settled = False

    def add_done_callback(self, cb):
        self._cbs.append(cb)
        if self._settled:
            cb(self)

    def set_result(self, value):
        self._result = value
        self._settled = True
        for cb in list(self._cbs):
            cb(self)

    def result(self, timeout=None):
        return self._result


def test_finalize_returns_immediately_for_unsettled_future():
    fut = _FakeFuture()  # never settles → old code would have blocked 0.5s
    t0 = time.time()
    hie._g12_finalize('prompt', 'teacher text', fut)
    assert time.time() - t0 < 0.05, 'must not block the reply path'
    assert len(fut._cbs) == 1, 'must register a done-callback, not join'


def test_finalize_noop_on_none_future():
    hie._g12_finalize('prompt', 'teacher', None)  # must not raise


def test_finalize_noop_on_empty_teacher():
    fut = _FakeFuture()
    hie._g12_finalize('prompt', '', fut)
    assert fut._cbs == [], 'no callback when there is no teacher text to pair'


def test_finalize_records_pair_when_student_completes(monkeypatch):
    wmb = pytest.importorskip('integrations.agent_engine.world_model_bridge')
    captured = {}

    class _Bridge:
        def record_teacher_student_pair(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(wmb, 'get_world_model_bridge', lambda: _Bridge())
    fut = _FakeFuture()
    hie._g12_finalize('the prompt', 'teacher answer', fut)
    assert not captured, 'nothing recorded until the student future settles'
    fut.set_result({'response': 'student answer', 'action_tensor': None})
    assert captured.get('prompt') == 'the prompt'
    assert captured.get('teacher_response') == 'teacher answer'
    assert captured.get('student_response') == 'student answer'
