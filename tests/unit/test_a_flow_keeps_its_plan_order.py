"""A flow keeps its plan order wherever its dependencies allow, and always saves its actions.

Found while fixing the flow end's dependency step (2026-11-26): the old step
almost always saved [] (every saved recipe of speech agent 95568353503's flows
0, 1 and 3 has no dependency at all), which hid two defects that the model's
real answers now reach:

- helper.topological_sort took ready actions first in, first out, so every
  action with a dependency ran after every dependency-free action behind it:
  "2 needs 1, 4 needs 3" ordered the flow 1, 3, 2, 4, and a speech session's
  steps would be played out of order.  Ready actions now run in plan order,
  so only a dependency on a later action reorders, and each reorder is
  logged with the action that moved and what is now ahead of it.
- A cycle the model cannot untie (fix_actions answers None when its reply
  does not parse) left the order None, and _save_flow_recipe saved the flow
  recipe with "actions": None, so REUSE had no step to run for that flow.
  The plan order is kept with a warning, and the saved "actions" is always a
  list.

    python -m pytest tests/unit/test_a_flow_keeps_its_plan_order.py -q -p no:cacheprovider
"""
import json
import logging
from types import SimpleNamespace

import pytest

from tests.unit.test_create_loop_end_to_end import (  # noqa: F401  (fixture)
    ACTIONS, PROMPT_ID, UP, USER_ID, _run, create_env)


@pytest.fixture
def idle_desktop(monkeypatch):
    """tests/conftest.py's idle_desktop, here so the file also runs under
    --noconftest: nobody is at this computer, so no background gate says stop."""
    from core import resource_governor
    monkeypatch.setattr(resource_governor, 'get_physical_input_state',
                        lambda **_kw: (0, None))
    from integrations.agent_engine import dispatch
    monkeypatch.setattr(dispatch, 'person_using_the_machine', lambda: None)
    monkeypatch.setattr(dispatch, 'should_yield_to_user', lambda: False)


def _plan(deps):
    """Actions 1..n in plan order, action k depending on deps[k-1]."""
    return [{'action_id': n, 'actions_this_action_depends_on': d}
            for n, d in enumerate(deps, 1)]


def _sort(deps, caplog):
    from hartos.helper import topological_sort
    with caplog.at_level(logging.WARNING):
        ok, ordered, cyclic = topological_sort(_plan(deps))
    assert ok is True and cyclic is None
    log = '\n'.join(r.getMessage() for r in caplog.records)
    return [a['action_id'] for a in ordered], log


def test_a_dependency_on_an_earlier_action_keeps_the_plan_order(caplog):
    order, log = _sort([[], [1], [], [3]], caplog)
    assert order == [1, 2, 3, 4], order
    assert 'moved behind' not in log, log


def test_a_dependency_on_a_later_action_moves_that_action_and_says_so(caplog):
    order, log = _sort([[3], [], [], []], caplog)
    assert order == [2, 3, 1, 4], order
    assert '[DEPENDENCIES] action 1 moved behind action(s) [2, 3]' in log, log


def test_a_dependency_on_itself_is_dropped_and_moves_nothing(caplog):
    order, log = _sort([[], [2], []], caplog)
    assert order == [1, 2, 3], order
    assert '2 (the action itself)' in log, log
    assert 'moved behind' not in log, log


def test_an_empty_plan_sorts_to_nothing():
    from hartos.helper import topological_sort
    assert topological_sort([]) == (True, [], None)


def test_a_cycle_the_model_cannot_untie_keeps_the_plan_order(
        create_env, monkeypatch, caplog):
    """Action 1 named 2 (and 9, which is no action, and itself), action 2
    named 1: a cycle.  The harness's model gives fix_actions no parsable
    answer, so the cycle survives the fix attempt."""
    env = create_env

    def model(content, schema, name, **kw):
        return {'1': [2, 9, 1], '2': [1], '3': []}
    import core.llm_json
    monkeypatch.setattr(core.llm_json, 'ask_model_json', model)

    seen = []
    real_sort = env.cr.topological_sort

    def sort(actions):
        seen.append({a['action_id']: list(a.get('actions_this_action_depends_on') or [])
                     for a in actions})
        return real_sort(actions)
    monkeypatch.setattr(env.cr, 'topological_sort', sort)

    with caplog.at_level(logging.INFO):
        replies = _run(env, turns=1)

    path = env.prompts / f'{PROMPT_ID}_0_recipe.json'
    assert path.exists(), replies
    saved = json.loads(path.read_text(encoding='utf-8'))
    assert isinstance(saved.get('actions'), list), saved.get('actions')
    assert [a.get('action_id') for a in saved['actions']] == [1, 2, 3], saved
    log = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'still depend on each other in a cycle; the flow keeps the plan order' in log
    # The filter dropped the id with no action and the action's own id
    # before any sort read the files.
    assert seen, 'the flow end never sorted its actions'
    for deps in seen:
        for aid, named in deps.items():
            assert aid not in named, (aid, named)
            assert all(1 <= d <= len(ACTIONS) for d in named), (aid, named)


def _saved_flow(env, final_actions, files):
    """_save_flow_recipe for flow 0 with ``final_actions`` as the scheduler
    step's order and action files 1..files on disk."""
    from hartos.helper import Action
    for n in range(1, files + 1):
        (env.prompts / f'{PROMPT_ID}_0_{n}.json').write_text(json.dumps({
            'status': 'done', 'action': ACTIONS[n - 1], 'action_id': n,
            'recipe': [], 'actions_this_action_depends_on': []}),
            encoding='utf-8')
    env.cr.user_tasks[UP] = Action(list(ACTIONS))
    env.cr.final_recipe[PROMPT_ID] = {'status': 'completed', 'actions': final_actions}
    try:
        with env.app.app_context():
            env.cr._save_flow_recipe(0, PROMPT_ID, UP, USER_ID,
                                     SimpleNamespace(messages=[]))
    finally:
        env.cr.final_recipe.pop(PROMPT_ID, None)
    return json.loads((env.prompts / f'{PROMPT_ID}_0_recipe.json')
                      .read_text(encoding='utf-8'))


def test_the_flow_recipe_always_lists_its_actions(create_env):
    saved = _saved_flow(create_env, None, len(ACTIONS))
    assert isinstance(saved.get('actions'), list), saved
    assert [a['action_id'] for a in saved['actions']] == [1, 2, 3], saved


def test_an_empty_flow_saves_an_empty_list(create_env):
    saved = _saved_flow(create_env, None, 0)
    assert saved.get('actions') == [], saved
