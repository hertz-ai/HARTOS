"""_ask_for_help: the one place the create loop asks for help (#106).

Both outer-loop exits (the NEEDS-INPUT escape and the user-input gate), and a
loop-break on an unverified action through that gate, end in _ask_for_help.  A
live user is asked directly (_needs_input_reply), and a step the gate did not
already hold is held for her answer (_hold_for_the_person).  On an
autonomous run nobody reads a question, so the action is held as waiting
(PENDING, which the ledger records as BLOCKED, with blocked_reason
input_required) and the goal that dispatched the turn is parked with the ask
through GoalManager.escalate_goal, where the owner and the co-pilot see it.

Extract-and-exec, like test_needs_input_escape (importing create_recipe hangs
a bare pytest env): the two functions run against stand-ins for the modules
and names they use.  core.chat_client is the real module.

    python -m pytest tests/unit/test_ask_for_help.py --noconftest -q
"""
import ast
import contextlib
import logging
import os
import sys
import types
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
_SRC_PATH = os.path.join(ROOT, 'hartos/create_recipe.py')


class _State:
    def __init__(self, value):
        self.value = value


ASSIGNED = _State('assigned')
IN_PROGRESS = _State('in_progress')
PENDING = _State('pending')


def _functions(names):
    tree = ast.parse(open(_SRC_PATH, encoding='utf-8').read())
    wanted = {'_needs_input_reply', '_ask_for_help', '_hold_for_the_person'}
    nodes = [n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert {n.name for n in nodes} == wanted, 'helpers missing from create_recipe'
    ns = dict(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), _SRC_PATH, 'exec'), ns)
    return ns


def _names(state=IN_PROGRESS, task=None, tasks=None):
    """The create_recipe globals _ask_for_help reads, and a record of the
    state changes it asks for."""
    # The reply prefixes are the real constants: the hive worker recognises
    # the reply by them (core.agent_tools.is_help_pause).
    from core.constants import (
        HELP_EXPERT_REPLY_PREFIX, HELP_PAUSED_REPLY_PREFIX,
        USER_INPUT_GATE_AWAITING_REPLY, USER_INPUT_GATE_HUMAN_REQUIRED)
    states, holds = [], []
    ledgers = {'7_1': types.SimpleNamespace(tasks={'action_2': task})} if task else {}
    return states, {
        'HELP_PAUSED_REPLY_PREFIX': HELP_PAUSED_REPLY_PREFIX,
        'HELP_EXPERT_REPLY_PREFIX': HELP_EXPERT_REPLY_PREFIX,
        'USER_INPUT_GATE_AWAITING_REPLY': USER_INPUT_GATE_AWAITING_REPLY,
        # The hold a live person's ask gets (_hold_for_the_person): the gate's
        # kind, its terminal rule, and the wait recorded in both lifecycle
        # authorities; `holds` records each mark_action_waiting_for_user.
        'USER_INPUT_GATE_HUMAN_REQUIRED': USER_INPUT_GATE_HUMAN_REQUIRED,
        'is_terminal_state': lambda s: False,
        'holds': holds,
        'mark_action_waiting_for_user': (
            lambda up, aid, reason, flow=None, asked_in=None:
            holds.append((up, aid, reason, flow, asked_in)) or True),
        'request_id_list': {'7_1': 'her-request-1'},
        # _ask_for_help reads the gate's kind off the session's task (G8).
        'user_tasks': tasks if tasks is not None else {},
        'user_ledgers': ledgers,
        'get_action_state': lambda up, aid: state,
        'safe_set_state': (lambda up, aid, s, reason='':
                           states.append((aid, s.value, reason)) or True),
        'ActionState': types.SimpleNamespace(
            ASSIGNED=ASSIGNED, IN_PROGRESS=IN_PROGRESS, PENDING=PENDING),
        'get_current_flow': lambda up: 0,
        'current_app': types.SimpleNamespace(
            logger=logging.getLogger('test_ask_for_help')),
    }


def _modules(autonomous, request_id='daemon_goal-7', escalate=None):
    @contextlib.contextmanager
    def _session(commit=False):
        yield 'db'
    return {
        'integrations.agent_engine.dispatch': types.SimpleNamespace(
            is_current_request_autonomous=lambda: autonomous),
        'hartos.threadlocal': types.SimpleNamespace(
            thread_local_data=types.SimpleNamespace(
                get_request_id=lambda: request_id)),
        'integrations.agent_engine.goal_manager': types.SimpleNamespace(
            GoalManager=types.SimpleNamespace(
                escalate_goal=escalate or (lambda *a: {'success': True}))),
        'integrations.social.models': types.SimpleNamespace(db_session=_session),
    }


def test_a_live_user_is_asked_the_question_as_before():
    states, names = _names()
    ns = _functions(names)
    escalate = mock.Mock()
    with mock.patch.dict(sys.modules, _modules(autonomous=False, escalate=escalate)):
        reply = ns['_ask_for_help']('7_1', 1, 2, 'Collect the sources', 'it looped')
    assert reply == ns['_needs_input_reply'](2, 'Collect the sources')
    escalate.assert_not_called()
    assert states == [], 'a live user answers; the action is left as it was'


