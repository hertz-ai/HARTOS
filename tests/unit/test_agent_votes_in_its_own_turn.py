"""A /chat agent can vote on a thought experiment during its own turn.

Review of 924b8e9dc: cast_experiment_vote had become uncallable.  Its only
caller was the MCP bridge, which (rightly) runs with no caller, and no /chat
agent carried the tool, so no agent could vote at all.

The tool now goes through the ONE existing registration path for in-process
tools: a native ServiceToolInfo registered on service_tool_registry (the
GhPrTool / SeoAuditTool shape), attached to create_recipe's and
reuse_recipe's agents by the Tier-1 gate (core.agent_tools.
filter_service_tools) when the turn is about a thought experiment
(marketing_tools.detect_goal_tags -> 'thought_experiment', which
goal_manager already maps to the 'thought_experiment' tool tag).  It runs
on the turn's thread, where /chat has put the agent's prompt_id
(hart_intelligence_entry sets thread_local_data before the agents run), so
the vote is the calling agent's.

End to end here: the real registry shim, the real gate, real autogen
agents registered by the real register_dual, the call executed by autogen's
own execute_function, a real SQLite database.  The only stand-in is the
LLM: the tool call is handed to the executor as the model would send it.
"""
import json
import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('SOCIAL_DB_PATH', ':memory:')

from hartos.threadlocal import thread_local_data  # noqa: E402
from integrations.social import models as social_models  # noqa: E402
from integrations.social.models import (  # noqa: E402
    Base, ExperimentVote, ThoughtExperiment, User)
from integrations.social.thought_experiment_service import (  # noqa: E402
    ThoughtExperimentService)

TURN = 'Vote on the thought experiment about cache warmup'

# autogen registers a tool schema only on an agent with an llm_config; no
# model is ever called here.
_NO_CALL_LLM = {'config_list': [{'model': 'none', 'api_key': 'none',
                                 'base_url': 'http://127.0.0.1:9'}]}


@pytest.fixture
def db(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path / 'turnvote.db'}")
    Base.metadata.create_all(eng)
    f = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(social_models, '_SessionLocal', f)
    s = f()
    yield s
    s.close()
    eng.dispose()


@pytest.fixture
def registry(monkeypatch):
    """A fresh registry, so the test sees exactly what registration adds."""
    from integrations.service_tools import registry as reg_mod
    fresh = reg_mod.ServiceToolRegistry(config_file='__none__.json')
    monkeypatch.setattr(reg_mod, 'service_tool_registry', fresh)
    import integrations.service_tools as st_pkg
    monkeypatch.setattr(st_pkg, 'service_tool_registry', fresh)
    import integrations.agent_engine.thought_experiment_tools as tet
    monkeypatch.setattr(tet, 'service_tool_registry', fresh, raising=False)
    return fresh


@pytest.fixture
def turn():
    """The /chat turn's thread-local state, restored afterwards."""
    saved = thread_local_data.snapshot()
    yield thread_local_data
    for key in list(vars(thread_local_data._local)):
        delattr(thread_local_data._local, key)
    thread_local_data.adopt(saved)


def _user(db, user_type='human', owner_id=None, agent_id=None):
    u = User(username=f'livetest_tv_{uuid.uuid4().hex[:8]}',
             user_type=user_type, owner_id=owner_id, agent_id=agent_id)
    db.add(u)
    db.commit()
    return u


def _experiment(db):
    e = ThoughtExperiment(
        id=str(uuid.uuid4()), creator_id=_user(db).id, title='Cache warmup',
        hypothesis='Faster cache warmup lowers latency', expected_outcome='o',
        status='voting')
    db.add(e)
    db.commit()
    return e


def _gated_tools(registry, text):
    """What the Tier-1 gate attaches for a turn about ``text``."""
    from core.agent_tools import filter_service_tools
    from integrations.agent_engine.marketing_tools import resolve_goal_tags
    tags = resolve_goal_tags(None, text)
    return filter_service_tools(tags, registry.get_all_tool_functions(),
                                registry.get_tool_definitions(), registry)


def test_the_vote_tool_is_registered_on_the_one_path(registry):
    from integrations.agent_engine.thought_experiment_tools import (
        ExperimentVoteTool)
    assert ExperimentVoteTool.register() is True
    assert 'cast_experiment_vote' in registry.get_all_tool_functions()


