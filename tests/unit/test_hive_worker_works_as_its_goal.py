"""A hive task is its goal's work, so the worker runs it under the goal's prompt_id.

Measured on central 2026-09-13: the three continuous goals' spark_spent stayed
at 531 / 364 / 448 after Guardian Convergence's hive task completed with a real
answer. Spark is charged only when a recipe flow finishes, by
budget_gate.charge_goal_work_completed(prompt_id), which finds the goal by
AgentGoal.prompt_id. Those rows carry dispatch.prompt_id_for_goal(goal.id),
for example '68276678525'. The worker sent /chat its own
f"{goal_type}_{task_id[:8]}" ('hive_growth_960a8332'), which matched no goal,
so hive work could never charge one. That string is not a number either, and
reuse_recipe does int(prompt_id) once a recipe exists for it.

Run:
  pytest tests/unit/test_hive_worker_works_as_its_goal.py -q
"""
import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
for p in (_ROOT, os.path.join(_ROOT, 'agent-ledger-opensource')):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent_ledger.core import SmartLedger, TaskStatus  # noqa: E402

# The three continuous goals on central and the prompt_id each row stores.
_CENTRAL_GOALS = {
    '960a8332-0aec-48cb-9558-e2ef2fa31f92': '68276678525',  # Guardian Convergence
    'c3602419-8df1-4a75-836c-0981d7101f3e': '67569219125',  # Compute Recruiter
    'eb67a57b-2cfa-497a-a999-4c04d2bcf61c': '66722327257',  # Hive Model Trainer
}
_GOAL = '960a8332-0aec-48cb-9558-e2ef2fa31f92'


class _MemBackend:
    def __init__(self):
        self.data = {}

    def load(self, key):
        return self.data.get(key)

    def save(self, key, data):
        self.data[key] = data

    def exists(self, key):
        return key in self.data


def _coordinator_with_goal(goal_id=_GOAL):
    """A goal submitted exactly the way dispatch_goal_distributed submits it."""
    from integrations.agent_engine.dispatch import _decompose_goal
    from integrations.distributed_agent.coordinator_backends import InMemoryTaskLock
    from integrations.distributed_agent.task_coordinator import (
        DistributedTaskCoordinator)
    led = SmartLedger(agent_id='coord', session_id='s', backend=_MemBackend())
    co = DistributedTaskCoordinator(
        ledger=led, task_lock=InMemoryTaskLock(),
        verifier=MagicMock(), baseline=MagicMock())
    prompt = 'Recruit compute from believers'
    co.submit_goal(prompt, _decompose_goal(prompt, goal_id, 'hive_growth', 'u1'),
                   {'goal_type': 'hive_growth', 'user_id': 'u1', 'prompt': prompt},
                   goal_id=goal_id)
    return led, co


def _loop():
    from integrations.distributed_agent.worker_loop import DistributedWorkerLoop
    return DistributedWorkerLoop()


def _allow_dispatch():
    return patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
                 side_effect=lambda p: (True, '', p))


def test_the_formula_still_gives_the_prompt_id_central_stores():
    """Pinned to real rows: changing the formula would orphan every goal's
    recipes and stop its spark from ever being charged again."""
    from integrations.agent_engine.dispatch import prompt_id_for_goal
    for goal_id, stored in _CENTRAL_GOALS.items():
        assert prompt_id_for_goal(goal_id) == stored, goal_id


def test_the_worker_runs_a_hive_task_under_its_goals_prompt_id():
    led, co = _coordinator_with_goal()
    loop = _loop()
    sent = {}

    def _chat(prompt, user_id, prompt_id, **kw):
        sent.update(prompt_id=prompt_id, user_id=user_id)
        return 'ok', 'Found three GPU owners on HN asking about local models.'

    with patch.object(loop, '_get_coordinator', return_value=co), \
         _allow_dispatch(), \
         patch('integrations.agent_engine.dispatch.local_chat_dispatch',
               side_effect=_chat), \
         patch('security.hive_guardrails.GuardrailEnforcer.after_response',
               return_value=(True, '')), \
         patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge',
               return_value=MagicMock()):
        loop._tick()

    assert sent, 'the worker never reached /chat'
    assert sent['prompt_id'] == _CENTRAL_GOALS[_GOAL], (
        f"ran as {sent['prompt_id']!r}, which charges no goal")
    int(sent['prompt_id'])  # reuse_recipe converts it once a recipe exists
    assert sent['user_id'] == 'u1'
    assert led.get_task(f'{_GOAL}_task_0').status == TaskStatus.COMPLETED


def test_the_http_fallback_sends_the_same_prompt_id():
    _, co = _coordinator_with_goal()
    task = co.claim_next_task('worker_a', capabilities=_loop()._capabilities)
    assert task is not None
    resp = MagicMock(status_code=503)
    with _allow_dispatch(), \
         patch('integrations.agent_engine.dispatch.local_chat_dispatch',
               return_value=('error', None)), \
         patch('integrations.agent_engine.dispatch._internal_auth_headers',
               return_value={}), \
         patch('integrations.distributed_agent.worker_loop.pooled_post',
               return_value=resp) as post:
        assert _loop()._execute_task(task) is None
    assert post.call_args.kwargs['json']['prompt_id'] == _CENTRAL_GOALS[_GOAL]


def test_the_http_fallback_turn_runs_autonomous():
    """#97, measured on central 2026-09-13: native HARTOS has no Nunba adapter,
    so the worker reaches /chat only through this POST, and its body carried
    no request_id. The handler bound None, is_current_request_autonomous()
    read that as a live user, and both rebuilt agents got the INTERACTIVE
    prompt: they greeted, asked a clarifying question nobody could answer,
    and saved no step in 45 minutes."""
    from core.chat_client import daemon_request_id
    from hartos.threadlocal import thread_local_data
    from integrations.agent_engine.dispatch import is_current_request_autonomous
    _, co = _coordinator_with_goal()
    task = co.claim_next_task('worker_a', capabilities=_loop()._capabilities)
    assert task is not None
    with _allow_dispatch(), \
         patch('integrations.agent_engine.dispatch.local_chat_dispatch',
               return_value=('unavailable', None)), \
         patch('integrations.agent_engine.dispatch._internal_auth_headers',
               return_value={}), \
         patch('integrations.distributed_agent.worker_loop.pooled_post',
               return_value=MagicMock(status_code=503)) as post:
        _loop()._execute_task(task)
    rid = post.call_args.kwargs['json']['request_id']
    # the tag local_chat_dispatch stamps on the in-process route: the GOAL's
    # id, which the create loop reads back to find the goal its ask-for-help
    # parks (tests/unit/test_worker_hands_help_to_the_goal.py)
    assert rid == daemon_request_id(task.parent_task_id)
    try:
        thread_local_data.set_request_id(rid)   # what the /chat handler does
        assert is_current_request_autonomous() is True
    finally:
        thread_local_data.set_request_id('')


def test_two_goals_never_share_one_agent():
    """The prompt_id keys the agent's live state, so two goals' hive tasks
    must not collapse onto one."""
    seen = set()
    for goal_id in _CENTRAL_GOALS:
        _, co = _coordinator_with_goal(goal_id)
        task = co.claim_next_task('worker_a', capabilities=_loop()._capabilities)
        with _allow_dispatch(), \
             patch('integrations.agent_engine.dispatch.local_chat_dispatch',
                   return_value=('deferred', None)) as chat:
            _loop()._execute_task(task)
        seen.add(chat.call_args.args[2])
    assert seen == set(_CENTRAL_GOALS.values())
