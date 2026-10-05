"""Every background agent stops between steps while the owner is using the
computer, and resumes where it stopped once they are idle (#129).

Owner ruling 2026-10-04 (verbatim): "yield to user shd not be just applicable
for VLM loop but for autogen agents as well , proper pause all agents, and
resume restore all states idempotent and proper resume for daemon goals when
user is actually idle, not multiple user idles paths".

MEASURED the same day (gui_app.log): the canonical gate
dispatch.should_yield_to_user was CLOSED on user_present 19:12:55-19:20:56,
and inside that window coding-daemon turn daemon_d69d24f8 (dispatched
before 19:03:38) ran autogen rounds straight through, holding the single
local-LLM permit ("LLM busy (1 in flight)" at 19:08:55 and 19:13:26).  The
gate was consulted once per daemon TICK; nothing in create_recipe,
reuse_recipe or the VLM loop asked it between steps.

ONE question for every long-runner: dispatch.background_work_must_yield
(whose turn it is x the one gate).  ONE mark on the session's task, set by
hartos.helper.yield_between_rounds from every speaker selector, read by the
outer turn loops, answered with core.agent_tools.user_pause_reply, which the
daemon and the hive worker classify as "resume later", never as a result or
a failure.  Pause and resume are idempotent: the same mark, the same reply,
no model call, no state change.
"""
import ast
import json
import os
import sys
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

DAEMON_RID = 'daemon_goal-129'


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


def _gate(closed, person=None):
    """The one gate as the daemon tick sees it, and the shared answer to
    "does the person need the machine": by default the two agree."""
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch.object(dispatch, 'should_yield_to_user', return_value=closed))
    stack.enter_context(patch.object(dispatch, 'gate_closed_for_the_person',
                                     return_value=closed if person is None else person))
    return stack


class TestTheOneQuestion:

    def test_a_background_turn_yields_only_while_the_gate_is_closed(self, as_daemon):
        with _gate(True):
            assert dispatch.background_work_must_yield() is True
        with _gate(False):
            assert dispatch.background_work_must_yield() is False

    def test_a_user_turn_never_yields_to_its_own_owner(self, as_user):
        with _gate(True):
            assert dispatch.background_work_must_yield() is False

    def test_a_gate_closed_only_by_a_timer_on_an_idle_machine_does_not_pause(self, as_daemon):
        """The ten-minute chat cooldown with nobody at the desk is idle
        starvation: the daemon's override admits work through it, so the
        between-steps yield must not undo that a moment later."""
        with _gate(True, person=False):
            assert dispatch.background_work_must_yield() is False
        with _gate(True, person=True):
            assert dispatch.background_work_must_yield() is True

    def test_a_raising_gate_never_stops_work(self, as_daemon):
        with patch.object(dispatch, 'should_yield_to_user',
                          side_effect=RuntimeError('gate broke')):
            assert dispatch.background_work_must_yield() is False


class TestWhetherThePersonNeedsTheMachine:
    """dispatch.gate_closed_for_the_person: the override's judgement, shared."""

    def test_a_request_in_flight_is_the_person(self):
        with patch('core.foreground.foreground_active', return_value=True),                 patch.object(dispatch, 'machine_is_idle', return_value=True):
            assert dispatch.gate_closed_for_the_person() is True

    def test_the_foreground_reason_is_the_person(self):
        with patch('core.foreground.foreground_active', return_value=False),                 patch.object(dispatch, 'get_last_yield_reason', return_value='foreground_request'),                 patch.object(dispatch, 'machine_is_idle', return_value=True):
            assert dispatch.gate_closed_for_the_person() is True

    def test_an_idle_machine_with_no_request_is_not_the_person(self):
        with patch('core.foreground.foreground_active', return_value=False),                 patch.object(dispatch, 'get_last_yield_reason', return_value='user_active'),                 patch.object(dispatch, 'machine_is_idle', return_value=True):
            assert dispatch.gate_closed_for_the_person() is False

    def test_a_busy_machine_is_the_person(self):
        with patch('core.foreground.foreground_active', return_value=False),                 patch.object(dispatch, 'get_last_yield_reason', return_value='user_present'),                 patch.object(dispatch, 'machine_is_idle', return_value=False):
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


