"""Behavioural tests for the 2026-05-29 yield-gate feedback-loop fix.

ROOT BUG (from frozen_debug.log): the agent_daemon dispatched goals via
the in-process Tier-1 path (dispatch_goal → hevolve_chat → chat()),
which stamped mark_user_chat_activity() UNCONDITIONALLY.  That re-armed
the 10-min user-activity cooldown on every dispatch, so
should_yield_to_user() read "user active" forever — the normal daemon
tick always yielded and only the 120s STARVATION OVERRIDE ever ran
(live evidence: yield gate blocked ~85821s / ~24h, 1142 overrides).
Net effect: the flywheel daemon never ran its normal path → "84 goals,
0 progress for days".

FIX: dispatch.is_genuine_user_request(request_id) — the canonical
discriminator.  chat() only stamps user activity for genuine user
requests; the daemon's own dispatches (request_id='daemon_<goal>')
do NOT stamp, so the gate clears after the real cooldown.

These tests pin the discriminator + the gate's clear-after-cooldown
behaviour against the real module globals (no mocks of the unit under
test).
"""
from __future__ import annotations

import os
import sys

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── is_genuine_user_request discriminator ─────────────────────────

@pytest.mark.parametrize('request_id,expected', [
    ('daemon_goal_20260529_abc', False),   # agent_daemon dispatch
    ('daemon_95dfbf02', False),            # daemon goal id
    ('c4b09d1f-1f09-40b3-9d39', True),     # genuine user request (uuid)
    ('req_user_123', True),                # genuine user request
    ('', False),                           # untagged → BACKGROUND/abortable (live 2026-06-17: empty is dominated by daemon calls that lost their tag, not users)
    (None, False),                         # None → background (same); inbound /chat keeps its own fail-open in mark_view._chat_request_is_genuine
])
def test_is_genuine_user_request(request_id, expected):
    from integrations.agent_engine.dispatch import is_genuine_user_request
    assert is_genuine_user_request(request_id) is expected


# ── The feedback loop is broken: daemon stamps don't re-arm the gate ──

def test_daemon_dispatch_does_not_keep_gate_stuck(monkeypatch):
    """Simulate the exact loop: repeated daemon dispatches must NOT
    keep should_yield_to_user() stuck-True.  A genuine user turn DOES
    arm the gate; once the cooldown passes the gate clears even if the
    daemon keeps dispatching."""
    from integrations.agent_engine import dispatch as d

    # Control time so we don't sleep 10 minutes in the test.
    fake_now = {'t': 1_000_000.0}
    monkeypatch.setattr(d._time, 'time', lambda: fake_now['t'])
    # Reset module globals to a clean baseline (gate clear).
    monkeypatch.setattr(d, '_last_user_chat_at', 0.0)
    monkeypatch.setattr(d, '_active_create_sessions', 0)

    # Simulate the chat() guard: stamp ONLY for genuine user requests.
    def simulate_chat(request_id):
        if d.is_genuine_user_request(request_id):
            d.mark_user_chat_activity()

    # 1. Daemon dispatches 5 times in a row (30s apart) — pre-fix this
    #    re-armed the cooldown every time. Post-fix: no stamp.
    for i in range(5):
        simulate_chat(f'daemon_goal_{i}')
        fake_now['t'] += 30
    assert d.is_user_recently_active() is False, (
        "daemon dispatches must NOT mark the user active — pre-fix this "
        "was True forever and the gate never cleared")

    # 2. A genuine user turn arms the gate.
    simulate_chat('real-user-uuid')
    assert d.is_user_recently_active() is True

    # 3. Daemon keeps dispatching while the cooldown is live — the gate
    #    stays True (correct: the user IS recently active), but the
    #    daemon stamps add nothing.
    for i in range(3):
        simulate_chat(f'daemon_goal_{i}')
        fake_now['t'] += 30
    assert d.is_user_recently_active() is True

    # 4. Advance past the cooldown with ONLY daemon dispatches — the
    #    gate MUST clear (pre-fix it never did).
    fake_now['t'] += d._USER_CHAT_COOLDOWN + 1
    simulate_chat('daemon_goal_final')
    assert d.is_user_recently_active() is False, (
        "after the cooldown passes and only daemon dispatches occurred, "
        "the gate MUST clear so the daemon's normal tick can run")


def test_create_session_counter_still_gates(monkeypatch):
    """The _active_create_sessions counter still forces yield while ANY
    create is in flight (daemon-initiated included — creates are
    LLM-heavy and must not pile up).  The counter is self-balancing, so
    once the create ends AND the cooldown passes, the gate clears."""
    from integrations.agent_engine import dispatch as d
    monkeypatch.setattr(d, '_last_user_chat_at', 0.0)
    monkeypatch.setattr(d, '_active_create_sessions', 0)
    fake_now = {'t': 2_000_000.0}
    monkeypatch.setattr(d._time, 'time', lambda: fake_now['t'])

    assert d.is_user_recently_active() is False
    # Daemon-initiated create: counter gates while in flight, but does
    # NOT stamp the user-activity timestamp.
    d.mark_create_start(request_id='daemon_goal_x')
    try:
        assert d.is_user_recently_active() is True  # counter > 0
    finally:
        d.mark_create_end()
    # Counter back to 0 AND no timestamp stamp (daemon) → gate clears
    # immediately, no cooldown wait.
    assert d.is_user_recently_active() is False, (
        "daemon-initiated create must not leave the gate armed after it "
        "finishes — the counter balances and no timestamp was stamped")


def test_genuine_user_create_stamps_timestamp(monkeypatch):
    """A genuine user CREATE re-arms the cooldown (correct: the user IS
    active) — so the gate stays True for the full cooldown after the
    create finishes, unlike the daemon case above."""
    from integrations.agent_engine import dispatch as d
    monkeypatch.setattr(d, '_last_user_chat_at', 0.0)
    monkeypatch.setattr(d, '_active_create_sessions', 0)
    fake_now = {'t': 3_000_000.0}
    monkeypatch.setattr(d._time, 'time', lambda: fake_now['t'])

    d.mark_create_start(request_id='real-user-uuid')
    d.mark_create_end()
    # Counter is 0 but the genuine-user timestamp keeps the gate armed.
    assert d.is_user_recently_active() is True
    # ...until the cooldown passes.
    fake_now['t'] += d._USER_CHAT_COOLDOWN + 1
    assert d.is_user_recently_active() is False
