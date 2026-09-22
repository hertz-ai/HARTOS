"""A hive worker's ask-for-help must reach the goal, and a pause is not a result.

Measured on the Nunba desktop 2026-09-15 (boot 14:43, gui_app.log): four
daemon turns ran through the distributed worker; three ended with

    [ASK-FOR-HELP] action 1 of <prompt>_92083231371: it did not complete
    after 3 attempts; goal 6d2a6ff6-...-35e004603aeb_task_0 handed to nobody

and every one of them was then "Worker completed task ..." with a result
hash.  Two defects on the worker path, both against the owner's 2026-09-14
ruling (create_recipe._ask_for_help: ask a human or an expert, never record
a completion that did not happen):

1. The worker stamps the turn with daemon_id=task.task_id, the coordinator
   task id ``<goal_id>_task_0`` (dispatch._decompose_goal), so
   daemon_goal_id() hands the create loop that string, GoalManager.escalate_goal
   finds no AgentGoal by it ("Goal not found") and the escape reports "handed
   to nobody".  The same function already derives the goal from
   task.parent_task_id for prompt_id_for_goal, for the same reason.

2. The "Paused for help: ..." / "Handed to the expert model: ..." reply is a
   held action, not work, and _after_response passed it to submit_result.
   Same family as tests/unit/test_worker_failed_turn_is_not_a_result.py: the
   sentence is defined once in core.constants and recognised by reference.
   Unlike a failed turn it must not be released for a retry either: the goal
   is parked, and orphan recovery would run a paused goal every ten minutes.
   The task is held (BLOCKED, blocked_reason input_required, the mark
   _ask_for_help puts on its own ledger) and comes back when the goal is
   dispatched again, which the daemon does only for an active goal.

3. dispatch_goal sent an expert's turn (model_config set, #106d) down the
   distributed branch, which drops model_config, so on a node with peers the
   expert never got its turn: the re-dispatch deduped onto the existing task
   set and _settle_expert_turn parked the goal for a person one tick later.

Run:
  pytest tests/unit/test_worker_hands_help_to_the_goal.py -q
"""
import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
for p in (_ROOT, os.path.join(_ROOT, 'agent-ledger-opensource')):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent_ledger.core import SmartLedger, TaskStatus  # noqa: E402
import pytest


@pytest.fixture(autouse=True)
def _worker_may_claim(monkeypatch):
    """Precondition these tests always assumed: the daemon gate is open.

    Since 2026-09-20 the worker asks should_yield_to_user, the provider
    breaker and the adapter's readiness BEFORE claiming (a claim the
    dispatcher would defer is three full ledger writes for nothing), so a
    test that drives _tick on a live box would otherwise inherit that box's
    pressure readings.  The gate itself is pinned in
    tests/unit/test_worker_tick_claims_nothing_it_would_defer.py.
    """
    from integrations.distributed_agent.worker_loop import DistributedWorkerLoop
    monkeypatch.setattr(DistributedWorkerLoop, '_dispatch_would_defer',
                        staticmethod(lambda: None))


class _Task:
    def __init__(self, task_id='g_task_0', parent='g'):
        self.task_id = task_id
        self.parent_task_id = parent
        self.description = 'draft the weekly digest'
        self.context = {'hop': 0, 'user_id': '42', 'prompt': 'draft the weekly digest'}


def _loop():
    from integrations.distributed_agent.worker_loop import DistributedWorkerLoop
    return DistributedWorkerLoop()


def _paused_reply():
    from core import constants as C
    return (f'{C.HELP_PAUSED_REPLY_PREFIX} step 1 ("search the web") could not '
            f'be finished autonomously (it did not complete after 3 attempts). '
            f'It is waiting for the owner or the co-pilot.')


# --------------------------------------------------------------------------
# 1. The turn is stamped with the GOAL's id
# --------------------------------------------------------------------------

def test_the_in_process_turn_carries_the_goal_id_not_the_task_id():
    loop = _loop()
    seen = {}

    def _dispatch(prompt, user_id, prompt_id, daemon_id=None, **kw):
        seen['daemon_id'] = daemon_id
        return 'ok', 'Digest drafted: three items.'

    with patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
               return_value=(True, '', 'draft the weekly digest')), \
         patch('integrations.agent_engine.dispatch.local_chat_dispatch', _dispatch), \
         patch('integrations.agent_engine.dispatch.prompt_id_for_goal', return_value='p'), \
         patch.object(loop, '_after_response', side_effect=lambda r, *a, **k: r):
        got = loop._execute_task(_Task())
    assert got == 'Digest drafted: three items.'
    assert seen['daemon_id'] == 'g', seen