def test_a_thought_experiment_turn_unlocks_it_and_others_do_not(registry):
    from integrations.agent_engine.thought_experiment_tools import (
        ExperimentVoteTool)
    ExperimentVoteTool.register()
    assert 'cast_experiment_vote' in _gated_tools(registry, TURN)
    assert 'cast_experiment_vote' not in _gated_tools(
        registry, 'What is the weather in Chennai today?')


def test_an_agent_votes_in_its_own_turn_end_to_end(db, registry, turn):
    autogen = pytest.importorskip('autogen')
    from core.agent_tools import register_dual
    from integrations.agent_engine.thought_experiment_tools import (
        ExperimentVoteTool)

    e = _experiment(db)
    owner = _user(db)
    agent = _user(db, 'agent', owner_id=owner.id, agent_id='55501')

    ExperimentVoteTool.register()
    tools = _gated_tools(registry, TURN)
    helper = autogen.ConversableAgent('helper', llm_config=_NO_CALL_LLM)
    executor = autogen.ConversableAgent('executor', llm_config=False,
                                        human_input_mode='NEVER')
    for name, fn in tools.items():
        register_dual(helper, executor, fn, name, fn.__doc__ or name)

    # What /chat sets before the agents run (hart_intelligence_entry).
    turn.set_user_id(owner.id)
    turn.set_prompt_id('55501')
    ok, result = executor.execute_function({
        'name': 'cast_experiment_vote',
        'arguments': json.dumps({'experiment_id': e.id, 'vote_value': 2,
                                 'reasoning': 'warm caches help'}),
    })
    assert ok, result
    body = json.loads(result['content'])
    assert body['success'] is True, body

    db.expire_all()
    rows = [(v.voter_id, v.voter_type, v.vote_value)
            for v in db.query(ExperimentVote).filter_by(experiment_id=e.id)]
    assert rows == [(agent.id, 'agent', 2)]
    tally = ThoughtExperimentService.tally_votes(db, e.id)
    assert tally['distinct_voters'] == 1          # counted as its owner


def test_in_a_turn_the_model_cannot_name_someone_else(db, registry, turn):
    autogen = pytest.importorskip('autogen')
    from core.agent_tools import register_dual
    from integrations.agent_engine.thought_experiment_tools import (
        ExperimentVoteTool)

    e = _experiment(db)
    _user(db, 'agent', owner_id=_user(db).id, agent_id='55502')
    person = _user(db)
    ExperimentVoteTool.register()
    fn = _gated_tools(registry, TURN)['cast_experiment_vote']
    helper = autogen.ConversableAgent('helper', llm_config=_NO_CALL_LLM)
    executor = autogen.ConversableAgent('executor', llm_config=False,
                                        human_input_mode='NEVER')
    register_dual(helper, executor, fn, 'cast_experiment_vote', 'vote')

    turn.set_prompt_id('55502')
    ok, result = executor.execute_function({
        'name': 'cast_experiment_vote',
        'arguments': json.dumps({'experiment_id': e.id, 'vote_value': 2,
                                 'voter_id': person.id}),
    })
    content = str(result.get('content') or '')
    try:
        refused = json.loads(content)['success'] is False
    except ValueError:
        # The tool-argument guard refused the call before it ran
        # (voter_id is not in the schema): refused all the same.
        refused = 'not run' in content
    assert refused, content
    db.expire_all()
    assert db.query(ExperimentVote).filter_by(experiment_id=e.id).count() == 0


# ── Review of d99b1aa88: reach the tool from how people ask ───────────
# It was attached only for the literal phrase "thought experiment", and in
# CREATE only at agent build time.  Now a turn that pairs a vote word
# (vote / voting / ballot) with the word "experiment", or with the id of an
# experiment that exists, unlocks it, in the per-turn attach both CREATE and
# REUSE run (core.agent_tool_menu.attach_for_turn).  Review of a4dc8cf3b
# (F5): "proposal" tripped political and business chat, and any UUID after
# a vote word unlocked it; both are gone.

_EXP_ID = '3f2a9c1e-7b4d-4e2a-9c1e-7b4d4e2a9c1e'


@pytest.mark.parametrize('turn_text', [
    'cast your vote on experiment abc',
    'please vote on the experiment about latency',
    'ballot for the thought experiment',
    'Voting on the experiment closes tonight, add yours',
    'Vote on the thought experiment about cache warmup',
])
def test_a_vote_on_an_experiment_unlocks_the_tool(turn_text):
    from integrations.agent_engine.marketing_tools import detect_goal_tags
    assert 'thought_experiment' in detect_goal_tags(turn_text)


