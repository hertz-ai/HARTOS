"""The create group's log must be the list autogen appends to, unchanged.

In autogen 0.2.37 GroupChatManager keeps copy.copy(group_chat) (register_reply)
and run_chat appends every turn to that copy, then sends the SAME dict to every
other seat. create_agents built its manager first and rebound
group_chat.messages afterwards, so the list the create loop reads never
received a turn.

Measured on central 2026-09-13 (9f83f428, Guardian Convergence 960a8332): one
[MSG-RECOVERY] rebuilt 6 messages, the length stayed 6, and loop iterations 1-3
all parsed messages[-2] as the same prose while the StatusVerifier posted
{"status": "completed", ...}. The action stayed at 1 and nothing was banked
(#99).

The hook it had also rewrote content to ASCII in place. It never ran, because
of the same late rebind; installed in time, it would have fed every seat '?'
for a Tamil user's words. The Tamil test below pins that the fix does not.

    python -m pytest tests/unit/test_create_group_log_is_the_list_autogen_appends_to.py -q
"""
import ast
from pathlib import Path
from unittest import mock

import pytest

_CREATE_SRC = (Path(__file__).resolve().parents[2] /
               'hartos' / 'create_recipe.py').read_text(encoding='utf-8')

_TAMIL = 'வணக்கம்! இன்று பின்னங்களைக் கற்றுக்கொள்ளலாமா? 🌱'
_PROMPT_ID = 7070707


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


@pytest.fixture
def create_group(monkeypatch):
    """A real two-seat group wired the way create_agents wires it, then one
    turn: the user's Tamil message and the StatusVerifier's reply."""
    import autogen
    import hartos.create_recipe as cr
    import integrations.channels.memory.shared_history as sh

    history = _FakeHistory()
    monkeypatch.setattr(sh, '_get_persistent_history', lambda user_id: history)
    try:
        import core.resonance_tuner as rt
        monkeypatch.setattr(rt, 'get_resonance_tuner', lambda: mock.Mock())
    except ImportError:
        pass

    user = autogen.ConversableAgent('User', llm_config=False,
                                    human_input_mode='NEVER',
                                    default_auto_reply='noted')
    verifier = autogen.ConversableAgent(
        'StatusVerifier', llm_config=False, human_input_mode='NEVER',
        default_auto_reply='{"status": "completed"}')
    group_chat = autogen.GroupChat(agents=[user, verifier], messages=[],
                                   max_round=2,
                                   speaker_selection_method='round_robin')
    graph = mock.Mock()
    cr._install_create_group_writeback(
        group_chat, 'u-create', _PROMPT_ID, f'u-create_{_PROMPT_ID}',
        simplemem_store=None, memory_graph=graph)
    manager = autogen.GroupChatManager(groupchat=group_chat, llm_config=False)
    user.initiate_chat(manager, message=_TAMIL)
    return group_chat, manager, verifier, history, graph


def test_the_list_the_create_loop_reads_holds_the_turn(create_group):
    group_chat, _manager, _verifier, _history, _graph = create_group
    contents = [m.get('content') for m in group_chat.messages]
    assert contents[:2] == [_TAMIL, '{"status": "completed"}'], (
        "the create loop parses group_chat.messages[-2] after each turn; the "
        f"list it reads holds {contents!r}")


def test_a_tamil_turn_reaches_the_other_seat_unchanged(create_group):
    _group_chat, manager, verifier, _history, _graph = create_group
    seen = [m.get('content') for m in verifier.chat_messages[manager]]
    assert _TAMIL in seen, (
        f"the StatusVerifier's prompt holds {seen!r}, not the user's words")


def test_the_turn_reaches_shared_history_and_the_graph(create_group):
    _group_chat, _manager, _verifier, history, graph = create_group
    written = [(m.content, md.get('prompt_id'))
               for m, md in zip(history.messages, history.metadata)]
    assert (_TAMIL, _PROMPT_ID) in written, written
    spoken = [c.args[1] for c in graph.register_conversation.call_args_list]
    assert _TAMIL in spoken, spoken


def _create_agents_sites():
    tree = ast.parse(_CREATE_SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == 'create_agents')
    managers, installs, rebinds = [], [], []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = (getattr(node.func, 'attr', None)
                    or getattr(node.func, 'id', None))
            if name == 'GroupChatManager':
                managers.append(node.lineno)
            elif name == '_install_create_group_writeback':
                installs.append(node.lineno)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Attribute)
                        and target.attr in ('messages', 'append')):
                    rebinds.append(node.lineno)
    return managers, installs, rebinds


def test_create_agents_installs_the_write_back_before_the_manager():
    managers, installs, rebinds = _create_agents_sites()
    assert managers and installs, (
        "create_agents has no manager or no write-back; the guard would be "
        "vacuous")
    assert max(installs) < min(managers), (
        f"write-back installed at line {max(installs)}, after the "
        f"GroupChatManager at line {min(managers)}. The manager keeps a copy of "
        "the group, so the list the create loop reads never gets a turn")
    assert not rebinds, (
        f"create_agents rebinds messages/append itself at lines {rebinds}; a "
        "second wrap goes around the one install and is the bug this guards")