class TestBetweenRounds:

    def test_marks_the_task_and_ends_the_round_idempotently(self, as_daemon):
        task = Action(['Draft the digest'])
        with _gate(True):
            assert h.yield_between_rounds(task) is True
            assert task._paused_for_user is True
            assert h.yield_between_rounds(task) is True   # again: same mark, same answer
            assert task._paused_for_user is True

    def test_an_open_gate_or_a_user_turn_changes_nothing(self, as_daemon):
        task = Action(['Draft the digest'])
        with _gate(False):
            assert h.yield_between_rounds(task) is False
        assert not getattr(task, '_paused_for_user', False)
        tld.set_request_id('req-user-129')
        with _gate(True):
            assert h.yield_between_rounds(task) is False
        assert not getattr(task, '_paused_for_user', False)

    def test_no_task_is_still_a_yield(self, as_daemon):
        with _gate(True):
            assert h.yield_between_rounds(None) is True


class TestTheCreateChatPauses:

    def test_the_real_selector_ends_the_round_for_a_background_turn(self, selector, as_daemon):
        s = selector
        instructor = MagicMock()
        instructor.name = 'ChatInstructor'
        group = SimpleNamespace(messages=[POSTED], agents=[])
        with _gate(True):
            assert s.select(instructor, group) is None
            assert s.task._paused_for_user is True
            assert s.select(instructor, group) is None        # idempotent
        s.task._paused_for_user = False
        with _gate(False):
            assert s.select(instructor, group) is not None    # the round goes on

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
        with _gate(True):
            assert s.select(helper_agent, group) is not None, (
                'the round ended with the tool call unanswered')
            assert not getattr(s.task, '_paused_for_user', False)
            group.messages.append({'role': 'tool', 'name': 'Executor',
                                   'content': 'saved',
                                   'tool_responses': [{'tool_call_id': 'call_yield_1',
                                                       'role': 'tool', 'content': 'saved'}]})
            executor = MagicMock()
            executor.name = 'Executor'
            assert s.select(executor, group) is None, 'the answered call did not pause'
            assert s.task._paused_for_user is True

    def test_the_one_check_knows_a_pending_call(self, as_daemon):
        task = Action(['Draft the digest'])
        pending = [{'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'c1', 'type': 'function',
                                    'function': {'name': 'x', 'arguments': '{}'}}]}]
        with _gate(True):
            assert h.yield_between_rounds(task, pending) is False
            assert not getattr(task, '_paused_for_user', False)
            assert h.yield_between_rounds(task, pending + [
                {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'}]) is True

    def test_a_user_turn_is_never_paused_by_the_selector(self, selector, as_user):
        s = selector
        instructor = MagicMock()
        instructor.name = 'ChatInstructor'
        with _gate(True):
            assert s.select(instructor, SimpleNamespace(messages=[POSTED], agents=[])) is not None
        assert not getattr(s.task, '_paused_for_user', False)


class TestTheOuterLoopAnswersForThePause:

    def test_the_turn_answers_the_pause_and_keeps_every_state(self, session, as_daemon):
        s = session
        s.task._needs_user_input_action_id = None
        s.task._needs_user_input_kind = None
        s.task._paused_for_user = True            # what the inner chat left behind
        before = get_action_state(s.up, 1)
        reply = cr.get_response_group(s.user_id, 'Build the agent now', s.prompt_id)
        assert agent_tools.is_user_pause(reply), reply
        assert s.task._paused_for_user is False
        assert get_action_state(s.up, 1) == before
        assert cr.messages[s.up] is s.group_chat.messages
        # Idempotent resume while the owner is still active: the same answer,
        # the same state, no error recorded.
        s.task._paused_for_user = True
        assert agent_tools.is_user_pause(
            cr.get_response_group(s.user_id, 'Build the agent now', s.prompt_id))
        assert get_action_state(s.up, 1) == before


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
        with _gate(True):
            result = _run_vlm_loop(_DONE, calls)
        assert result['exit_reason'] == 'user_active', result
        assert calls == [], calls

    def test_a_run_the_owner_asked_for_keeps_going(self, as_user):
        calls = []
        with _gate(True):
            result = _run_vlm_loop(_DONE, calls)
        assert result['exit_reason'] == 'done', result
        assert 'screenshot' in calls


# ─── The dispatchers classify the pause ────────────────────────────────────────

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


# ─── The collapse ships its guard ──────────────────────────────────────────────

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
