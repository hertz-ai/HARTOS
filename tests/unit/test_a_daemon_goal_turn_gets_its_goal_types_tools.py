"""A daemon goal turn's agent carries the tool families of its goal's own type.

Measured live on central 2026-11-16 (build d9afa4f41), task #284: outreach
goal turns failed 32 times in 25 minutes with "the get_pipeline_status call
could not be done: no tool named get_pipeline_status is registered with this
agent", and the agent then wrote "total_prospects: 0 ... CRM memory is empty"
while the prospect store held 65.  Both agent builders (CREATE's
create_agents, REUSE's create_agents_for_user) take their Tier-2 families
from goal_tags = resolve_goal_tags(record's stored goal_tags, words), and a
daemon goal's agent record stores none: CREATE's words are the session's
helper.Action, whose str() is a repr, and REUSE's are the gathered goal text.
So outreach, sales and marketing goals ran without their CRM, journey and
marketing tools.

The rule pinned here: the node's own daemon turn for a goal gets the tool
tags of that goal's TYPE -- AgentGoal.goal_type read from the goal's row,
mapped by goal_manager.get_tool_tags, the per-type table every goal type
registers (outreach: CRM + journey, as its prompt names both; sales: CRM +
journey + marketing; marketing: marketing).  Never from the turn's words and
never from the request body.

The trust boundary pinned here: a turn's request id (daemon_<goal id>)
travels in the /chat body, so any client can name a goal's id.  Only a turn
this node's own dispatch started gets the goal's families: the in-process
call (dispatch.local_chat_dispatch, the route a bundled desktop and native
HARTOS both take) on that same thread, or the node's own HTTP self-POST
(dispatch_goal's Tier 2, the hive worker's fallback) carrying the proof that
POST minted for that turn.  A client naming the id, on any thread, with or
without a replayed copy of an earlier turn's headers, gets nothing.

Driven through the REAL dispatch_goal / DistributedWorkerLoop._execute_task
and the REAL builders with real autogen agents (the ``seats`` harness of
tests/unit/test_reel_tools_wired_both_pipelines.py) and a real in-memory
social DB holding the goal's row.  Mocked boundaries only: the guardrail and
budget gates, the machine's user-activity reading, the node's model config,
and the /chat transport -- a stand-in for Nunba's in-process adapter (or for
the loopback POST) that does what /chat does on that call, in that thread:
records the turn's request id where recipe() / chat_agent() record it, then
builds the session's agents.

    python -m pytest tests/unit/test_a_daemon_goal_turn_gets_its_goal_types_tools.py -q -p no:cacheprovider
"""
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.unit.test_create_loop_end_to_end import (  # noqa: F401  (fixture)
    UP, create_env)
from tests.unit.test_reel_tools_wired_both_pipelines import (  # noqa: F401
    PIPELINES, runs, schema, seats)

GOAL = 'goal-284-outreach'
CRM = {'get_pipeline_status', 'bulk_import_prospects', 'send_outreach_email'}
JOURNEY = {'view_journey_pipeline', 'run_journey_tick'}
MARKETING = {'create_campaign', 'create_social_post'}
FAMILIES = CRM | JOURNEY | MARKETING
A_PERSONS_REQUEST_ID = '1731740000'


@pytest.fixture
def goal_row():
    """``make(goal_id, goal_type)`` writes the goal's row to the in-memory
    social DB, the row dispatch_goal reads for its guardrail checks."""
    from integrations.social.models import AgentGoal, Base, db_session, get_engine
    Base.metadata.create_all(get_engine())
    made = []

    def make(goal_id, goal_type):
        with db_session() as db:
            db.merge(AgentGoal(id=goal_id, goal_type=goal_type,
                               title='Find buyers for the product',
                               created_by='system_bootstrap'))
        made.append(goal_id)
        return goal_id
    yield make
    with db_session() as db:
        for gid in made:
            db.query(AgentGoal).filter_by(id=gid).delete()


def _turn(request_id):
    """What chat() does with the turn's request id before it builds:
    recipe() and chat_agent() record it for the session."""
    import hartos.create_recipe as cr
    import hartos.reuse_recipe as rr
    cr.request_id_list[UP] = request_id
    rr.request_id_list[UP] = request_id


