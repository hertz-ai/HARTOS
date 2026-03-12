"""
Distributed Coding Agent Test Suite
=====================================
Thin layer on top of existing CREATE/REUSE pipeline.
Tests covering:
- CodingGoal model CRUD
- Migration v14 (tables still exist in schema)
- IdleDetectionService: opt-in/opt-out, idle detection, stats
- CodingGoalManager: create, get, update, list, input sanitization, build_prompt
- dispatch_to_chat: /chat integration
- CodingAgentDaemon: start/stop lifecycle
- API endpoints (auth, central-only, allowlist, opt-in ownership)

All external calls mocked -- in-memory SQLite.
"""
import os
import sys
import uuid
import pytest
import requests as req_module
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from integrations.social.models import Base, User, CodingGoal, AgentGoal


# =====================================================================
# FIXTURES
# =====================================================================

@pytest.fixture(scope='session')
def engine():
    return create_engine('sqlite://', echo=False,
                         connect_args={"check_same_thread": False})

@pytest.fixture(scope='session')
def tables(engine):
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)

@pytest.fixture
def db(engine, tables):
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.rollback()
    session.close()

@pytest.fixture
def sample_user(db):
    user = User(username=f'agent_{uuid.uuid4().hex[:8]}', display_name='Agent',
                user_type='agent', idle_compute_opt_in=True)
    db.add(user)
    db.flush()
    return user

@pytest.fixture
def admin_user(db):
    user = User(username=f'admin_{uuid.uuid4().hex[:8]}', display_name='Admin',
                user_type='human', is_admin=True,
                api_token=f'tok_{uuid.uuid4().hex[:8]}')
    db.add(user)
    db.flush()
    return user

@pytest.fixture
def regular_user(db):
    user = User(username=f'reg_{uuid.uuid4().hex[:8]}', display_name='Regular',
                user_type='human', is_admin=False,
                api_token=f'tok_{uuid.uuid4().hex[:8]}')
    db.add(user)
    db.flush()
    return user

@pytest.fixture
def sample_goal(db):
    """Create a coding goal via the unified AgentGoal table."""
    goal = AgentGoal(
        goal_type='coding',
        title='Activate HiveMind',
        description='Enable distributed thinking',
        config_json={
            'repo_url': 'hertz-ai/Hevolve-Continual-Learner-Framework-Zero-Forgetting',
            'repo_branch': 'main-withpycharmplugin-and-slowness',
            'target_path': 'src/hevolveai/embodied_ai',
        },
        status='active', created_by='admin',
    )
    db.add(goal)
    db.flush()
    return goal


# =====================================================================
# TEST: Models
# =====================================================================

class TestCodingModels:

    def test_goal_create(self, db):
        goal = CodingGoal(title='Test', description='d', repo_url='t/r', repo_branch='main')
        db.add(goal)
        db.flush()
        assert goal.id is not None
        assert goal.status == 'active'

    def test_goal_to_dict(self, db, sample_goal):
        d = sample_goal.to_dict()
        assert d['title'] == 'Activate HiveMind'
        assert d['repo_url'] == 'hertz-ai/Hevolve-Continual-Learner-Framework-Zero-Forgetting'
        assert 'id' in d and 'created_at' in d

    def test_user_idle_compute_column(self, db):
        user = User(username=f'u_{uuid.uuid4().hex[:6]}', display_name='T', user_type='agent')
        db.add(user)
        db.flush()
        assert user.idle_compute_opt_in is False
        user.idle_compute_opt_in = True
        db.flush()
        assert user.idle_compute_opt_in is True

    def test_user_to_dict_includes_idle_opt_in(self, db, sample_user):
        d = sample_user.to_dict()
        assert 'idle_compute_opt_in' in d
        assert d['idle_compute_opt_in'] is True


# =====================================================================
# TEST: Migration v14
# =====================================================================

class TestMigrationV14:

    def test_schema_version(self):
        from integrations.social.migrations import SCHEMA_VERSION
        assert SCHEMA_VERSION >= 14

    def test_coding_goals_table_exists(self, engine, tables):
        with engine.connect() as conn:
            r = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='coding_goals'"))
            assert r.fetchone() is not None

    def test_coding_tasks_table_exists(self, engine, tables):
        """Table still exists in schema even though services don't use it."""
        with engine.connect() as conn:
            r = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='coding_tasks'"))
            assert r.fetchone() is not None

    def test_coding_submissions_table_exists(self, engine, tables):
        """Table still exists in schema even though services don't use it."""
        with engine.connect() as conn:
            r = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='coding_submissions'"))
            assert r.fetchone() is not None


