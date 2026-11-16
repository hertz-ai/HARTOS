"""The Actions list in a REUSE seat's system prompt names a trace-saved
step's tool and argument names, never the creation run's values.

WHAT WENT WRONG (desktop build 75, 14 Nov 2026, llm_outbound.jsonl)
  Two agents sent their creation session's words to people who had never
  spoken to them.  The cafe coach (1793372451267) answered a new person's
  "Hi, I want to practise ordering at a cafe." with "Of course, Meera! A
  cappuccino and croissant sounds delicious ..." (21:06 IST, request
  msg_1794670543717_sim), and the singing agent (1793373034536) sang its
  creation run's "Red ball red ball bounce so high" to a new child.
  The system message held the cappuccino line twice, both times inside the
  "Actions: <actionsStart>...<actionEnd>" section.  That section is built from
  {prompt}_{flow}_recipe.json, whose trace-saved steps keep the creation call
  word for word: send_message_to_user({"text": "Of course, Meera! ..."}).
  fd4a8c353 had already shown the other two copies of a saved recipe without
  values (the dispatch message's steps and the generalized-functions block),
  but not this third one.

THE RULE
  Every copy of a saved recipe a REUSE model is shown reads a trace-saved step
  as "tool(arg, arg)".  The action's own title is unchanged, so a message the
  step quotes word for word (the Tamil speech agent's prompts) still reaches
  the model.  The recipe the ledger runs, and the file on disk, keep the saved
  call.

    python -m pytest tests/unit/test_the_actions_list_carries_no_creation_values.py -q --noconftest
"""
import json
from unittest import mock

import pytest

from tests.unit.test_create_loop_end_to_end import (  # noqa: F401  (fixture)
    PROMPT_ID, UP, USER_ID, create_env)

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


_SAID = 'Of course, Meera! A cappuccino and croissant sounds delicious.'
_SAVED_SEND = ('send_message_to_user({"expects_reply":false,"text":"'
               + _SAID + '"})')
_TITLE = ("send_message_to_user: respond in the scenario to the latest user "
          "message and give exactly one pronunciation tip")
_QUOTED_TITLE = "send_message_to_user to Arjun: 'Super Arjun!'"


def _write_flow(env, steps_recipe, title=_TITLE):
    (env.prompts / f'{PROMPT_ID}_0_recipe.json').write_text(json.dumps({
        'status': 'done', 'actions': [{
            'action_id': 1, 'action': title,
            'persona': 'Researcher', 'can_perform_without_user_input': 'yes',
            'recipe_source': 'execution_trace',
            'recipe': steps_recipe}]}), encoding='utf-8')


def _build(env, monkeypatch):
    import core.llm_outbound_logger as lol
    import hartos.reuse_recipe as rr
    from apscheduler.schedulers.background import BackgroundScheduler

    monkeypatch.setattr(rr, 'HAS_SIMPLEMEM', False)
    for name in ('user_tasks', 'recipes', 'final_recipe', 'request_id_list',
                 'user_agents', 'time_actions', 'user_ledgers'):
        cache = getattr(rr, name)
        if hasattr(cache, '_loader'):
            monkeypatch.setattr(cache, '_loader', None)
        cache.clear()
    if getattr(rr, 'get_production_backend', None) is not None:
        from agent_ledger.backends import InMemoryBackend
        monkeypatch.setattr(rr, 'get_production_backend',
                            lambda *a, **k: InMemoryBackend())
    monkeypatch.setattr(lol, 'schema_token_room', lambda: 100_000)
    scheduler = mock.Mock(spec=BackgroundScheduler)
    scheduler.running = True
    monkeypatch.setattr(rr, 'scheduler', scheduler)
    with env.app.app_context():
        built = rr.create_agents_for_user(USER_ID, PROMPT_ID)
    return rr, built


def _system_messages(built):
    """Every seat's prompt that carries an Actions list: the agents returned
    and the seats inside each group chat (the Executor lives only there)."""
    seats = []
    for item in built:
        seats.append(item)
        seats.extend(getattr(item, 'agents', None) or [])
    out, seen = [], set()
    for agent in seats:
        text = getattr(agent, 'system_message', None)
        if isinstance(text, str) and 'actionsStart' in text and id(agent) not in seen:
            seen.add(id(agent))
            out.append(text)
    return out


