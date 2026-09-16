"""A multi-persona agent's role group must execute update_persona.

create_agents_for_role registers update_persona for execution on Helper
only.  Its state_transition, however, handed every Assistant turn to
user_proxy, which has no LLM and max_consecutive_auto_reply=0, so it
replies None and ends the chat.  The Assistant's call was never
executed, the role chat never reached 'terminate', and chat_agent kept
the agent in persona selection on every turn.

Live 2026-09-13 23:22:02 (MathCoach Coco, 20260824301): the model called
update_persona(name='Teacher', new=true), and "INSIDE update_persona"
appears 0 times across all six app logs.  140 of 751 prompt files have
more than one persona, so they all enter this group first.

    python -m pytest tests/unit/test_role_group_runs_update_persona.py --noconftest -q
"""
import json
from pathlib import Path

import pytest

_REUSE_SRC = (Path(__file__).resolve().parents[2] /
              'hartos' / 'reuse_recipe.py').read_text(encoding='utf-8')


class _FakeHistory:
    def __init__(self):
        self.messages = []

    def add_message(self, msg, metadata=None):
        self.messages.append(msg)

    def search_by_metadata(self, **_kw):
        return []


@pytest.fixture
def role_group(tmp_path, monkeypatch):
    import flask
    import hartos.reuse_recipe as rr
    import integrations.channels.memory.shared_history as sh

    prompt_id = 424243
    prompt = tmp_path / f'{prompt_id}.json'
    prompt.write_text(json.dumps({'personas': [
        {'name': 'Teacher', 'description': 'Explains fractions.'},
        {'name': 'Student', 'description': 'Asks one question.'},
    ]}), encoding='utf-8')
    monkeypatch.setattr(rr.helper_fun, 'safe_prompt_path',
                        lambda *parts, **kw: str(prompt))
    monkeypatch.setattr(rr, 'get_coding_workspace_dir', lambda: str(tmp_path))
    monkeypatch.setattr(sh, '_get_persistent_history',
                        lambda user_id: _FakeHistory())
    key = f'u-persona_{prompt_id}'
    with flask.Flask('role-persona-test').app_context():
        try:
            yield rr, rr.create_agents_for_role('u-persona', prompt_id), key
        finally:
            rr.agents_session.pop(key, None)
            rr.agents_roles.pop(key, None)


def _calls_update_persona(recipient, messages=None, sender=None, config=None):
    """What the model sent live: pick a persona and start a new chat."""
    return True, {'content': None, 'tool_calls': [{
        'id': 'call_persona_1', 'type': 'function',
        'function': {'name': 'update_persona', 'arguments': json.dumps({
            'name': 'Teacher', 'description': 'Explains fractions.',
            'new': True, 'contact_number': '555-0100'})}}]}


def test_the_persona_call_is_executed_and_ends_the_role_chat(role_group):
    import autogen
    rr, agents, key = role_group
    assistant, user_proxy, group_chat, manager, _helper, stop = agents
    assert stop is False, 'two personas must build the role group'
    assistant.register_reply([autogen.Agent, None], _calls_update_persona,
                             position=0)
    # The call chat_agent makes on the role path.
    user_proxy.initiate_chat(manager, message='hello from the walk',
                             speaker_selection={"speaker": "assistant"},
                             clear_history=False)
    assert key in rr.agents_session, (
        "update_persona never ran: the Assistant's call was not routed to "
        "Helper, the only agent that executes it")
    last = group_chat.messages[-1]
    assert 'terminate' in (last.get('content') or '').lower(), (
        "chat_agent leaves persona selection only on 'terminate'; the role "
        f"chat ended on {last!r}")


def test_one_tool_call_routing_rule():
    """Both reuse state_transitions route calls through one helper."""
    import ast
    tree = ast.parse(_REUSE_SRC)
    owners = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call)
                        and getattr(node.func, 'attr', None) == 'can_execute_function'):
                    owners.append(fn.name)
    # ast.walk visits a nested function inside its parent too, so name the
    # innermost owner: the helper is module-level and holds the only call.
    assert set(owners) == {'_agent_that_executes'}, (
        "the tool-call routing rule is written out outside "
        f"_agent_that_executes: {sorted(set(owners))}")