# =====================================================================
# TEST: Idle Detection
# =====================================================================

class TestIdleDetection:

    def test_opt_in(self, db):
        from integrations.coding_agent.idle_detection import IdleDetectionService
        user = User(username=f'oi_{uuid.uuid4().hex[:6]}', display_name='OI',
                    user_type='agent', idle_compute_opt_in=False)
        db.add(user)
        db.flush()
        result = IdleDetectionService.opt_in(db, user.id)
        assert result['success'] is True
        assert result['idle_compute_opt_in'] is True

    def test_opt_out(self, db, sample_user):
        from integrations.coding_agent.idle_detection import IdleDetectionService
        result = IdleDetectionService.opt_out(db, sample_user.id)
        assert result['success'] is True
        assert result['idle_compute_opt_in'] is False

    def test_opt_in_not_found(self, db):
        from integrations.coding_agent.idle_detection import IdleDetectionService
        assert IdleDetectionService.opt_in(db, 'nope')['success'] is False

    def test_opt_out_not_found(self, db):
        from integrations.coding_agent.idle_detection import IdleDetectionService
        assert IdleDetectionService.opt_out(db, 'nope')['success'] is False

    @patch('integrations.coding_agent.idle_detection.IdleDetectionService.is_agent_idle',
           return_value=True)
    def test_get_idle_opted_in(self, mock_idle, db):
        from integrations.coding_agent.idle_detection import IdleDetectionService
        user = User(username=f'ia_{uuid.uuid4().hex[:6]}', display_name='IA',
                    user_type='agent', idle_compute_opt_in=True)
        db.add(user)
        db.flush()
        idle = IdleDetectionService.get_idle_opted_in_agents(db)
        assert any(a['user_id'] == user.id for a in idle)

    def test_get_idle_stats(self, db, sample_user):
        from integrations.coding_agent.idle_detection import IdleDetectionService
        sample_user.idle_compute_opt_in = True
        db.flush()
        stats = IdleDetectionService.get_idle_stats(db)
        assert 'total_opted_in' in stats
        assert stats['total_opted_in'] >= 1


# =====================================================================
# TEST: Goal Manager
# =====================================================================

class TestGoalManager:

    def test_create_goal(self, db):
        from integrations.coding_agent.goal_manager import CodingGoalManager
        result = CodingGoalManager.create_goal(db, title='Test', description='d',
                                                repo_url='hertz-ai/test', branch='main')
        assert result['title'] == 'Test'
        assert result['status'] == 'active'

    def test_create_goal_invalid_repo(self, db):
        from integrations.coding_agent.goal_manager import CodingGoalManager
        with pytest.raises(ValueError, match='Invalid repo_url'):
            CodingGoalManager.create_goal(db, title='X', description='d',
                                           repo_url='../../etc/passwd', branch='main')

    def test_create_goal_invalid_branch(self, db):
        from integrations.coding_agent.goal_manager import CodingGoalManager
        with pytest.raises(ValueError, match='Invalid branch'):
            CodingGoalManager.create_goal(db, title='X', description='d',
                                           repo_url='a/b', branch='main; rm -rf /')

    def test_get_goal(self, db, sample_goal):
        from integrations.coding_agent.goal_manager import CodingGoalManager
        result = CodingGoalManager.get_goal(db, sample_goal.id)
        assert result['success'] is True
        assert result['goal']['title'] == 'Activate HiveMind'

    def test_get_goal_not_found(self, db):
        from integrations.coding_agent.goal_manager import CodingGoalManager
        assert CodingGoalManager.get_goal(db, 'nope')['success'] is False

    def test_update_status(self, db):
        from integrations.coding_agent.goal_manager import CodingGoalManager
        goal = AgentGoal(goal_type='coding', title='S', description='d',
                         config_json={'repo_url': 'a/b'}, status='active')
        db.add(goal)
        db.flush()
        result = CodingGoalManager.update_goal_status(db, goal.id, 'paused')
        assert result['goal']['status'] == 'paused'

    def test_update_status_not_found(self, db):
        from integrations.coding_agent.goal_manager import CodingGoalManager
        assert CodingGoalManager.update_goal_status(db, 'nope', 'x')['success'] is False

    def test_list_goals(self, db, sample_goal):
        from integrations.coding_agent.goal_manager import CodingGoalManager
        assert len(CodingGoalManager.list_goals(db)) >= 1

    def test_list_goals_filtered(self, db, sample_goal):
        from integrations.coding_agent.goal_manager import CodingGoalManager
        assert all(g['status'] == 'active'
                   for g in CodingGoalManager.list_goals(db, status='active'))

    def test_build_prompt(self):
        from integrations.coding_agent.goal_manager import CodingGoalManager
        prompt = CodingGoalManager.build_prompt({
            'repo_url': 'hertz-ai/repo', 'repo_branch': 'main',
            'target_path': 'src/', 'title': 'Fix bugs', 'description': 'Fix all bugs',
        })
        assert 'hertz-ai/repo' in prompt
        assert 'Fix bugs' in prompt
        assert 'Clone the repo' in prompt