def _build(seats, pipeline, request_id):
    _turn(request_id)
    return seats(pipeline)


@pytest.fixture
def node(monkeypatch):
    """The gates dispatch_goal asks before a turn, open; nobody at the
    computer; no expert configured; the node's own model breaker closed."""
    from integrations.agent_engine import budget_gate, dispatch
    monkeypatch.setattr(budget_gate, 'pre_dispatch_budget_gate',
                        lambda *a, **k: (True, 'ok'))
    monkeypatch.setattr(budget_gate, 'release_goal_reservations',
                        lambda *a, **k: None)
    monkeypatch.setattr(dispatch, 'is_user_recently_active', lambda: False)
    monkeypatch.setattr(dispatch, '_get_distributed_coordinator', lambda: None)
    monkeypatch.setattr(dispatch, 'revenue_expert_config', lambda *a, **k: None)
    monkeypatch.setattr(dispatch, '_dispatch_provider_host', lambda *a, **k: '')
    from security import hive_guardrails
    monkeypatch.setattr(hive_guardrails.GuardrailEnforcer, 'before_dispatch',
                        staticmethod(lambda prompt, **k: (True, 'ok', prompt)))
    monkeypatch.setattr(hive_guardrails.GuardrailEnforcer, 'after_response',
                        staticmethod(lambda *a, **k: (True, 'ok')))
    monkeypatch.setitem(sys.modules, 'integrations.agent_engine.world_model_bridge',
                        MagicMock(get_world_model_bridge=MagicMock(
                            return_value=MagicMock())))
    return dispatch


def _in_process_turn(node, monkeypatch, goal_id, goal_type, on_turn):
    """dispatch_goal's Tier 1 (local_chat_dispatch), as a bundled desktop
    runs it: Nunba's in-process adapter posts /chat through Flask's test
    client in this same thread.  ``on_turn(request_id)`` is that /chat."""
    seen = []

    def adapter_chat(text=None, user_id=None, agent_id=None, request_id=None, **_kw):
        seen.append(on_turn(request_id))
        return {'text': 'Imported three prospects into the pipeline.'}
    monkeypatch.setitem(sys.modules, 'routes.hartos_backend_adapter',
                        SimpleNamespace(chat=adapter_chat))
    reply = node.dispatch_goal('Find buyers for the product', 'u1', goal_id,
                               goal_type)
    assert reply == 'Imported three prospects into the pipeline.', reply
    assert len(seen) == 1, 'the in-process /chat was not reached'
    return seen[0]


def _http_turn(node, seats, pipeline, goal_id, goal_type):
    """dispatch_goal's Tier 2: the loopback POST of /chat, served on the
    HTTP server's thread with the headers the POST sent."""
    posts = []

    def post(url, json=None, headers=None, timeout=None, **_kw):
        with seats.env.app.test_request_context(
                '/chat', method='POST', json=json, headers=headers or {}):
            built = _build(seats, pipeline, json['request_id'])
        posts.append(SimpleNamespace(body=json, headers=dict(headers or {}),
                                     built=built))
        return SimpleNamespace(status_code=200,
                               json=lambda: {'response': 'Imported three prospects.'})
    with patch.object(node, 'local_chat_dispatch',
                      return_value=('unavailable', None)), \
            patch.object(node, '_cb_is_open', return_value=False), \
            patch.object(node, '_local_dispatch_base_url',
                         return_value='http://127.0.0.1:1'), \
            patch.object(node, '_internal_auth_headers',
                         return_value={'Authorization': 'Bearer the-nodes-token'}), \
            patch.object(node, 'pooled_post', side_effect=post):
        reply = node.dispatch_goal('Find buyers for the product', 'u1', goal_id,
                                   goal_type)
    assert reply == 'Imported three prospects.', reply
    assert len(posts) == 1
    return posts[0]


# ── the node's own daemon turn gets its goal type's families ──────────────

