"""On a FLAT node, this node's own experiments evolve here without a vote.

OWNER RULINGS (verbatim):
  2026-11-14: "voting shd not be a prerequisite to evolve locally untill we
  cross the intelligence barrier".
  2026-11-15: "the no ovite experiment applies only for topology flat, for
  regional and central it shd remain with voites".

MEASURED 2026-11-14 on the owner's desktop (read-only queries of
~/Documents/Nunba/data/hevolve_database.db): 812 thought experiments, 9
votes ever; 505 created by the node's own 'nunba' machine account; only a
September test probe ever passed the vote, so nothing else ever ran.

FIRST ATTEMPT (e7b769936) was REJECTED and reverted (1f5be2a79): it counted
every account no person owns as "this node", and POST /api/social/auth/
register makes exactly such an account for anyone (user_type 'agent', no
password, owner_id from the request body).  Stranger -> register -> create
-> evaluate started an agent goal with no vote (AE-7 reopened); a vote
AGAINST was ignored for own experiments; and it applied on central too.

The rule now (voting_rules.evaluation_permit, asked by the evaluation-goal
writer and auto-evolve's ranking):
  - approved by the vote: evaluated, on every topology, as before;
  - otherwise ONLY on a flat node, ONLY this node's own experiment, ONLY
    outside steward contexts, and ONLY while no vote is against it;
  - "own" is an account only this node holds (voting_rules.node_holds):
    the owner HEVOLVE_OWNER_USER_ID names, or a 'system' account owned by
    no one that no one can sign in to (no password; ensure_system_user's
    'nunba').  Never an agent, never a 'system' account with a password:
    /auth/register can make both.

Real SQLite file, the real service, the real orchestrator, the real REST
routes; only the session factories are pointed at the test database.
Each check: {what, check, expected, tolerance 0 (exact invariant)}.
"""
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('SOCIAL_DB_PATH', ':memory:')

from integrations.social.models import (  # noqa: E402
    AgentGoal, Base, ExperimentVote, ThoughtExperiment, User)
from integrations.social.thought_experiment_service import (  # noqa: E402
    ThoughtExperimentService)

DEFAULT = ('Cache warmup', 'Faster cache warmup lowers latency')
SECURITY = ('Tighten the security guardrail',
            'A stricter guardrail blocks the vulnerability')


@pytest.fixture
def engine(tmp_path, monkeypatch):
    # A file, not :memory: -- the cycle dispatches on a worker thread, and
    # an in-memory SQLite database is a different, empty one per thread.
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)
    monkeypatch.delenv('HEVOLVE_NODE_TIER', raising=False)  # unset = flat
    eng = create_engine(f"sqlite:///{tmp_path / 'evolve.db'}",
                        connect_args={'check_same_thread': False})
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _user(db, user_type='human', owner_id=None, username=None,
          password_hash=None):
    u = User(username=username or f'livetest_local_{uuid.uuid4().hex[:8]}',
             user_type=user_type, owner_id=owner_id,
             password_hash=password_hash)
    db.add(u)
    db.flush()
    return u


def _nunba(db):
    """The node's own machine account, made by the real bootstrap every
    in-process creator uses (request_agent_evaluation, the benchmark
    prover, the agent daemon)."""
    from integrations.social.services import UserService
    return UserService.ensure_system_user(db, 'nunba', display_name='Nunba')


def _experiment(db, creator_id, status='voting', title_hypothesis=DEFAULT,
                created_at=None):
    title, hypothesis = title_hypothesis
    e = ThoughtExperiment(
        id=str(uuid.uuid4()), creator_id=creator_id, title=title,
        hypothesis=hypothesis, expected_outcome='o', status=status,
        created_at=created_at or datetime.utcnow())
    db.add(e)
    db.commit()
    return e


def _vote(db, exp_id, value):
    db.add(ExperimentVote(
        experiment_id=exp_id, voter_id=_user(db).id, voter_type='human',
        vote_value=value, confidence=1.0))
    db.commit()


def _approve(db, exp_id):
    """Three distinct people FOR: quorum met, ratio 1.0."""
    for _ in range(3):
        _vote(db, exp_id, 2)


