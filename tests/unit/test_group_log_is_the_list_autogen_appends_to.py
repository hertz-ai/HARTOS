"""The group log reuse reads must be the list autogen appends to.

In autogen 0.2.37, GroupChatManager registers run_chat with config=groupchat,
and ConversableAgent.register_reply stores copy.copy(config)
(conversable_agent.py:364).  run_chat then appends every turn to that copy
(groupchat.py:1161).  A shallow copy shares the messages list until the
original rebinds the attribute, and install_history_writeback does exactly
that: group_chat.messages = HookedMessageList(...).  Both reuse factories
installed it AFTER building the manager.  The manager kept appending to the old
list, the list reuse reads got nothing, and the write-back hook never ran.

Measured 2026-09-13: five /chat POSTs raised IndexError at chat_agent's
`last_message = group_chat.messages[-1]` (22:24:56, 23:01:38, 23:01:48,
23:16:30, 23:22:02).  Two of them were the walks of 12165936867 and
20260824301, and Nunba reported each as "your local AI is busy".  Before
10b40bcff scoped the seed to the agent, the same unappended list held 8
seeded messages from other agents, and [-1] returned one of those as this
agent's reply.  #725 ("the group log gets zero appends") is the same defect on
the main reuse groups.

    python -m pytest tests/unit/test_group_log_is_the_list_autogen_appends_to.py --noconftest -q
"""
import ast
import json
from pathlib import Path

import pytest

_REUSE_SRC = (Path(__file__).resolve().parents[2] /
              'hartos' / 'reuse_recipe.py').read_text(encoding='utf-8')


class _FakeHistory:
    """Stands in for PersistentChatHistory: records what the hook writes."""

    def __init__(self):
        self.messages = []
        self.metadata = []

    def add_message(self, msg, metadata=None):
        self.messages.append(msg)
        self.metadata.append(metadata or {})

    def search_by_metadata(self, **_kw):
        return []


def _canned(text):
    def _reply(recipient, messages=None, sender=None, config=None):
        return True, text
    return _reply


def test_a_late_rebind_detaches_the_group_from_its_manager():
    """autogen's behaviour the fix rests on: rebinding group_chat.messages
    once the manager exists leaves the manager appending elsewhere."""
    import autogen
    a = autogen.ConversableAgent('a', llm_config=False,
                                 human_input_mode='NEVER',
                                 default_auto_reply='from a')
    b = autogen.ConversableAgent('b', llm_config=False,
                                 human_input_mode='NEVER',
                                 default_auto_reply='from b')
    gc = autogen.GroupChat(agents=[a, b], messages=[], max_round=2,
                           speaker_selection_method='round_robin')
    mgr = autogen.GroupChatManager(groupchat=gc, llm_config=False)
    gc.messages = list(gc.messages)  # what a hook installed late does
    a.initiate_chat(mgr, message='ping')
    assert gc.messages == []


@pytest.fixture
def role_group(tmp_path, monkeypatch):
    import flask
    import hartos.reuse_recipe as rr
    import integrations.channels.memory.shared_history as sh

    prompt_id = 424242
    prompt = tmp_path / f'{prompt_id}.json'
    prompt.write_text(json.dumps({'personas': [
        {'name': 'Teacher', 'description': 'Explains fractions.'},
        {'name': 'Student', 'description': 'Asks one question.'},
    ]}), encoding='utf-8')
    monkeypatch.setattr(rr.helper_fun, 'safe_prompt_path',
                        lambda *parts, **kw: str(prompt))
    monkeypatch.setattr(rr, 'get_coding_workspace_dir', lambda: str(tmp_path))
    history = _FakeHistory()
    monkeypatch.setattr(sh, '_get_persistent_history', lambda user_id: history)

    with flask.Flask('role-group-test').app_context():
        yield rr.create_agents_for_role('u-role', prompt_id), history, prompt_id


def _run_role_turn(agents, text):
    import autogen
    assistant, user_proxy, group_chat, manager, _helper, stop = agents
    assert stop is False, 'two personas must build the role group'
    assistant.register_reply([autogen.Agent, None],
                             _canned('Which persona are you: Teacher or Student?'),
                             position=0)
    # The call chat_agent makes on the role path.
    user_proxy.initiate_chat(manager, message=text,
                             speaker_selection={"speaker": "assistant"},
                             clear_history=False)
    return group_chat


def test_role_group_log_holds_the_turn_chat_agent_reads(role_group):
    agents, _history, _pid = role_group
    group_chat = _run_role_turn(agents, 'hello from the walk')
    contents = [m.get('content') for m in group_chat.messages]
    assert contents[:2] == ['hello from the walk',
                            'Which persona are you: Teacher or Student?'], (
        "chat_agent reads group_chat.messages[-1] after this chat; the list "
        f"it reads holds {contents!r}")


def test_role_group_turn_reaches_the_agents_history(role_group):
    agents, history, prompt_id = role_group
    _run_role_turn(agents, 'hello from the walk')
    written = [(m.content, md.get('prompt_id'))
               for m, md in zip(history.messages, history.metadata)]
    assert ('hello from the walk', prompt_id) in written, (
        "the role group's turn never reached the shared history, so the "
        f"agent's next seed cannot see it: {written!r}")


def _hook_and_manager_lines(fn_name):
    tree = ast.parse(_REUSE_SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == fn_name)
    managers, installs = [], []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = (getattr(node.func, 'attr', None)
                    or getattr(node.func, 'id', None))
            if name == 'GroupChatManager':
                managers.append(node.lineno)
            elif name == 'install_history_writeback':
                installs.append(node.lineno)
    return managers, installs


@pytest.mark.parametrize('fn_name', ['create_agents_for_role',
                                     'create_agents_for_user'])
def test_every_history_hook_is_installed_before_any_manager(fn_name):
    managers, installs = _hook_and_manager_lines(fn_name)
    assert managers and installs, (
        f"{fn_name}: no manager or no write-back found; the guard would be "
        "vacuous")
    assert max(installs) < min(managers), (
        f"{fn_name}: write-back installed at line {max(installs)}, after the "
        f"GroupChatManager at line {min(managers)}. The manager keeps a copy "
        "of the group and appends there, so the rebound list never receives "
        "a message")
