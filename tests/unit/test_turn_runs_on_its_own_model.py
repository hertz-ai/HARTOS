"""A turn runs on the model chosen for it, even when its session's agents are
cached (#172).

MEASURED on central (2026-10-18, 10-20, 10-23): a revenue goal's step was
"Handed to the expert model" (Claude, the revenue lane's expert, day cap
$3.30) and the goal's next daemon turn ran with that expert as its
model_config override, yet the turn called openrouter and failed "Reasoning
is mandatory"; api.anthropic.com was called 0 times all day.  The session's
agents had been built on an earlier turn, on the node's default model, and
autogen builds an agent's client once (ConversableAgent._validate_llm_config),
so a cached agent kept that model for every later turn.  The same cache works
the other way: agents first built on an expert turn would keep the expert for
every ordinary turn after it, outside the day-cap check, which runs only
where the expert is offered.

Contracts, through the REAL get_response_group (CREATE) and chat_agent
(REUSE) with REAL autogen agents; only the LLM call boundary is captured:
  - an expert turn on a cached session calls the expert; the next ordinary
    turn calls the default model again; registered tools stay registered;
  - a session whose other turn is still running is not switched under it
    (that turn keeps its model, and the skip is logged);
  - a REUSE turn marks itself as the session's running turn, so the guard
    above sees it.
"""
import logging
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

autogen = pytest.importorskip('autogen', reason='autogen not installed')

from hartos import create_recipe as cr  # noqa: E402
from hartos import helper as helper_fun  # noqa: E402
from hartos import reuse_recipe as rr  # noqa: E402
from hartos.helper import Action  # noqa: E402
from hartos.lifecycle_hooks import clear_action_states  # noqa: E402
from hartos.threadlocal import thread_local_data  # noqa: E402

DEFAULT = [{'model': 'free-model', 'api_key': 'k', 'base_url': 'http://127.0.0.1:9/v1'}]
EXPERT = [{'model': 'expert-model', 'api_key': 'k2', 'base_url': 'http://127.0.0.1:9/v2'},
          {'model': 'free-model', 'api_key': 'k', 'base_url': 'http://127.0.0.1:9/v1'}]


def _probe(text: str) -> str:
    """A registered tool, so the test sees whether binding keeps tools."""
    return text


def _llm_agents():
    """The session's LLM agents as create_agents builds them: real autogen
    agents on the node's default model, the Helper carrying a tool."""
    assistant = autogen.AssistantAgent('Assistant', llm_config={'config_list': list(DEFAULT), 'cache_seed': None})
    helper = autogen.AssistantAgent('Helper', llm_config={'config_list': list(DEFAULT), 'cache_seed': None})
    helper.register_for_llm(name='probe', description='echo')(_probe)
    user = autogen.UserProxyAgent('User', llm_config=False, human_input_mode='NEVER',
                                  code_execution_config=False)
    group_chat = autogen.GroupChat(agents=[assistant, helper], messages=[], max_round=2)
    manager = autogen.GroupChatManager(groupchat=group_chat,
                                       llm_config={'config_list': list(DEFAULT), 'cache_seed': None})
    return assistant, helper, user, group_chat, manager


def _models(*agents):
    return [a.client._config_list[0]['model'] for a in agents]


@pytest.fixture
def no_override():
    thread_local_data.clear_model_config_override()
    yield
    thread_local_data.clear_model_config_override()


@pytest.fixture
def create_session(test_user_id, test_prompt_id, mock_flask_app, no_override):
    """A cached CREATE session (user_agents hit), its turn's LLM call
    captured at the one boundary: the user agent's initiate_chat."""
    up = f'{test_user_id}_{test_prompt_id}'
    clear_action_states(up)
    assistant, helper, user, group_chat, manager = _llm_agents()
    seen = []
    starter = MagicMock()
    starter.initiate_chat.side_effect = lambda **kw: seen.append(
        _models(assistant, helper, manager))
    agents_object = {'user': starter, 'helper': helper, 'assistant': assistant,
                     'verify': MagicMock(), 'executor': user}
    chat_instructor = MagicMock()
    chat_instructor.chat_messages = {}
    cached = (MagicMock(), assistant, user, group_chat, manager, chat_instructor, agents_object)
    patches = [
        patch.object(cr, 'user_tasks', {up: Action(['Do the step'])}),
        patch.object(cr, 'user_agents', {up: cached}),
        patch.object(cr, 'messages', {up: [{'role': 'user', 'content': 'start'}]}),
        patch.object(cr, 'request_id_list', {up: 'req-172'}),
        patch.object(cr, 'config_list', list(DEFAULT)),
        patch.object(cr, '_resume_prior_user_input_block', return_value=False),
        patch.object(cr, '_attach_for_create_turn'),
    ]
    for p in patches:
        p.start()
    try:
        yield SimpleNamespace(up=up, seen=seen, helper=helper, user_id=test_user_id,
                              prompt_id=test_prompt_id)
    finally:
        for p in reversed(patches):
            p.stop()
        clear_action_states(up)