def _evaluate(db, e):
    result = ThoughtExperimentService.request_agent_evaluation(db, e.id)
    db.commit()
    return result, db.query(AgentGoal).count()


def _refused(result, goals, why):
    assert result['success'] is False
    assert result['reason'] == 'not_approved'
    assert result['permit']['reason'] == why
    assert result['hint'], 'a refusal says what would let it run'
    assert goals == 0


@contextmanager
def _pointed_at(db):
    @contextmanager
    def _session(commit=False):
        yield db
    with patch('integrations.social.models.db_session', _session):
        yield


# ── the evaluation-goal writer (every caller goes through it) ───────────

class TestTheWriterOnAFlatNode:
    def test_the_nodes_machine_account_experiment_starts_without_a_vote(self, db):
        """RED on HEAD (not_approved): the 505 'nunba' rows never ran."""
        e = _experiment(db, _nunba(db).id)
        result, goals = _evaluate(db, e)
        assert result['success'] is True and result['goal_id']
        assert result['approved_by_vote'] is False
        assert goals == 1
        db.refresh(e)
        assert e.status == 'evaluating'

    def test_flat_said_out_loud_is_flat(self, db, monkeypatch):
        monkeypatch.setenv('HEVOLVE_NODE_TIER', ' Flat ')
        result, goals = _evaluate(db, _experiment(db, _nunba(db).id))
        assert result['success'] is True and goals == 1

    def test_the_owners_own_experiment_starts_without_a_vote(self, db, monkeypatch):
        owner = _user(db)
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', owner.id)
        result, goals = _evaluate(db, _experiment(db, owner.id))
        assert result['success'] is True
        assert goals == 1

    def test_an_agent_is_never_the_node_even_one_owned_by_the_owner(
            self, db, monkeypatch):
        """/auth/register takes owner_id from the request body, so 'owned
        by the owner' is a claim anyone can make."""
        owner = _user(db)
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', owner.id)
        agent = _user(db, user_type='agent', owner_id=owner.id)
        result, goals = _evaluate(db, _experiment(db, agent.id))
        _refused(result, goals, 'not_this_nodes_own')

    def test_an_ownerless_agent_is_not_the_node(self, db):
        """e7b769936's hole: what /auth/register makes for anyone."""
        agent = _user(db, user_type='agent')
        result, goals = _evaluate(db, _experiment(db, agent.id))
        _refused(result, goals, 'not_this_nodes_own')

    def test_a_system_account_someone_can_sign_in_to_is_not_the_node(self, db):
        """/auth/register takes user_type from the body and always sets a
        password, so a 'system' account with one is someone's sign-in."""
        signed = _user(db, user_type='system', password_hash='x$y')
        result, goals = _evaluate(db, _experiment(db, signed.id))
        _refused(result, goals, 'not_this_nodes_own')

    def test_an_owned_system_account_is_not_the_node(self, db):
        owned = _user(db, user_type='system', owner_id=_user(db).id)
        result, goals = _evaluate(db, _experiment(db, owned.id))
        _refused(result, goals, 'not_this_nodes_own')

    def test_another_persons_unvoted_experiment_is_still_refused(self, db, monkeypatch):
        """Preservation of AE-7: GREEN on both trees."""
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', _user(db).id)
        e = _experiment(db, _user(db).id)
        result, goals = _evaluate(db, e)
        _refused(result, goals, 'not_this_nodes_own')
        db.refresh(e)
        assert e.status == 'voting'

    def test_a_creator_with_no_account_here_is_not_the_node(self, db):
        """A made-up creator id must not be a free pass."""
        result, goals = _evaluate(db, _experiment(db, 'no-such-account'))
        _refused(result, goals, 'not_this_nodes_own')

    def test_a_vote_against_the_nodes_own_experiment_sends_it_to_the_vote(self, db):
        """e7b769936 ignored it.  One person AGAINST: no quorum, so not
        rejected by the vote either -- it must not run until the vote
        approves it."""
        e = _experiment(db, _nunba(db).id)
        _vote(db, e.id, -2)
        result, goals = _evaluate(db, e)
        _refused(result, goals, 'vote_against')

    def test_a_vote_for_the_nodes_own_experiment_does_not_hold_it(self, db):
        e = _experiment(db, _nunba(db).id)
        _vote(db, e.id, 2)
        result, goals = _evaluate(db, e)
        assert result['success'] is True and goals == 1

    def test_the_nodes_security_experiment_still_needs_the_vote(self, db):
        """security_guardrail requires the steward: changes to security
        stay with people even on this node."""
        e = _experiment(db, _nunba(db).id, title_hypothesis=SECURITY)
        assert ThoughtExperimentService.tally_votes(
            db, e.id)['decision_context'] == 'security_guardrail'
        result, goals = _evaluate(db, e)
        _refused(result, goals, 'steward_required')

    @pytest.mark.parametrize('ended', ['decided', 'archived'])
    def test_the_nodes_closed_experiment_does_not_go_backwards(self, db, ended):
        e = _experiment(db, _nunba(db).id, status=ended)
        result, goals = _evaluate(db, e)
        assert result['success'] is False
        assert result['reason'] == 'invalid_status'
        assert goals == 0
        db.refresh(e)
        assert e.status == ended


