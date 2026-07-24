"""check_health.learning_active reflects real throughput, not a live pointer.

2026-07-24 audit (docs/audit/ux_degrading_design_choices_2026-07-24.md #1.3): the
product-core learning pipeline reported ``learning_active: True`` whenever the
in-process provider object was non-null, or on any HTTP 200 -- a hardcoded literal.
The 50-batch flush buffer and the local-model 0-spark trap both left the bridge
"healthy + learning_active" while nothing reached HevolveAI (buffering into the
void). ``/status`` and the dashboard therefore showed the pipeline green while it
learned nothing.

The fix keeps ``healthy`` for REACHABILITY but derives ``learning_active`` from a
completed flush within an active window, and exposes the raw counters + buffer depth
so the void is visible.

Behavioural: constructs the REAL WorldModelBridge, drives the real ``check_health``
via the in-process branch, and asserts the verdict tracks actual flush activity.
Imports clean (no full-app boot).

Run (dev box, targeted):
    python -m pytest tests/unit/test_world_model_learning_active.py -v \
        --noconftest -p no:cacheprovider
"""
import time

import pytest

from integrations.agent_engine.world_model_bridge import WorldModelBridge


def _bridge():
    # Force the in-process branch of check_health (provider non-null) so we exercise
    # the real code path without a live HevolveAI.
    b = WorldModelBridge()
    b._in_process = True
    b._provider = object()
    return b


def test_reachable_but_never_flushed_is_not_learning():
    b = _bridge()
    b._last_flush_at = None
    h = b.check_health()
    assert h['healthy'] is True            # the pipeline is reachable
    assert h['learning_active'] is False   # ...but nothing has reached the core
    assert h['total_flushed'] == 0
    assert h['last_flush_age_seconds'] is None


def test_learning_active_true_after_a_recent_flush():
    b = _bridge()
    b._last_flush_at = time.time()
    h = b.check_health()
    assert h['learning_active'] is True


def test_learning_active_false_when_flush_is_stale():
    b = _bridge()
    b._active_window_s = 60
    b._last_flush_at = time.time() - 600   # 10 min ago, well past the window
    h = b.check_health()
    assert h['learning_active'] is False
    assert h['last_flush_age_seconds'] >= 590


def test_buffering_into_the_void_is_visible_not_hidden():
    # The exact trap: experiences recorded + buffered, but ZERO flushed. Old code
    # reported learning_active True; now the void is visible and honest.
    b = _bridge()
    b._last_flush_at = None
    b._stats['total_recorded'] = 40
    b._stats['total_flushed'] = 0
    for i in range(40):
        b._experience_queue.append({'i': i})
    h = b.check_health()
    assert h['learning_active'] is False
    assert h['total_recorded'] == 40
    assert h['total_flushed'] == 0
    assert h['experiences_buffered'] == 40   # buffering into the void -> visible


def test_flush_stamps_last_flush_at_making_learning_active_true():
    # End-to-end of the signal: a real successful in-process flush stamps the
    # timestamp, which flips learning_active True. Uses a stub provider whose
    # create_chat_completion succeeds.
    b = _bridge()

    class _StubProvider:
        def create_chat_completion(self, **_kw):
            return {'ok': True}

    b._provider = _StubProvider()
    assert b.check_health()['learning_active'] is False  # nothing flushed yet
    # One batch through the real flush path.
    b._flush_to_world_model([{'prompt': 'p', 'response': 'r', 'user_id': 'u'}])
    h = b.check_health()
    assert h['learning_active'] is True
    assert h['total_flushed'] >= 1
    assert h['last_flush_age_seconds'] is not None
