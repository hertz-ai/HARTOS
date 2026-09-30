"""Resuming a build sets the CURRENT flow's action states and nothing else.

THE LIVE FAILURE (2026-09-13 10:15:31, installed Nunba, agent 87400889007 --
2 flows, flow 0 banked, fresh flow-1 ledger minted by fe2bf87fe):

    [TARGET] Action 1: assigned -> in_progress (auto-path: resumed - previous flow)
    Auto-synced action_1 -> in_progress
    [TARGET] Action 1: status_verification_requested -> completed (... previous flow)
    Auto-synced action_1 -> completed
    ... the same for actions 2..8 ...
    [AUTO-ADVANCE] action 1 done but recipe not saved -- requesting recipe

ActionState is keyed (user_prompt, action_id): one flow's worth of keys, and
every gate reads get_action_state(user_prompt, N) as "the current flow's
action N".  set_states_from_progress also forced every EARLIER flow's actions
through in_progress -> completed -> terminated on those same keys, and each
transition auto-synced into the ledger registered for the session -- flow 1's
fresh ledger, because initialize_with_resume builds it first.  The ledger
file afterwards: all 7 flow-1 tasks COMPLETED at 10:15:31-32, history
"Task created -> ActionState: in_progress -> ActionState: completed", with
nothing executed.  The current-flow pass then set the keys back to ASSIGNED,
but a terminal ledger task is skipped by the sync, so AUTO-ADVANCE's ledger
check (_ca_ledger_done) read every flow-1 action as done and requested its
recipe before it ran.
"""

import sys
from unittest.mock import patch

import hartos.create_recipe  # noqa: F401  (ensure cached)
from agent_ledger import InMemoryBackend, TaskStatus, create_ledger_from_actions
from hartos import lifecycle_hooks

cr = sys.modules['hartos.create_recipe']

PROMPT = '87400889007'
FLOW_0 = ['verify_device_permissions', 'fetch_current_location',
          'query_weather_api']
FLOW_1 = ['verify_device_permissions', 'fetch_activity_metrics']


def _progress(flow0_done, flow1_done):
    return {
        0: {'total_actions': len(FLOW_0), 'completed_actions': flow0_done,
            'flow_complete': len(flow0_done) == len(FLOW_0),
            'last_completed_action': max(flow0_done or [0])},
        1: {'total_actions': len(FLOW_1), 'completed_actions': flow1_done,
            'flow_complete': False,
            'last_completed_action': max(flow1_done or [0])},
    }


def _resume_into_flow_1(user_prompt, progress):
    ledger = create_ledger_from_actions(
        agent_id=PROMPT, session_id=user_prompt + '_1', actions=FLOW_1,
        backend=InMemoryBackend(), flow_id=1)
    lifecycle_hooks.register_ledger_for_session(user_prompt, ledger)
    config = {'flows': [{'actions': FLOW_0}, {'actions': FLOW_1}]}
    with patch.object(cr, 'get_prompt_config_json', return_value=config):
        cr.set_states_from_progress(user_prompt, PROMPT, 1, progress)
    return ledger


def test_earlier_flows_are_not_replayed_into_the_current_ledger():
    """THE LIVE SHAPE: flow 0 banked, flow 1 not started."""
    up = 'u_resume_live_%d' % id(object())
    ledger = _resume_into_flow_1(up, _progress([1, 2, 3], []))
    statuses = {tid: t.status for tid, t in ledger.tasks.items()}
    assert statuses == {'action_1': TaskStatus.PENDING,
                        'action_2': TaskStatus.PENDING}, (
        "flow 0's completed history was synced into flow 1's ledger: %s" % statuses)
    assert cr.get_action_state(up, 1) == cr.ActionState.ASSIGNED
    assert cr.get_action_state(up, 2) == cr.ActionState.ASSIGNED


def test_the_current_flows_banked_actions_are_still_terminated():
    """What the resume IS for: an action whose file exists is done."""
    up = 'u_resume_banked_%d' % id(object())
    ledger = _resume_into_flow_1(up, _progress([1, 2, 3], [1]))
    assert cr.get_action_state(up, 1) == cr.ActionState.TERMINATED
    assert cr.get_action_state(up, 2) == cr.ActionState.ASSIGNED
    assert ledger.tasks['action_1'].status == TaskStatus.COMPLETED
    assert ledger.tasks['action_2'].status == TaskStatus.PENDING