class TestRegionalAndCentralKeepTheVote:
    @pytest.mark.parametrize('tier', ['regional', 'central', 'local', ''])
    def test_the_nodes_own_experiment_needs_the_vote(self, db, monkeypatch, tier):
        """Owner 2026-11-15.  Anything but flat keeps the vote, including a
        value this rule does not know."""
        monkeypatch.setenv('HEVOLVE_NODE_TIER', tier)
        result, goals = _evaluate(db, _experiment(db, _nunba(db).id))
        _refused(result, goals, 'topology_keeps_the_vote')

    @pytest.mark.parametrize('tier', ['regional', 'central'])
    def test_the_owners_own_experiment_needs_the_vote(self, db, monkeypatch, tier):
        owner = _user(db)
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', owner.id)
        monkeypatch.setenv('HEVOLVE_NODE_TIER', tier)
        result, goals = _evaluate(db, _experiment(db, owner.id))
        _refused(result, goals, 'topology_keeps_the_vote')

    @pytest.mark.parametrize('tier', ['regional', 'central'])
    def test_an_approved_experiment_still_runs(self, db, monkeypatch, tier):
        """Control: the vote path is the same everywhere."""
        monkeypatch.setenv('HEVOLVE_NODE_TIER', tier)
        e = _experiment(db, _user(db).id)
        _approve(db, e.id)
        result, goals = _evaluate(db, e)
        assert result['success'] is True and goals == 1
        assert result['approved_by_vote'] is True


# ── the live hole, through the real routes ──────────────────────────────

class TestAStrangerThroughTheRoutes:
    """register -> create -> evaluate, with the token register hands back,
    through the real require_auth.  On e7b769936 the agent case started a
    goal (the reviewer's finding); here every case is refused."""

    @pytest.fixture
    def client(self, engine, monkeypatch):
        from flask import Flask

        import integrations.social.api as social_api
        import integrations.social.rate_limiter as limiter
        from integrations.social.api_thought_experiments import (
            thought_experiments_bp)

        factory = sessionmaker(bind=engine)

        @contextmanager
        def _session(commit=True):
            s = factory()
            try:
                yield s
                if commit:
                    s.commit()
            finally:
                s.close()

        unlimited = {'max_tokens': 100000, 'refill_rate': 100000 / 60}
        for action in ('global', 'register'):
            monkeypatch.setitem(limiter.LIMITS, action, dict(unlimited))
        monkeypatch.setattr(social_api, 'db_session', _session)
        monkeypatch.setattr('integrations.social.models.get_db', factory)
        app = Flask(__name__)
        app.config['TESTING'] = True
        app.register_blueprint(social_api.social_bp)
        app.register_blueprint(thought_experiments_bp)
        return app.test_client()

    def _register_create_evaluate(self, client, body):
        reg = client.post('/api/social/auth/register', json=body)
        assert reg.status_code == 201, reg.get_json()
        token = reg.get_json()['data']['api_token']
        auth = {'Authorization': f'Bearer {token}'}
        made = client.post('/api/social/experiments', headers=auth, json={
            'title': DEFAULT[0], 'hypothesis': DEFAULT[1]})
        assert made.status_code == 201, made.get_json()
        exp_id = made.get_json()['data']['id']
        return client.post(f'/api/social/experiments/{exp_id}/evaluate',
                           headers=auth)

    def test_a_registered_ownerless_agent_starts_no_goal(self, client, db):
        resp = self._register_create_evaluate(client, {
            'username': 'quiet.amber.falcon', 'user_type': 'agent'})
        body = resp.get_json()
        assert resp.status_code == 400
        assert body['reason'] == 'not_approved'
        assert body['permit']['reason'] == 'not_this_nodes_own'
        assert db.query(AgentGoal).count() == 0

    def test_an_agent_claiming_the_owner_starts_no_goal(
            self, client, db, monkeypatch):
        owner = _user(db)
        db.commit()
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', owner.id)
        resp = self._register_create_evaluate(client, {
            'username': 'bright.cedar.otter', 'user_type': 'agent',
            'owner_id': owner.id})
        assert resp.status_code == 400
        assert resp.get_json()['permit']['reason'] == 'not_this_nodes_own'
        assert db.query(AgentGoal).count() == 0

    def test_a_registered_system_account_starts_no_goal(self, client, db):
        resp = self._register_create_evaluate(client, {
            'username': 'livetest_sys_squat', 'password': 'correct-horse-9',
            'user_type': 'system'})
        assert resp.status_code == 400
        assert resp.get_json()['permit']['reason'] == 'not_this_nodes_own'
        assert db.query(AgentGoal).count() == 0