# =====================================================================
# TEST: dispatch_to_chat
# =====================================================================

class TestDispatch:

    @patch('integrations.coding_agent.task_distributor.requests.post')
    def test_dispatch_success(self, mock_post):
        from integrations.coding_agent.task_distributor import dispatch_to_chat
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'response': 'Code generated'}
        mock_post.return_value = mock_resp

        result = dispatch_to_chat('Fix bugs', 'user1', 'goal123456')
        assert result == 'Code generated'
        mock_post.assert_called_once()
        call_json = mock_post.call_args[1]['json']
        assert call_json['create_agent'] is True
        assert call_json['prompt'] == 'Fix bugs'
        assert call_json['prompt_id'] == 'coding_goal1234'

    @patch('integrations.coding_agent.task_distributor.requests.post',
           side_effect=req_module.RequestException('fail'))
    def test_dispatch_failure(self, mock_post):
        from integrations.coding_agent.task_distributor import dispatch_to_chat
        assert dispatch_to_chat('x', 'u', 'g1234567') is None

    @patch('integrations.coding_agent.task_distributor.requests.post')
    def test_dispatch_non_200(self, mock_post):
        from integrations.coding_agent.task_distributor import dispatch_to_chat
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_post.return_value = mock_resp
        assert dispatch_to_chat('x', 'u', 'g1234567') is None


# =====================================================================
# TEST: Daemon
# =====================================================================

class TestCodingDaemon:

    def test_start_stop(self):
        from integrations.coding_agent.coding_daemon import CodingAgentDaemon
        daemon = CodingAgentDaemon()
        daemon._interval = 1
        daemon.start()
        assert daemon._running is True
        assert daemon._thread.is_alive()
        daemon.stop()
        assert daemon._running is False

    def test_double_start(self):
        from integrations.coding_agent.coding_daemon import CodingAgentDaemon
        daemon = CodingAgentDaemon()
        daemon._interval = 1
        daemon.start()
        daemon.start()
        assert daemon._running is True
        daemon.stop()

    def test_singleton(self):
        from integrations.coding_agent.coding_daemon import coding_daemon, CodingAgentDaemon
        assert isinstance(coding_daemon, CodingAgentDaemon)


# =====================================================================
# TEST: SyncEngine operation types (still supported)
# =====================================================================

class TestSyncEngineOperations:

    def test_receive_coding_task_assign(self):
        from integrations.social.sync_engine import SyncEngine
        result = SyncEngine.receive_sync_batch(None, [
            {'id': 'i1', 'operation_type': 'coding_task_assign', 'payload': {}},
        ])
        assert 'i1' in result['processed']

    def test_receive_coding_submission(self):
        from integrations.social.sync_engine import SyncEngine
        result = SyncEngine.receive_sync_batch(None, [
            {'id': 'i2', 'operation_type': 'coding_submission', 'payload': {}},
        ])
        assert 'i2' in result['processed']


# =====================================================================
# TEST: API Endpoints (Auth-Protected)
# =====================================================================

def _mock_auth(user, db):
    def mock_fn(token):
        return user, db
    return mock_fn