def test_the_http_fallback_carries_the_goal_id_too():
    loop = _loop()
    posted = {}

    def _post(url, json=None, headers=None, timeout=None):
        posted.update(json or {})
        resp = MagicMock(status_code=200)
        resp.json.return_value = {'response': 'Digest drafted.'}
        return resp

    with patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
               return_value=(True, '', 'draft the weekly digest')), \
         patch('integrations.agent_engine.dispatch.local_chat_dispatch',
               return_value=('unavailable', '')), \
         patch('integrations.agent_engine.dispatch.prompt_id_for_goal', return_value='p'), \
         patch('integrations.agent_engine.dispatch._internal_auth_headers', return_value={}), \
         patch('integrations.distributed_agent.worker_loop.pooled_post', _post), \
         patch.object(loop, '_after_response', side_effect=lambda r, *a, **k: r):
        loop._execute_task(_Task())
    assert posted.get('request_id') == 'daemon_g', posted


def test_a_task_with_no_parent_is_its_own_goal():
    loop = _loop()
    seen = {}

    def _dispatch(prompt, user_id, prompt_id, daemon_id=None, **kw):
        seen['daemon_id'] = daemon_id
        return 'ok', 'done'

    with patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
               return_value=(True, '', 'x')), \
         patch('integrations.agent_engine.dispatch.local_chat_dispatch', _dispatch), \
         patch('integrations.agent_engine.dispatch.prompt_id_for_goal', return_value='p'), \
         patch.object(loop, '_after_response', side_effect=lambda r, *a, **k: r):
        loop._execute_task(_Task(task_id='solo', parent=None))
    assert seen['daemon_id'] == 'solo'


# --------------------------------------------------------------------------
# 2. A pause for help is not a result: the task is held, not completed
# --------------------------------------------------------------------------

def test_the_two_help_replies_are_recognised_and_are_not_errors():
    from core import constants as C
    from core.agent_tools import is_help_pause, is_user_facing_error
    expert = (f'{C.HELP_EXPERT_REPLY_PREFIX} step 2 could not be finished '
              f'autonomously (...). It takes this goal\'s next turn.')
    assert is_help_pause(_paused_reply())
    assert is_help_pause(expert)
    assert not is_user_facing_error(_paused_reply())
    for reply in ('Found three GPU owners on HN.', '', None, 42):
        assert not is_help_pause(reply), repr(reply)


def test_after_response_holds_a_help_pause_instead_of_completing():
    from integrations.distributed_agent.worker_loop import HeldForHelp
    loop = _loop()
    with patch('security.hive_guardrails.GuardrailEnforcer.after_response',
               return_value=(True, '')):
        got = loop._after_response(_paused_reply(), _Task())
    assert isinstance(got, HeldForHelp)
    assert got.reason == _paused_reply()


def test_tick_holds_the_task_and_neither_submits_nor_abandons_it():
    from integrations.distributed_agent.worker_loop import HeldForHelp
    loop = _loop()
    coord = MagicMock()
    coord.claim_next_task.return_value = _Task()
    with patch.object(loop, '_get_coordinator', return_value=coord), \
         patch.object(loop, '_execute_task',
                      return_value=HeldForHelp(_paused_reply())):
        loop._tick()
    coord.hold_task.assert_called_once_with('g_task_0', loop._node_id,
                                            _paused_reply())
    coord.submit_result.assert_not_called()
    coord.abandon_task.assert_not_called()


def test_create_recipe_takes_the_help_prefixes_from_constants():
    """The detector matches by reference; an inline copy of the sentence in
    create_recipe would drift from it without anything noticing."""
    import ast
    with open(os.path.join(_ROOT, 'hartos', 'create_recipe.py'), encoding='utf-8') as fh:
        src = fh.read()
    assert 'Paused for help:' not in src.replace('HELP_PAUSED_REPLY_PREFIX', '')
    assert 'Handed to the expert model:' not in src.replace('HELP_EXPERT_REPLY_PREFIX', '')
    tree = ast.parse(src)
    shared = {a.name for n in ast.walk(tree)
              if isinstance(n, ast.ImportFrom) and n.module == 'core.constants'
              for a in n.names}
    assert {'HELP_PAUSED_REPLY_PREFIX', 'HELP_EXPERT_REPLY_PREFIX'} <= shared, shared


# --------------------------------------------------------------------------
# 3. The coordinator side: held now, back when the goal is dispatched again
# --------------------------------------------------------------------------

class _MemBackend:
    def __init__(self):
        self.data = {}

    def load(self, key):
        return self.data.get(key)

    def save(self, key, data):
        self.data[key] = data

    def exists(self, key):
        return key in self.data


def _coordinator():
    from integrations.distributed_agent.coordinator_backends import InMemoryTaskLock
    from integrations.distributed_agent.task_coordinator import (
        DistributedTaskCoordinator)
    led = SmartLedger(agent_id='coord', session_id='s', backend=_MemBackend())
    co = DistributedTaskCoordinator(
        ledger=led, task_lock=InMemoryTaskLock(),
        verifier=MagicMock(), baseline=MagicMock())
    co.submit_goal('obj', [{'task_id': 'g_task_0', 'description': 'd'}],
                   {'user_id': '42', 'prompt': 'obj'}, goal_id='g')
    return led, co