@pytest.mark.parametrize('pipeline', PIPELINES)
@pytest.mark.parametrize('goal_type, gets, lacks', [
    ('outreach', CRM | JOURNEY, MARKETING),
    ('sales', CRM | JOURNEY | MARKETING, set()),
    ('marketing', MARKETING, CRM | JOURNEY),
])
def test_a_daemon_goal_turn_carries_its_goal_types_families(
        seats, goal_row, node, monkeypatch, pipeline, goal_type, gets, lacks):
    goal_row(GOAL, goal_type)
    built = _in_process_turn(node, monkeypatch, GOAL, goal_type,
                             lambda rid: _build(seats, pipeline, rid))
    shown, ran = schema(built.helper), runs(built.assistant)
    assert gets <= shown, (
        f'{pipeline}: the daemon turn of a {goal_type} goal was built without '
        f'{sorted(gets - shown)} on the Helper -- the live #284 failure '
        f'("no tool named get_pipeline_status is registered with this agent")')
    assert gets <= ran, (
        f'{pipeline}: the Assistant cannot run {sorted(gets - ran)}')
    assert not lacks & shown, (
        f'{pipeline}: a {goal_type} goal got {sorted(lacks & shown)}, which '
        f'its goal type does not name')
    assert goal_type in built.assistant._hart_unlocked_tags


@pytest.mark.parametrize('pipeline', PIPELINES)
def test_the_nodes_own_http_self_post_carries_the_families(
        seats, goal_row, node, pipeline):
    """dispatch_goal's Tier 2 and the hive worker's fallback reach /chat
    over the loopback, served on another thread: the POST's own proof is
    what makes the turn the node's."""
    goal_row(GOAL, 'outreach')
    turn = _http_turn(node, seats, pipeline, GOAL, 'outreach')
    assert CRM | JOURNEY <= schema(turn.built.helper), pipeline
    assert turn.headers.get('Authorization') == 'Bearer the-nodes-token', (
        "the node's own credential still rides the self-POST")


def test_the_hive_workers_http_turn_carries_the_families(seats, goal_row,
                                                         node, monkeypatch):
    from tests.unit.test_hive_worker_works_as_its_goal import (
        _GOAL, _allow_dispatch, _coordinator_with_goal, _loop)
    goal_row(_GOAL, 'outreach')
    monkeypatch.setattr('integrations.distributed_agent.requesters.this_node_id',
                        lambda: 'node-here')
    _, co = _coordinator_with_goal()
    task = co.claim_next_task('worker_a', capabilities=_loop()._capabilities)
    assert task is not None
    built = []

    def post(url, json=None, headers=None, timeout=None, **_kw):
        with seats.env.app.test_request_context(
                '/chat', method='POST', json=json, headers=headers or {}):
            built.append(_build(seats, 'create', json['request_id']))
        return SimpleNamespace(status_code=200,
                               json=lambda: {'response': 'Imported three prospects.'})
    with _allow_dispatch(), \
            patch.object(node, 'local_chat_dispatch',
                         return_value=('unavailable', None)), \
            patch.object(node, '_internal_auth_headers', return_value={}), \
            patch('integrations.distributed_agent.worker_loop.pooled_post',
                  side_effect=post):
        _loop()._execute_task(task)
    assert built, 'the worker never posted /chat'
    assert CRM | JOURNEY <= schema(built[0].helper)


# ── the trust boundary: only the node's own dispatch ─────────────────────

@pytest.mark.parametrize('pipeline', PIPELINES)
def test_a_client_naming_the_goals_request_id_gets_no_family(
        seats, goal_row, pipeline):
    """A /chat body's request_id reaches the builder as it is; naming a
    goal's daemon id makes nobody that goal's daemon."""
    from core.chat_client import daemon_request_id
    goal_row(GOAL, 'outreach')
    built = _build(seats, pipeline, daemon_request_id(GOAL))
    assert not FAMILIES & schema(built.helper), pipeline
    assert not FAMILIES & runs(built.assistant), pipeline


