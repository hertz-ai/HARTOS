"""Every background agent stops between steps while a person is using the
computer, and resumes where it stopped once they are idle (#129, reworked
per the review of 96a9ca9f8, #139).

Owner ruling 2026-10-04 (verbatim): "yield to user shd not be just applicable
for VLM loop but for autogen agents as well , proper pause all agents, and
resume restore all states idempotent and proper resume for daemon goals when
user is actually idle, not multiple user idles paths".

MEASURED 2026-10-04 (gui_app.log): the gate dispatch.should_yield_to_user was
CLOSED on user_present 19:12:55-19:20:56 while coding-daemon turn
daemon_d69d24f8 ran autogen rounds straight through it, holding the single
local-LLM permit.  Nothing asked between the rounds of a running turn.

The review of 96a9ca9f8 (2026-10-05 05:33Z) measured four defects in the
first cut; each has a class below.
  * Blocker 5: the between-steps question re-asked the TICK gate, so a daemon
    CREATE turn was paused by its own create_in_flight marker, or by a
    governor in ACTIVE mode with nobody at the desk, and the daemon
    re-dispatched it every tick.  Now there is ONE definition of "a person is
    using this computer" (dispatch.person_using_the_machine).  The tick
    gate's person reasons read it, and the between-steps question reads only
    it.
  * Blocker 3: the pause mark lived on the session's task and outlived the
    turn, so a person's own turn got 'Paused for the user'.  Now the mark
    belongs to the turn (the thread running it), every exit of the turn
    loops reads it, and a turn begins with none.
  * Blocker 4: consumers recorded a paused turn as finished.  A parallel
    subtask was COMPLETED, an instruction was completed, and one-shot
    dispatches and the /chat autonomous first dispatch answered as if the
    work had been done.  Now dispatch_goal holds it (no response, a transient
    reason) and every consumer puts the work back for later.
  * Blocker 2: tests of daemon turns read the live desk.  They now declare an
    idle desktop (tests/conftest.py idle_desktop) or patch the one predicate.
"""
import ast
import functools
import io
import json
import logging
import os
import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip('autogen', reason='autogen not installed')

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
for _p in (_ROOT, os.path.join(_ROOT, 'agent-ledger-opensource')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import agent_tools  # noqa: E402
from core import constants as C  # noqa: E402
from hartos import create_recipe as cr  # noqa: E402
from hartos import helper as h  # noqa: E402
from hartos.helper import Action  # noqa: E402
from hartos.lifecycle_hooks import get_action_state  # noqa: E402
from hartos.threadlocal import thread_local_data as tld  # noqa: E402
from integrations.agent_engine import dispatch  # noqa: E402
# The real-closure and cached-session fixtures of the gate tests.
from tests.unit.test_create_gate_stops_the_chat import (  # noqa: E402,F401
    POSTED, selector, session,
)
# The context a real dispatch_goal call runs in (its gates stubbed).
from tests.unit.test_failed_turn_is_not_work import _gates, _through_tier1  # noqa: E402
from tests.unit.module_swap import swap_modules  # noqa: E402

DAEMON_RID = 'daemon_goal-129'


@pytest.fixture(autouse=True)
def _no_pause_pending():
    """Every test begins and ends with no pause pending on its thread."""
    h.clear_pause_request()
    yield
    h.clear_pause_request()


@pytest.fixture
def as_daemon():
    prev = tld.get_request_id()
    tld.set_request_id(DAEMON_RID)
    yield
    tld.set_request_id(prev or '')


@pytest.fixture
def as_user():
    prev = tld.get_request_id()
    tld.set_request_id('req-user-129')
    yield
    tld.set_request_id(prev or '')


def _person(reason):
    """Who the one definition says is using this computer right now."""
    return patch.object(dispatch, 'person_using_the_machine', return_value=reason)


def _a_round_the_selector_ends(*_a, **_kw):
    """What a round looks like when its speaker selector asked first and the
    person is at the computer: the selector ends it before any model call."""
    h.yield_between_rounds([])


# ─── One definition of the person ─────────────────────────────────────────────

class TestOnePersonDefinition:
    """dispatch.person_using_the_machine answers "is a person using this
    computer right now?" for the tick gate and for every step."""

    def test_a_request_being_served_is_the_person(self):
        with patch('core.foreground.foreground_active', return_value=True):
            assert dispatch.person_using_the_machine() == 'foreground_request'

    def test_input_the_live_monitor_saw_is_the_person(self):
        gov = SimpleNamespace(user_present=lambda: True)
        with patch('core.foreground.foreground_active', return_value=False), \
                patch('core.resource_governor.get_governor', return_value=gov):
            assert dispatch.person_using_the_machine() == 'user_present'

    def test_nobody(self):
        gov = SimpleNamespace(user_present=lambda: False)
        with patch('core.foreground.foreground_active', return_value=False), \
                patch('core.resource_governor.get_governor', return_value=gov):
            assert dispatch.person_using_the_machine() is None

    def test_a_mock_governor_cannot_vouch_for_a_person(self):
        """A MagicMock's user_present() is truthy but is not True."""
        with patch('core.foreground.foreground_active', return_value=False), \
                patch('core.resource_governor.get_governor', return_value=MagicMock()):
            assert dispatch.person_using_the_machine() is None

    def test_unreadable_signals_are_nobody(self):
        with patch('core.foreground.foreground_active', side_effect=RuntimeError('fg')), \
                patch('core.resource_governor.get_governor', side_effect=RuntimeError('gov')):
            assert dispatch.person_using_the_machine() is None

    def test_the_tick_gate_names_the_person_from_the_same_answer(self, monkeypatch):
        """should_yield_to_user's person reasons are this answer, in the same
        order (a request in flight before chat activity before presence) and
        under the same labels."""
        monkeypatch.setattr(dispatch, '_last_yield_reason', None)
        monkeypatch.setattr(dispatch, '_active_create_sessions', 0)
        monkeypatch.setattr(dispatch, '_last_user_chat_at', 0.0)
        monkeypatch.setattr(dispatch, '_user_chat_marker_recent', lambda: False)
        with _person('user_present'):
            assert dispatch.should_yield_to_user() is True
            assert dispatch.get_last_yield_reason() == 'user_present'
        monkeypatch.setattr(dispatch, '_active_create_sessions', 1)
        with _person('foreground_request'):
            assert dispatch.should_yield_to_user() is True
            assert dispatch.get_last_yield_reason() == 'foreground_request'
        with _person('user_present'):
            dispatch.should_yield_to_user()
            assert dispatch.get_last_yield_reason() == 'create_in_flight'

    def test_a_person_at_the_desk_is_never_an_idle_machine(self):
        """The daemon's starvation override admits work only on an idle
        machine (gate_closed_for_the_person), so a turn it admits is never
        paused by its first step for 'user_present'.  That holds because the
        monitor, the only writer of MODE_IDLE, never picks it from a sample
        that saw input, and that sample is what user_present() reports."""
        from core.resource_governor import MODE_IDLE, ResourceGovernor
        gov = ResourceGovernor()
        for ext_cpu in (0.0, 0.5, 0.99):
            for mem in (0.0, 0.5, 0.99):
                for level, on_battery in ((1.0, False), (0.5, True), (0.01, True)):
                    assert gov._target_mode_for(
                        False, ext_cpu, mem, level, on_battery) != MODE_IDLE


# ─── The one question between steps ───────────────────────────────────────────

class TestTheOneQuestion:

    def test_a_background_turn_yields_only_while_a_person_uses_the_machine(self, as_daemon):
        for who in ('foreground_request', 'user_present'):
            with _person(who):
                assert dispatch.background_work_must_yield() is True, who
        with _person(None):
            assert dispatch.background_work_must_yield() is False

    def test_a_user_turn_never_yields_to_its_own_owner(self, as_user):
        with _person('user_present'):
            assert dispatch.background_work_must_yield() is False

    def test_a_daemon_turn_is_not_paused_by_its_own_create_or_a_busy_cpu(
            self, as_daemon, monkeypatch):
        """Review of 96a9ca9f8, blocker 5 (measured): with nobody at the desk
        under external load, the tick admitted a daemon CREATE turn, and its
        first step paused it on the create_in_flight its own
        mark_create_start had set and on a governor in ACTIVE mode, the
        governor's starting state.  The daemon then re-dispatched it every
        tick.  New work still waits for the tick; running work is paused
        only for a person."""
        busy_nobody = SimpleNamespace(user_present=lambda: False,
                                      get_mode=lambda: 'active',
                                      get_throttle=lambda: 0.05)
        monkeypatch.setattr(dispatch, '_last_yield_reason', None)
        with patch('core.foreground.foreground_active', return_value=False), \
                patch('core.resource_governor.get_governor', return_value=busy_nobody):
            dispatch.mark_create_start(DAEMON_RID)
            try:
                assert dispatch.should_yield_to_user() is True
                assert dispatch.background_work_must_yield() is False
            finally:
                dispatch.mark_create_end()

    def test_a_raising_check_never_stops_work(self, as_daemon):
        with patch.object(dispatch, 'person_using_the_machine',
                          side_effect=RuntimeError('broke')):
            assert dispatch.background_work_must_yield() is False


class TestWhetherThePersonNeedsTheMachine:
    """dispatch.gate_closed_for_the_person: the starvation override's own
    judgement (a person, or a machine the idle detector says is busy)."""

    def test_a_request_in_flight_is_the_person(self):
        with patch('core.foreground.foreground_active', return_value=True), \
                patch.object(dispatch, 'machine_is_idle', return_value=True):
            assert dispatch.gate_closed_for_the_person() is True

    def test_the_foreground_reason_is_the_person(self):
        with patch('core.foreground.foreground_active', return_value=False), \
                patch.object(dispatch, 'get_last_yield_reason', return_value='foreground_request'), \
                patch.object(dispatch, 'machine_is_idle', return_value=True):
            assert dispatch.gate_closed_for_the_person() is True

    def test_an_idle_machine_with_no_request_is_not_the_person(self):
        with patch('core.foreground.foreground_active', return_value=False), \
                patch.object(dispatch, 'get_last_yield_reason', return_value='user_active'), \
                patch.object(dispatch, 'machine_is_idle', return_value=True):
            assert dispatch.gate_closed_for_the_person() is False

    def test_a_busy_machine_is_the_person(self):
        with patch('core.foreground.foreground_active', return_value=False), \
                patch.object(dispatch, 'get_last_yield_reason', return_value='user_present'), \
                patch.object(dispatch, 'machine_is_idle', return_value=False):
            assert dispatch.gate_closed_for_the_person() is True

    def test_an_unreadable_governor_fails_closed_as_not_idle(self):
        from core import resource_governor
        with patch.object(resource_governor, 'get_governor',
                          side_effect=RuntimeError('no governor')):
            assert dispatch.machine_is_idle() is False
        from integrations.agent_engine import agent_daemon
        with patch.object(dispatch, 'machine_is_idle', return_value=False):
            assert agent_daemon._idle_only_blocked({'idle_only': True}) is True
        with patch.object(dispatch, 'machine_is_idle', return_value=True):
            assert agent_daemon._idle_only_blocked({'idle_only': True}) is False
        assert agent_daemon._idle_only_blocked({}) is False


class TestThePauseReply:

    def test_the_reply_is_recognised_by_its_prefix_and_is_not_a_help_pause(self):
        reply = agent_tools.user_pause_reply(2)
        assert reply.startswith(C.PAUSED_FOR_USER_REPLY_PREFIX)
        assert 'step 2' in reply
        assert agent_tools.is_user_pause(reply)
        assert agent_tools.is_user_pause('  ' + reply)
        assert not agent_tools.is_help_pause(reply)
        assert not agent_tools.is_action_error_reply(reply)
        assert not agent_tools.is_user_pause(f'{C.HELP_PAUSED_REPLY_PREFIX} step 1 ...')
        assert not agent_tools.is_user_pause('Done.')
        assert not agent_tools.is_user_pause(None)


# ─── The turn's mark ──────────────────────────────────────────────────────────

class TestTheTurnsMark:

    def test_a_yield_marks_this_turn_and_is_idempotent(self, as_daemon):
        with _person('user_present'):
            assert h.yield_between_rounds() is True
            assert h.pause_requested() is True
            assert h.yield_between_rounds() is True     # again: same mark, same answer
            assert h.pause_requested() is True

    def test_nobody_or_a_user_turn_marks_nothing(self, as_daemon):
        with _person(None):
            assert h.yield_between_rounds() is False
        assert h.pause_requested() is False
        tld.set_request_id('req-user-129')
        with _person('user_present'):
            assert h.yield_between_rounds() is False
        assert h.pause_requested() is False

    def test_the_mark_belongs_to_the_turn_that_asked(self, as_daemon):
        """Another turn of the same session runs on another thread (a
        person's own /chat): it never sees this turn's mark."""
        with _person('user_present'):
            assert h.yield_between_rounds() is True
        seen = []
        t = threading.Thread(target=lambda: seen.append(h.pause_requested()))
        t.start()
        t.join(10)
        assert seen == [False]
        assert h.pause_requested() is True

    def test_a_turn_begins_with_no_pause_pending(self, as_daemon):
        with _person('user_present'):
            h.yield_between_rounds()
        h.clear_pause_request()
        assert h.pause_requested() is False

    def test_never_between_a_tool_call_and_its_execution(self, as_daemon):
        pending = [{'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'c1', 'type': 'function',
                                    'function': {'name': 'x', 'arguments': '{}'}}]}]
        with _person('user_present'):
            assert h.yield_between_rounds(pending) is False
            assert h.pause_requested() is False
            assert h.yield_between_rounds(pending + [
                {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'}]) is True
            assert h.pause_requested() is True


class TestTheCreateChatPauses:

    def test_the_real_selector_ends_the_round_for_a_background_turn(self, selector, as_daemon):
        s = selector
        instructor = MagicMock()
        instructor.name = 'ChatInstructor'
        group = SimpleNamespace(messages=[POSTED], agents=[])
        with _person('user_present'):
            assert s.select(instructor, group) is None
            assert h.pause_requested() is True
            assert s.select(instructor, group) is None        # idempotent
        h.clear_pause_request()
        with _person(None):
            assert s.select(instructor, group) is not None    # the round goes on
        assert h.pause_requested() is False

    def test_a_tool_call_is_executed_before_the_round_pauses(self, selector, as_daemon):
        """Owner 2026-10-04: never yield between a tool call and its execution.

        A pause that ends the round while the last message is an unanswered
        tool call strands that call: the turn that resumes finds it orphaned
        (the 'Processing a tool now' state of 2026-10-04 18:28).  The round
        goes on until the call has its answer, and pauses at the NEXT choice.
        """
        s = selector
        helper_agent = MagicMock()
        helper_agent.name = 'Helper'
        # Measured shape (gui_app.log 2026-10-05 05:21): a tool call's
        # content is a string, '' or the model's preamble, never None.
        call = {'role': 'assistant', 'name': 'Helper', 'content': '',
                'tool_calls': [{'id': 'call_yield_1', 'type': 'function',
                                'function': {'name': 'save_data_in_memory',
                                             'arguments': '{"key": "k"}'}}]}
        group = SimpleNamespace(messages=[POSTED, call], agents=[])
        with _person('user_present'):
            assert s.select(helper_agent, group) is not None, (
                'the round ended with the tool call unanswered')
            assert h.pause_requested() is False
            group.messages.append({'role': 'tool', 'name': 'Executor',
                                   'content': 'saved',
                                   'tool_responses': [{'tool_call_id': 'call_yield_1',
                                                       'role': 'tool', 'content': 'saved'}]})
            executor = MagicMock()
            executor.name = 'Executor'
            assert s.select(executor, group) is None, 'the answered call did not pause'
            assert h.pause_requested() is True

    def test_a_user_turn_is_never_paused_by_the_selector(self, selector, as_user):
        s = selector
        instructor = MagicMock()
        instructor.name = 'ChatInstructor'
        with _person('user_present'):
            assert s.select(instructor, SimpleNamespace(messages=[POSTED], agents=[])) is not None
        assert h.pause_requested() is False


@pytest.fixture
def turn_entry():
    """What recipe() reads before the loop, for a cached session."""
    with patch.object(cr, 'agent_data', {456: {}}), \
            patch.object(cr, 'scheduler_check', {}):
        yield


class TestTheCreateTurnAnswersForThePause:

    def test_a_pause_this_turn_made_is_answered_and_keeps_every_state(self, session, as_daemon):
        s = session
        s.task._needs_user_input_action_id = None
        s.task._needs_user_input_kind = None
        s.agents_object['user'].initiate_chat.side_effect = _a_round_the_selector_ends
        before = get_action_state(s.up, 1)
        with _person('user_present'):
            reply = cr.get_response_group(s.user_id, 'Build the agent now', s.prompt_id)
        assert agent_tools.is_user_pause(reply), reply
        assert get_action_state(s.up, 1) == before
        assert cr.messages[s.up] is s.group_chat.messages

    def test_a_turn_while_the_person_is_still_there_pauses_again_and_moves_nothing(
            self, session, as_daemon, turn_entry):
        """Resume is idempotent: the goal's next dispatch, if the person is
        still there, gets the same answer and changes no state."""
        s = session
        s.task._needs_user_input_action_id = None
        s.task._needs_user_input_kind = None
        s.agents_object['user'].initiate_chat.side_effect = _a_round_the_selector_ends
        before = get_action_state(s.up, 1)
        with _person('user_present'):
            first = cr.recipe(s.user_id, 'Build the agent now', s.prompt_id, None, DAEMON_RID)
            again = cr.recipe(s.user_id, 'Build the agent now', s.prompt_id, None, DAEMON_RID)
        assert agent_tools.is_user_pause(first), first
        assert agent_tools.is_user_pause(again), again
        assert get_action_state(s.up, 1) == before

    def test_a_mark_an_earlier_turn_left_is_not_this_turns(self, session, as_daemon, turn_entry):
        """Review of 96a9ca9f8, blocker 3 (measured): a genuine user turn got
        back 'Paused for the user: ...', because a daemon turn on the same
        thread had set the mark after its last read.  A turn begins with no
        pause pending."""
        s = session
        with _person('user_present'):
            assert h.yield_between_rounds() is True     # the earlier daemon turn's late mark
        tld.set_request_id('req-user-139')              # the person's own turn, same thread
        reply = cr.recipe(s.user_id, 'ok, a philosophical one', s.prompt_id, None, 'req-user-139')
        assert not agent_tools.is_user_pause(reply), reply
        assert reply == cr._needs_input_reply(1, 'Respond to user')


# ─── The REUSE turn ───────────────────────────────────────────────────────────

_R_USER = 'reuse-pause-u1'
_R_PROMPT = 4343
_R_UP = f'{_R_USER}_{_R_PROMPT}'


def _reuse_turn(monkeypatch, *, autonomous, on_round):
    """Run the REAL reuse_recipe.get_agent_response for one turn.  Every
    initiate_chat posts its message and then calls on_round(message, group):
    what that round did.  Fakes only at the boundaries: the autogen agents,
    the HTTP pool and the scheduler."""
    from flask import Flask
    from hartos import reuse_recipe as rr
    action = {'action_id': 1, 'action': 'Report the overdue invoices',
              'can_perform_without_user_input': 'yes' if autonomous else 'no',
              'recipe': []}
    monkeypatch.setattr(rr, 'recipes', {_R_UP: {'actions': [action]}})
    monkeypatch.setattr(rr, 'user_tasks', {_R_UP: rr.Action([action])})
    monkeypatch.setattr(rr, 'user_ledgers', {})
    monkeypatch.setattr(rr, 'request_id_list', {_R_UP: DAEMON_RID})
    monkeypatch.setattr(rr, 'request_id_list_sent_intermediate', {})
    group_chat = SimpleNamespace(messages=[], agents=[])
    manager = SimpleNamespace(_oai_messages={})
    posted = []

    def initiate_chat(recipient, message=None, **_kw):
        posted.append(message)
        group_chat.messages.append(
            {'role': 'user', 'name': 'ChatInstructor', 'content': message})
        on_round(message, group_chat)

    user_proxy = SimpleNamespace(initiate_chat=initiate_chat)
    chat_instructor = SimpleNamespace(initiate_chat=initiate_chat)
    with patch.object(rr, 'pooled_post'), patch.object(rr, 'scheduler') as sched, \
            Flask('reuse-pause').app_context():
        sched.get_job.return_value = None
        reply = rr.get_agent_response(
            SimpleNamespace(name='Assistant'), chat_instructor,
            SimpleNamespace(name='helper'), user_proxy, manager, group_chat,
            'how many invoices are overdue?', 'user', _R_USER, _R_PROMPT,
            DAEMON_RID)
    return reply, posted


def _say(group, name, content):
    group.messages.append({'role': 'assistant', 'name': name, 'content': content})


_PENDING_VERDICT = json.dumps({
    'status': 'pending', 'action': 'Report the overdue invoices', 'action_id': 1,
    'message': 'waiting for the invoice export'})


class TestTheReuseTurnAnswersForThePause:

    def test_a_round_the_selector_ended_is_answered_with_the_pause(self, monkeypatch, as_daemon):
        with _person('user_present'):
            reply, posted = _reuse_turn(
                monkeypatch, autonomous=False,
                on_round=lambda message, group: h.yield_between_rounds(group.messages))
        assert agent_tools.is_user_pause(reply), reply
        assert len(posted) == 1, posted

    def test_a_round_the_person_interrupted_mid_work_gets_no_nudge(self, monkeypatch, as_daemon):
        """The loop top reads the mark before anything acts on the round.  An
        autonomous action whose round ended for the person while it was still
        working is what the loop's next step nudges ('complete this task
        independently'), and every nudge is another round posted into the
        session the goal will resume; without the check the loop nudges until
        the turn's round allowance runs out.  The turn answers the pause and
        posts nothing more."""
        def on_round(message, group):
            _say(group, 'Assistant', 'Looking at the invoice export now.')
            h.yield_between_rounds(group.messages)

        with _person('user_present'):
            reply, posted = _reuse_turn(monkeypatch, autonomous=True, on_round=on_round)
        assert agent_tools.is_user_pause(reply), reply
        assert len(posted) == 1, posted

    def test_a_pause_in_the_closing_synthesis_round_is_answered(self, monkeypatch, as_daemon):
        """Review of 96a9ca9f8, blocker 3: the synthesis round runs after the
        loop's last read of the mark.  When the person arrives during it, the
        turn answers the pause, not the steer or the verifier's verdict left
        at the tail."""
        rounds = []

        def on_round(message, group):
            rounds.append(message)
            if len(rounds) == 1:
                _say(group, 'StatusVerifier', _PENDING_VERDICT)   # a non-answer tail
            else:
                h.yield_between_rounds(group.messages)          # the person arrives

        with _person('user_present'):
            reply, posted = _reuse_turn(monkeypatch, autonomous=False, on_round=on_round)
        assert len(posted) == 2, posted        # the seed, then the synthesis steer
        assert agent_tools.is_user_pause(reply), reply

    def test_a_turn_already_paused_posts_no_synthesis_round(self, monkeypatch, as_daemon):
        """The loop's last round ended for the person and the loop broke on
        its tail: the turn answers the pause without posting another steer
        into a session it will resume."""
        rounds = []

        def on_round(message, group):
            rounds.append(message)
            if len(rounds) == 1:
                _say(group, 'Assistant', 'Looking at the invoice export now.')
            elif len(rounds) == 2:
                # The autonomy nudge's round: the Assistant echoed the answer
                # template (a measured shape, not an answer), then the next
                # choice found the person at the computer.
                _say(group, 'Assistant', json.dumps({'message2userfinal': '<your answer here>'}))
                h.yield_between_rounds(group.messages)

        with _person('user_present'):
            reply, posted = _reuse_turn(monkeypatch, autonomous=True, on_round=on_round)
        assert agent_tools.is_user_pause(reply), reply
        assert len(posted) == 2, posted

    def test_a_reuse_turn_begins_with_no_pause_pending(self, monkeypatch, as_daemon):
        from hartos import reuse_recipe as rr
        with _person('user_present'):
            assert h.yield_between_rounds() is True     # an earlier daemon turn's late mark
        tld.set_request_id('req-user-139')              # the person's own turn, same thread
        agents = tuple(MagicMock() for _ in range(12))
        monkeypatch.setattr(rr, 'user_agents', {_R_UP: agents})
        monkeypatch.setattr(rr, 'user_tasks', {_R_UP: rr.Action(['Report the overdue invoices'])})
        monkeypatch.setattr(rr, 'user_journey', {_R_UP: 'UseBot'})
        monkeypatch.setattr(rr, 'llm_call_track', {_R_UP: {'count': 0, 'original_prompt': False}})
        monkeypatch.setattr(rr, 'request_id_list', {})
        monkeypatch.setattr(rr, 'clear_action_states', lambda *a, **k: None)
        monkeypatch.setattr(rr, '_start_reuse_action', lambda *a, **k: None)
        monkeypatch.setattr(rr, 'get_role', lambda *a, **k: 'user')
        monkeypatch.setattr(rr, 'get_agent_response',
                            lambda *a, **k: 'paused' if h.pause_requested() else 'the answer')
        from flask import Flask
        with Flask('reuse-entry').app_context():
            assert rr.chat_agent(_R_USER, 'how many?', str(_R_PROMPT), None,
                                 'req-user-139') == 'the answer'

    def test_a_paused_role_selection_answers_the_pause(self, monkeypatch, as_daemon):
        """The persona-selection chat has its own selector, which yields too;
        its round ending for the person is the turn's pause, not the last
        line of the role chat."""
        from hartos import reuse_recipe as rr
        group = SimpleNamespace(messages=[{'role': 'assistant', 'name': 'Assistant',
                                           'content': 'Which role should I play?'}])
        proxy = MagicMock()
        proxy.initiate_chat.side_effect = _a_round_the_selector_ends
        monkeypatch.setattr(rr, 'user_agents', {})
        monkeypatch.setattr(rr, 'user_journey', {_R_UP: 'Roles'})
        monkeypatch.setattr(rr, 'role_agents',
                            {_R_UP: (MagicMock(), proxy, group, MagicMock(), MagicMock(), False)})
        monkeypatch.setattr(rr, 'llm_call_track', {})
        monkeypatch.setattr(rr, 'request_id_list', {})
        monkeypatch.setattr(rr, 'user_tasks', {})
        monkeypatch.setattr(rr, 'clear_action_states', lambda *a, **k: None)
        from flask import Flask
        with _person('user_present'), Flask('reuse-roles').app_context():
            reply = rr.chat_agent(_R_USER, 'hi', str(_R_PROMPT), None, DAEMON_RID)
        assert agent_tools.is_user_pause(reply), reply


# ─── The VLM loop ─────────────────────────────────────────────────────────────

def _run_vlm_loop(response, calls):
    from agent_ledger import SmartLedger
    from agent_ledger.backends import InMemoryBackend
    from integrations.vlm import activity_stream as act
    from integrations.vlm import local_computer_tool as lct
    from integrations.vlm import local_loop
    from integrations.vlm import qwen3vl_backend
    backend = MagicMock()
    backend.route_task.return_value = 'multi_step'
    backend._call_api.return_value = response
    backend.try_taskbar_pre_check.return_value = None
    backend.detect_grounding_bias.return_value = None
    backend.retry_with_elimination.return_value = None
    ledger = SmartLedger('42', 'yield_probe', backend=InMemoryBackend())
    with patch.object(lct, 'take_screenshot', side_effect=lambda *a, **k: calls.append('screenshot') or 'b64'), \
            patch.object(lct, 'execute_action', side_effect=lambda *a, **k: calls.append('action') or {'output': '', 'status': 'ok'}), \
            patch.object(qwen3vl_backend, 'get_qwen3vl_backend', return_value=backend), \
            patch.object(act, 'resolve_steering_agent_id', return_value=''), \
            patch.object(act, '_ledger_for', return_value=ledger), \
            patch('integrations.social.realtime.on_notification'), \
            patch.object(local_loop.time, 'sleep'):
        return local_loop.run_local_agentic_loop(
            {'instruction_to_vlm_agent': 'Open the notes file', 'max_ETA_in_seconds': 30,
             'user_id': '42', 'prompt_id': '77'},
            tier='inprocess', max_iterations=3)


_DONE = json.dumps({'Next Action': 'None', 'Status': 'DONE', 'Reasoning': 'nothing to do'})


@pytest.mark.usefixtures('computer_control_granted')
class TestTheVlmLoopPauses:

    def test_a_background_run_stops_before_touching_the_screen(self, as_daemon):
        calls = []
        with _person('user_present'):
            result = _run_vlm_loop(_DONE, calls)
        assert result['exit_reason'] == 'user_active', result
        assert calls == [], calls

    def test_a_run_the_owner_asked_for_keeps_going(self, as_user):
        calls = []
        with _person('user_present'):
            result = _run_vlm_loop(_DONE, calls)
        assert result['exit_reason'] == 'done', result
        assert 'screenshot' in calls


# ─── Every consumer holds a paused turn for later ─────────────────────────────

@pytest.fixture
def nothing_else_transient(monkeypatch):
    """Only the pause can make a no-response transient here."""
    monkeypatch.setattr(dispatch, 'is_user_recently_active', lambda: False)
    monkeypatch.setattr(dispatch, '_cb_is_open', lambda: False)
    monkeypatch.setattr('core.foreground.preempted_recently', lambda *a, **k: False)


class TestDispatchGoalHoldsAPausedTurn:

    def test_a_paused_turn_is_no_response_and_a_transient_reason(self, nothing_else_transient):
        with _through_tier1(agent_tools.user_pause_reply(2)):
            assert dispatch.dispatch_goal('draft the digest', 'u1', 'g-paused') is None
        assert dispatch.is_transient_deferral('g-paused') is True
        assert dispatch.is_transient_deferral('g-other') is False
        assert dispatch.dispatch_failure_reason('g-paused').startswith('deferred:')

    def test_the_next_dispatch_of_the_goal_clears_it(self, nothing_else_transient):
        with _through_tier1(agent_tools.user_pause_reply(2)):
            dispatch.dispatch_goal('draft the digest', 'u1', 'g-resumed')
        with _through_tier1('Drafted the digest.'):
            assert dispatch.dispatch_goal('draft the digest', 'u1', 'g-resumed') == \
                'Drafted the digest.'
        assert dispatch.is_transient_deferral('g-resumed') is False

    def test_two_turns_of_one_goal_each_read_their_own_reason(self, nothing_else_transient):
        """A goal's parallel subtasks run as concurrent dispatch_goal calls
        under ONE goal id.  Each must read why its own turn returned nothing:
        a shared slot let one subtask's call erase the other's reason, so a
        paused subtask read as failed and a failed one as nothing."""
        replies = {'failed-turn': f"{agent_tools._COULD_NOT_FINISH_PREFIX}Error code: 500",
                   'paused-turn': agent_tools.user_pause_reply(1)}
        modules = _gates('unused')
        modules['routes.hartos_backend_adapter'] = MagicMock(
            chat=lambda **_kw: {'text': replies[threading.current_thread().name]})
        first_done, second_done = threading.Event(), threading.Event()
        read = {}

        def first():
            dispatch.dispatch_goal('the prompt', 'u1', 'g-shared')
            first_done.set()
            second_done.wait(10)
            read['failed-turn'] = dispatch.dispatch_failure_reason('g-shared')

        def second():
            first_done.wait(10)
            dispatch.dispatch_goal('the prompt', 'u1', 'g-shared')
            read['paused-turn'] = dispatch.is_transient_deferral('g-shared')
            second_done.set()

        with swap_modules(modules), \
                patch.object(dispatch, '_get_distributed_coordinator', return_value=None):
            threads = [threading.Thread(target=first, name='failed-turn'),
                       threading.Thread(target=second, name='paused-turn')]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        assert read['paused-turn'] is True
        assert str(read['failed-turn']).startswith('turn failed: '), read


def _parallel_ledger(session_id):
    from agent_ledger import SmartLedger, Task, TaskType
    from agent_ledger.backends import InMemoryBackend
    from agent_ledger.core import ExecutionMode
    ledger = SmartLedger(agent_id='test', session_id=session_id, backend=InMemoryBackend())
    ledger.add_task(Task(task_id=f'root-{session_id}', description='Root goal',
                         task_type=TaskType.AUTONOMOUS,
                         execution_mode=ExecutionMode.SEQUENTIAL))
    siblings = ledger.create_sibling_tasks(
        parent_task_id=f'root-{session_id}', sibling_descriptions=['A', 'B'],
        task_type=TaskType.PRE_ASSIGNED)
    for sibling in siblings:
        sibling.execution_mode = ExecutionMode.PARALLEL
        sibling.pending_reason = 'ready'
        if sibling.task_id not in ledger.task_order:
            ledger.task_order.append(sibling.task_id)
    return ledger, siblings


def _fan_out(reply, ledger, goal_id):
    from integrations.agent_engine.agent_daemon import AgentDaemon
    daemon = AgentDaemon()
    goal = SimpleNamespace(id=goal_id, goal_type='marketing', user_id='u1')
    with _through_tier1(reply), patch.object(daemon, '_get_goal_ledger', return_value=ledger):
        return daemon._try_parallel_dispatch(
            goal, [{'user_id': 'a1'}, {'user_id': 'a2'}], 0, 10)


class TestAParallelSubtaskIsHeldNotFinished:

    def test_a_paused_subtask_goes_back_to_the_queue(self, nothing_else_transient):
        from agent_ledger import TaskStatus
        ledger, siblings = _parallel_ledger('paused-fan-out')
        fan_out = _fan_out(agent_tools.user_pause_reply(1), ledger, 'g-parallel-paused')
        assert fan_out == {'completed': 0, 'failed': 0, 'deferred': 2}, fan_out
        for s in siblings:
            task = ledger.tasks[s.task_id]
            assert task.status == TaskStatus.PENDING, task.status
            assert task.pending_reason == 'ready'
        assert {t.task_id for t in ledger.get_parallel_executable_tasks()} == \
            {s.task_id for s in siblings}

    def test_an_error_reply_is_a_failed_subtask_not_a_completed_one(self, nothing_else_transient):
        """Owner ruling 2026-10-04: a daemon agent completes for real.  The
        daemon's own tick already counts the {"status":"error"} envelope as a
        failure (593beaf08); its parallel subtasks were marked COMPLETED."""
        from agent_ledger import TaskStatus
        ledger, siblings = _parallel_ledger('error-fan-out')
        error = json.dumps({'status': 'error', 'action': 'Post the thread',
                            'action_id': 1, 'message': 'the page did not load'})
        fan_out = _fan_out(error, ledger, 'g-parallel-error')
        assert fan_out['failed'] == 2 and fan_out['completed'] == 0, fan_out
        assert {ledger.tasks[s.task_id].status for s in siblings} == {TaskStatus.FAILED}


    def test_a_ledger_run_stops_when_its_work_is_held(self):
        """dispatch_goal_with_ledger would be offered the held work again at
        once, so it stops instead of re-dispatching it until its 100-pass
        cap."""
        from agent_ledger import TaskStatus
        from integrations.agent_engine.parallel_dispatch import dispatch_goal_with_ledger
        ledger, siblings = _parallel_ledger('held-ledger-run')
        calls = []

        def held(task):
            calls.append(task.task_id)
            return {'success': False, 'deferred': True,
                    'error': 'deferred: paused between steps'}

        result = dispatch_goal_with_ledger(ledger, held)
        assert (result['completed'], result['failed'], result['deferred']) == (0, 0, 2), result
        assert sorted(calls) == sorted(s.task_id for s in siblings), calls
        assert {ledger.tasks[s.task_id].status for s in siblings} == {TaskStatus.PENDING}


class TestTheInstructionQueueRetriesAPausedTurn:

    _INSTRUCTION = SimpleNamespace(id='inst-0129', text='draft the digest')

    def test_a_paused_turn_is_a_deferral_not_a_result(self):
        with patch.object(dispatch, 'local_chat_dispatch',
                          return_value=('ok', agent_tools.user_pause_reply(1))):
            iid, result, error = dispatch._dispatch_single_instruction(
                'http://127.0.0.1:1', 'u1', self._INSTRUCTION, 'batch1')
        assert (iid, result) == ('inst-0129', None)
        assert error.startswith('deferred:'), error

    def test_the_drain_requeues_it_without_spending_an_attempt(self):
        queue = MagicMock()
        queue.acquire_drain_lock.return_value = True
        queue.pull_execution_plan.return_value = SimpleNamespace(
            waves=[[self._INSTRUCTION]], batch_id='batch1', total_instructions=1)
        with patch('integrations.agent_engine.instruction_queue.get_queue',
                   return_value=queue), \
                patch.object(dispatch, '_local_dispatch_base_url',
                             return_value='http://127.0.0.1:1'), \
                patch.object(dispatch, 'local_chat_dispatch',
                             return_value=('ok', agent_tools.user_pause_reply(1))):
            assert dispatch.drain_instruction_queue('u1') is None
        queue.complete_instruction.assert_not_called()
        (iid, error), kwargs = queue.fail_instruction.call_args
        assert iid == 'inst-0129' and kwargs == {'transient': True}, queue.fail_instruction.call_args


def _coding_tick(goal, transient):
    """One real CodingAgentDaemon tick whose dispatch returned nothing."""
    from integrations.coding_agent.coding_daemon import CodingAgentDaemon
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [goal]
    with patch('integrations.social.models.get_db', return_value=db), \
            patch('integrations.coding_agent.idle_detection.'
                  'IdleDetectionService.get_idle_agent_personas',
                  return_value=[{'user_id': 'agent-1', 'username': 'a1'}]), \
            patch('integrations.agent_engine.dispatch.should_yield_to_user',
                  return_value=False), \
            patch('integrations.agent_engine.budget_gate.'
                  'check_platform_affordability', return_value=(True, {})), \
            patch('integrations.agent_engine.dispatch.'
                  'max_autonomous_concurrency', side_effect=lambda cap: cap), \
            patch('integrations.agent_engine.dispatch.is_transient_deferral',
                  side_effect=lambda goal_id=None: transient and goal_id == goal.id), \
            patch('integrations.coding_agent.task_distributor.dispatch_to_chat',
                  return_value=None):
        CodingAgentDaemon()._tick()


class TestTheCodingDaemonHoldsAPausedGoal:

    @staticmethod
    def _goal():
        from tests.unit.test_coding_daemon_declined_goal_keeps_its_agent import _fix_goal
        return _fix_goal('paused')

    def test_a_paused_goal_is_not_a_dispatch_failure(self):
        goal = self._goal()
        _coding_tick(goal, transient=True)
        assert '_dispatch_failures' not in goal.config_json, goal.config_json
        assert goal.status == 'active'

    def test_a_failed_dispatch_still_counts(self):
        goal = self._goal()
        _coding_tick(goal, transient=False)
        assert goal.config_json.get('_dispatch_failures') == 1


# ─── The /chat autonomous first dispatch ──────────────────────────────────────

_HIE = os.path.join(_ROOT, 'hart_intelligence_entry.py')


@functools.lru_cache(maxsize=1)
def _autonomous_create_block():
    """The REAL `if autonomous:` block of /chat's first creation phase,
    compiled as a function body.  The module cannot be imported in the test
    venv (it starts the whole backend), so its block runs with the names it
    reads bound to fakes."""
    tree = ast.parse(io.open(_HIE, encoding='utf-8', errors='replace').read())
    chat = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == 'chat')
    blocks = [n for n in ast.walk(chat)
              if isinstance(n, ast.If) and isinstance(n.test, ast.Name)
              and n.test.id == 'autonomous']
    assert len(blocks) == 1, f'expected one `if autonomous:` in chat(), found {len(blocks)}'
    fn = ast.parse('def _autonomous_turn():\n    pass\n').body[0]
    fn.body = [blocks[0]]
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    return compile(module, _HIE, 'exec')


def _first_dispatch(tmp_path, recipe_reply):
    """Run the block for a daemon's autonomous CREATE turn whose recipe()
    answers recipe_reply.  Returns (reply text, reply payload)."""
    (tmp_path / '777.json').write_text('{}', encoding='utf-8')
    ns = {
        'autonomous': True, 'prompt': 'Draft the weekly digest',
        'PROMPTS_DIR': str(tmp_path), 'os': os, 'logging': logging,
        'app': MagicMock(), 'thread_local_data': tld, 'chat_agent': MagicMock(),
        'user_id': 'u1', 'file_id': None, 'request_id': DAEMON_RID, 'prompt_id': '777',
        '_autonomous_gather_info': lambda *a: 'GATHER OUTPUT',
        'recipe': lambda *a: recipe_reply,
        '_user_lock': threading.Lock(), 'review_agents': {}, '_ak': 'u1_777',
        'conversation_agent': {}, '_touch_agent_timestamp': lambda *a: None,
        '_create_social_agent_from_prompt': lambda *a: None,
        '_record_lifecycle': lambda *a, **k: None,
        '_push_workflow_flowchart': lambda *a, **k: None,
        '_chat_reply': lambda uid, rid, text, **payload: (text, payload),
    }
    exec(_autonomous_create_block(), ns)
    router = types.ModuleType('integrations.agentic_router')
    router.find_matching_agent = lambda *a, **k: None
    with swap_modules({'integrations.agentic_router': router}):
        return ns['_autonomous_turn']()


class TestTheChatFirstDispatchAnswersThePause:
    """Review of 96a9ca9f8, blocker 4: when recipe() stopped for the owner,
    /chat answered the gather output, so the daemon took the turn for a
    result and recorded a 0-spark strike."""

    def test_a_paused_create_answers_the_pause(self, tmp_path):
        text, payload = _first_dispatch(tmp_path, agent_tools.user_pause_reply(1))
        assert agent_tools.is_user_pause(text), text

    def test_a_finished_create_and_an_unfinished_one_answer_as_before(self, tmp_path):
        text, payload = _first_dispatch(tmp_path, 'Agent Created Successfully')
        assert text == 'Agent Created Successfully'
        assert payload['Agent_status'] == 'completed'
        text, payload = _first_dispatch(tmp_path, 'Could not finish action 2.')
        assert text == 'GATHER OUTPUT'
        assert payload['Agent_status'] == 'Review Mode'


# ─── The hive worker ──────────────────────────────────────────────────────────

class _Task:
    def __init__(self, task_id='g_task_0', parent='g'):
        self.task_id = task_id
        self.parent_task_id = parent
        self.description = 'draft the weekly digest'
        self.context = {'hop': 0, 'user_id': '42', 'prompt': 'draft the weekly digest'}


class TestTheHiveWorkerReleasesAPausedTurn:

    def test_a_paused_turn_is_deferred_not_recorded_not_held(self, monkeypatch):
        """A pause is the worker's own 'yielded to an active user' case: the
        canonical DEFERRED lifecycle (DeferredForRetry), never None, which
        _tick logs as 'Worker execution failed' and abandons."""
        from integrations.distributed_agent.worker_loop import (
            DeferredForRetry, DistributedWorkerLoop, HeldForHelp)
        monkeypatch.setattr(DistributedWorkerLoop, '_dispatch_would_defer',
                            staticmethod(lambda: None))
        loop = DistributedWorkerLoop()
        with patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
                   return_value=(True, '', 'draft the weekly digest')), \
             patch('integrations.agent_engine.dispatch.local_chat_dispatch',
                   return_value=('ok', agent_tools.user_pause_reply(1))), \
             patch('integrations.agent_engine.dispatch.prompt_id_for_goal', return_value='p'):
            got = loop._execute_task(_Task())
        assert isinstance(got, DeferredForRetry), got
        assert not isinstance(got, HeldForHelp)


# ─── The collapse ships its guard ─────────────────────────────────────────────

def _selectors(path):
    """Every function a GroupChat in ``path`` is given as speaker_selection_method."""
    tree = ast.parse(open(path, encoding='utf-8').read())
    names = set()
    for node in ast.walk(tree):
        # GroupChat(speaker_selection_method=fn) ...
        if (isinstance(node, ast.keyword) and node.arg == 'speaker_selection_method'
                and isinstance(node.value, ast.Name)):
            names.add(node.value.id)
        # ... and GroupChat(**{'speaker_selection_method': fn, ...}), the shape
        # create_agents builds its kwargs in.
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == 'speaker_selection_method'
                        and isinstance(v, ast.Name)):
                    names.add(v.id)
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            found.setdefault(node.name, []).append(node)
    assert names and set(found) == names, (path, names, set(found))
    return found


def _asks_between_rounds(fn):
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Attribute) and f.attr == 'yield_between_rounds') or \
                    (isinstance(f, ast.Name) and f.id == 'yield_between_rounds'):
                return True
    return False


@pytest.mark.parametrize('rel, expected', [
    ('hartos/create_recipe.py', 2),
    ('hartos/reuse_recipe.py', 4),
])
def test_source_guard_every_speaker_selector_asks_the_one_question(rel, expected):
    """DRY guard across the two pipelines: a selector that stops asking
    yield_between_rounds (or a new one that never did) is a background chat
    the owner cannot pause -- the parallel path this ruling forbids.  The
    behaviour itself is pinned above through the real CREATE closure; this
    keeps REUSE's four selectors, which no test can drive cheaply, on the
    same rule."""
    found = _selectors(os.path.join(_ROOT, rel))
    missing = [name for name, defs in found.items()
               if not all(_asks_between_rounds(d) for d in defs)]
    assert not missing, f'{rel}: speaker selector(s) that never ask yield_between_rounds: {missing}'
    assert sum(len(d) for d in found.values()) == expected, found.keys()
