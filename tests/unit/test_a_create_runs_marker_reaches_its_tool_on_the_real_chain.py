"""A CREATE run's ask is marked as the build's on autogen's real tool chain.

person_ask.create_run marks an ask made while a CREATE run runs (a
contextvar) so the build's finish can close it.  The other tests drive a
stand-in recipe(); this one runs the real chain a step's question takes:
autogen's ConversableAgent / GroupChat / GroupChatManager, HARTOS's patched
execute_function (hartos.helper.force_apply_autogen_json_fix),
core.agent_tool_menu.register_core_tools, core.tool_logging
.log_tool_execution and person_ask.ask_person_tool, inside a function with
create_recipe.recipe's own decorator stack (with_llm_context outside
person_ask.create_run).  Only the Helper's reply is scripted (a tool call to
ask_person), so no LLM is called.

Cases of the reviewer's probe (rv_41b2, probe_thread.py), confirmed against
the owner's incident log (thread nunba_8):
  A  a real tool call in a run that does not finish: the ask is marked with
     the build's key, on recipe()'s own thread, and stays open;
  B  a run that finishes: its ask closes on every surface;
  C  the chat moved to a ThreadPoolExecutor thread with no copied context:
     the marker is lost and the ask stays open (the boundary a future change
     to recipe()'s threading must keep in mind);
  D  the same hop with contextvars.copy_context().run: carried again.

Harness of test_person_ask.py (`legs`).

    python -m pytest tests/unit/test_a_create_runs_marker_reaches_its_tool_on_the_real_chain.py -q --noconftest
"""
from __future__ import annotations

import contextvars
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.unit.test_person_ask import (  # noqa: E402,F401
    _answered_events, _no_open_asks, _row, _settle, legs)

autogen = pytest.importorskip('autogen')

BUILT = 'Agent Created Successfully'
LLM = {'config_list': [{'model': 'scripted', 'api_key': 'x',
                        'base_url': 'http://127.0.0.1:9/v1'}], 'cache_seed': None}


@pytest.fixture()
def chain(legs, monkeypatch):
    """The real chain, with what ask() saw recorded."""
    from integrations.agent_engine import person_ask
    from core.llm_outbound_logger import turn_request_id
    from hartos.helper import force_apply_autogen_json_fix
    force_apply_autogen_json_fix()
    seen = []
    orig = person_ask.ask

    def spy(*a, **k):
        seen.append({'thread': threading.get_ident(),
                     'run': person_ask._create_run_of.get(),
                     'turn': turn_request_id()})
        return orig(*a, **k)
    monkeypatch.setattr(person_ask, 'ask', spy)
    import integrations.agent_engine.dispatch as dispatch
    monkeypatch.setattr(dispatch, 'release_holds_answered', lambda pid: 0, raising=False)
    return {'seen': seen, 'legs': legs}


def _build_chat(user_id, prompt_id, question):
    from autogen import Agent, ConversableAgent, GroupChat, GroupChatManager
    from core.agent_tool_menu import register_core_tools
    from core.tool_logging import log_tool_execution
    from integrations.agent_engine import person_ask
    name, desc, fn = person_ask.ask_person_tool(user_id, prompt_id,
                                                request_id=lambda: 'msg_T1')
    helper = ConversableAgent('Helper', llm_config=LLM, human_input_mode='NEVER',
                              code_execution_config=False)
    executor = ConversableAgent('Executor', llm_config=False, human_input_mode='NEVER',
                                code_execution_config=False)
    register_core_tools([(name, desc, log_tool_execution(fn))], helper, executor)
    state = {'n': 0}

    def scripted(recipient, messages=None, sender=None, config=None):
        state['n'] += 1
        if state['n'] == 1:
            return True, {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'call_1', 'type': 'function', 'function': {
                    'name': name, 'arguments': json.dumps({
                        'component': json.dumps({'type': 'approval', 'action': 'name_it',
                                                 'description': question}),
                        'wait_seconds': 0})}}]}
        return True, 'TERMINATE'
    helper.register_reply([Agent, None], scripted, position=0)
    gc = GroupChat(agents=[executor, helper], messages=[], max_round=4,
                   speaker_selection_method=lambda last, g: helper if last is executor else executor,
                   allow_repeat_speaker=False)
    return executor, GroupChatManager(groupchat=gc, llm_config=False), gc


def _recipe(question, reply, hop=None):
    """A function decorated as create_recipe.recipe is, running the chat."""
    from core.llm_outbound_logger import with_llm_context
    from integrations.agent_engine import person_ask
    last = {}

    @with_llm_context('autogen.create')
    @person_ask.create_run
    def recipe(user_id, text, prompt_id, file_id, request_id, autonomous=False,
               text_is_the_persons=True):
        last['entry'] = {'thread': threading.get_ident(),
                         'run': person_ask._create_run_of.get()}
        executor, manager, gc = _build_chat(user_id, prompt_id, question)

        def go():
            executor.initiate_chat(manager, message=text, clear_history=False)
        if hop == 'executor':
            with ThreadPoolExecutor(1) as ex:
                ex.submit(go).result()
        elif hop == 'executor+copy_context':
            ctx = contextvars.copy_context()
            with ThreadPoolExecutor(1) as ex:
                ex.submit(ctx.run, go).result()
        else:
            go()
        last['ask_ids'] = [json.loads(m['content'])['ask_id'] for m in gc.messages
                           if (m.get('role') == 'tool' or m.get('tool_responses'))
                           and str(m.get('content') or '').startswith('{')]
        return reply
    return recipe, last


def _state(ask_id):
    from integrations.agent_engine import person_ask
    return person_ask.ask_state(ask_id, '10202')['status']


def test_A_the_real_chain_marks_the_ask_as_the_builds_on_recipes_thread(chain):
    recipe, last = _recipe('Call it Reely?', 'Waiting.')
    assert recipe('10202', 'build it', '342', None, 'msg_T1') == 'Waiting.'
    (seen,) = chain['seen']
    assert last['entry']['run'] == ('342', '10202')
    assert seen['run'] == ('342', '10202')
    assert seen['thread'] == last['entry']['thread']
    assert seen['turn'] == 'msg_T1'
    assert _state(last['ask_ids'][-1]) == 'open'


def test_B_the_runs_finish_closes_that_ask_on_every_surface(chain):
    recipe, last = _recipe('Call it Clipper?', BUILT)
    recipe('10202', 'build it', '342', None, 'msg_T1')
    _settle()
    ask_id = last['ask_ids'][-1]
    assert _state(ask_id) == 'expired'
    assert len(_answered_events(chain['legs'], ask_id)) == 1
    assert _row(ask_id)['is_read'] is True


def test_C_a_pool_thread_without_the_context_loses_the_marker(chain):
    recipe, last = _recipe('Call it Snippy?', BUILT, hop='executor')
    recipe('10202', 'build it', '342', None, 'msg_T1')
    _settle()
    (seen,) = chain['seen']
    assert seen['run'] is None and seen['thread'] != last['entry']['thread']
    assert _state(last['ask_ids'][-1]) == 'open'


def test_D_the_same_hop_with_copy_context_carries_it(chain):
    recipe, last = _recipe('Call it Zippy?', BUILT, hop='executor+copy_context')
    recipe('10202', 'build it', '342', None, 'msg_T1')
    _settle()
    (seen,) = chain['seen']
    assert seen['run'] == ('342', '10202')
    assert _state(last['ask_ids'][-1]) == 'expired'
