"""A flow whose end failed is ended by the next turn, not left open.

LIVE (2026-11-26, installed build, speech agent 95568353503): flow 4's end
raised KeyError in topological_sort at 01:17:56, after the action pointer had
moved past the flow's last action (current_action 15 of 14) and before the
flow recipe was saved or the flow index moved.  The session kept its open
group chat, which ended on a tool call with no response.  Each later turn
continued that chat; the create loop then read "current action 15 is greater
than length 14" and broke out, so the flow never ended.  At 02:40:51 and
02:46:43 the person was told '"Speech Therapy Companion" is not finished yet
... ran again from its saved plan'; flow 5 never started.

Now the loop ends the flow there with the sequence a finished last action
runs (create_recipe._finish_flow): the flow recipe is written unless it is on
disk already, dependencies are asked only for action files without them, the
flow index moves and the next flow runs in the same turn.  The scheduler step
and the next flow run on the scripted agents of the create-loop harness; the
model's dependency answer is a stand-in for core.llm_json.ask_model_json.

    python -m pytest tests/unit/test_a_flow_end_that_failed_ends_on_the_next_turn.py -q -p no:cacheprovider
"""
import json
import logging

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


def _path(env, flow, part):
    return env.prompts / f'{PROMPT_ID}_{flow}_{part}.json'


def _two_flows(env):
    """The agent's plan has a second flow, as the live one had ten."""
    config_path = env.prompts / f'{PROMPT_ID}.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config['flows'] = [
        {'persona': 'Researcher', 'sub_goal': 'weekly summary', 'actions': ACTIONS},
        {'persona': 'Researcher', 'sub_goal': 'monthly summary', 'actions': ACTIONS},
    ]
    config_path.write_text(json.dumps(config), encoding='utf-8')


def _the_model_answers(env, monkeypatch):
    """The stateless dependency question's stand-in: no dependencies, and a
    record of which flow asked about which actions."""
    asked = []

    def model(content, schema, name, **kw):
        asked.append((env.cr.get_current_flow(UP), sorted(schema['properties'])))
        return {key: [] for key in schema['properties']}
    import core.llm_json
    monkeypatch.setattr(core.llm_json, 'ask_model_json', model)
    return asked


def _the_first_flow_end_fails(env, monkeypatch):
    """The live failure: the flow end raises inside the sort, once."""
    real_sort = env.cr.topological_sort
    calls = []

    def sort(actions):
        calls.append(1)
        if len(calls) == 1:
            raise KeyError('அம்மா')
        return real_sort(actions)
    monkeypatch.setattr(env.cr, 'topological_sort', sort)


def _the_flow_end_failed(env):
    """Turn 1 runs flow 0 to its end and the end fails: the live state."""
    first = _run(env, turns=1)
    tasks = env.cr.user_tasks[UP]
    assert tasks.current_action == len(ACTIONS) + 1, (
        f'precondition: the pointer is past the last action; {first!r}')
    assert env.cr.get_current_flow(UP) == 0, first
    assert not _path(env, 0, 'recipe').exists(), first
    for n in range(1, len(ACTIONS) + 1):
        assert _path(env, 0, n).exists(), f'precondition: action {n} is saved'
    # The live chat ended on a tool call that no turn answered, and the
    # session kept that chat (871 messages before init at 02:40:51): the next
    # turn continues it.
    env.script.gc.messages.append({
        'role': 'assistant', 'name': 'Assistant', 'content': '',
        'tool_calls': [{'id': 'call_left_open', 'type': 'function',
                        'function': {'name': 'send_message_to_user',
                                     'arguments': json.dumps({'text': 'பந்து என்ன?'})}}]})
    env.cr.messages[UP] = env.script.gc.messages
    return first


def _drop_dependencies(env, flow, numbers):
    for n in numbers:
        path = _path(env, flow, n)
        data = json.loads(path.read_text(encoding='utf-8'))
        data.pop('actions_this_action_depends_on', None)
        path.write_text(json.dumps(data), encoding='utf-8')


def test_the_next_turn_saves_the_flow_and_starts_the_next(create_env, monkeypatch):
    env = create_env
    _two_flows(env)
    asked = _the_model_answers(env, monkeypatch)
    _the_first_flow_end_fails(env, monkeypatch)
    first = _the_flow_end_failed(env)
    # Action 2's file has no dependencies yet; actions 1 and 3 keep theirs.
    _drop_dependencies(env, 0, [2])
    asked.clear()

    second = _run(env, turns=1)

    assert _path(env, 0, 'recipe').exists(), (
        f'flow 0 was left open: no flow recipe after the next turn; '
        f'turn 1 {first!r}, turn 2 {second!r}')
    assert _path(env, 1, 1).exists(), (
        f'flow 1 did not start on the next turn; turn 2 {second!r}')
    assert [keys for flow, keys in asked if flow == 0] == [['2']], (
        f'flow 0 should ask only about the action without dependencies: {asked}')