@pytest.mark.parametrize('turn_text', [
    'vote for the best pizza place tonight',
    'run an experiment on the cache and tell me the latency',
    f'what is the status of {_EXP_ID}?',
    'What is the weather in Chennai today?',
    'the devotee was experimenting',   # no word starts with vote
    'Voting on proposal 12 closes tonight, add yours',
    'vote on the budget proposal in parliament',
    f'vote 2 on {_EXP_ID}',            # a UUID that is no experiment
])
def test_other_turns_do_not(turn_text, db):
    from integrations.agent_engine.marketing_tools import detect_goal_tags
    assert 'thought_experiment' not in detect_goal_tags(turn_text)


def test_a_vote_naming_a_known_experiment_id_unlocks_the_tool(db):
    """The id of an experiment that exists counts as the experiment word."""
    from integrations.agent_engine.marketing_tools import detect_goal_tags
    e = _experiment(db)
    assert 'thought_experiment' in detect_goal_tags(f'vote 2 on {e.id}')
    assert 'thought_experiment' in detect_goal_tags(f'Ballot: {e.id.upper()} +2')
    assert 'thought_experiment' not in detect_goal_tags(f'what is {e.id}?')


def _agents_built_for(registry, goal_text):
    """Agents as create/reuse build them: Tier-1 gate on the build-time goal,
    register_dual, and the per-conversation ledger the turn attach reads."""
    autogen = pytest.importorskip('autogen')
    from core.agent_tools import register_dual
    from integrations.agent_engine.marketing_tools import resolve_goal_tags
    helper = autogen.ConversableAgent('helper', llm_config=_NO_CALL_LLM)
    executor = autogen.ConversableAgent('executor', llm_config=False,
                                        human_input_mode='NEVER')
    tools = _gated_tools(registry, goal_text)
    for name, fn in tools.items():
        register_dual(helper, executor, fn, name, fn.__doc__ or name)
    from core.agent_tool_menu import arm_turn_attach
    arm_turn_attach(executor, set(tools), resolve_goal_tags(None, goal_text))
    return helper, executor


def _tool_reply(executor, helper, experiment_id, value=2):
    """The executor answering a model's tool call through autogen's own
    reply machinery (generate_reply -> tool-call reply -> the function)."""
    return executor.generate_reply(messages=[{
        'role': 'assistant', 'content': None,
        'tool_calls': [{'id': 'call_1', 'type': 'function', 'function': {
            'name': 'cast_experiment_vote',
            'arguments': json.dumps({'experiment_id': experiment_id,
                                     'vote_value': value})}}],
    }], sender=helper)


def test_a_later_turn_attaches_the_tool_the_build_goal_did_not(
        db, registry, turn):
    """An agent built for something else is asked, mid-conversation, to vote:
    the turn attach gives it the tool before the model sees the turn."""
    from core.agent_tool_menu import attach_for_turn
    from integrations.agent_engine.thought_experiment_tools import (
        ExperimentVoteTool)
    from integrations.service_tools import registry as reg_mod

    ExperimentVoteTool.register()
    helper, executor = _agents_built_for(registry, 'summarise my inbox')
    assert 'cast_experiment_vote' not in executor._hart_attached_tools

    new, n = attach_for_turn('please vote on the experiment about latency',
                             helper, executor, reg_mod.service_tool_registry)
    assert 'thought_experiment' in new and n >= 1
    assert 'cast_experiment_vote' in executor._hart_attached_tools
    # Idempotent: the same turn again attaches nothing more.
    assert attach_for_turn('vote on the experiment again', helper, executor,
                           reg_mod.service_tool_registry) == ([], 0)

    e = _experiment(db)
    agent = _user(db, 'agent', owner_id=_user(db).id, agent_id='55503')
    turn.set_prompt_id('55503')
    reply = _tool_reply(executor, helper, e.id)
    assert json.loads(reply['tool_responses'][0]['content'])['success'] is True
    db.expire_all()
    assert [(v.voter_id, v.voter_type) for v in
            db.query(ExperimentVote).filter_by(experiment_id=e.id)] == [
                (agent.id, 'agent')]


