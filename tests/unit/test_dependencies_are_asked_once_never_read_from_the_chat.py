"""A flow's dependencies are asked once, statelessly, and never read from the chat.

LIVE (2026-11-25/26, installed build, speech agent 95568353503): at each flow
end the create loop put "Check if the current_action depends on any other
action ..." to the flow's agents, one action at a time, in the build's group
chat with their tools live, and took the FIRST bracketed list found in ANY of
the last three messages as the answer.  The agents talked to the child while
they answered ("பந்து என்ன?"), and the child's reply was read as the answer:
flow 2's action 9 was saved as depending on ['பந்து'] (2_9.json) and flow 4's
action 8 on ['அம்மா', 'பந்து'] (4_8.json).  helper.topological_sort then
raised KeyError on the word and the person heard
"I couldn't finish that: 'அம்மா'".

Now the question is ONE stateless call to the node's model
(core.llm_json.ask_model_json, as the open-mic judge asks) whose schema holds
every answer to an integer array; the answer keeps only this flow's other
actions by number (no bool, no word, no id out of range, never the action
itself, each once), every drop is logged, and the step posts and changes no
chat message.  A list already saved with words in it is read the same way:
topological_sort skips them with a warning instead of raising.

    python -m pytest tests/unit/test_dependencies_are_asked_once_never_read_from_the_chat.py -q -p no:cacheprovider
"""
import json
import logging

import pytest

from tests.unit.test_create_loop_end_to_end import (  # noqa: F401  (fixture)
    ACTIONS, PROMPT_ID, UP, _action_file, _content, _run, create_env)

_QUESTION = 'Check if the current_action depends'


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


def test_a_saved_word_is_skipped_by_the_sort_not_raised(caplog):
    """The live saved lists: flow 4's action 8 depends on two of the child's
    words.  The sort reads them as the writer now reads the model's answer:
    no word, no id with no action, never the action itself, each id once, a
    string of digits is its number."""
    from hartos.helper import topological_sort
    actions = [
        {'action_id': 1, 'actions_this_action_depends_on': []},
        {'action_id': 2, 'actions_this_action_depends_on': ['அம்மா', 'பந்து']},
        {'action_id': 3, 'actions_this_action_depends_on': ['2', True, 9, 3, 1, 1]},
    ]
    with caplog.at_level(logging.WARNING):
        ok, ordered, cyclic = topological_sort(actions)
    assert ok is True and cyclic is None
    ids = [a['action_id'] for a in ordered]
    assert sorted(ids) == [1, 2, 3]
    assert ids.index(2) < ids.index(3), ids
    assert ids.index(1) < ids.index(3), ids
    log = '\n'.join(r.getMessage() for r in caplog.records)
    for skipped in ("'அம்மா' (not an action id)", "'பந்து' (not an action id)",
                    'True (not an action id)', '9 (no saved action)',
                    '3 (the action itself)', '1 (already listed)'):
        assert skipped in log, f'{skipped!r} was not logged:\n{log}'


def test_the_question_is_one_stateless_call_and_the_chat_is_not_touched(
        create_env, monkeypatch, caplog):
    env = create_env
    script = env.script

    # The old step put the question to the agents.  Answer it the way the
    # live chat did: the child's word in brackets.
    agent_reply = script.assistant

    def assistant(recipient, messages=None, sender=None, config=None):
        if _QUESTION in _content(script._last()):
            return True, "['பந்து']"
        return agent_reply(recipient, messages=messages, sender=sender, config=config)
    script.assistant = assistant

    def chat_now():
        return [(m.get('name'), m.get('role'), _content(m))
                for m in script.gc.messages]

    # The chat as the flow end found it: right after the last action's
    # pointer moved past it, before the dependency step.
    at_flow_end = []
    real_increment = env.cr.safe_increment_action

    def increment(user_prompt):
        real_increment(user_prompt)
        at_flow_end.append(chat_now())
    monkeypatch.setattr(env.cr, 'safe_increment_action', increment)

    asked = []

    def model(content, schema, name, **kw):
        if name != 'action_dependencies':
            # The flow's schedule, asked at the same flow end: none.
            return {key: [] for key in schema['properties']}
        # The turn's request id, as core.http_pool reads it for the model's
        # scheduler: with none the call queues as background work.
        from core.llm_outbound_logger import _get_request_id
        asked.append({'content': content, 'schema': schema, 'name': name,
                      'kw': kw, 'request_id': _get_request_id()})
        # A server that does not hold its reply to the schema: words, a
        # bool, a float, an id with no action, the action's own id, repeats.
        return {'1': ['பந்து'], '2': [1, True, '1', 2, 9],
                '3': [2.0, 'அம்மா', '2']}
    import core.llm_json
    monkeypatch.setattr(core.llm_json, 'ask_model_json', model)

    # The chat as the dependency step left it: when the sort reads the files.
    at_sort = []
    real_sort = env.cr.topological_sort

    def sort(actions):
        at_sort.append(chat_now())
        return real_sort(actions)
    monkeypatch.setattr(env.cr, 'topological_sort', sort)

    with caplog.at_level(logging.INFO):
        replies = _run(env, turns=1)

    assert (env.prompts / f'{PROMPT_ID}_0_recipe.json').exists(), (
        f'the flow end did not save the flow recipe; replies {replies!r}')
    assert replies == ['Agent Created Successfully'], replies

    deps = {n: (_action_file(env, n) or {}).get('actions_this_action_depends_on')
            for n in (1, 2, 3)}
    assert deps == {1: [], 2: [1], 3: [2]}, deps

    assert len(asked) == 1, f'expected one question for the flow, got {len(asked)}'
    schema = asked[0]['schema']
    assert sorted(schema['properties']) == ['1', '2', '3'], schema
    assert sorted(schema['required']) == ['1', '2', '3'], schema
    for answer in schema['properties'].values():
        assert answer == {'type': 'array', 'items': {'type': 'integer'}}, schema
    # One send on the turn's own thread, its wait sized to the answer.
    kw = asked[0]['kw']
    assert asked[0]['request_id'] == 'daemon_e2e_create', asked[0]['request_id']
    assert kw.get('once') is True, kw
    assert kw.get('timeout') == pytest.approx(15.0 + kw['max_tokens'] / 20.0), kw

    assert not any(_QUESTION in _content(m) for m in script.gc.messages), (
        'the dependency question was posted to the build chat')
    assert at_flow_end and at_sort, (at_flow_end, at_sort)
    assert at_sort[0] == at_flow_end[-1], (
        'the dependency step added or changed a chat message')

    log = '\n'.join(r.getMessage() for r in caplog.records)
    for dropped in ("'பந்து' (not an action id)", 'True (not an action id)',
                    "'1' (already listed)", '2 (the action itself)',
                    '9 (no saved action)', '2.0 (not an action id)',
                    "'அம்மா' (not an action id)"):
        assert dropped in log, f'{dropped!r} was not logged as dropped'


