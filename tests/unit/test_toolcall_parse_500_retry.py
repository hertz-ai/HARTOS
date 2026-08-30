"""Behavioural tests for the toolcall-parse-500 retry + self-heal wiring
(#4 from the 2026-05-29 log review).

ROOT BUG (frozen_debug.log): the local model occasionally emits a
malformed-JSON tool call (e.g. trailing `"windows"}.`), llama.cpp
rejects it with HTTP 500 'Failed to parse tool call arguments as JSON'.
The wrapper caught the 500 and returned a generic "rephrase" string —
but the tool call was LOST, contributing to lifecycle FSM churn.

FIX: a malformed tool call is almost always a sampling fluke. The
wrapper now re-samples generate_reply up to 2x (autogen runs temp>0 so
the fluke usually clears) before giving up; on exhaustion it routes to
the canonical self-heal pipeline (report_subsystem_failure) so a
SUSTAINED pattern creates a self_heal goal.

These tests wrap a fake agent whose generate_reply raises the parse-500
N times then succeeds, and assert recovery / fallback / self-heal push.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


_PARSE_500 = (
    "Error code: 500 - {'error': {'code': 500, 'message': "
    "'Failed to parse tool call arguments as JSON: parse error at "
    "line 1, column 435: invalid literal; last read: '\"windows\"}.'"
    "; expected end of input', 'type': 'server_error'}}"
)


class _FakeAgent:
    """Minimal autogen-shaped agent: has generate_reply, no _execute_function."""
    def __init__(self, fail_times, exc_factory):
        self._calls = 0
        self._fail_times = fail_times
        self._exc_factory = exc_factory

    def generate_reply(self, *args, **kwargs):
        self._calls += 1
        if self._calls <= self._fail_times:
            raise self._exc_factory()
        return f"REAL_TOOLCALL_RESULT(call={self._calls})"


def _wrap(agent):
    """Build the wrapper's generate_reply wrapper around a fake agent
    without requiring is_enabled() / the full wrap machinery."""
    from integrations.agent_lightning.wrapper import AgentLightningWrapper
    # __new__ to skip __init__ (which calls get_agent_config + wrapping).
    w = AgentLightningWrapper.__new__(AgentLightningWrapper)
    w.agent = agent
    w.agent_id = 'Assistant'
    w.track_rewards = False
    w.auto_trace = False
    w.tracer = None
    w.reward_calculator = None
    w.execution_count = 0
    return w._wrap_generate_reply(agent.generate_reply)


# ── Recovery: a one-shot fluke clears on the first retry ──────────

def test_recovers_on_retry_after_single_parse_500():
    agent = _FakeAgent(fail_times=1, exc_factory=lambda: RuntimeError(_PARSE_500))
    wrapped = _wrap(agent)
    result = wrapped('hi')
    assert result == "REAL_TOOLCALL_RESULT(call=2)", (
        "a single malformed-JSON fluke must be recovered by re-sampling "
        "— the tool call should NOT be lost")
    assert agent._calls == 2  # 1 fail + 1 successful retry


def test_recovers_on_second_retry():
    agent = _FakeAgent(fail_times=2, exc_factory=lambda: RuntimeError(_PARSE_500))
    wrapped = _wrap(agent)
    result = wrapped('hi')
    assert result == "REAL_TOOLCALL_RESULT(call=3)"
    assert agent._calls == 3  # original + 2 retries, 3rd succeeds


# ── Exhaustion: persistent failure → fallback + self-heal push ────

def test_exhausted_retries_returns_fallback_and_reports_self_heal():
    agent = _FakeAgent(fail_times=99, exc_factory=lambda: RuntimeError(_PARSE_500))
    wrapped = _wrap(agent)

    pushes = []
    from hartos import exception_collector as ec
    with patch.object(ec, 'report_subsystem_failure',
                      side_effect=lambda **kw: pushes.append(kw)):
        result = wrapped('hi')

    # Original + 2 retries = 3 calls, all fail.
    assert agent._calls == 3
    # Graceful fallback string (not the raw 500).
    assert 'rephrase' in result.lower() or 'smaller steps' in result.lower()
    assert '500' not in result
    # Self-heal push fired with the canonical llm.<agent_id> shape.
    assert len(pushes) == 1
    push = pushes[0]
    assert push['subsystem'] == 'llm'
    assert push['identifier'] == 'Assistant'
    assert push['function'] == 'generate_reply'
    assert push['failure_kind'] == 'toolcall_json_parse_500'


# ── A different error on retry stops the retry loop (no masking) ──

def test_different_error_on_retry_stops_and_reports():
    """If the FIRST call is a parse-500 but a retry raises a DIFFERENT
    error, we must stop retrying (don't mask the new failure as a
    parse-500) and fall through to fallback + self-heal."""
    calls = {'n': 0}

    class _MixedAgent:
        agent_id = 'Helper'
        def generate_reply(self, *a, **k):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError(_PARSE_500)
            raise RuntimeError("Error code: 500 - context window exceeded")

    agent = _MixedAgent()
    wrapped = _wrap(agent)
    from hartos import exception_collector as ec
    with patch.object(ec, 'report_subsystem_failure', side_effect=lambda **kw: None):
        result = wrapped('hi')
    # call 1 (parse-500) + call 2 (different 500) → break, no call 3.
    assert calls['n'] == 2
    assert 'rephrase' in result.lower() or 'smaller steps' in result.lower()


# ── Non-parse-500 errors are NOT caught here (must propagate) ─────

def test_non_parse500_error_propagates():
    """A regular exception (not the toolcall-parse-500 signature) must
    propagate — the retry/fallback path is ONLY for the parse-500."""
    agent = _FakeAgent(fail_times=99,
                       exc_factory=lambda: ValueError("totally unrelated bug"))
    wrapped = _wrap(agent)
    with pytest.raises(ValueError, match="totally unrelated bug"):
        wrapped('hi')
    assert agent._calls == 1  # no retries for non-parse-500


# ── Happy path: no error, no retry, no self-heal ──────────────────

def test_happy_path_single_call():
    agent = _FakeAgent(fail_times=0, exc_factory=lambda: RuntimeError(_PARSE_500))
    wrapped = _wrap(agent)
    result = wrapped('hi')
    assert result == "REAL_TOOLCALL_RESULT(call=1)"
    assert agent._calls == 1