# ── the other doors that name a creator (claude-autostart-d2's read) ────

class TestTheToolCannotNameThisNode:
    """create_thought_experiment takes creator_id from its caller (agents,
    the MCP bridge).  Naming the owner or the node's machine account would
    make an experiment that runs with no vote on a flat node."""

    @pytest.fixture
    def make(self, db, monkeypatch):
        import json as _json

        from hartos.threadlocal import thread_local_data
        from integrations.agent_engine.thought_experiment_tools import (
            create_thought_experiment)

        def _make(creator_id, turn_user=None):
            monkeypatch.setattr(thread_local_data, 'get_user_id',
                                lambda: turn_user)
            with _pointed_at(db):
                out = _json.loads(create_thought_experiment(
                    creator_id, DEFAULT[0], DEFAULT[1], 'o'))
            db.commit()
            return out
        return _make

    def _count(self, db, creator_id):
        return db.query(ThoughtExperiment).filter(
            ThoughtExperiment.creator_id == creator_id).count()

    def test_no_turn_cannot_create_as_the_owner(self, db, make, monkeypatch):
        """The MCP bridge runs tools with no turn."""
        owner = _user(db)
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', owner.id)
        out = make(owner.id)
        assert out['success'] is False
        assert out['reason'] == 'creator_is_this_node' and out['detail']
        assert self._count(db, owner.id) == 0

    def test_another_turn_cannot_create_as_the_nodes_machine_account(
            self, db, make):
        nunba = _nunba(db)
        stranger = _user(db)
        out = make(nunba.id, turn_user=stranger.id)
        assert out['success'] is False
        assert out['reason'] == 'creator_is_this_node'
        assert self._count(db, nunba.id) == 0

    def test_even_a_turn_running_as_the_node_cannot(self, db, make):
        """A goal dispatched through MCP runs AS 'nunba' (mcp_server): its
        goal text must not mint a no-vote experiment as the node."""
        nunba = _nunba(db)
        out = make(nunba.id, turn_user=nunba.id)
        assert out['success'] is False
        assert out['reason'] == 'creator_is_this_node'
        assert self._count(db, nunba.id) == 0

    def test_even_the_owners_own_turn_cannot(self, db, make, monkeypatch):
        """The owner creates their own through the signed-in route."""
        owner = _user(db)
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', owner.id)
        out = make(owner.id, turn_user=owner.id)
        assert out['success'] is False
        assert self._count(db, owner.id) == 0

    def test_anyone_else_is_created_and_still_needs_the_vote(self, db, make):
        stranger = _user(db)
        out = make(stranger.id)
        assert out['success'] is True
        e = db.query(ThoughtExperiment).filter(
            ThoughtExperiment.creator_id == stranger.id).one()
        result, goals = _evaluate(db, e)
        _refused(result, goals, 'not_this_nodes_own')