class TestCodingEndpoints:

    @pytest.fixture
    def app(self):
        from flask import Flask
        app = Flask(__name__)
        app.config['TESTING'] = True
        from integrations.coding_agent.api import coding_agent_bp
        app.register_blueprint(coding_agent_bp)
        return app

    @pytest.fixture
    def client(self, app):
        return app.test_client()

    def _headers(self):
        return {'Authorization': 'Bearer test'}

    # Auth enforcement
    def test_unauth_rejected(self, client):
        assert client.get('/api/coding/goals').status_code == 401

    def test_unauth_post_rejected(self, client):
        assert client.post('/api/coding/opt-in', json={'user_id': 'x'}).status_code == 401

    # Central-only
    @patch('integrations.social.auth._get_user_from_token')
    def test_non_central_rejected(self, mock_fn, client, db, admin_user):
        mock_fn.side_effect = _mock_auth(admin_user, db)
        import integrations.coding_agent.api as m
        orig = m._IS_CENTRAL
        m._IS_CENTRAL = False
        try:
            resp = client.post('/api/coding/goals', json={'title': 'T', 'repo_url': 'a/b'},
                              headers=self._headers())
            assert resp.status_code == 403
            assert 'Central' in resp.get_json()['error']
        finally:
            m._IS_CENTRAL = orig

    # Admin-only
    @patch('integrations.social.auth._get_user_from_token')
    def test_non_admin_rejected(self, mock_fn, client, db, regular_user):
        mock_fn.side_effect = _mock_auth(regular_user, db)
        import integrations.coding_agent.api as m
        orig = m._IS_CENTRAL
        m._IS_CENTRAL = True
        try:
            resp = client.post('/api/coding/goals', json={'title': 'T', 'repo_url': 'a/b'},
                              headers=self._headers())
            assert resp.status_code == 403
            assert 'Admin' in resp.get_json()['error']
        finally:
            m._IS_CENTRAL = orig

    # Repo allowlist
    @patch('integrations.social.auth._get_user_from_token')
    def test_allowlist_blocks(self, mock_fn, client, db, admin_user):
        mock_fn.side_effect = _mock_auth(admin_user, db)
        import integrations.coding_agent.api as m
        orig_c, orig_r = m._IS_CENTRAL, m.ALLOWED_REPOS
        m._IS_CENTRAL, m.ALLOWED_REPOS = True, ['ok/repo']
        try:
            resp = client.post('/api/coding/goals', json={'title': 'T', 'repo_url': 'bad/repo'},
                              headers=self._headers())
            assert resp.status_code == 400
            assert 'allowlist' in resp.get_json()['error']
        finally:
            m._IS_CENTRAL, m.ALLOWED_REPOS = orig_c, orig_r

    @patch('integrations.social.auth._get_user_from_token')
    def test_allowlist_allows(self, mock_fn, client, db, admin_user):
        mock_fn.side_effect = _mock_auth(admin_user, db)
        import integrations.coding_agent.api as m
        orig_c, orig_r = m._IS_CENTRAL, m.ALLOWED_REPOS
        m._IS_CENTRAL, m.ALLOWED_REPOS = True, ['ok/repo']
        try:
            resp = client.post('/api/coding/goals', json={
                'title': 'T', 'description': 'd', 'repo_url': 'ok/repo'},
                headers=self._headers())
            assert resp.status_code == 200
            assert resp.get_json()['success'] is True
        finally:
            m._IS_CENTRAL, m.ALLOWED_REPOS = orig_c, orig_r

    # Repo validation
    @patch('integrations.social.auth._get_user_from_token')
    def test_invalid_repo_rejected(self, mock_fn, client, db, admin_user):
        mock_fn.side_effect = _mock_auth(admin_user, db)
        import integrations.coding_agent.api as m
        orig = m._IS_CENTRAL
        m._IS_CENTRAL = True
        try:
            assert client.post('/api/coding/goals', json={'title': 'T', 'repo_url': '../../etc'},
                              headers=self._headers()).status_code == 400
        finally:
            m._IS_CENTRAL = orig

    # Opt-in ownership
    @patch('integrations.social.auth._get_user_from_token')
    def test_opt_in_self(self, mock_fn, client, db, regular_user):
        mock_fn.side_effect = _mock_auth(regular_user, db)
        resp = client.post('/api/coding/opt-in', json={'user_id': str(regular_user.id)},
                          headers=self._headers())
        assert resp.status_code == 200

    @patch('integrations.social.auth._get_user_from_token')
    def test_opt_in_other_rejected(self, mock_fn, client, db, regular_user):
        mock_fn.side_effect = _mock_auth(regular_user, db)
        resp = client.post('/api/coding/opt-in', json={'user_id': 'other'},
                          headers=self._headers())
        assert resp.status_code == 403
        assert 'yourself' in resp.get_json()['error']

    @patch('integrations.social.auth._get_user_from_token')
    def test_opt_out_other_rejected(self, mock_fn, client, db, regular_user):
        mock_fn.side_effect = _mock_auth(regular_user, db)
        assert client.post('/api/coding/opt-out', json={'user_id': 'other'},
                          headers=self._headers()).status_code == 403

    @patch('integrations.social.auth._get_user_from_token')
    def test_admin_opt_in_other(self, mock_fn, client, db, admin_user, sample_user):
        mock_fn.side_effect = _mock_auth(admin_user, db)
        sample_user.idle_compute_opt_in = False
        db.flush()
        resp = client.post('/api/coding/opt-in', json={'user_id': str(sample_user.id)},
                          headers=self._headers())
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True

    # Read endpoints
    @patch('integrations.social.auth._get_user_from_token')
    def test_list_goals(self, mock_fn, client, db, regular_user, sample_goal):
        mock_fn.side_effect = _mock_auth(regular_user, db)
        resp = client.get('/api/coding/goals', headers=self._headers())
        assert resp.status_code == 200
        assert len(resp.get_json()['goals']) >= 1

    @patch('integrations.social.auth._get_user_from_token')
    def test_idle_stats(self, mock_fn, client, db, admin_user):
        mock_fn.side_effect = _mock_auth(admin_user, db)
        resp = client.get('/api/coding/idle-stats', headers=self._headers())
        assert resp.status_code == 200
        assert 'total_opted_in' in resp.get_json()

    # Admin create end-to-end
    @patch('integrations.social.auth._get_user_from_token')
    def test_admin_create_goal(self, mock_fn, client, db, admin_user):
        mock_fn.side_effect = _mock_auth(admin_user, db)
        expected_id = str(admin_user.id)
        import integrations.coding_agent.api as m
        orig_c, orig_r = m._IS_CENTRAL, m.ALLOWED_REPOS
        m._IS_CENTRAL, m.ALLOWED_REPOS = True, []
        try:
            resp = client.post('/api/coding/goals', json={
                'title': 'Admin Goal', 'description': 'd', 'repo_url': 'hertz-ai/repo'},
                headers=self._headers())
            assert resp.status_code == 200
            data = resp.get_json()
            assert data['goal']['title'] == 'Admin Goal'
            assert data['goal']['created_by'] == expected_id
        finally:
            m._IS_CENTRAL, m.ALLOWED_REPOS = orig_c, orig_r