class TestCreateTurnRunsOnItsModel:

    def test_an_expert_turn_on_a_cached_session_calls_the_expert(self, create_session):
        s = create_session
        thread_local_data.set_model_config_override(list(EXPERT))
        cr.get_response_group(s.user_id, 'take the step', s.prompt_id)
        assert s.seen == [['expert-model'] * 3], (
            'the expert turn ran on the model the cached agents were built on: '
            'central 10-23, 0 api.anthropic.com calls after "Handed to the expert model"')
        assert [t['function']['name'] for t in s.helper.llm_config['tools']] == ['probe']

    def test_the_next_ordinary_turn_is_back_on_the_default_model(self, create_session):
        s = create_session
        thread_local_data.set_model_config_override(list(EXPERT))
        cr.get_response_group(s.user_id, 'take the step', s.prompt_id)
        thread_local_data.clear_model_config_override()
        cr.get_response_group(s.user_id, 'and the next', s.prompt_id)
        assert s.seen[-1] == ['free-model'] * 3, (
            'an ordinary turn after an expert turn stayed on the expert: Claude '
            'spent outside the day-cap check')

    def test_a_running_turn_of_the_session_is_not_switched(self, create_session, caplog):
        s = create_session
        other = []
        t = threading.Thread(target=lambda: other.append(threading.get_ident()))
        t.start()
        t.join()
        thread_local_data.set_model_config_override(list(EXPERT))
        cr._TURNS_IN_FLIGHT[s.up] = {other[0]: 1}
        try:
            with caplog.at_level(logging.WARNING):
                cr.get_response_group(s.user_id, 'take the step', s.prompt_id)
        finally:
            cr._TURNS_IN_FLIGHT.pop(s.up, None)
        assert s.seen == [['free-model'] * 3], 'a running turn had its model switched under it'
        assert any('another turn' in r.getMessage() and s.up in r.getMessage()
                   for r in caplog.records), 'the skipped switch was not logged'


@pytest.fixture
def reuse_session(test_user_id, test_prompt_id, mock_flask_app, no_override):
    """A cached REUSE session (user_agents hit, journey UseBot), its turn's
    LLM call captured at the one boundary: get_agent_response."""
    up = f'{test_user_id}_{test_prompt_id}'
    assistant, helper, user, group_chat, manager = _llm_agents()
    seen, registered = [], []

    def respond(*args, **kwargs):
        seen.append(_models(assistant, helper, manager))
        probe = []
        t = threading.Thread(target=lambda: probe.append(cr._another_turn_in_flight(up)))
        t.start()
        t.join()
        registered.append(probe[0])
        return 'done'

    cached = (assistant, user, group_chat, manager, helper, MagicMock(), MagicMock(),
              MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())
    patches = [
        patch.object(rr, 'user_agents', {up: cached}),
        patch.object(rr, 'user_journey', {up: 'UseBot'}),
        patch.object(rr, 'user_tasks', {}),
        patch.object(rr, 'llm_call_track', {up: {'count': 0, 'original_prompt': False}}),
        patch.object(rr, 'request_id_list', {}),
        patch.object(rr, 'config_list', list(DEFAULT)),
        patch.object(rr, 'clear_action_states'),
        patch.object(rr, 'get_role', return_value='role'),
        patch.object(rr, 'get_agent_response', side_effect=respond),
    ]
    for p in patches:
        p.start()
    try:
        yield SimpleNamespace(up=up, seen=seen, registered=registered,
                              user_id=test_user_id, prompt_id=test_prompt_id)
    finally:
        for p in reversed(patches):
            p.stop()


class TestReuseTurnRunsOnItsModel:

    def test_an_expert_turn_then_an_ordinary_turn(self, reuse_session):
        s = reuse_session
        thread_local_data.set_model_config_override(list(EXPERT))
        rr.chat_agent(s.user_id, 'go', s.prompt_id, None, 'req-172-r1')
        thread_local_data.clear_model_config_override()
        rr.chat_agent(s.user_id, 'again', s.prompt_id, None, 'req-172-r2')
        assert s.seen == [['expert-model'] * 3, ['free-model'] * 3]

    def test_a_reuse_turn_marks_itself_as_the_running_turn(self, reuse_session):
        s = reuse_session
        rr.chat_agent(s.user_id, 'go', s.prompt_id, None, 'req-172-r3')
        assert s.registered == [True], (
            'a REUSE turn is invisible to the running-turn guard, so another '
            'turn of the session could switch its model mid-turn')
        assert not cr._another_turn_in_flight(s.up)


def test_binding_keeps_tools_and_leaves_agents_without_a_model_alone(no_override):
    assistant, helper, user, group_chat, manager = _llm_agents()
    thread_local_data.set_model_config_override(list(EXPERT))
    changed = helper_fun.bind_agents_to_turn_model(
        (assistant, user, group_chat, manager, {'helper': helper}), list(DEFAULT), 'bind_probe')
    assert changed is True
    assert _models(assistant, helper, manager) == ['expert-model'] * 3
    assert user.llm_config is False and user.client is None
    assert [t['function']['name'] for t in helper.llm_config['tools']] == ['probe']
    assert helper_fun.bind_agents_to_turn_model(
        (assistant, helper, manager), list(DEFAULT), 'bind_probe') is False, \
        'agents already on the turn model were rebuilt again'
