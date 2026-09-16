"""A flow's actions keep the same task ids however often the ledger is built.

THE LIVE FAILURE (2026-09-13, installed Nunba, agent 87400889007):

    create_ledger_from_actions: resuming in-flight session ..._1789153289150
    Loaded 8 tasks from ledger backend
    Added task action_9: verify_device_permissions
    ...                                   (through action_16: return_to_idle)

That prompt's actions are plain strings, which carry no action_id, so the
factory numbered them ``len(ledger.tasks) + 1``.  That equals the 1-based
position only while the ledger is empty; after a resume has loaded the
session's 8 tasks it yields 9, 10, ...  hartos.lifecycle_hooks
._auto_sync_to_ledger addresses ``f"action_{action_id}"`` with the action's
position, so the copies are never updated, stay pending, and keep the session
"unfinished" for _find_resumable_session -- the next build resumes it again
and adds another 8.

Measured on the box the same day: 208 of 2,516 ledger files hold pre-assigned
tasks with a repeated description, 201 of them with one still non-terminal.
"""

from agent_ledger import InMemoryBackend, SmartLedger
from agent_ledger.core import create_ledger_from_actions

AGENT = '87400889007'
SESSION = 'u_87400889007_1789153289150'
FLOW_0 = ['verify_device_permissions', 'fetch_current_location',
          'query_weather_api']


def _ids(ledger):
    return sorted(ledger.tasks, key=lambda t: int(t.split('_', 1)[1]))


def _build(actions, backend):
    return create_ledger_from_actions(agent_id=AGENT, session_id=SESSION,
                                      actions=actions, backend=backend)


def test_resume_does_not_renumber_string_actions():
    """THE LIVE SHAPE: the same session built twice from the same strings."""
    backend = InMemoryBackend()
    _build(FLOW_0, backend)
    resumed = _build(FLOW_0, backend)
    assert _ids(resumed) == ['action_1', 'action_2', 'action_3'], (
        'resuming a session re-added its string actions under new ids '
        f'{_ids(resumed)} -- the copies are never synced and keep the '
        'session resumable forever')


def test_id_less_actions_take_their_position_and_explicit_ids_win():
    ledger = create_ledger_from_actions(
        agent_id=AGENT, session_id=SESSION, backend=InMemoryBackend(),
        actions=['x', {'description': 'y'}, {'action_id': 7, 'description': 'z'}])
    assert _ids(ledger) == ['action_1', 'action_2', 'action_7']


def test_a_longer_list_adds_only_the_new_positions():
    """The entire_actions reset path rebuilds a flow that has grown."""
    from agent_ledger.core import add_actions_to_ledger
    ledger = _build(FLOW_0[:2], InMemoryBackend())
    assert add_actions_to_ledger(ledger, FLOW_0) == 1
    assert _ids(ledger) == ['action_1', 'action_2', 'action_3']
    assert ledger.tasks['action_3'].description == 'query_weather_api'


def test_added_tasks_are_stamped_and_sealed_like_the_factory():
    """One conversion: a task added to an existing ledger looks like one the
    factory made -- the dashboard groups by these fields."""
    from agent_ledger.core import add_actions_to_ledger
    ledger = SmartLedger(AGENT, SESSION, backend=InMemoryBackend())
    add_actions_to_ledger(ledger, FLOW_0, flow_id=1)
    assert _ids(ledger) == ['action_1', 'action_2', 'action_3']
    for task in ledger.tasks.values():
        assert task.recipe_prompt_id == AGENT
        assert task.recipe_flow_id == 1
        assert task.data_hash is not None and task.verify_integrity()