def test_a_step_that_gave_up_is_left_out_of_the_flow_end_not_a_crash(
        create_env, monkeypatch, caplog):
    """Live 2026-11-26, flow 5 of agent 95568353503: action 3 gave up at
    03:30:26 (completion claimed without a verifiable receipt), so no
    5_3.json was banked, and actions 4-9 were.  At 03:44:44 the flow end's
    dependency step opened 5_3.json and raised; the person read
    "[Errno 2] No such file or directory: ...5_3.json".  Here the completion
    gate gives step 2 up the way it gave flow 5's step 3 up: the verifier
    hook records GAVE_UP ("completion claimed without a verifiable receipt")
    and answers 'gave_up', and the loop moves on to step 3.  The model's
    call also times out here (ask_model_json answers None): every asked
    action keeps [] and the warning says so."""
    env = create_env
    from hartos.lifecycle_hooks import ActionState
    real_hook = env.cr.lifecycle_hook_process_verifier_response

    def gate(user_prompt, json_obj, user_tasks):
        if (user_prompt == UP and env.cr.get_current_flow(UP) == 0
                and env.cr.user_tasks[UP].current_action == 2):
            env.lh.force_state_through_valid_path(
                UP, 2, ActionState.GAVE_UP,
                'completion claimed without a verifiable receipt')
            return {'action': 'gave_up', 'message': (
                'Action 2 could not be verified: no completed verdict for it '
                'cited a real result.')}
        return real_hook(user_prompt, json_obj, user_tasks)
    monkeypatch.setattr(env.cr, 'lifecycle_hook_process_verifier_response', gate)
    asked = []

    def model(content, schema, name, **kw):
        if name == 'action_dependencies':
            asked.append(sorted(schema['properties']))
        return None
    import core.llm_json
    monkeypatch.setattr(core.llm_json, 'ask_model_json', model)

    with caplog.at_level(logging.INFO):
        replies = _run(env, turns=1)

    assert env.lh.get_action_state(UP, 2).value == 'gave_up', (
        f'precondition: step 2 gave up; replies {replies!r}')
    assert _action_file(env, 2) is None, 'precondition: no file for a step that gave up'
    assert 'No such file or directory' not in str(replies), replies
    path = env.prompts / f'{PROMPT_ID}_0_recipe.json'
    assert path.exists(), f'the flow end did not finish; replies {replies!r}'
    saved = json.loads(path.read_text(encoding='utf-8'))
    assert [a.get('action_id') for a in saved['actions']] == [1, 3], saved
    assert saved.get('actions_not_saved') == [
        {'action_id': 2, 'action': ACTIONS[1], 'state': 'gave_up'}], saved
    assert asked == [['1', '3']], asked
    assert [_action_file(env, n).get('actions_this_action_depends_on')
            for n in (1, 3)] == [[], []]
    log = '\n'.join(r.getMessage() for r in caplog.records)
    assert '[DEPENDENCIES] flow 0 action 2 has no saved step (gave_up); left out' in log, log[-3000:]
    assert ('[DEPENDENCIES] flow 0: the model gave no answer; actions [1, 3] '
            'are saved with no dependencies') in log, log[-3000:]
