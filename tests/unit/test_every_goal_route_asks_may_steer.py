"""Every route that acts on, or reads, ONE agent goal asks dashboard_service.may_steer.

Review of 924b8e9dc (2026-09-27, REJECTED): "one may_steer for every steering
verb" held only on /api/social/dashboard/agents/<id>/{inject,pause,resume,
cancel}.  Beside it:

* PATCH /api/goals/<id>/status and DELETE /api/goals/<id> had a second rule
  (``goal.created_by and created_by != g.user.id``): any signed-in user could
  pause or archive a goal whose created_by was NULL; an archived goal could
  be revived; the owner and an admin were refused when created_by was a
  label; 404 vs 403 said which ids exist.
* /tracker/experiments/<post_id>/inject and /interview checked only that a
  token existed: user B wrote into A's agent's memory and ran A's agent as A.
  /tracker/dual-context cloned A's goal into new goals OWNED BY A.
* /dashboard/agents/<id>/snapshot, /chat and /a2a had no auth at all: a
  remote caller with no token read another user's live GroupChat.

ROUTES below is the vocabulary: every goal-scoped rule on these blueprints
must be in it (test_every_goal_scoped_route_is_classified reads the app's
url_map), and every entry is driven through the real app: a non-owner is
refused with the SAME answer as an unknown id, the owner is admitted.  A new
goal route without the rule fails here.

Behavioural: real blueprints, real dashboard_service / GoalManager, SQLite.
Patched boundaries: get_db, the token store, the audit log, /chat's HTTP
call, the memory graph's disk.
"""
import os
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('SOCIAL_DB_PATH', ':memory:')

from hartos.lifecycle_hooks import register_groupchat_for_session  # noqa: E402
from integrations.agent_engine.api import agent_engine_bp  # noqa: E402
from integrations.coding_agent.api import coding_agent_bp  # noqa: E402
from integrations.distributed_agent.api import distributed_agent_bp  # noqa: E402
from integrations.social.api_audit import audit_bp  # noqa: E402
from integrations.social.api_dashboard import dashboard_bp  # noqa: E402
from integrations.social.api_tracker import tracker_bp  # noqa: E402
from integrations.social.models import AgentGoal, Base, User  # noqa: E402

REMOTE = {'REMOTE_ADDR': '203.0.113.7'}
TOKEN = {'Authorization': 'Bearer t'}

# (method, rule) -> how the route is called for goal ``gid`` / post ``pid``.
ROUTES = {
    ('POST', '/api/social/dashboard/agents/<agent_id>/inject'):
        lambda gid, pid: ('post', f'/api/social/dashboard/agents/{gid}/inject', {'instruction': 'go'}),
    ('POST', '/api/social/dashboard/agents/<agent_id>/pause'):
        lambda gid, pid: ('post', f'/api/social/dashboard/agents/{gid}/pause', {}),
    ('POST', '/api/social/dashboard/agents/<agent_id>/resume'):
        lambda gid, pid: ('post', f'/api/social/dashboard/agents/{gid}/resume', {}),
    ('POST', '/api/social/dashboard/agents/<agent_id>/cancel'):
        lambda gid, pid: ('post', f'/api/social/dashboard/agents/{gid}/cancel', {}),
    ('GET', '/api/social/dashboard/agents/<agent_id>/snapshot'):
        lambda gid, pid: ('get', f'/api/social/dashboard/agents/{gid}/snapshot', None),
    ('GET', '/api/social/dashboard/agents/<agent_id>/chat'):
        lambda gid, pid: ('get', f'/api/social/dashboard/agents/{gid}/chat', None),
    ('GET', '/api/social/dashboard/agents/<agent_id>/a2a'):
        lambda gid, pid: ('get', f'/api/social/dashboard/agents/{gid}/a2a', None),
    ('GET', '/api/goals/<goal_id>'):
        lambda gid, pid: ('get', f'/api/goals/{gid}', None),
    ('PATCH', '/api/goals/<goal_id>/status'):
        lambda gid, pid: ('patch', f'/api/goals/{gid}/status', {'status': 'paused'}),
    ('DELETE', '/api/goals/<goal_id>'):
        lambda gid, pid: ('delete', f'/api/goals/{gid}', None),
    ('POST', '/api/social/tracker/experiments/<post_id>/inject'):
        lambda gid, pid: ('post', f'/api/social/tracker/experiments/{pid}/inject', {'variable': 'v'}),
    ('POST', '/api/social/tracker/experiments/<post_id>/interview'):
        lambda gid, pid: ('post', f'/api/social/tracker/experiments/{pid}/interview', {'question': 'q'}),
    ('GET', '/api/coding/goals/<goal_id>'):
        lambda gid, pid: ('get', f'/api/coding/goals/{gid}', None),
    ('GET', '/api/distributed/goals/<goal_id>/progress'):
        lambda gid, pid: ('get', f'/api/distributed/goals/{gid}/progress', None),
    ('GET', '/api/social/audit/agents/<agent_id>/timeline'):
        lambda gid, pid: ('get', f'/api/social/audit/agents/{gid}/timeline', None),
    ('GET', '/api/social/audit/agents/<agent_id>/conversations'):
        lambda gid, pid: ('get', f'/api/social/audit/agents/{gid}/conversations', None),
    ('GET', '/api/social/audit/agents/<agent_id>/thinking'):
        lambda gid, pid: ('get', f'/api/social/audit/agents/{gid}/thinking', None),
    ('POST', '/api/social/tracker/dual-context'):
        lambda gid, pid: ('post', '/api/social/tracker/dual-context',
                          {'post_id': pid, 'contexts': [{'label': 'a'}, {'label': 'b'}]}),
}