def test_a_real_autogen_tool_call_runs_on_the_turns_thread(db, registry):
    """Measured, not assumed: autogen executes the tool on the thread that
    drives the reply, so it sees that thread's prompt_id.  A thread that
    carries the agent's prompt_id votes as it; another thread with none is
    refused."""
    import threading
    from integrations.agent_engine.thought_experiment_tools import (
        ExperimentVoteTool)

    ExperimentVoteTool.register()
    helper, executor = _agents_built_for(registry, TURN)
    e = _experiment(db)
    agent = _user(db, 'agent', owner_id=_user(db).id, agent_id='55504')
    out = {}

    def _turn(key, prompt_id):
        thread_local_data.set_prompt_id(prompt_id)
        reply = _tool_reply(executor, helper, e.id)
        out[key] = json.loads(reply['tool_responses'][0]['content'])

    for key, pid in (('with', '55504'), ('without', None)):
        t = threading.Thread(target=_turn, args=(key, pid))
        t.start()
        t.join(60)
    assert out['with']['success'] is True, out
    assert out['without']['success'] is False, out
    db.expire_all()
    assert [v.voter_id for v in
            db.query(ExperimentVote).filter_by(experiment_id=e.id)] == [agent.id]


_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _lift(rel, name, **inject):
    """One top-level function out of a module a bare pytest env cannot
    import (create_recipe waits on live services), exec'd with its
    collaborators injected -- the way test_is_autonomous_is_one_rule lifts
    create's should_continue_autonomously."""
    import ast
    src = open(os.path.join(_ROOT, rel), encoding='utf-8').read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = dict(inject)
    exec(ast.get_source_segment(src, fn), ns)
    return ns[name]


def _offered(agent):
    """The tool names an agent's LLM schema offers."""
    cfg = agent.llm_config if isinstance(agent.llm_config, dict) else {}
    return {(t.get('function') or {}).get('name')
            for t in cfg.get('tools', [])}