def test_a_system_row_an_inbound_sync_made_is_not_the_node(db):
    """sync_engine._handle_sync_user makes users with the payload's own id
    and user_type, no owner, no password, never verified."""
    from integrations.social.sync_engine import SyncEngine
    SyncEngine._handle_sync_user(db, {
        'user_id': 'synced-sys-1', 'username': 'livetest_synced_sys',
        'user_type': 'system'})
    db.commit()
    row = db.query(User).filter(User.id == 'synced-sys-1').one()
    assert row.user_type == 'system' and not row.password_hash
    result, goals = _evaluate(db, _experiment(db, row.id))
    _refused(result, goals, 'not_this_nodes_own')


# ── the auto-evolve cycle ───────────────────────────────────────────────

class TestTheCycle:
    def _orch(self):
        from integrations.agent_engine.auto_evolve import (
            AutoEvolveOrchestrator, EvolveSession)
        return AutoEvolveOrchestrator(), EvolveSession(status='selecting')

    def test_the_nodes_own_unvoted_experiment_is_gathered_behind_newer_rows(self, db):
        """RED on HEAD: only voted rows were gathered."""
        now = datetime.utcnow()
        own = _experiment(db, _nunba(db).id, status='evaluating',
                          created_at=now - timedelta(days=150))
        stranger = _user(db)
        for i in range(60):
            _experiment(db, stranger.id, status='evaluating',
                        created_at=now - timedelta(minutes=i))
        orch, session = self._orch()
        with _pointed_at(db):
            ids = [c['id'] for c in orch._gather_candidates(
                session, ['voting', 'evaluating'])]
        assert ids == [own.id], 'a stranger\'s unvoted rows are still not gathered'

    @pytest.mark.parametrize('tier', ['regional', 'central'])
    def test_off_flat_the_nodes_own_unvoted_row_is_not_gathered(
            self, db, monkeypatch, tier):
        monkeypatch.setenv('HEVOLVE_NODE_TIER', tier)
        _experiment(db, _nunba(db).id, status='evaluating')
        orch, session = self._orch()
        with _pointed_at(db):
            assert orch._gather_candidates(session, ['evaluating']) == []

    def test_vote_approved_rank_first_then_the_nodes_own_newest_first(self, db):
        """RED on HEAD: the two own rows were rejected by the vote gate."""
        now = datetime.utcnow()
        nunba = _nunba(db)
        old_own = _experiment(db, nunba.id, status='evaluating',
                              created_at=now - timedelta(days=3))
        new_own = _experiment(db, nunba.id, status='evaluating',
                              created_at=now - timedelta(days=1))
        approved = _experiment(db, _user(db).id, status='evaluating',
                               created_at=now - timedelta(days=9))
        _approve(db, approved.id)
        orch, session = self._orch()
        with _pointed_at(db), patch.object(
                type(orch), '_active_learning_multiplier', return_value=1.0):
            ranked = orch._rank_by_votes(
                session, orch._gather_candidates(session, ['evaluating']), 0.3)
        assert [e['id'] for e in ranked] == [approved.id, new_own.id, old_own.id]
        assert [e['_approved_by_vote'] for e in ranked] == [True, False, False]
        assert [e['_this_nodes_own'] for e in ranked] == [False, True, True]

    def test_the_nodes_own_row_with_a_vote_against_is_not_ranked(self, db):
        e = _experiment(db, _nunba(db).id, status='evaluating')
        _vote(db, e.id, -1)
        orch, session = self._orch()
        with _pointed_at(db), patch.object(
                type(orch), '_active_learning_multiplier', return_value=1.0):
            assert orch._rank_by_votes(
                session, orch._gather_candidates(session, ['evaluating']),
                0.3) == []

    def test_a_full_cycle_dispatches_the_nodes_own_experiment(self, db):
        """RED on HEAD: the cycle ended 'No eligible experiments found'."""
        e = _experiment(db, _nunba(db).id, status='evaluating')
        db.commit()
        orch, session = self._orch()
        with _pointed_at(db), patch.object(
                type(orch), '_active_learning_multiplier', return_value=1.0), \
                patch.object(type(orch), '_emit_event'):
            orch._execute_cycle(session, 5, 0.3, ['voting', 'evaluating'],
                                'system')
        assert session.dispatched == 1, session.errors
        assert session.status == 'running'
        item = session.experiments[0]
        assert item['id'] == e.id
        assert item['this_nodes_own'] is True
        assert item['approved_by_vote'] is False
        goal = db.query(AgentGoal).filter(AgentGoal.id == item['goal_id']).one()
        assert (goal.config_json or {}).get('experiment_id') == e.id

    def test_a_full_cycle_on_central_dispatches_nothing_unvoted(
            self, db, monkeypatch):
        """What central did from 11-14 18:33Z on e7b769936: five goals on
        experiments with 0 votes (hartos-ee, read-only, 11-15)."""
        monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
        _experiment(db, _nunba(db).id, status='evaluating')
        db.commit()
        orch, session = self._orch()
        with _pointed_at(db), patch.object(type(orch), '_emit_event'):
            orch._execute_cycle(session, 5, 0.3, ['voting', 'evaluating'],
                                'system')
        assert session.dispatched == 0
        assert db.query(AgentGoal).count() == 0