def test_a_held_task_is_not_completed_and_not_claimable():
    from datetime import datetime, timedelta
    from integrations.distributed_agent import task_coordinator as tc
    led, co = _coordinator()
    got = co.claim_next_task('worker_a')
    assert got is not None and got.task_id == 'g_task_0'

    co.hold_task('g_task_0', 'worker_a', _paused_reply())
    task = led.get_task('g_task_0')
    assert task.status == TaskStatus.BLOCKED
    assert task.blocked_reason == 'input_required'
    assert task.error_message == _paused_reply()
    assert not co._lock.is_task_locked('g_task_0'), 'the claim was not released'
    assert co.get_goal_progress('g')['completed'] == 0

    # Not a retry candidate, and not an orphan either: the goal is parked,
    # and running it every orphan window would be running a paused goal.
    old = datetime.now() - timedelta(seconds=tc._ORPHAN_AFTER_S + 60)
    task.context['claimed_at'] = old.isoformat()
    assert co.claim_next_task('worker_b') is None


def test_the_goals_next_dispatch_brings_the_held_task_back():
    """The daemon dispatches only active goals, so a re-dispatch means the
    owner resumed it (or the expert leg is running): the answer is in."""
    led, co = _coordinator()
    co.claim_next_task('worker_a')
    co.hold_task('g_task_0', 'worker_a', _paused_reply())

    # A re-dispatch of the SAME goal: the dedup branch of submit_goal.
    co.submit_goal('obj', [{'task_id': 'g_task_0', 'description': 'd'}],
                   {'user_id': '42', 'prompt': 'obj'}, goal_id='g')
    task = led.get_task('g_task_0')
    assert task.status == TaskStatus.PENDING
    assert task.blocked_reason is None
    assert task.error_message is None
    assert 'claimed_by' not in task.context
    assert task.context.get('runs') == 1
    again = co.claim_next_task('worker_b')
    assert again is not None and again.task_id == 'g_task_0'
    assert len(led.tasks) == 2, 'the re-dispatch minted a new task'


def test_a_dependency_block_is_not_mistaken_for_a_help_hold():
    """Only tasks held for input come back on re-dispatch; a task blocked on
    a prerequisite keeps its block."""
    led, co = _coordinator()
    co.claim_next_task('worker_a')
    task = led.get_task('g_task_0')
    assert led.update_task_status('g_task_0', TaskStatus.BLOCKED,
                                  reason='waiting on a sibling')
    task.set_blocked_reason('Blocked by prerequisite g_task_9')
    co.submit_goal('obj', [{'task_id': 'g_task_0', 'description': 'd'}],
                   {'user_id': '42', 'prompt': 'obj'}, goal_id='g')
    assert led.get_task('g_task_0').status == TaskStatus.BLOCKED


# --------------------------------------------------------------------------
# 4. The expert's turn is a local turn, not a hive submission
# --------------------------------------------------------------------------

def _dispatch_expert_turn(robot_capable):
    """dispatch_goal with a model override on a node with peers; returns
    (result, distributed mock, local chat mock).  robot_capable=False takes
    the robot branch, the second distributed dispatch in the function."""
    from integrations.agent_engine import dispatch as dispatch_mod
    guard = MagicMock()
    guard.GuardrailEnforcer.before_dispatch.return_value = (True, 'ok', 'prompt')
    with patch.dict('sys.modules', {
            'integrations.agent_engine.budget_gate': MagicMock(
                pre_dispatch_budget_gate=MagicMock(return_value=(True, 'ok'))),
            'security.hive_guardrails': guard,
            'security.immutable_audit_log': MagicMock(
                get_audit_log=MagicMock(return_value=MagicMock()))}), \
         patch.object(dispatch_mod, '_get_distributed_coordinator',
                      return_value=MagicMock()), \
         patch.object(dispatch_mod, '_has_hive_peers', return_value=True), \
         patch.object(dispatch_mod, 'dispatch_goal_distributed',
                      return_value='dist_id') as dist, \
         patch.object(dispatch_mod, '_check_robot_capability_match',
                      return_value=robot_capable), \
         patch.object(dispatch_mod, '_dispatch_provider_host', return_value=None), \
         patch.object(dispatch_mod, 'local_chat_dispatch',
                      return_value=('ok', 'the expert finished step 1')) as chat:
        got = dispatch_mod.dispatch_goal(
            'prompt', 'u1', 'g1', 'robot' if not robot_capable else 'marketing',
            model_config=[{'model': 'claude-opus-5', 'api_key': 'x'}])
    return got, dist, chat


def test_dispatch_goal_runs_an_expert_turn_locally_even_with_peers():
    got, dist, chat = _dispatch_expert_turn(robot_capable=True)
    dist.assert_not_called()
    assert chat.called, 'the expert turn did not run locally'
    assert got == 'the expert finished step 1'


def test_a_robot_goal_this_node_cannot_serve_still_runs_its_expert_turn_here():
    """The robot branch is a second distributed dispatch; the guard must
    cover it too (hartos-3e review of 510392ae4: it did not)."""
    got, dist, chat = _dispatch_expert_turn(robot_capable=False)
    dist.assert_not_called()
    assert chat.called
    assert got == 'the expert finished step 1'
