"""
Unit tests for the morphable Nunba agent (R2).

Tests cover:
  - task_ledger.TaskLedger as a pure-data record (no decisions).
  - task_ledger.get_or_create singleton behaviour.
  - morphable_agent._match_specialist delegation to agentic_router.
  - morphable_agent._rotation_for_state pure-logic rotation table.
  - morphable_agent.dispatch_morphable_turn end-to-end (heuristic
    fallback path).
  - morphable_agent.is_nunba_conversation handle matching.

Deterministic + offline-friendly.  The live reply path now goes through
the canonical /chat brain (agentic_router.dispatch_via_chat); it is
fail-safe (None on any failure -> heuristic).  An autouse fixture stubs
it to None so the heuristic-path tests run without a real loopback POST,
and TestDispatchLiveReply covers the real-reply wire + its fallbacks.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

from integrations.social import task_ledger, morphable_agent


@pytest.fixture(autouse=True)
def reset_ledger_store():
    task_ledger._LEDGERS.clear()
    yield
    task_ledger._LEDGERS.clear()


@pytest.fixture(autouse=True)
def stub_chat_unreachable():
    # In the unit env there's no /chat server, so the real code's loopback
    # would just fail to None.  Make that deterministic (and avoid a real
    # network attempt) by stubbing dispatch_via_chat -> None, so the
    # heuristic-path tests below exercise the fallback exactly.  Tests that
    # want the live path re-patch this target inside their own `with`.
    with patch('integrations.agentic_router.dispatch_via_chat',
               return_value=None):
        yield


# ── TaskLedger (pure data record) ────────────────────────────────────────

class TestTaskLedger:
    def test_defaults(self):
        led = task_ledger.TaskLedger(user_id='u1')
        assert led.user_id == 'u1'
        assert led.conversation_id == 'nunba'
        assert led.state == 'casual'
        assert led.active_specialist is None
        assert led.history == []

    def test_record_appends(self):
        led = task_ledger.TaskLedger(user_id='u1')
        led.record(state='specialist', prompt='help me',
                   specialist='solar-architect')
        assert led.state == 'specialist'
        assert led.active_specialist == 'solar-architect'
        assert len(led.history) == 1
        assert led.history[0] == {
            'state': 'specialist',
            'prompt': 'help me',
            'specialist': 'solar-architect',
        }

    def test_record_no_decisions(self):
        # Ledger doesn't classify anything — caller passes whatever
        # state it wants and ledger writes it.
        led = task_ledger.TaskLedger(user_id='u1')
        led.record(state='banana', prompt='', specialist=None)
        assert led.state == 'banana'

    def test_history_bounded(self):
        led = task_ledger.TaskLedger(user_id='u1', max_history=3)
        for i in range(10):
            led.record(state='casual', prompt=f"msg {i}")
        assert len(led.history) == 3
        assert led.history[-1]['prompt'] == 'msg 9'

    def test_to_metadata(self):
        led = task_ledger.TaskLedger(user_id='u1')
        led.record(state='specialist', prompt='', specialist='marius')
        assert led.to_metadata() == {
            'state': 'specialist', 'specialist': 'marius'}


class TestGetOrCreate:
    def test_singleton_per_user(self):
        a = task_ledger.get_or_create('u1')
        b = task_ledger.get_or_create('u1')
        assert a is b

    def test_separate_per_conversation(self):
        a = task_ledger.get_or_create('u1', conversation_id='nunba')
        b = task_ledger.get_or_create('u1', conversation_id='nunba:work')
        assert a is not b

    def test_reset(self):
        led = task_ledger.get_or_create('u1')
        led.record(state='specialist', specialist='X')
        task_ledger.reset('u1')
        led2 = task_ledger.get_or_create('u1')
        assert led2.state == 'casual'
        assert led2 is not led


# ── morphable_agent._match_specialist (delegates to agentic_router) ──────

class TestMatchSpecialist:
    def test_returns_none_when_router_unavailable(self):
        # Default test env has no LLM keys → agentic_router falls
        # through to None.  _match_specialist should propagate that.
        result = morphable_agent._match_specialist("hello")
        assert result is None

    def test_returns_none_for_empty(self):
        assert morphable_agent._match_specialist("") is None
        assert morphable_agent._match_specialist(None) is None

    def test_uses_router_when_available(self):
        # Patch find_matching_agent to simulate a successful match.
        with patch('integrations.agentic_router.find_matching_agent') as m:
            m.return_value = {'name': 'solar-architect', 'agent_id': 42}
            assert morphable_agent._match_specialist(
                "anything") == 'solar-architect'
            m.assert_called_once()

    def test_uses_agent_id_when_name_missing(self):
        with patch('integrations.agentic_router.find_matching_agent') as m:
            m.return_value = {'agent_id': 7}
            assert morphable_agent._match_specialist("x") == '7'


# ── Rotation policy ──────────────────────────────────────────────────────

class TestRotationForState:
    def test_casual(self):
        assert morphable_agent._rotation_for_state(
            'casual', '') == morphable_agent.PERSONA_CHAT_INSTRUCTOR

    def test_specialist_marker(self):
        assert morphable_agent._rotation_for_state(
            'specialist', '') == '__specialist__'

    def test_returning(self):
        assert morphable_agent._rotation_for_state(
            'returning',
            'solar-architect') == morphable_agent.PERSONA_CHAT_INSTRUCTOR

    def test_unknown_falls_back(self):
        # Unknown state → chat_instructor (safe default).
        assert morphable_agent._rotation_for_state(
            'whatever', '') == morphable_agent.PERSONA_CHAT_INSTRUCTOR


# ── make_speaker_selection callback ──────────────────────────────────────

class _FakeAgent:
    def __init__(self, name):
        self.name = name


class _FakeGroupChat:
    def __init__(self, names):
        self.agents = [_FakeAgent(n) for n in names]


class TestSpeakerSelection:
    def test_casual_returns_chat_instructor(self):
        led = task_ledger.TaskLedger(user_id='u1')
        select = morphable_agent.make_speaker_selection(led)
        chat = _FakeGroupChat([
            morphable_agent.PERSONA_CHAT_INSTRUCTOR,
            morphable_agent.PERSONA_ASSISTANT,
        ])
        chosen = select(_FakeAgent(''), chat)
        assert chosen.name == morphable_agent.PERSONA_CHAT_INSTRUCTOR

    def test_specialist_routes_to_named(self):
        led = task_ledger.TaskLedger(user_id='u1')
        led.record(state='specialist', specialist='solar-architect')
        select = morphable_agent.make_speaker_selection(led)
        chat = _FakeGroupChat([
            morphable_agent.PERSONA_CHAT_INSTRUCTOR,
            'solar-architect',
        ])
        chosen = select(_FakeAgent('chat_instructor'), chat)
        assert chosen.name == 'solar-architect'

    def test_specialist_missing_falls_back(self):
        led = task_ledger.TaskLedger(user_id='u1')
        led.record(state='specialist', specialist='not-registered')
        select = morphable_agent.make_speaker_selection(led)
        chat = _FakeGroupChat([
            morphable_agent.PERSONA_CHAT_INSTRUCTOR,
        ])
        chosen = select(_FakeAgent(''), chat)
        assert chosen.name == morphable_agent.PERSONA_CHAT_INSTRUCTOR


# ── End-to-end dispatch (heuristic fallback) ─────────────────────────────

class TestDispatchMorphableTurn:
    def test_casual_default(self):
        reply, meta = morphable_agent.dispatch_morphable_turn(
            'u-casual', "hello")
        assert meta['state'] == 'casual'
        assert meta['specialist'] is None
        assert meta['persona'] == morphable_agent.PERSONA_CHAT_INSTRUCTOR
        assert 'chat_instructor' in reply

    def test_specialist_routes_via_router(self):
        with patch('integrations.agentic_router.find_matching_agent') as m:
            m.return_value = {'name': 'solar-architect'}
            reply, meta = morphable_agent.dispatch_morphable_turn(
                'u-spec', "what tilt is Saturn at?")
        assert meta['state'] == 'specialist'
        assert meta['specialist'] == 'solar-architect'
        assert meta['persona'] == 'solar-architect'
        assert 'solar-architect' in reply

    def test_returning_after_specialist(self):
        # First turn matches specialist
        with patch('integrations.agentic_router.find_matching_agent') as m:
            m.return_value = {'name': 'solar-architect'}
            morphable_agent.dispatch_morphable_turn(
                'u-return', "Saturn?")
        # Second turn — router returns no match → we should land in
        # 'returning' (not 'casual') because we were in 'specialist'.
        with patch('integrations.agentic_router.find_matching_agent') as m:
            m.return_value = None
            reply, meta = morphable_agent.dispatch_morphable_turn(
                'u-return', "thanks")
        assert meta['state'] == 'returning'
        assert meta['persona'] == morphable_agent.PERSONA_CHAT_INSTRUCTOR

    def test_ledger_records_each_turn(self):
        morphable_agent.dispatch_morphable_turn('u-led', "first")
        morphable_agent.dispatch_morphable_turn('u-led', "second")
        led = task_ledger.get_or_create('u-led')
        assert len(led.history) == 2


# ── Live reply via the canonical /chat brain (#115) ──────────────────────

class TestDispatchLiveReply:
    """The reply now comes from the canonical /chat brain when reachable
    (agentic_router.dispatch_via_chat); the heuristic is only the fail-safe
    fallback.  These re-patch dispatch_via_chat inside the test, overriding
    the autouse None-stub."""

    def test_uses_chat_reply_when_available(self):
        with patch('integrations.agentic_router.dispatch_via_chat',
                   return_value="Here's the real answer.") as d:
            reply, meta = morphable_agent.dispatch_morphable_turn(
                'u-live', "weather?")
        assert reply == "Here's the real answer."      # real reply, not heuristic
        assert 'chat_instructor' not in reply
        # agent_id=None -> /chat LangChain; owner_id carries the real user so
        # /chat recall reads THEIR history; prompt forwarded verbatim.
        d.assert_called_once_with(None, "weather?", {'owner_id': 'u-live'})

    def test_falls_back_to_heuristic_on_none(self):
        with patch('integrations.agentic_router.dispatch_via_chat',
                   return_value=None):
            reply, _ = morphable_agent.dispatch_morphable_turn(
                'u-none', "hello")
        assert 'chat_instructor' in reply              # None -> heuristic

    def test_falls_back_to_heuristic_on_error(self):
        with patch('integrations.agentic_router.dispatch_via_chat',
                   side_effect=RuntimeError('boom')):
            reply, _ = morphable_agent.dispatch_morphable_turn(
                'u-err', "hello")
        assert 'chat_instructor' in reply              # error -> heuristic, no raise

    def test_empty_chat_reply_falls_back(self):
        # dispatch_via_chat's contract is non-empty-or-None (it strips +
        # nulls blanks itself), but morphable also treats any falsy reply
        # as no-reply -> heuristic.
        with patch('integrations.agentic_router.dispatch_via_chat',
                   return_value=""):
            reply, _ = morphable_agent.dispatch_morphable_turn(
                'u-blank', "hello")
        assert 'chat_instructor' in reply


# ── Conversation handle matching ─────────────────────────────────────────

class TestIsNunbaConversation:
    def test_canonical(self):
        assert morphable_agent.is_nunba_conversation('nunba')

    def test_per_user_variant(self):
        assert morphable_agent.is_nunba_conversation('nunba:u1')
        assert morphable_agent.is_nunba_conversation('nunba:42')

    def test_not_nunba(self):
        assert not morphable_agent.is_nunba_conversation('abc-123')
        assert not morphable_agent.is_nunba_conversation('')
        assert not morphable_agent.is_nunba_conversation(None)