# Tracker rules that act on a post, not on its agent: classified here so a
# NEW tracker rule has to be put in one list or the other.
TRACKER_NOT_AGENT = {
    ('GET', '/api/social/tracker/experiments'),
    ('GET', '/api/social/tracker/experiments/<post_id>'),
    ('GET', '/api/social/tracker/experiments/<post_id>/conversations'),
    ('POST', '/api/social/tracker/experiments/<post_id>/approve'),
    ('POST', '/api/social/tracker/experiments/<post_id>/reject'),
    ('GET', '/api/social/tracker/notifications'),
    ('GET', '/api/social/tracker/experiments/<post_id>/pledges'),
    ('GET', '/api/social/tracker/experiments/<post_id>/pledge-summary'),
    ('POST', '/api/social/tracker/experiments/<post_id>/pledge'),
    ('DELETE', '/api/social/tracker/experiments/<post_id>/pledge/<int:escrow_id>'),
    ('POST', '/api/social/tracker/experiments/<post_id>/consume'),
    ('GET', '/api/social/tracker/experiments/<post_id>/insights'),
    ('GET', '/api/social/tracker/pledges/mine'),
    ('GET', '/api/social/tracker/pledges/all'),
    ('POST', '/api/social/tracker/pledges/<int:escrow_id>/verify'),
    ('GET', '/api/social/tracker/encounters'),
}


_SESSIONS = []  # the module's sessionmaker, for _as (require_auth's g.db)


# Goal routes that only an admin reaches at all (require_admin); an admin is
# admitted by may_steer, so they are the rule's by construction.
ADMIN_ONLY = {
    ('PATCH', '/api/coding/goals/<goal_id>'),
}

# Parameterised rules on these blueprints whose parameter is NOT a goal (a
# product, a patent, a ledger or distributed task, a speculation).  The
# resolver sweep below still drives each one as a stranger, so a route
# listed here that does reach a goal's content fails anyway.
NOT_GOAL = {
    ('GET', '/api/marketing/products/<product_id>'),
    ('PUT', '/api/marketing/products/<product_id>'),
    ('DELETE', '/api/marketing/products/<product_id>'),
    ('GET', '/api/agent-engine/speculation/<speculation_id>'),
    ('GET', '/api/agent-engine/ledger/tasks/<task_id>'),
    ('GET', '/api/ip/patents/<patent_id>'),
    ('PATCH', '/api/ip/patents/<patent_id>/status'),
    ('POST', '/api/distributed/tasks/<task_id>/submit'),
    ('POST', '/api/distributed/tasks/<task_id>/verify'),
}

BLUEPRINTS = (dashboard_bp, agent_engine_bp, tracker_bp, coding_agent_bp,
              distributed_agent_bp, audit_bp)

# What a goal's content reader returns in this suite.  A stranger's answer
# from ANY route must never carry it.
SECRET = 'GOAL-CONTENT-7f3a'