def test_a_live_user_whose_step_already_asked_is_told_it_waits():
    """G8: a `completed` + `no` step has already asked (its send went out),
    so the loop's own line says the step waits for the reply, not that it
    "isn't coming together" (hartos-00's mutant M8 survived without this)."""
    from core.constants import USER_INPUT_GATE_AWAITING_REPLY
    from tests.unit.module_swap import swap_modules
    # The gate sets the action and the kind together (state_transition).
    task = types.SimpleNamespace(_needs_user_input_action_id=2,
                                 _needs_user_input_kind=USER_INPUT_GATE_AWAITING_REPLY)
    states, names = _names(tasks={'7_1': task})
    ns = _functions(names)
    with swap_modules(_modules(autonomous=False)):
        reply = ns['_ask_for_help']('7_1', 1, 2, 'Collect the sources', 'it looped')
    assert reply == ns['_needs_input_reply'](2, 'Collect the sources', waiting=True)
    assert 'is waiting for your reply before I continue' in reply, reply
    assert states == []
    # The gate's own hold stands: no second wait, its kind kept.
    assert names['holds'] == []
    assert task._needs_user_input_kind == USER_INPUT_GATE_AWAITING_REPLY


def test_a_live_users_step_is_held_for_her_answer():
    """Review of 4bcad9583, finding 2: an ask the gate did not raise (the
    attempts bound on a freed run) holds the step the way the gate does, so
    her next message is taken as its answer.  A `pending` verdict left the
    step PENDING; the ledger blocks a running task, so it is resumed first."""
    from core.constants import USER_INPUT_GATE_HUMAN_REQUIRED
    task = types.SimpleNamespace()
    states, names = _names(state=PENDING, tasks={'7_1': task})
    ns = _functions(names)
    escalate = mock.Mock()
    with mock.patch.dict(sys.modules, _modules(autonomous=False, escalate=escalate)):
        reply = ns['_ask_for_help']('7_1', 1, 2, 'Collect the sources',
                                    'it did not complete after 3 attempts')
    assert reply == ns['_needs_input_reply'](2, 'Collect the sources')
    escalate.assert_not_called()
    assert (task._needs_user_input_action_id,
            task._needs_user_input_kind) == (2, USER_INPUT_GATE_HUMAN_REQUIRED)
    assert states == [(2, 'in_progress', 'held for the person')]
    assert names['holds'] == [(
        '7_1', 2, 'asked the person: it did not complete after 3 attempts',
        0, 'her-request-1')]


def test_a_finished_step_is_not_held_for_her():
    """A terminal step waits for nobody (_should_block_on_user_input's rule)."""
    task = types.SimpleNamespace()
    states, names = _names(tasks={'7_1': task})
    names['is_terminal_state'] = lambda s: True
    ns = _functions(names)
    with mock.patch.dict(sys.modules, _modules(autonomous=False)):
        ns['_ask_for_help']('7_1', 1, 2, 'Collect the sources', 'it looped')
    assert getattr(task, '_needs_user_input_action_id', None) is None
    assert states == [] and names['holds'] == []


def test_an_autonomous_run_parks_the_goal_and_holds_the_action():
    task = types.SimpleNamespace(blocked_reason=None)
    task.set_blocked_reason = lambda r: setattr(task, 'blocked_reason', r)
    states, names = _names(task=task)
    ns = _functions(names)
    asks = []

    def escalate(db, goal_id, escalation):
        asks.append((db, goal_id, escalation))
        return {'success': True}
    with mock.patch.dict(sys.modules, _modules(autonomous=True, escalate=escalate)):
        reply = ns['_ask_for_help']('7_1', 1, 2, 'Collect the sources', 'it looped')
    assert [(db, g, e['action_id'], e['reason'], e['tried'])
            for db, g, e in asks] == [('db', 'goal-7', 2, 'it looped', ['local'])]
    assert asks[0][2]['action'] == 'Collect the sources'
    # What the daemon needs to find the action's banked recipe (#106d).
    assert (asks[0][2]['user_prompt'], asks[0][2]['prompt_id'],
            asks[0][2]['flow']) == ('7_1', 1, 0)
    assert task.blocked_reason == 'input_required'
    assert states == [(2, 'pending', 'asked for help: it looped')]
    assert reply.startswith('Paused for help: step 2'), reply


def test_a_node_with_an_expert_hands_the_step_to_it():
    """#106d: escalate_goal hands the action to this node's expert model
    first; the reply says so and the action waits for that turn."""
    states, names = _names()
    ns = _functions(names)
    with mock.patch.dict(sys.modules, _modules(
            autonomous=True,
            escalate=lambda *a: {'success': True, 'stage': 'expert'})):
        reply = ns['_ask_for_help']('7_1', 1, 2, 'Collect the sources', 'it looped')
    assert reply.startswith('Handed to the expert model: step 2'), reply
    assert states[-1][1] == 'pending'


def test_an_action_that_never_started_passes_through_in_progress():
    """ASSIGNED has no edge to PENDING in the state machine."""
    states, names = _names(state=ASSIGNED)
    ns = _functions(names)
    with mock.patch.dict(sys.modules, _modules(autonomous=True)):
        ns['_ask_for_help']('7_1', 1, 2, 'x', 'r')
    assert [s for _, s, _ in states] == ['in_progress', 'pending']


def test_a_turn_with_no_goal_still_holds_the_action():
    """A turn whose request id names no goal: the action is still held, and
    nothing is parked."""
    states, names = _names()
    ns = _functions(names)
    escalate = mock.Mock()
    with mock.patch.dict(sys.modules, _modules(autonomous=True, request_id='',
                                               escalate=escalate)):
        reply = ns['_ask_for_help']('7_1', 1, 2, 'x', 'r')
    escalate.assert_not_called()
    assert states[-1][1] == 'pending'
    assert reply.startswith('Paused for help')