def test_a_flow_whose_recipe_is_saved_starts_the_next_and_asks_nothing(
        create_env, monkeypatch):
    """The reconciler (core.flow_recipe_reconcile) saved flow 4's recipe from
    its action files at 01:23:40, six minutes after the failed flow end."""
    env = create_env
    _two_flows(env)
    asked = _the_model_answers(env, monkeypatch)
    _the_first_flow_end_fails(env, monkeypatch)
    _the_flow_end_failed(env)
    _drop_dependencies(env, 0, range(1, len(ACTIONS) + 1))
    saved = {'status': 'completed', 'actions': [
        json.loads(_path(env, 0, n).read_text(encoding='utf-8'))
        for n in range(1, len(ACTIONS) + 1)]}
    _path(env, 0, 'recipe').write_text(json.dumps(saved), encoding='utf-8')
    before = _path(env, 0, 'recipe').read_bytes()
    asked.clear()

    second = _run(env, turns=1)

    assert _path(env, 1, 1).exists(), (
        f'flow 1 did not start on the next turn; turn 2 {second!r}')
    assert [keys for flow, keys in asked if flow == 0] == [], (
        f'flow 0 was asked about its dependencies again: {asked}')
    assert _path(env, 0, 'recipe').read_bytes() == before, (
        'the saved flow recipe was rewritten')
    for n in range(1, len(ACTIONS) + 1):
        data = json.loads(_path(env, 0, n).read_text(encoding='utf-8'))
        assert 'actions_this_action_depends_on' not in data, (n, data)


def test_an_action_left_open_is_closed_with_the_flow_not_gone_round(
        create_env, monkeypatch, caplog):
    """hartos-17's check of the turn past the last action: an action left
    in a state this loop does not run (here STATUS_VERIFICATION_REQUESTED)
    must not keep the turn going round.  The flow ends the way
    [FLOW-COMPLETE] ends it: the unverified action is an honest GAVE_UP, the
    flow recipe is saved and the next flow starts, in the same turn."""
    env = create_env
    _two_flows(env)
    _the_model_answers(env, monkeypatch)
    _the_first_flow_end_fails(env, monkeypatch)
    _the_flow_end_failed(env)
    from hartos.lifecycle_hooks import ActionState
    with env.lh._state_lock:
        env.lh.action_states[UP][2] = ActionState.STATUS_VERIFICATION_REQUESTED
    seen = len(env.events)

    with caplog.at_level(logging.INFO):
        second = _run(env, turns=1)

    log = '\n'.join(r.getMessage() for r in caplog.records)
    assert '[STALL-GUARD]' not in log and '[CYCLE-GUARD]' not in log, (
        'the turn went round on the open action')
    assert _path(env, 0, 'recipe').exists(), (
        f'flow 0 was left open; turn 2 {second!r}')
    assert _path(env, 1, 1).exists(), f'flow 1 did not start; turn 2 {second!r}'
    closed = [state for aid, state, _ in env.events[seen:] if aid == 2]
    assert closed[:1] == ['gave_up'], closed


def test_the_next_flow_starts_on_no_words_of_this_turn(create_env, monkeypatch):
    """hartos-17's check: this turn's words went to the flow that ended (on a
    person's turn, the answer its last step took, as the child's "பந்து"
    did).  The next flow's steps must not get them as the person's latest
    message."""
    env = create_env
    _two_flows(env)
    _the_model_answers(env, monkeypatch)
    posted = []
    real_step = env.cr.step_instruction

    def step(action_no, action_text, user_text):
        posted.append((env.cr.get_current_flow(UP), action_no, user_text))
        return real_step(action_no, action_text, user_text)
    monkeypatch.setattr(env.cr, 'step_instruction', step)

    with env.app.app_context():
        reply = env.cr.recipe(USER_ID, 'பந்து', PROMPT_ID, None,
                              'daemon_e2e_create')

    first = [words for flow, _, words in posted if flow == 0]
    second = [words for flow, _, words in posted if flow == 1]
    assert first and second, f'both flows should run this turn: {posted}; {reply!r}'
    assert 'பந்து' in first, first
    assert not any('பந்து' in (words or '') for words in second), second
