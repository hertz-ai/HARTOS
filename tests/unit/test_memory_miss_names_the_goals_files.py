"""A goal's memory recall that finds nothing names the files its goal's
folder holds, so the agent opens them before asking anyone.

WHAT WENT WRONG (central, 2026-11-20 14:35Z)
  A daemon goal turn's config had files_root '/app/agent_data/lanes', and
  that folder holds EMAIL_FACTS.md with a "Trade lane: rice export" section
  of the owner's rules.  The rice buyer goal's plan step was
  "search_long_term_memory: find saved owner rules on the lane floor,
  approved SKUs, mill vetting and payment terms".  Memory held nothing, and
  the reply was core.tool_refusal.nothing_recalled ("No relevant memories
  found for ... carry on with this step from what you already have").  The
  agent never opened the goal's files, the step checker kept the step
  pending, and after 3 tries the goal paused needs_help.  A sibling goal
  asked the owner for the same facts (the owner already had 51 unanswered
  asks).  The facts were on disk in the goal's own folder all along.

THE RULE (integrations.agent_engine.goal_files.goal_files_hint, which
core.agent_tools.nothing_recalled_on_this_turn adds to the answer
search_long_term_memory and recall_memory give when nothing matched)
  - on this node's own goal turn, whose goal was made by an account this node
    holds and names a files_root holding something, the answer also names
    what that folder holds and says to open the file that may hold the answer
    with get_user_uploaded_file before asking anyone;
  - only names inside files_root are listed, and the answer holds no braces
    (it is LangChain tool text);
  - a person's turn, a turn that only names a goal, a goal with no
    files_root, a goal a stranger made, an empty folder and a goal that
    cannot be read answer exactly as before;
  - a recall that finds something is unchanged.

    python -m pytest tests/unit/test_memory_miss_names_the_goals_files.py -q
"""
from unittest import mock

import pytest

from core.constants import tool_reply_failed
from core.tool_refusal import nothing_recalled

OWNER = 'owner-lanes'
STRANGER = 'someone-signed-in'
SESSION = 'system_daemon_mm'
QUERY = ('saved owner rules on the lane floor, approved SKUs, mill vetting '
         'and payment terms')
RULES = ('# Email facts\n## Trade lane: rice export\nFloor: 2% of FOB. '
         'Approved SKUs: 1121 basmati. Payment: LC at sight.\n')
FACT = 'Rice lane payment terms: LC at sight, saved by the owner.'


@pytest.fixture
def lanes(tmp_path):
    """The lane folder in miniature, a sibling folder that only shares its
    prefix, a file beside both, and an empty folder."""
    root = tmp_path / 'lanes'
    (root / 'trade').mkdir(parents=True)
    (root / 'EMAIL_FACTS.md').write_text(RULES, encoding='utf-8')
    (root / 'trade' / 'rice_mills.md').write_text('Mill vetting list',
                                                   encoding='utf-8')
    sibling = tmp_path / 'lanes_private'
    sibling.mkdir()
    (sibling / 'secret.txt').write_text('SIBLING SECRET', encoding='utf-8')
    (tmp_path / 'outside.txt').write_text('OUTSIDE SECRET', encoding='utf-8')
    (tmp_path / 'empty').mkdir()
    return root


@pytest.fixture
def goals(lanes, monkeypatch):
    """Goal rows in the real social DB; who this node holds is the boundary
    (ThoughtExperimentService.node_held_ids: the owner, the system account)."""
    from integrations.social.models import AgentGoal, Base, db_session, get_engine
    from integrations.social.thought_experiment_service import (
        ThoughtExperimentService)
    rows = {
        'g-mm-rice': ('Rice buyer growth', {'files_root': str(lanes)}, OWNER),
        'g-mm-nofolder': ('A goal with no folder', {}, OWNER),
        'g-mm-stranger': ('A stranger names the folder',
                          {'files_root': str(lanes)}, STRANGER),
        'g-mm-empty': ('An empty folder',
                       {'files_root': str(lanes.parent / 'empty')}, OWNER),
    }
    Base.metadata.create_all(get_engine())
    with db_session() as db:
        for gid, (title, cfg, creator) in rows.items():
            db.merge(AgentGoal(id=gid, goal_type='outreach', title=title,
                               config_json=dict(cfg), created_by=creator))
    monkeypatch.setattr(ThoughtExperimentService, 'node_held_ids',
                        staticmethod(lambda db, ids: {i for i in ids if i == OWNER}))
    yield rows
    with db_session() as db:
        for gid in rows:
            db.query(AgentGoal).filter_by(id=gid).delete()


@pytest.fixture
def graph(tmp_path):
    from integrations.channels.memory.memory_graph import MemoryGraph
    g = MemoryGraph(db_path=str(tmp_path / 'graph'), user_id='system')
    yield g
    g.close()


class _EmptySimpleMem:
    """SimpleMem's cloud store, holding nothing."""
    async def search(self, query):
        return []