@pytest.fixture(scope='module')
def sf():
    eng = create_engine('sqlite://', connect_args={'check_same_thread': False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    maker = sessionmaker(bind=eng)
    _SESSIONS[:] = [maker]
    return maker


@pytest.fixture
def app(sf, monkeypatch, tmp_path):
    for var in ('TRUSTED_PROXY', 'NUNBA_CI', 'HEVOLVE_TRUST_KONG',
                'HEVOLVE_CLOUD_MODE'):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-1')
    a = Flask(__name__)
    a.config['TESTING'] = True
    for bp in BLUEPRINTS:
        a.register_blueprint(bp)
    chat = SimpleNamespace(status_code=200, json=lambda: {'response': 'ok'})
    # The goal-content READERS every goal route resolves through, each
    # answering SECRET; and the status WRITER, recorded.  The resolver sweep
    # (test_no_route_gives_a_stranger_a_goals_content) keys on these, not on
    # a parameter's name.
    from integrations.agent_engine.goal_manager import GoalManager
    real_status_write = GoalManager.update_goal_status
    writes = []

    def _recorded_write(db, goal_id, status):
        import inspect
        callers = [f.function for f in inspect.stack()[1:6]]
        writes.append((goal_id, status, callers))
        return real_status_write(db, goal_id, status)

    coordinator = MagicMock()
    # Plain values for the rest of the coordinator the routes call, so a
    # route's jsonify never meets a MagicMock.
    coordinator.submit_result.return_value = True
    coordinator.verify_result.return_value = False
    coordinator.claim_next_task.return_value = None
    coordinator.create_baseline.return_value = 'snap-1'
    coordinator.submit_goal.return_value = 'dist-new'
    coordinator.get_goal_progress.side_effect = lambda gid: {
        'goal_id': gid, 'context': {}, 'tasks': [SECRET]}
    with patch('integrations.social.models.get_db', side_effect=lambda: sf()), \
         patch.object(GoalManager, 'get_goal', staticmethod(
             lambda db, gid: {'success': True, 'goal': {'title': SECRET}})), \
         patch.object(GoalManager, 'update_goal_status', staticmethod(_recorded_write)), \
         patch('integrations.social.dashboard_service.DashboardService.get_agent_snapshot',
               side_effect=lambda db, gid: {'agent': {'title': SECRET}}), \
         patch('integrations.social.dashboard_service.DashboardService.get_agent_chat_tail',
               side_effect=lambda gid, **kw: {'messages': [SECRET]}), \
         patch('integrations.social.dashboard_service.get_a2a_graph',
               side_effect=lambda gid, **kw: {'nodes': [SECRET]}), \
         patch('integrations.distributed_agent.api._get_coordinator',
               return_value=coordinator), \
         patch('integrations.distributed_agent.coordinator_backends.GossipTaskBridge') as gossip, \
         patch('integrations.distributed_agent.requesters._dir',
               return_value=str(tmp_path)), \
         patch('integrations.coding_agent.api._IS_CENTRAL', True), \
         patch('security.immutable_audit_log.get_audit_log'), \
         patch('core.http_pool.pooled_post', return_value=chat) as posted, \
         patch('integrations.channels.memory.memory_graph.MemoryGraph') as graph, \
         patch('core.platform_paths.get_memory_graph_dir', return_value=str(tmp_path)), \
         patch('integrations.social.realtime.publish_event'):
        graph.return_value.register.return_value = 'm1'
        # The audit routes read a goal's memories: they answer SECRET too.
        graph.return_value.get_session_memories.return_value = [
            SimpleNamespace(to_dict=lambda: {'content': SECRET,
                                             'memory_type': 'conversation'}),
            SimpleNamespace(to_dict=lambda: {'content': SECRET,
                                             'memory_type': 'thinking'}),
        ]
        a.chat_post, a.memory_graph, a.writes = posted, graph, writes
        a.gossip = gossip
        yield a


@pytest.fixture
def client(app):
    return app.test_client()


def _user(sf, user_type='human', owner_id=None):
    db = sf()
    u = User(username=f'u_{uuid.uuid4().hex[:10]}', user_type=user_type,
             owner_id=owner_id)
    db.add(u)
    db.commit()
    uid = str(u.id)
    db.close()
    return uid


def _goal(sf, owner_id=None, created_by=None, status='active'):
    db = sf()
    gid, pid = uuid.uuid4().hex, uuid.uuid4().hex[:12]
    prompt = str(uuid.uuid4().int % 10**9)
    db.add(AgentGoal(id=gid, owner_id=owner_id, created_by=created_by,
                     goal_type='thought_experiment', title='secret title',
                     prompt_id=prompt, status=status,
                     config_json={'post_id': pid}))
    db.commit()
    db.close()
    gc = SimpleNamespace(messages=[{'role': 'user', 'content': 'PRIVATE'}])
    register_groupchat_for_session(f'{owner_id or "system"}_{prompt}', gc)
    return gid, pid, gc


def _status(sf, gid):
    db = sf()
    try:
        return db.query(AgentGoal).filter(AgentGoal.id == gid).first().status
    finally:
        db.close()


def _goal_count(sf):
    db = sf()
    try:
        return db.query(AgentGoal).count()
    finally:
        db.close()


def _as(uid, is_admin=False, role='flat', is_banned=False):
    user = SimpleNamespace(id=uid, is_admin=is_admin, role=role,
                           is_banned=is_banned)
    # require_auth keeps the returned session as g.db, so it must be real.
    return patch('integrations.social.auth._get_user_from_token',
                 side_effect=lambda token: (user, _SESSIONS[0]()))


def _call(client, key, gid, pid, environ=None, headers=None):
    method, url, body = ROUTES[key](gid, pid)
    kw = {'environ_base': environ or {}, 'headers': headers or {}}
    if body is not None:
        kw['json'] = body
    return getattr(client, method)(url, **kw)


def _goal_scoped_rules(app):
    """Every rule on BLUEPRINTS that takes a parameter (whatever it is
    called: review of dc32b1146, a guard keyed on '<goal_id>' missed routes
    whose parameter had another name), plus the tracker's."""
    names = {bp.name for bp in BLUEPRINTS}
    out = set()
    for r in app.url_map.iter_rules():
        if r.endpoint.split('.', 1)[0] not in names:
            continue  # the test app's own /static
        methods = r.methods - {'HEAD', 'OPTIONS'}
        for m in methods:
            if '<' in r.rule or r.rule.startswith('/api/social/tracker/'):
                out.add((m, r.rule))
    return out


def _fill(rule, value):
    """A concrete URL for ``rule`` with every parameter set to ``value``
    (an int converter gets 1)."""
    import re
    url = re.sub(r'<int:[^>]+>', '1', rule)
    return re.sub(r'<(?:[^:>]+:)?[^>]+>', value, url)


# ── the vocabulary guard ────────────────────────────────────────────────

def test_every_goal_scoped_route_is_classified(app):
    rules = _goal_scoped_rules(app)
    assert rules, 'enumeration found nothing -- it is broken'
    unclassified = rules - set(ROUTES) - TRACKER_NOT_AGENT - ADMIN_ONLY - NOT_GOAL
    assert not unclassified, (
        f'goal-scoped routes nobody decided about: {sorted(unclassified)}; '
        'add each to ROUTES (it must ask may_steer), ADMIN_ONLY, NOT_GOAL, or '
        'for a tracker route that acts on a post and not its agent, '
        'TRACKER_NOT_AGENT')
    stale = (set(ROUTES) | TRACKER_NOT_AGENT | ADMIN_ONLY | NOT_GOAL) - rules
    assert not stale, f'classified routes that no longer exist: {sorted(stale)}'


@pytest.mark.parametrize('key', sorted(ROUTES), ids=lambda k: f'{k[0]} {k[1]}')
def test_a_stranger_gets_the_unknown_id_answer(app, client, sf, key):
    """Another user's goal and a goal that does not exist answer alike, and
    nothing about the goal changes."""
    owner = _user(sf)
    gid, pid, gc = _goal(sf, owner_id=owner, created_by=None)
    before, goals_before = _status(sf, gid), _goal_count(sf)
    with _as(_user(sf)):
        theirs = _call(client, key, gid, pid, REMOTE, TOKEN)
        missing = _call(client, key, uuid.uuid4().hex, uuid.uuid4().hex[:12],
                        REMOTE, TOKEN)
    assert theirs.status_code == 403, (key, theirs.status_code, theirs.get_json())
    assert missing.status_code == theirs.status_code, key
    assert missing.get_json() == theirs.get_json(), key
    assert 'secret title' not in theirs.get_data(as_text=True)
    assert 'PRIVATE' not in theirs.get_data(as_text=True)
    assert _status(sf, gid) == before
    assert _goal_count(sf) == goals_before
    assert gc.messages == [{'role': 'user', 'content': 'PRIVATE'}]
    app.memory_graph.return_value.register.assert_not_called()
    app.chat_post.assert_not_called()


@pytest.mark.parametrize('key', sorted(ROUTES), ids=lambda k: f'{k[0]} {k[1]}')
def test_the_owner_is_admitted(app, client, sf, key):
    owner = _user(sf)
    gid, pid, _ = _goal(sf, owner_id=owner, created_by='agent_daemon',
                        status='paused' if key[1].endswith('/resume') else 'active')
    with _as(owner):
        r = _call(client, key, gid, pid, REMOTE, TOKEN)
    assert r.status_code in (200, 201), (key, r.status_code, r.get_json())


@pytest.mark.parametrize('key', sorted(ROUTES), ids=lambda k: f'{k[0]} {k[1]}')
def test_no_token_from_another_machine_is_refused(client, sf, key):
    gid, pid, _ = _goal(sf, owner_id=_user(sf))
    r = _call(client, key, gid, pid, REMOTE)
    assert r.status_code == 401, (key, r.status_code)
    # The structured answer a client keys on, whatever the prose says.
    assert r.get_json().get('needs_sign_in') is True, (key, r.get_json())


@pytest.mark.parametrize('key', sorted(ROUTES), ids=lambda k: f'{k[0]} {k[1]}')
def test_an_expired_token_from_another_machine_says_sign_in(client, sf, key):
    gid, pid, _ = _goal(sf, owner_id=_user(sf))
    with patch('integrations.social.auth._get_user_from_token',
               side_effect=lambda token: (None, None)):
        r = _call(client, key, gid, pid, REMOTE, TOKEN)
    assert r.status_code == 401, (key, r.status_code)
    assert r.get_json().get('needs_sign_in') is True, (key, r.get_json())


# ── the specific findings ───────────────────────────────────────────────

def test_a_goal_with_no_created_by_is_not_anyones_to_archive(client, sf):
    owner = _user(sf)
    gid, pid, _ = _goal(sf, owner_id=owner, created_by=None)
    with _as(_user(sf)):
        assert client.delete(f'/api/goals/{gid}', headers=TOKEN,
                             environ_base=REMOTE).status_code == 403
    assert _status(sf, gid) == 'active'


def test_a_cancelled_goal_cannot_be_revived(client, sf):
    owner = _user(sf)
    gid, pid, _ = _goal(sf, owner_id=owner, status='archived')
    with _as(owner):
        r = client.patch(f'/api/goals/{gid}/status', json={'status': 'active'},
                         headers=TOKEN, environ_base=REMOTE)
    assert r.status_code == 400, r.get_json()
    assert _status(sf, gid) == 'archived'


def test_an_admin_manages_any_goal(client, sf):
    gid, pid, _ = _goal(sf, owner_id=_user(sf), created_by='agent_daemon')
    with _as(_user(sf), is_admin=True):
        r = client.patch(f'/api/goals/{gid}/status', json={'status': 'paused'},
                         headers=TOKEN, environ_base=REMOTE)
    assert r.status_code == 200, r.get_json()
    assert _status(sf, gid) == 'paused'


def test_patch_status_takes_only_a_steering_verbs_status(client, sf):
    owner = _user(sf)
    gid, pid, _ = _goal(sf, owner_id=owner)
    with _as(owner):
        r = client.patch(f'/api/goals/{gid}/status', json={'status': 'completed'},
                         headers=TOKEN, environ_base=REMOTE)
    assert r.status_code == 400
    assert _status(sf, gid) == 'active'


def test_interview_runs_the_agent_as_its_owner_only_for_the_owner(app, client, sf):
    owner = _user(sf)
    gid, pid, _ = _goal(sf, owner_id=owner)
    with _as(owner):
        r = client.post(f'/api/social/tracker/experiments/{pid}/interview',
                        json={'question': 'why?'}, headers=TOKEN, environ_base=REMOTE)
    assert r.status_code == 200, r.get_json()
    assert app.chat_post.call_args.kwargs['json']['user_id'] == owner


def test_a_banned_token_on_this_machine_is_not_that_user(client, sf):
    """Review M5 (survived): a banned user's token on a loopback request must
    not stand for that user; the local owner answers instead."""
    banned = _user(sf)
    gid, pid, gc = _goal(sf, owner_id=banned)
    with _as(banned, is_banned=True):
        r = client.post(f'/api/social/dashboard/agents/{gid}/inject',
                        json={'instruction': 'x'}, headers=TOKEN)
    assert r.status_code == 403
    assert len(gc.messages) == 1


def test_a_signed_in_caller_on_this_machine_steers_a_machine_goal(client, sf):
    """A token names the caller, and the loopback test still says the call
    is this machine's: a goal no person owns (a seeded flywheel goal) is
    steerable from here through /api/goals as through the dashboard."""
    gid, pid, _ = _goal(sf, created_by='system_bootstrap')
    with _as(_user(sf)):
        r = client.patch(f'/api/goals/{gid}/status', json={'status': 'paused'},
                         headers=TOKEN)
    assert r.status_code == 200, r.get_json()
    assert _status(sf, gid) == 'paused'


# ── by resolver, not by name (review of dc32b1146) ──────────────────────

def test_no_route_gives_a_stranger_a_goals_content(app, client, sf):
    """Every parameterised rule on BLUEPRINTS, whatever its parameter is
    called, driven as a signed-in stranger with every parameter set to a
    real goal's id (which is also its post id): no answer carries what a goal
    content reader returns, no status is written, nothing is injected."""
    owner = _user(sf)
    db = sf()
    gid = uuid.uuid4().hex
    db.add(AgentGoal(id=gid, owner_id=owner, goal_type='thought_experiment',
                     title='secret title', prompt_id='4242', status='active',
                     config_json={'post_id': gid}))
    db.commit()
    db.close()
    leaked = []
    with _as(_user(sf)):
        for method, rule in sorted(_goal_scoped_rules(app)):
            r = getattr(client, method.lower())(
                _fill(rule, gid), json={}, headers=TOKEN, environ_base=REMOTE)
            body = r.get_data(as_text=True)
            # Both what the mocked readers return AND the row itself: a route
            # that reads the AgentGoal directly (and was filed as NOT_GOAL)
            # would carry its title (review of 275e8e361).
            if SECRET in body or 'secret title' in body:
                leaked.append((method, rule, r.status_code))
    assert not leaked, f'goal content reached a stranger: {leaked}'
    assert app.writes == []
    assert _status(sf, gid) == 'active'


@pytest.mark.parametrize('key', sorted(k for k in ROUTES if k[0] == 'GET'),
                         ids=lambda k: k[1])
def test_the_owner_reads_through_the_spied_readers(client, sf, key):
    """Control: the SECRET readers are the ones these routes use, so the
    sweep above can see a leak."""
    owner = _user(sf)
    gid, pid, _ = _goal(sf, owner_id=owner)
    with _as(owner):
        r = _call(client, key, gid, pid, REMOTE, TOKEN)
    assert r.status_code == 200, (key, r.get_json())
    assert SECRET in r.get_data(as_text=True), key


# ── a terminal goal stays terminal (review of dc32b1146) ────────────────

@pytest.mark.parametrize('terminal', ['completed', 'failed', 'error', 'archived'])
def test_a_finished_goal_cannot_be_paused_and_resumed_back_to_life(client, sf, terminal):
    owner = _user(sf)
    gid, pid, _ = _goal(sf, owner_id=owner, status=terminal)
    with _as(owner):
        paused = client.post(f'/api/social/dashboard/agents/{gid}/pause',
                             headers=TOKEN, environ_base=REMOTE)
        resumed = client.post(f'/api/social/dashboard/agents/{gid}/resume',
                              headers=TOKEN, environ_base=REMOTE)
    assert paused.status_code == 400, paused.get_json()
    assert resumed.status_code == 400, resumed.get_json()
    assert _status(sf, gid) == terminal


def test_a_paused_goal_is_not_paused_again(client, sf):
    """Only an active goal may be paused: the rule names the one status it
    leaves, rather than the ones it may not."""
    owner = _user(sf)
    gid, pid, _ = _goal(sf, owner_id=owner, status='paused')
    with _as(owner):
        r = client.post(f'/api/social/dashboard/agents/{gid}/pause',
                        headers=TOKEN, environ_base=REMOTE)
    assert r.status_code == 400
    assert _status(sf, gid) == 'paused'


# ── the tracker never runs an agent as nobody (review of dc32b1146) ─────

def test_interviewing_a_machine_goal_runs_it_as_the_caller(app, client, sf):
    """A goal no person owns has no owner to run /chat as; it was posted
    with user_id=None.  The caller this machine admitted asks, so the turn
    is theirs."""
    gid, pid, _ = _goal(sf, created_by='system_bootstrap')
    me = _user(sf)
    with _as(me):
        r = client.post(f'/api/social/tracker/experiments/{pid}/interview',
                        json={'question': 'why?'}, headers=TOKEN)
    assert r.status_code == 200, r.get_json()
    assert app.chat_post.call_args.kwargs['json']['user_id'] == me


def test_distributed_progress_of_an_api_submitted_goal_is_its_submitters(
        app, client, sf):
    """A goal submitted to /api/distributed/goals has no AgentGoal row.  Who
    submitted it is kept on THIS node (requesters.record_submitter), never in the
    coordinator's shared context or the gossip announce."""
    submitter = _user(sf)
    from integrations.distributed_agent import api as dist_api
    coord = dist_api._get_coordinator()
    coord.submit_goal.return_value = 'dist-1'
    with _as(submitter):
        made = client.post('/api/distributed/goals', json={
            'objective': 'o', 'tasks': [{'task_id': 't1', 'description': 'd'}]},
            headers=TOKEN, environ_base=REMOTE)
    assert made.status_code == 200, made.get_json()
    with _as(submitter):
        mine = client.get('/api/distributed/goals/dist-1/progress',
                          headers=TOKEN, environ_base=REMOTE)
    with _as(_user(sf)):
        theirs = client.get('/api/distributed/goals/dist-1/progress',
                            headers=TOKEN, environ_base=REMOTE)
    assert mine.status_code == 200 and SECRET in mine.get_data(as_text=True)
    assert theirs.status_code == 403
    assert SECRET not in theirs.get_data(as_text=True)


def test_the_submitter_never_leaves_this_node(app, client, sf):
    """Review of 275e8e361: the submitter's id was stamped into the context
    that goes to the shared coordinator and to every peer (announce_goal,
    plaintext to a peer without X25519) -- egress under the 09-26 ruling."""
    from integrations.distributed_agent import api as dist_api
    coord = dist_api._get_coordinator()
    me = _user(sf)
    with _as(me):
        r = client.post('/api/distributed/goals', json={
            'objective': 'o', 'tasks': [{'task_id': 't1', 'description': 'd'}],
            'context': {'repo_url': 'a/b'}},
            headers=TOKEN, environ_base=REMOTE)
    assert r.status_code == 200, r.get_json()
    shared = coord.submit_goal.call_args.args[2]
    announced = app.gossip.return_value.announce_goal.call_args.args[3]
    for ctx in (shared, announced):
        assert me not in repr(ctx)
        assert 'user_id' not in ctx
        assert ctx.get('repo_url') == 'a/b'


def test_a_peers_goal_is_this_machines_to_read(client, sf):
    """A goal a peer gossiped here names ITS user in the context; that id is
    not an owner on this node.  The goal is the machine's: this machine's
    own callers read its progress (review of 275e8e361: they got 403), a
    remote stranger does not."""
    from integrations.distributed_agent import api as dist_api
    coord = dist_api._get_coordinator()
    coord.get_goal_progress.side_effect = lambda gid: {
        'goal_id': gid, 'context': {'user_id': 'a-user-on-another-node'},
        'tasks': [SECRET]}
    with _as(_user(sf)):
        local = client.get('/api/distributed/goals/peer-goal/progress',
                           headers=TOKEN)
        remote = client.get('/api/distributed/goals/peer-goal/progress',
                            headers=TOKEN, environ_base=REMOTE)
    assert local.status_code == 200, local.get_json()
    assert remote.status_code == 403


# ── ONE status writer (review of 275e8e361) ─────────────────────────────

def test_no_route_revives_a_finished_goal_even_for_an_admin(app, client, sf):
    """Every parameterised rule, driven as a central admin with a body that
    asks for 'active' (and with none): a completed goal never becomes active
    or paused, and every status write goes through the one writer,
    dashboard_service._write_goal_status.  PATCH /api/coding/goals/<id>
    wrote any status (default 'active') and revived completed goals."""
    revived, stray = [], []
    with _as(_user(sf), is_admin=True, role='central'):
        for method, rule in sorted(_goal_scoped_rules(app)):
            for body in ({'status': 'active'}, {}):
                gid, pid, _ = _goal(sf, owner_id=_user(sf), status='completed')
                db = sf()
                row = db.query(AgentGoal).filter(AgentGoal.id == gid).first()
                row.config_json = {'post_id': gid}
                db.commit()
                db.close()
                getattr(client, method.lower())(
                    _fill(rule, gid), json=body, headers=TOKEN,
                    environ_base=REMOTE)
                if _status(sf, gid) in ('active', 'paused'):
                    revived.append((method, rule, body))
    for goal_id, status, callers in app.writes:
        if '_write_goal_status' not in callers:
            stray.append((goal_id, status, callers))
    assert not revived, f'finished goals revived: {revived}'
    assert not stray, f'status written outside _write_goal_status: {stray}'


def test_patch_coding_goal_status_goes_through_the_steering_rule(client, sf):
    gid, pid, _ = _goal(sf, owner_id=_user(sf), status='active')
    with _as(_user(sf), is_admin=True, role='central'):
        paused = client.patch(f'/api/coding/goals/{gid}', json={'status': 'paused'},
                              headers=TOKEN, environ_base=REMOTE)
        empty = client.patch(f'/api/coding/goals/{gid}', json={},
                             headers=TOKEN, environ_base=REMOTE)
    assert paused.status_code == 200, paused.get_json()
    assert empty.status_code == 400
    assert _status(sf, gid) == 'paused'



# ── review of d4146f843 ─────────────────────────────────────────────────

AUDIT = ('timeline', 'conversations', 'thinking')


@pytest.mark.parametrize('route', AUDIT)
def test_a_trained_agents_owner_reads_its_history_from_another_machine(
        client, sf, route):
    """The dashboard lists a trained agent by its users-row id; its owner
    reads its history from Hevolve web, which is always another machine.
    The id no goal claims was treated as the machine's: the owner got 403."""
    owner = _user(sf)
    agent = _user(sf, user_type='agent', owner_id=owner)
    url = f'/api/social/audit/agents/{agent}/{route}'
    with _as(owner):
        mine = client.get(url, headers=TOKEN, environ_base=REMOTE)
    with _as(agent):
        itself = client.get(url, headers=TOKEN, environ_base=REMOTE)
    with _as(_user(sf)):
        theirs = client.get(url, headers=TOKEN, environ_base=REMOTE)
    assert mine.status_code == 200, mine.get_json()
    assert itself.status_code == 200, itself.get_json()
    assert theirs.status_code == 403
    assert SECRET not in theirs.get_data(as_text=True)


@pytest.mark.parametrize('route', AUDIT)
def test_a_person_reads_their_own_history_by_their_user_id(client, sf, route):
    me = _user(sf)
    with _as(me):
        r = client.get(f'/api/social/audit/agents/{me}/{route}',
                       headers=TOKEN, environ_base=REMOTE)
    assert r.status_code == 200, r.get_json()


def test_an_ownerless_agent_account_stays_the_machines(client, sf):
    agent = _user(sf, user_type='agent')
    with _as(_user(sf)):
        remote = client.get(f'/api/social/audit/agents/{agent}/timeline',
                            headers=TOKEN, environ_base=REMOTE)
        local = client.get(f'/api/social/audit/agents/{agent}/timeline',
                           headers=TOKEN)
    assert remote.status_code == 403
    assert local.status_code == 200, local.get_json()


def test_a_run_with_no_live_groupchat_says_so_structurally(client, sf):
    owner = _user(sf)
    db = sf()
    gid = uuid.uuid4().hex
    db.add(AgentGoal(id=gid, owner_id=owner, goal_type='coding', title='t',
                     prompt_id='31337', status='active'))
    db.commit()
    db.close()
    with _as(owner):
        r = client.post(f'/api/social/dashboard/agents/{gid}/inject',
                        json={'instruction': 'go'}, headers=TOKEN,
                        environ_base=REMOTE)
    assert r.status_code == 400
    assert r.get_json()['data'].get('not_steerable') is True


def test_progress_of_a_goal_this_node_stamped_is_its_requesters(app, client, sf):
    """No submitter record (the goal came through dispatch, or predates the
    record): the requester its own context names counts when THIS node
    stamped it -- a handle minted here.  A raw id with no source is a
    peer's word and names nobody."""
    me = _user(sf)
    from integrations.distributed_agent import api as dist_api
    from integrations.distributed_agent.requesters import requester_handle
    coord = dist_api._get_coordinator()
    handle = requester_handle('dist-h', me)
    contexts = {'dist-h': {'user_id': handle, 'source_node': 'node-x'},
                'dist-raw': {'user_id': me}}
    coord.get_goal_progress.side_effect = lambda gid: {
        'goal_id': gid, 'context': contexts[gid], 'tasks': [SECRET]}
    with patch('integrations.distributed_agent.requesters.this_node_id',
               return_value='node-x'), _as(me):
        mine = client.get('/api/distributed/goals/dist-h/progress',
                          headers=TOKEN, environ_base=REMOTE)
        raw = client.get('/api/distributed/goals/dist-raw/progress',
                         headers=TOKEN, environ_base=REMOTE)
    assert mine.status_code == 200, mine.get_json()
    assert raw.status_code == 403