def test_the_daemons_own_turn_does_not_reach_another_thread(
        seats, goal_row, node, monkeypatch):
    """While the goal's own turn runs in-process, a client's /chat naming
    the same request id on another thread (the HTTP server's) gets nothing."""
    goal_row(GOAL, 'outreach')
    other = {}

    def on_turn(rid):
        t = threading.Thread(
            target=lambda: other.update(built=_build(seats, 'create', rid)))
        t.start()
        t.join(600)
        assert 'built' in other, 'the other thread did not build'
        return _build(seats, 'create', rid)
    built = _in_process_turn(node, monkeypatch, GOAL, 'outreach', on_turn)
    assert CRM <= schema(built.helper)
    assert not FAMILIES & schema(other['built'].helper)


def test_a_turns_mark_is_for_its_own_goal_only(seats, goal_row, node,
                                               monkeypatch):
    """Inside goal A's own turn, a build naming goal B's id is not B's turn."""
    from core.chat_client import daemon_request_id
    goal_row(GOAL, 'marketing')
    other_goal = goal_row('goal-284-other', 'outreach')
    seen = {}

    def on_turn(rid):
        seen['other'] = _build(seats, 'create', daemon_request_id(other_goal))
        return _build(seats, 'create', rid)
    built = _in_process_turn(node, monkeypatch, GOAL, 'marketing', on_turn)
    assert MARKETING <= schema(built.helper)
    assert not FAMILIES & schema(seen['other'].helper)


def test_the_mark_ends_with_the_turn(seats, goal_row, node, monkeypatch):
    """The daemon thread's next build, after the turn, is no longer it."""
    from core.chat_client import daemon_request_id
    goal_row(GOAL, 'outreach')
    _in_process_turn(node, monkeypatch, GOAL, 'outreach',
                     lambda rid: _build(seats, 'create', rid))
    after = _build(seats, 'create', daemon_request_id(GOAL))
    assert not FAMILIES & schema(after.helper)


def test_a_replay_of_an_earlier_self_posts_headers_gets_no_family(
        seats, goal_row, node):
    """Every header the node's own POST sent, replayed by a client with the
    same request id once that turn is over, proves nothing."""
    goal_row(GOAL, 'outreach')
    turn = _http_turn(node, seats, 'create', GOAL, 'outreach')
    assert CRM <= schema(turn.built.helper)
    with seats.env.app.test_request_context(
            '/chat', method='POST', json=turn.body, headers=turn.headers):
        replayed = _build(seats, 'create', turn.body['request_id'])
    assert not FAMILIES & schema(replayed.helper)


def test_a_daemon_turn_for_a_goal_with_no_row_gets_no_family(seats, node,
                                                             monkeypatch):
    """The goal's ROW decides, never the goal_type a caller passes."""
    built = _in_process_turn(node, monkeypatch, 'goal-284-no-row', 'outreach',
                             lambda rid: _build(seats, 'create', rid))
    assert not FAMILIES & schema(built.helper)


# ── a person's turn is built exactly as before ───────────────────────────

@pytest.mark.parametrize('pipeline', PIPELINES)
def test_a_persons_turn_is_built_as_before(seats, goal_row, pipeline):
    goal_row(GOAL, 'outreach')
    built = _build(seats, pipeline, A_PERSONS_REQUEST_ID)
    assert not FAMILIES & schema(built.helper), pipeline
    assert built.assistant._hart_unlocked_tags == set(), pipeline


@pytest.mark.parametrize('pipeline', PIPELINES)
def test_a_client_asking_for_outreach_in_its_body_unlocks_nothing(seats,
                                                                  pipeline):
    """/chat reads the body's goal_tags through
    marketing_tools.user_assignable_goal_tags and _save_and_enter_review
    stamps the record with stamp_goal_tags(that hint, the gathered record's
    own tags): a family name in the body never reaches the record, so the
    person's agent is built without it."""
    from integrations.agent_engine.marketing_tools import (
        stamp_goal_tags, user_assignable_goal_tags)
    hint = user_assignable_goal_tags(['outreach', 'sales', 'marketing'])
    assert hint == []
    stamped = stamp_goal_tags(hint, None)
    assert stamped is None, 'the record keeps no goal_tags of the body'
    _turn(A_PERSONS_REQUEST_ID)
    built = seats(pipeline, goal_tags=stamped)
    assert not FAMILIES & schema(built.helper), pipeline