def test_every_seat_with_an_actions_list_is_checked(create_env, monkeypatch):
    """The Assistant, Helper, Executor and time_agent prompts all carry the
    list; a check that saw fewer would let one seat keep the creation words."""
    _write_flow(create_env, [_SEND_STEP])
    rr, built = _build(create_env, monkeypatch)
    try:
        names = set()
        for item in built:
            for agent in [item] + list(getattr(item, 'agents', None) or []):
                text = getattr(agent, 'system_message', None)
                if isinstance(text, str) and 'actionsStart' in text:
                    names.add(getattr(agent, 'name', None))
        assert {'Assistant', 'Helper', 'Executor', 'time_agent'} <= names, names
    finally:
        rr.user_agents.pop(UP, None)
        rr.request_id_list.pop(UP, None)


def _actions_section(system):
    return system.split('<actionsStart>', 1)[1].split('<actionEnd>', 1)[0]


_SEND_STEP = {'steps': _SAVED_SEND, 'tool_name': 'send_message_to_user',
              'generalized_functions': '', 'agent_to_perform_this_action': 'Helper'}


def test_no_seat_is_shown_the_creation_runs_words(create_env, monkeypatch):
    _write_flow(create_env, [_SEND_STEP])
    rr, built = _build(create_env, monkeypatch)
    try:
        seats = _system_messages(built)
        assert seats, 'no seat prompt carries an Actions section'
        for system in seats:
            actions = _actions_section(system)
            assert 'cappuccino' not in actions, (
                "a seat's Actions list carries the creation run's words: a new "
                "person is answered with another person's conversation")
            assert 'Meera' not in actions
            assert 'send_message_to_user(expects_reply, text)' in actions
    finally:
        rr.user_agents.pop(UP, None)
        rr.request_id_list.pop(UP, None)


def test_the_action_title_still_reaches_every_seat(create_env, monkeypatch):
    _write_flow(create_env, [_SEND_STEP])
    rr, built = _build(create_env, monkeypatch)
    try:
        for system in _system_messages(built):
            assert 'pronunciation tip' in _actions_section(system)
    finally:
        rr.user_agents.pop(UP, None)
        rr.request_id_list.pop(UP, None)


def test_a_message_the_title_quotes_still_reaches_the_seat(create_env, monkeypatch):
    """The speech agent's shape: the words to say live in the title."""
    _write_flow(create_env, [dict(_SEND_STEP, steps=(
        'send_message_to_user({"text":"Super Arjun!"})'))], title=_QUOTED_TITLE)
    rr, built = _build(create_env, monkeypatch)
    try:
        for system in _system_messages(built):
            assert "'Super Arjun!'" in _actions_section(system)
    finally:
        rr.user_agents.pop(UP, None)
        rr.request_id_list.pop(UP, None)


def test_the_ledger_and_the_file_keep_the_saved_call(create_env, monkeypatch):
    _write_flow(create_env, [_SEND_STEP])
    rr, built = _build(create_env, monkeypatch)
    try:
        assert rr.recipes[UP]['actions'][0]['recipe'][0]['steps'] == _SAVED_SEND
        on_disk = json.loads((create_env.prompts / f'{PROMPT_ID}_0_recipe.json')
                             .read_text(encoding='utf-8'))
        assert on_disk['actions'][0]['recipe'][0]['steps'] == _SAVED_SEND
    finally:
        rr.user_agents.pop(UP, None)
        rr.request_id_list.pop(UP, None)


def test_a_model_authored_step_is_shown_as_written(create_env, monkeypatch):
    (create_env.prompts / f'{PROMPT_ID}_0_recipe.json').write_text(json.dumps({
        'status': 'done', 'actions': [{
            'action_id': 1, 'action': _TITLE, 'persona': 'Researcher',
            'can_perform_without_user_input': 'yes',
            'recipe': [{'steps': 'greet the person warmly by their name',
                        'tool_name': 'send_message_to_user'}]}]}),
        encoding='utf-8')
    rr, built = _build(create_env, monkeypatch)
    try:
        for system in _system_messages(built):
            assert 'greet the person warmly by their name' in _actions_section(system)
    finally:
        rr.user_agents.pop(UP, None)
        rr.request_id_list.pop(UP, None)
