"""Behavioural tests for the generation-failure retry + self-heal wiring in
AgentLightningWrapper._wrap_generate_reply.

ROOT BUG (frozen_debug.log, 2026-05-29 review): the local model occasionally
emits a malformed-JSON tool call, llama.cpp rejects it with HTTP 500 and the
tool call was LOST, contributing to lifecycle FSM churn.

FIX: a bad sample is almost always a fluke.  The wrapper re-samples
generate_reply up to 2x (autogen runs temp>0 so the fluke usually clears)
before giving up; on exhaustion it routes to the canonical self-heal
pipeline (report_subsystem_failure) so a SUSTAINED pattern creates a
self_heal goal, and returns a plain fallback reply instead of propagating.

The trigger is the OpenAI-compatible exception CLASS every serving engine
speaks (5xx status, or a connection that dropped / timed out), never an
engine's message text: llama.cpp answers 500 when the tool-call text will
not parse, vLLM/TGI/hosted endpoints fail their own way, and a dropped socket
looks the same from here.  4xx is the caller's request (context overflow,
auth) and propagates to its own handlers, as does any non-openai exception.

These tests wrap a fake agent whose generate_reply raises N times then
succeeds, and assert recovery / fallback / self-heal push.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import httpx
import openai
import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


_REQ = httpx.Request('POST', 'http://127.0.0.1:8080/v1/chat/completions')

_PARSE_500_TEXT = (
    "Error code: 500 - {'error': {'code': 500, 'message': "
    "'Failed to parse tool call arguments as JSON: parse error at "
    "line 1, column 435: invalid literal; last read: '\"windows\"}.'"
    "; expected end of input', 'type': 'server_error'}}"
)


def _server_500():
    # The production type: openai's client raises this for any 5xx body.
    return openai.InternalServerError(
        _PARSE_500_TEXT, response=httpx.Response(500, request=_REQ), body=None)


def _connection_dropped():
    return openai.APIConnectionError(request=_REQ)


def _bad_request_400():
    return openai.BadRequestError(
        'context size exceeded', response=httpx.Response(400, request=_REQ),
        body=None)


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
    w.current_span_id = None
    w.execution_count = 0
    return w._wrap_generate_reply(agent.generate_reply)


# ── Classifier: exception class + status range, never message text ─

def test_classifier_uses_class_and_status_not_message_text():
    from integrations.agent_lightning.wrapper import (
        _is_recoverable_generation_failure as rec)
    assert rec(_server_500())
    assert rec(_connection_dropped())
    assert rec(openai.APITimeoutError(request=_REQ))
    assert not rec(_bad_request_400())
    # llama's message text on a plain exception is NOT the trigger.
    assert not rec(RuntimeError(_PARSE_500_TEXT))


# ── Recovery: a one-shot fluke clears on the first retry ──────────

def test_recovers_on_retry_after_single_parse_500():
    agent = _FakeAgent(fail_times=1, exc_factory=_server_500)
    wrapped = _wrap(agent)
    result = wrapped('hi')
    assert result == "REAL_TOOLCALL_RESULT(call=2)", (
        "a single malformed-JSON fluke must be recovered by re-sampling "
        "— the tool call should NOT be lost")
    assert agent._calls == 2  # 1 fail + 1 successful retry


def test_recovers_on_second_retry():
    agent = _FakeAgent(fail_times=2, exc_factory=_server_500)
    wrapped = _wrap(agent)
    result = wrapped('hi')
    assert result == "REAL_TOOLCALL_RESULT(call=3)"
    assert agent._calls == 3  # original + 2 retries, 3rd succeeds


def test_connection_drop_recovers_on_retry():
    # The first failure in the 2026-09-03 live trace was this class; a
    # message-text match on the 500 never reached it.
    agent = _FakeAgent(fail_times=1, exc_factory=_connection_dropped)
    wrapped = _wrap(agent)
    assert wrapped('hi') == "REAL_TOOLCALL_RESULT(call=2)"
    assert agent._calls == 2


# ── Exhaustion: persistent failure → fallback + self-heal push ────

def test_exhausted_retries_returns_fallback_and_reports_self_heal():
    agent = _FakeAgent(fail_times=99, exc_factory=_server_500)
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
    assert push['failure_kind'] == 'generation_5xx'
    assert push['retries_exhausted'] == 2


def test_connection_drop_exhaustion_reports_its_own_kind():
    agent = _FakeAgent(fail_times=99, exc_factory=_connection_dropped)
    wrapped = _wrap(agent)
    pushes = []
    from hartos import exception_collector as ec
    with patch.object(ec, 'report_subsystem_failure',
                      side_effect=lambda **kw: pushes.append(kw)):
        result = wrapped('hi')
    assert agent._calls == 3
    assert 'rephrase' in result.lower() or 'smaller steps' in result.lower()
    assert pushes[0]['failure_kind'] == 'generation_connection'


# ── A different error on retry stops the retry loop (no masking) ──

def test_different_error_on_retry_stops_and_reports():
    """If the FIRST call is a 5xx but a retry raises a DIFFERENT class of
    error, we must stop retrying (don't mask the new failure) and fall
    through to fallback + self-heal."""
    calls = {'n': 0}

    class _MixedAgent:
        agent_id = 'Helper'
        def generate_reply(self, *a, **k):
            calls['n'] += 1
            if calls['n'] == 1:
                raise _server_500()
            raise RuntimeError("Error code: 500 - context window exceeded")

    agent = _MixedAgent()
    wrapped = _wrap(agent)
    from hartos import exception_collector as ec
    with patch.object(ec, 'report_subsystem_failure', side_effect=lambda **kw: None):
        result = wrapped('hi')
    # call 1 (5xx) + call 2 (a plain RuntimeError) → break, no call 3.
    assert calls['n'] == 2
    assert 'rephrase' in result.lower() or 'smaller steps' in result.lower()


# ── Non-recoverable errors are NOT caught here (must propagate) ───

def test_unrelated_exception_propagates():
    """A regular exception (not a 5xx / connection failure) must propagate
    — the retry/fallback path is ONLY for engine-side generation failures."""
    agent = _FakeAgent(fail_times=99,
                       exc_factory=lambda: ValueError("totally unrelated bug"))
    wrapped = _wrap(agent)
    with pytest.raises(ValueError, match="totally unrelated bug"):
        wrapped('hi')
    assert agent._calls == 1  # no retries


def test_4xx_propagates_to_its_own_handlers():
    """4xx is the caller's request (context overflow, auth): the ladder is
    never entered, the exception reaches the handlers that own it."""
    agent = _FakeAgent(fail_times=99, exc_factory=_bad_request_400)
    wrapped = _wrap(agent)
    with pytest.raises(openai.BadRequestError):
        wrapped('hi')
    assert agent._calls == 1


# ── Happy path: no error, no retry, no self-heal ──────────────────

def test_happy_path_single_call():
    agent = _FakeAgent(fail_times=0, exc_factory=_server_500)
    wrapped = _wrap(agent)
    result = wrapped('hi')
    assert result == "REAL_TOOLCALL_RESULT(call=1)"
    assert agent._calls == 1