def test_create_turn_attaches_the_vote_tool_to_creates_helper_pair(
        db, registry, turn):
    """CREATE, behaviourally (review of a4dc8cf3b, F2): agents shaped the
    way create_agents builds them (service tools register_dual'ed on
    helper/assistant, the ledger armed on the assistant), then CREATE's own
    per-turn attach, lifted from create_recipe.py, on a vote turn.  The
    Helper is offered the tool, the Assistant executes it, no other agent
    gets it, and the vote is the calling agent's."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from integrations.agent_engine.thought_experiment_tools import (
        ExperimentVoteTool)

    ExperimentVoteTool.register()
    helper, assistant = _agents_built_for(registry, 'plan my week')
    other = pytest.importorskip('autogen').ConversableAgent(
        'author', llm_config=False)
    agents_object = {'helper': helper, 'assistant': assistant,
                     'author': other, 'user': other}
    attach = _lift('hartos/create_recipe.py', '_attach_for_create_turn',
                   current_app=SimpleNamespace(logger=MagicMock()))
    assert 'cast_experiment_vote' not in _offered(helper)

    attach(agents_object, 'please vote on the experiment about latency',
           'u_1')
    assert 'cast_experiment_vote' in _offered(helper)
    assert 'cast_experiment_vote' in assistant._function_map
    assert 'cast_experiment_vote' not in getattr(other, '_function_map', {})

    e = _experiment(db)
    agent = _user(db, 'agent', owner_id=_user(db).id, agent_id='55505')
    turn.set_prompt_id('55505')
    reply = _tool_reply(assistant, helper, e.id)
    assert json.loads(reply['tool_responses'][0]['content'])['success'] is True
    db.expire_all()
    assert [v.voter_id for v in
            db.query(ExperimentVote).filter_by(experiment_id=e.id)] == [agent.id]


def test_source_guard_create_and_reuse_turns_both_attach_per_turn():
    """Wiring guard (the behaviour is pinned above: the shared helper, and
    CREATE's own lifted turn attach): CREATE's turn (get_response_group ->
    _attach_for_create_turn) and REUSE's (get_agent_response) reach
    attach_for_turn, and both builders arm the ledger it reads, once, on the
    agent they execute service tools on."""
    import ast

    def _calls(fn):
        return {c.func.id if isinstance(c.func, ast.Name) else c.func.attr
                for c in ast.walk(fn) if isinstance(c, ast.Call)
                and isinstance(c.func, (ast.Name, ast.Attribute))}

    for rel, turn_fns in (
            ('hartos/create_recipe.py',
             ('get_response_group', '_attach_for_create_turn')),
            ('hartos/reuse_recipe.py', ('get_agent_response',))):
        tree = ast.parse(open(os.path.join(_ROOT, rel), encoding='utf-8').read())
        fns = {n.name: n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)}
        reached = set().union(*(_calls(fns[f]) for f in turn_fns))
        assert 'attach_for_turn' in reached, rel
        if len(turn_fns) > 1:
            assert turn_fns[1] in _calls(fns[turn_fns[0]]), rel
        arms = [c for c in ast.walk(tree)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                and c.func.id == 'arm_turn_attach']
        assert len(arms) == 1, rel
        assert ast.unparse(arms[0].args[0]) == 'assistant', rel


# ── A turn /chat hands to a matched agent is that agent's turn ────────
# Autonomous /chat routes a request to an existing agent that matches it
# (hart_intelligence_entry: find_matching_agent -> chat_agent(..., _mid)).
# The thread-local prompt_id still held the ORIGINAL prompt, so a vote cast
# in that turn was recorded as the wrong agent.  The routed turn now runs
# inside thread_local_data.turn_of(_mid).

def test_a_vote_in_a_routed_turn_is_the_matched_agents(db):
    from integrations.agent_engine.thought_experiment_tools import (
        cast_experiment_vote)

    e = _experiment(db)
    requester = _user(db, 'agent', owner_id=_user(db).id, agent_id='66601')
    matched = _user(db, 'agent', owner_id=_user(db).id, agent_id='66602')
    saved = thread_local_data.snapshot()
    try:
        thread_local_data.set_prompt_id('66601')      # what /chat stamped
        thread_local_data.set_user_id('owner-x')
        thread_local_data.set_ui_actions([])          # the handler's, pre-turn
        with thread_local_data.turn_of('66602'):      # the routed turn
            out = json.loads(cast_experiment_vote(e.id, '', vote_value=2))
            thread_local_data.set_ui_actions([{'route': '/x'}])
        assert out['success'] is True, out
        # Back to the request's own agent; what the turn set for the
        # handler (its ui_actions) is kept; the user is untouched.
        assert thread_local_data.get_prompt_id() == '66601'
        assert thread_local_data.get_user_id() == 'owner-x'
        assert thread_local_data.get_ui_actions() == [{'route': '/x'}]
    finally:
        for key in list(vars(thread_local_data._local)):
            delattr(thread_local_data._local, key)
        thread_local_data.adopt(saved)
    db.expire_all()
    voters = [v.voter_id for v in
              db.query(ExperimentVote).filter_by(experiment_id=e.id)]
    assert voters == [matched.id], (voters, requester.id)


def test_turn_of_restores_even_when_the_turn_raises():
    saved = thread_local_data.snapshot()
    try:
        thread_local_data.set_prompt_id('71')
        with pytest.raises(RuntimeError):
            with thread_local_data.turn_of('72'):
                assert thread_local_data.get_prompt_id() == '72'
                raise RuntimeError('turn failed')
        assert thread_local_data.get_prompt_id() == '71'
    finally:
        for key in list(vars(thread_local_data._local)):
            delattr(thread_local_data._local, key)
        thread_local_data.adopt(saved)


def test_source_guard_the_matched_agent_route_runs_as_that_agent():
    """hart_intelligence_entry cannot be imported in a unit test (the reason
    test_capability_consent_canonical reads it by AST).  Every
    chat_agent(...) call whose agent argument is not the request's own
    prompt_id must sit inside `with thread_local_data.turn_of(<that id>)`."""
    import ast
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    src = open(os.path.join(root, 'hart_intelligence_entry.py'),
               encoding='utf-8').read()
    tree = ast.parse(src)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    routed = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'chat_agent' and len(node.args) >= 3
                and not (isinstance(node.args[2], ast.Name)
                         and node.args[2].id == 'prompt_id')):
            routed.append(node)
    assert routed, 'the matched-agent route (chat_agent(..., _mid, ...)) moved'
    for call in routed:
        agent_arg = ast.unparse(call.args[2])
        p, inside = parents.get(call), False
        while p is not None:
            if isinstance(p, ast.With) and any(
                    'turn_of' in ast.unparse(item.context_expr)
                    and agent_arg in ast.unparse(item.context_expr)
                    for item in p.items):
                inside = True
                break
            p = parents.get(p)
        assert inside, (f'line {call.lineno}: chat_agent for {agent_arg} '
                        f'runs without turn_of({agent_arg})')