# ── the one rule ────────────────────────────────────────────────────────

def _tally(**over):
    """A tally as tally_votes returns it, unvoted, default context."""
    t = {'decision_context': 'technical_improvement', 'quorum_met': False,
         'total_for': 0.0, 'total_against': 0.0, 'steward_vote': None}
    t.update(over)
    return t


_APPROVED = dict(quorum_met=True, total_for=3.0)


@pytest.mark.parametrize('tally, own, tier, allowed, reason', [
    (_tally(**_APPROVED), False, 'central', True, 'approved'),
    (_tally(**_APPROVED), False, 'flat', True, 'approved'),
    (_tally(), True, 'flat', True, 'this_nodes_own'),
    (_tally(total_for=1.0), True, 'flat', True, 'this_nodes_own'),
    (_tally(), False, 'flat', False, 'not_this_nodes_own'),
    (_tally(), True, 'regional', False, 'topology_keeps_the_vote'),
    (_tally(), True, 'central', False, 'topology_keeps_the_vote'),
    (_tally(), True, 'nonsense', False, 'topology_keeps_the_vote'),
    (_tally(decision_context='security_guardrail'), True, 'flat', False,
     'steward_required'),
    (_tally(total_against=0.6), True, 'flat', False, 'vote_against'),
])
def test_evaluation_permit(tally, own, tier, allowed, reason):
    from integrations.social.voting_rules import evaluation_permit
    permit = evaluation_permit(tally, own, node_tier=tier)
    assert permit['allowed'] is allowed
    assert permit['reason'] == reason
    assert permit['approved_by_vote'] is (reason == 'approved')
    assert permit['hint']


def test_node_holds_reads_only_what_a_request_cannot_set():
    from integrations.social.voting_rules import node_holds

    class Row:
        def __init__(self, **kw):
            self.id = kw.pop('id', 'x')
            self.owner_id = kw.pop('owner_id', None)
            self.password_hash = kw.pop('password_hash', None)
            self.is_verified = kw.pop('is_verified', True)
            self.settings = None
            self.user_type = kw.pop('user_type')

    owner = Row(id='own-1', user_type='human')
    rows = {'own-1': owner}
    with patch.dict(os.environ, {'HEVOLVE_OWNER_USER_ID': 'own-1'}):
        assert node_holds(owner, rows.get) is True
        assert node_holds(Row(user_type='human'), rows.get) is False
        assert node_holds(Row(user_type='system'), rows.get) is True
        assert node_holds(Row(user_type='system', password_hash='h'), rows.get) is False
        assert node_holds(Row(user_type='system', owner_id='own-1'), rows.get) is False
        assert node_holds(Row(user_type='system', is_verified=False), rows.get) is False
        assert node_holds(Row(user_type='agent'), rows.get) is False
        assert node_holds(Row(user_type='agent', owner_id='own-1'), rows.get) is False
        assert node_holds(None, rows.get) is False