# =====================================================================
# TEST: Repo Validation
# =====================================================================

class TestRepoValidation:

    def test_valid(self):
        from integrations.coding_agent.api import _validate_repo
        assert _validate_repo('owner/repo') is None
        assert _validate_repo('hertz-ai/My_Repo.v2') is None

    def test_empty(self):
        from integrations.coding_agent.api import _validate_repo
        assert _validate_repo('') is not None

    def test_no_slash(self):
        from integrations.coding_agent.api import _validate_repo
        assert _validate_repo('justrepo') is not None

    def test_too_many_slashes(self):
        from integrations.coding_agent.api import _validate_repo
        assert _validate_repo('a/b/c') is not None

    def test_shell_injection(self):
        from integrations.coding_agent.api import _validate_repo
        assert _validate_repo('a/b;rm -rf /') is not None

    def test_path_traversal(self):
        from integrations.coding_agent.api import _validate_repo
        assert _validate_repo('../etc/passwd') is not None

    def test_special_chars(self):
        from integrations.coding_agent.api import _validate_repo
        assert _validate_repo('a/b$(whoami)') is not None

    def test_allowlist(self):
        from integrations.coding_agent.api import _validate_repo
        import integrations.coding_agent.api as m
        orig = m.ALLOWED_REPOS
        m.ALLOWED_REPOS = ['ok/repo']
        try:
            assert _validate_repo('ok/repo') is None
            assert _validate_repo('bad/repo') is not None
        finally:
            m.ALLOWED_REPOS = orig


# =====================================================================
# TEST: Package Init
# =====================================================================

class TestPackageInit:

    def test_disabled_by_default(self):
        from flask import Flask
        app = Flask(__name__)
        with patch.dict(os.environ, {'HEVOLVE_CODING_AGENT_ENABLED': 'false'}):
            from integrations.coding_agent import init_coding_agent
            init_coding_agent(app)
            assert 'coding_agent' not in [bp.name for bp in app.blueprints.values()]