def _core_tools(graph=None, simplemem=None):
    from core import agent_tools
    import core.chat_attachments as ca
    ctx = {k: None for k in (
        'user_id', 'prompt_id', 'agent_data', 'helper_fun', 'user_prompt',
        'request_id_list', 'recent_file_id', 'scheduler',
        'send_message_to_user1', 'retrieve_json', 'strip_json_values',
        'save_conversation_db')}
    ctx.update(user_id='system', prompt_id='daemon_mm', agent_data={},
               user_prompt=SESSION, request_id_list={}, recent_file_id={},
               memory_graph=graph, simplemem_store=simplemem,
               helper_fun=mock.Mock(), send_message_to_user1=mock.Mock(),
               retrieve_json=lambda v: v)
    with mock.patch.object(ca, 'latest_inbound', lambda *a, **k: []):
        return {n: f for n, _, f in agent_tools.build_core_tool_closures(ctx)}


def _recall_tool(leg, graph):
    """The recall tool ``leg`` names, over ``graph`` (or SimpleMem)."""
    if leg == 'recall_memory':
        from integrations.channels.memory.agent_memory_tools import (
            create_memory_tools)
        return create_memory_tools(graph, 'system', SESSION)['recall_memory'][0]
    if leg == 'search_long_term_memory/simplemem':
        return _core_tools(simplemem=_EmptySimpleMem())['search_long_term_memory']
    return _core_tools(graph=graph)['search_long_term_memory']


LEGS = ['search_long_term_memory', 'search_long_term_memory/simplemem',
        'recall_memory']


@pytest.fixture(params=LEGS)
def recall(request, graph):
    return _recall_tool(request.param, graph)


def _own_turn(goal_id, fn, *args, **kw):
    """``fn`` in ``goal_id``'s own turn, bound and marked the way
    dispatch.local_chat_dispatch runs it."""
    from core.chat_client import own_daemon_turn
    from core.llm_outbound_logger import request_id_context
    rid = f'daemon_{goal_id}'
    with request_id_context(rid), own_daemon_turn(rid):
        return fn(*args, **kw)


def _named_turn(turn_id, fn, *args, **kw):
    """``fn`` in a turn whose id is ``turn_id`` and that this node's dispatch
    did not start (a person's, or a client naming a goal)."""
    from core.llm_outbound_logger import request_id_context
    with request_id_context(turn_id):
        return fn(*args, **kw)


# -- the goal's own turn, its folder may be opened ---------------------------

def test_a_goal_turn_that_recalls_nothing_is_told_what_its_folder_holds(
        goals, recall):
    out = _own_turn('g-mm-rice', recall, QUERY)
    assert out.startswith(nothing_recalled(QUERY)), out
    assert 'EMAIL_FACTS.md' in out and 'trade/' in out, out
    assert 'Before asking anyone, open the file' in out, out
    assert 'get_user_uploaded_file with name set to that file' in out, out
    # Still the answer a step may end on, never a failed call.
    assert not tool_reply_failed(out), out
    # LangChain tool text: no braces.
    assert '{' not in out and '}' not in out, out


def test_nothing_outside_the_folder_is_listed(goals, recall):
    out = _own_turn('g-mm-rice', recall, QUERY)
    for outside in ('lanes_private', 'secret.txt', 'outside.txt', 'empty',
                    'SECRET'):
        assert outside not in out, out


def test_the_file_it_names_opens_with_the_tool_it_names(goals, graph):
    tools = _core_tools(graph=graph)
    out = _own_turn('g-mm-rice', tools['search_long_term_memory'], QUERY)
    assert 'EMAIL_FACTS.md' in out, out
    opened = _own_turn('g-mm-rice', tools['get_user_uploaded_file'],
                       name='EMAIL_FACTS.md')
    assert 'Trade lane: rice export' in opened, opened


# -- every other case answers as before -------------------------------------

def test_a_persons_turn_and_a_turn_only_naming_the_goal_are_unchanged(
        goals, recall):
    assert _named_turn('msg_1794670427623', recall, QUERY) == nothing_recalled(QUERY)
    assert _named_turn('daemon_g-mm-rice', recall, QUERY) == nothing_recalled(QUERY)
    assert recall(QUERY) == nothing_recalled(QUERY)      # no turn id at all


def test_a_goal_with_no_files_root_is_unchanged(goals, recall):
    assert _own_turn('g-mm-nofolder', recall, QUERY) == nothing_recalled(QUERY)


def test_a_goal_a_stranger_made_is_unchanged_and_lists_nothing(goals, recall):
    out = _own_turn('g-mm-stranger', recall, QUERY)
    assert out == nothing_recalled(QUERY), out
    assert 'EMAIL_FACTS.md' not in out and 'trade/' not in out, out


def test_a_goal_whose_folder_is_empty_is_unchanged(goals, recall):
    assert _own_turn('g-mm-empty', recall, QUERY) == nothing_recalled(QUERY)


def test_a_goal_that_cannot_be_read_is_unchanged(goals, recall, monkeypatch):
    from integrations.agent_engine import goal_files

    def _db_down(goal_id):
        raise RuntimeError('database is locked')
    monkeypatch.setattr(goal_files, '_goal_record', _db_down)
    assert _own_turn('g-mm-rice', recall, QUERY) == nothing_recalled(QUERY)


@pytest.mark.parametrize('leg', ['search_long_term_memory', 'recall_memory'])
def test_a_recall_that_finds_a_saved_fact_is_unchanged(goals, graph, leg):
    graph.register(FACT, {'memory_type': 'fact', 'source_agent': 'System',
                          'session_id': SESSION})
    out = _own_turn('g-mm-rice', _recall_tool(leg, graph),
                    'rice lane payment terms')
    assert 'LC at sight' in out, out
    assert 'EMAIL_FACTS.md' not in out, out
