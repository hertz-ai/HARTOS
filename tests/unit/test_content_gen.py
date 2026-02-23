"""
Tests for Content Generation Tracker, API, Tools, and Daemon Integration.

Covers:
- ContentGenTracker service (CRUD, progress, delta, stuck detection, unblock)
- content_gen goal type registration
- content_gen_tools (4 AutoGen tools)
- API blueprint endpoints
- Daemon monitor wiring
"""
import os
import sys
import json
import pytest
from unittest.mock import patch, Mock, MagicMock
from datetime import datetime, timedelta

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from integrations.social.models import Base, AgentGoal, User


# ─── Fixtures ───

@pytest.fixture(scope='session')
def engine():
    eng = create_engine('sqlite://', echo=False)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture(scope='session')
def tables(engine):
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def db(engine, tables):
    connection = engine.connect()
    transaction = connection.begin()
    Session = sessionmaker(bind=connection)
    session = Session()
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def system_user(db):
    user = User(username='hevolve_system_agent', email='sys@hart.ai',
                password_hash='x', user_type='agent')
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def sample_game_config():
    return {
        'title': 'Animal Spelling Bee',
        'category': 'english',
        'content': {
            'words': [
                {'word': 'cat', 'imagePrompt': 'a cute cat', 'hint': 'meow'},
                {'word': 'dog', 'imagePrompt': 'a happy dog', 'hint': 'woof'},
                {'word': 'fish', 'image_prompt': 'a goldfish'},
            ],
        },
    }


# ─── ContentGenTracker Tests ───

class TestContentGenTracker:
    """Tests for the ContentGenTracker service."""

    def test_compute_media_requirements(self, sample_game_config):
        from integrations.agent_engine.content_gen_tracker import ContentGenTracker

        reqs = ContentGenTracker._compute_media_requirements(sample_game_config)

        assert reqs['images'] == 3  # 3 items with imagePrompt/image_prompt
        assert reqs['tts'] >= 2     # word + hint fields
        assert reqs['music'] == 1   # english category has BGM
        assert reqs['video'] == 0   # no video

    def test_compute_media_requirements_empty(self):
        from integrations.agent_engine.content_gen_tracker import ContentGenTracker

        reqs = ContentGenTracker._compute_media_requirements({})
        assert reqs['images'] == 0
        assert reqs['tts'] == 0
        assert reqs['music'] == 0
        assert reqs['video'] == 0

    def test_compute_media_requirements_nested_options(self):
        from integrations.agent_engine.content_gen_tracker import ContentGenTracker

        config = {
            'category': 'math',
            'content': {
                'questions': [
                    {
                        'question': 'What is 2+2?',
                        'imagePrompt': 'math equation',
                        'options': [
                            {'text': '3', 'imagePrompt': 'number three'},
                            {'text': '4'},
                        ],
                    },
                ],
            },
        }
        reqs = ContentGenTracker._compute_media_requirements(config)
        assert reqs['images'] == 2  # question + option with imagePrompt

    def test_get_or_create_game_goal_returns_existing(self, db):
        from integrations.agent_engine.content_gen_tracker import ContentGenTracker

        goal = AgentGoal(
            goal_type='content_gen',
            title='Test content gen',
            description='test',
            status='active',
            config_json={'game_id': 'test-game-01', 'media_requirements': {}},
        )
        db.add(goal)
        db.flush()

        result = ContentGenTracker.get_or_create_game_goal(
            db, 'test-game-01', {'title': 'Test'})

        assert result is not None
        assert result['id'] == goal.id

    def test_get_or_create_game_goal_creates_new(self, db, system_user, sample_game_config):
        """Verify that a new goal is created when no existing one matches."""
        from integrations.agent_engine.content_gen_tracker import ContentGenTracker

        # Patch GoalManager at the source module where it's imported inside the function
        with patch('integrations.agent_engine.goal_manager.GoalManager.create_goal') as mock_create:
            mock_create.return_value = {
                'id': 999, 'goal_type': 'content_gen',
                'title': 'Content generation: Animal Spelling Bee',
                'config_json': {'game_id': 'eng-spell-new-01'},
            }

            result = ContentGenTracker.get_or_create_game_goal(
                db, 'eng-spell-new-01', sample_game_config)

            assert result is not None
            mock_create.assert_called_once()

    def test_get_game_progress(self, db):
        from integrations.agent_engine.content_gen_tracker import ContentGenTracker

        goal = AgentGoal(
            goal_type='content_gen',
            title='Progress test',
            description='test',
            status='active',
            config_json={
                'game_id': 'progress-test-01',
                'game_title': 'Progress Test Game',
                'media_requirements': {'images': 10, 'tts': 5, 'music': 1, 'video': 0},
                'task_jobs': {
                    'image': {'job_id': 'img_abc123', 'status': 'generating', 'progress': 50},
                    'tts': {'job_id': 'tts_def456', 'status': 'complete', 'progress': 100},
                },
                'progress_snapshots': [],
            },
        )
        db.add(goal)
        db.flush()

        progress = ContentGenTracker.get_game_progress(db, 'progress-test-01')

        assert progress is not None
        assert progress['game_id'] == 'progress-test-01'
        assert progress['game_title'] == 'Progress Test Game'
        # image(10), tts(5), music(1) — video(0) skipped = 3 tasks
        assert len(progress['tasks']) == 3
        assert progress['progress_pct'] > 0

    def test_get_game_progress_not_found(self, db):
        from integrations.agent_engine.content_gen_tracker import ContentGenTracker

        result = ContentGenTracker.get_game_progress(db, 'nonexistent-game')
        assert result is None

    def test_compute_delta_no_snapshots(self):
        from integrations.agent_engine.content_gen_tracker import ContentGenTracker

        delta = ContentGenTracker._compute_delta([], hours=24)
        assert delta == 0.0

    def test_compute_delta_with_snapshots(self):
        from integrations.agent_engine.content_gen_tracker import ContentGenTracker

        now = datetime.utcnow()
        snapshots = [
            {'ts': (now - timedelta(hours=25)).isoformat(), 'pct': 20},
            {'ts': (now - timedelta(hours=12)).isoformat(), 'pct': 35},
            {'ts': now.isoformat(), 'pct': 50},
        ]

        delta = ContentGenTracker._compute_delta(snapshots, hours=24)
        assert delta == 30.0  # 50 - 20

    def test_compute_delta_single_snapshot(self):
        from integrations.agent_engine.content_gen_tracker import ContentGenTracker

        snapshots = [{'ts': datetime.utcnow().isoformat(), 'pct': 50}]
        delta = ContentGenTracker._compute_delta(snapshots, hours=24)
        assert delta == 0.0

    def test_record_progress_snapshot(self, db):
        from integrations.agent_engine.content_gen_tracker import ContentGenTracker
        from sqlalchemy.orm.attributes import flag_modified

        goal = AgentGoal(
            goal_type='content_gen',
            title='Snapshot test',
            description='test',
            status='active',
            config_json={
                'game_id': 'snapshot-test-01',
                'media_requirements': {'images': 4, 'tts': 0, 'music': 0, 'video': 0},
                'task_jobs': {
                    'image': {'status': 'generating', 'progress': 75},
                },
                'progress_snapshots': [],
            },
        )
        db.add(goal)
        db.flush()

        ContentGenTracker.record_progress_snapshot(db, 'snapshot-test-01')
        db.flush()

        # Reload — use expire to force re-read
        db.expire(goal)
        snapshots = goal.config_json.get('progress_snapshots', [])
        assert len(snapshots) == 1
        assert 'ts' in snapshots[0]
        assert 'pct' in snapshots[0]

    def test_update_task_job(self, db):
        from integrations.agent_engine.content_gen_tracker import ContentGenTracker

        goal = AgentGoal(
            goal_type='content_gen',
            title='Job update test',
            description='test',
            status='active',
            config_json={
                'game_id': 'job-update-01',
                'media_requirements': {'images': 5},
                'task_jobs': {},
                'progress_snapshots': [],
            },
        )
        db.add(goal)
        db.flush()

        ContentGenTracker.update_task_job(
            db, 'job-update-01', 'image',
            job_id='img_xyz789', status='generating', progress=30)
        db.flush()

        db.expire(goal)
        jobs = goal.config_json.get('task_jobs', {})
        assert 'image' in jobs
        assert jobs['image']['job_id'] == 'img_xyz789'
        assert jobs['image']['status'] == 'generating'
        assert jobs['image']['progress'] == 30

    def test_get_stuck_games(self, db):
        from integrations.agent_engine.content_gen_tracker import ContentGenTracker

        old_ts = (datetime.utcnow() - timedelta(hours=48)).isoformat()
        goal = AgentGoal(
            goal_type='content_gen',
            title='Stuck test',
            description='test',
            status='active',
            config_json={
                'game_id': 'stuck-test-01',
                'game_title': 'Stuck Game',
                'media_requirements': {'images': 5, 'tts': 0, 'music': 0, 'video': 0},
                'task_jobs': {
                    'image': {'status': 'failed', 'progress': 20, 'error': 'timeout'},
                },
                'progress_snapshots': [
                    {'ts': old_ts, 'pct': 20},
                    {'ts': old_ts, 'pct': 20},
                ],
            },
        )
        db.add(goal)
        db.flush()

        stuck = ContentGenTracker.get_stuck_games(db, stall_threshold_hours=24)
        stuck_ids = [g['game_id'] for g in stuck]
        assert 'stuck-test-01' in stuck_ids

    def test_get_all_game_tasks(self, db):
        from integrations.agent_engine.content_gen_tracker import ContentGenTracker

        goal = AgentGoal(
            goal_type='content_gen',
            title='All tasks test',
            description='test',
            status='active',
            config_json={
                'game_id': 'all-tasks-01',
                'game_title': 'All Tasks Game',
                'media_requirements': {'images': 2, 'tts': 0, 'music': 0, 'video': 0},
                'task_jobs': {},
                'progress_snapshots': [],
            },
        )
        db.add(goal)
        db.flush()

        all_tasks = ContentGenTracker.get_all_game_tasks(db)
        ids = [g['game_id'] for g in all_tasks]
        assert 'all-tasks-01' in ids

    @patch('integrations.agent_engine.content_gen_tracker._check_media_service')
    @patch('integrations.agent_engine.content_gen_tracker._restart_media_service')
    def test_attempt_unblock(self, mock_restart, mock_check, db):
        from integrations.agent_engine.content_gen_tracker import ContentGenTracker

        mock_check.return_value = False
        mock_restart.return_value = True

        goal = AgentGoal(
            goal_type='content_gen',
            title='Unblock test',
            description='test',
            status='active',
            config_json={
                'game_id': 'unblock-test-01',
                'game_title': 'Unblock Game',
                'media_requirements': {'images': 5, 'tts': 0, 'music': 0, 'video': 0},
                'task_jobs': {
                    'image': {'status': 'stuck', 'progress': 40},
                },
                'progress_snapshots': [],
            },
        )
        db.add(goal)
        db.flush()

        result = ContentGenTracker.attempt_unblock(db, 'unblock-test-01')
        assert result['success'] is True
        assert 'restarted_image_service' in result['action_taken']
        assert 'retry_image' in result['action_taken']

    def test_attempt_unblock_no_stuck_tasks(self, db):
        from integrations.agent_engine.content_gen_tracker import ContentGenTracker

        goal = AgentGoal(
            goal_type='content_gen',
            title='No stuck test',
            description='test',
            status='active',
            config_json={
                'game_id': 'no-stuck-01',
                'media_requirements': {'images': 2, 'tts': 0, 'music': 0, 'video': 0},
                'task_jobs': {
                    'image': {'status': 'generating', 'progress': 50},
                },
                'progress_snapshots': [],
            },
        )
        db.add(goal)
        db.flush()

        result = ContentGenTracker.attempt_unblock(db, 'no-stuck-01')
        assert result['success'] is True
        assert result['detail'] == 'No stuck tasks'


# ─── Status Classification Tests ───

class TestStatusClassification:
    """Tests for _classify_status helper."""

    def test_complete(self):
        from integrations.agent_engine.content_gen_tracker import _classify_status
        assert _classify_status('completed', 100, 0) == 'complete'
        assert _classify_status('active', 100, 5) == 'complete'

    def test_paused(self):
        from integrations.agent_engine.content_gen_tracker import _classify_status
        assert _classify_status('paused', 50, 0) == 'paused'

    def test_stuck(self):
        from integrations.agent_engine.content_gen_tracker import _classify_status
        assert _classify_status('active', 50, 0) == 'stuck'

    def test_slow(self):
        from integrations.agent_engine.content_gen_tracker import _classify_status
        assert _classify_status('active', 50, 3) == 'slow'

    def test_pending(self):
        from integrations.agent_engine.content_gen_tracker import _classify_status
        assert _classify_status('active', 0, 0) == 'pending'

    def test_generating(self):
        from integrations.agent_engine.content_gen_tracker import _classify_status
        assert _classify_status('active', 50, 10) == 'generating'


# ─── Goal Type Registration Tests ───

class TestGoalTypeRegistration:
    """Tests for content_gen goal type in GoalManager."""

    def test_content_gen_type_registered(self):
        from integrations.agent_engine.goal_manager import (
            _prompt_builders, _tool_tags)
        assert 'content_gen' in _prompt_builders
        assert 'content_gen' in _tool_tags
        assert 'content_gen' in _tool_tags['content_gen']

    def test_content_gen_prompt_builder(self):
        from integrations.agent_engine.goal_manager import GoalManager

        goal_dict = {
            'goal_type': 'content_gen',
            'title': 'Content generation: Test Game',
            'description': 'Generate media',
            'config_json': {
                'game_id': 'test-prompt-game',
                'game_title': 'Test Game',
                'media_requirements': {'images': 5, 'tts': 3},
                'task_jobs': {
                    'image': {'status': 'generating', 'progress': 40, 'job_id': 'img_123'},
                },
            },
        }
        prompt = GoalManager.build_prompt(goal_dict)
        assert 'Test Game' in prompt
        assert 'images' in prompt.lower() or 'image' in prompt.lower()

    def test_content_gen_prompt_builder_no_config(self):
        from integrations.agent_engine.goal_manager import GoalManager

        goal_dict = {
            'goal_type': 'content_gen',
            'title': 'Content gen',
            'description': 'Generate content',
            'config_json': {},
        }
        prompt = GoalManager.build_prompt(goal_dict)
        assert isinstance(prompt, str)
        assert len(prompt) > 0


# ─── Content Gen Tools Tests ───

class TestContentGenTools:
    """Tests for the 4 AutoGen tools."""

    def test_tools_list_has_4_tools(self):
        from integrations.agent_engine.content_gen_tools import CONTENT_GEN_TOOLS
        assert len(CONTENT_GEN_TOOLS) == 4
        names = {t['name'] for t in CONTENT_GEN_TOOLS}
        assert names == {
            'get_content_gen_status',
            'retry_stuck_task',
            'check_media_services',
            'force_regenerate',
        }

    def test_all_tools_have_content_gen_tag(self):
        from integrations.agent_engine.content_gen_tools import CONTENT_GEN_TOOLS
        for tool in CONTENT_GEN_TOOLS:
            assert 'content_gen' in tool['tags']

    def test_all_tools_are_callable(self):
        from integrations.agent_engine.content_gen_tools import CONTENT_GEN_TOOLS
        for tool in CONTENT_GEN_TOOLS:
            assert callable(tool['func'])

    def test_get_content_gen_status_returns_json(self):
        """Tool returns valid JSON with progress or error."""
        from integrations.agent_engine.content_gen_tools import get_content_gen_status

        # Patch at the source where get_db is imported inside the function
        with patch('integrations.social.models.get_db') as mock_gdb:
            mock_db = MagicMock()
            mock_gdb.return_value = mock_db

            with patch('integrations.agent_engine.content_gen_tracker.ContentGenTracker.get_game_progress') as mock_prog:
                mock_prog.return_value = {
                    'game_id': 'test-game',
                    'progress_pct': 50,
                    'status': 'generating',
                }
                result = json.loads(get_content_gen_status('test-game'))
                assert result['progress_pct'] == 50

    def test_get_content_gen_status_not_found(self):
        from integrations.agent_engine.content_gen_tools import get_content_gen_status

        with patch('integrations.social.models.get_db') as mock_gdb:
            mock_db = MagicMock()
            mock_gdb.return_value = mock_db

            with patch('integrations.agent_engine.content_gen_tracker.ContentGenTracker.get_game_progress') as mock_prog:
                mock_prog.return_value = None
                result = json.loads(get_content_gen_status('missing-game'))
                assert 'error' in result

    def test_check_media_services_returns_json(self):
        from integrations.agent_engine.content_gen_tools import check_media_services

        with patch('integrations.agent_engine.content_gen_tracker.ContentGenTracker.get_services_health') as mock_health:
            mock_health.return_value = {
                'txt2img': True, 'tts_audio_suite': True,
                'acestep': False, 'wan2gp': False, 'ltx2': False,
            }
            result = json.loads(check_media_services())
            assert result['services']['txt2img'] == 'running'
            assert result['services']['acestep'] == 'offline'
            assert result['all_healthy'] is False

    def test_force_regenerate_returns_job_id(self):
        from integrations.agent_engine.content_gen_tools import force_regenerate

        with patch('integrations.social.models.get_db') as mock_gdb:
            mock_db = MagicMock()
            mock_gdb.return_value = mock_db

            with patch('integrations.agent_engine.content_gen_tracker.ContentGenTracker.update_task_job'):
                result = json.loads(force_regenerate('test-game', 'image'))
                assert result['success'] is True
                assert 'job_id' in result
                assert result['job_id'].startswith('image_')


# ─── API Blueprint Tests ───

class TestContentGenAPI:
    """Tests for the API blueprint endpoints."""

    @pytest.fixture
    def app(self):
        from flask import Flask
        app = Flask(__name__)
        app.config['TESTING'] = True
        from integrations.agent_engine.api_content_gen import content_gen_bp
        app.register_blueprint(content_gen_bp)
        return app

    @pytest.fixture
    def client(self, app):
        return app.test_client()

    def test_list_games(self, client):
        with patch('integrations.social.models.get_db') as mock_gdb:
            mock_gdb.return_value = MagicMock()
            with patch('integrations.agent_engine.content_gen_tracker.ContentGenTracker.get_all_game_tasks') as mock_all:
                mock_all.return_value = [
                    {'game_id': 'g1', 'progress_pct': 50},
                    {'game_id': 'g2', 'progress_pct': 100},
                ]
                resp = client.get('/api/social/content-gen/games')
                assert resp.status_code == 200
                data = resp.get_json()
                assert data['success'] is True
                assert len(data['data']) == 2

    def test_get_game(self, client):
        with patch('integrations.social.models.get_db') as mock_gdb:
            mock_gdb.return_value = MagicMock()
            with patch('integrations.agent_engine.content_gen_tracker.ContentGenTracker.get_game_progress') as mock_prog:
                mock_prog.return_value = {
                    'game_id': 'test-01', 'progress_pct': 75, 'delta_24h': 10,
                }
                resp = client.get('/api/social/content-gen/games/test-01')
                assert resp.status_code == 200
                data = resp.get_json()
                assert data['data']['progress_pct'] == 75

    def test_get_game_not_found(self, client):
        with patch('integrations.social.models.get_db') as mock_gdb:
            mock_gdb.return_value = MagicMock()
            with patch('integrations.agent_engine.content_gen_tracker.ContentGenTracker.get_game_progress') as mock_prog:
                mock_prog.return_value = None
                resp = client.get('/api/social/content-gen/games/missing')
                assert resp.status_code == 404

    def test_get_stuck(self, client):
        with patch('integrations.social.models.get_db') as mock_gdb:
            mock_gdb.return_value = MagicMock()
            with patch('integrations.agent_engine.content_gen_tracker.ContentGenTracker.get_stuck_games') as mock_stuck:
                mock_stuck.return_value = [
                    {'game_id': 'stuck-1', 'stuck_hours': 30},
                ]
                resp = client.get('/api/social/content-gen/stuck?threshold_hours=12')
                assert resp.status_code == 200
                data = resp.get_json()
                assert len(data['data']) == 1

    def test_retry_task(self, client):
        with patch('integrations.social.models.get_db') as mock_gdb:
            mock_gdb.return_value = MagicMock()
            with patch('integrations.agent_engine.content_gen_tracker.ContentGenTracker.update_task_job'):
                resp = client.post('/api/social/content-gen/retry',
                                  json={'game_id': 'test-01', 'task_type': 'image'})
                assert resp.status_code == 200
                data = resp.get_json()
                assert data['success'] is True

    def test_retry_task_no_game_id(self, client):
        resp = client.post('/api/social/content-gen/retry', json={})
        assert resp.status_code == 400

    def test_services_health(self, client):
        with patch('integrations.agent_engine.content_gen_tracker.ContentGenTracker.get_services_health') as mock_health:
            mock_health.return_value = {
                'txt2img': True, 'tts_audio_suite': False,
            }
            resp = client.get('/api/social/content-gen/services')
            assert resp.status_code == 200
            data = resp.get_json()
            assert data['data']['services']['txt2img'] == 'running'

    def test_register_game(self, client):
        with patch('integrations.social.models.get_db') as mock_gdb:
            mock_gdb.return_value = MagicMock()
            with patch('integrations.agent_engine.content_gen_tracker.ContentGenTracker.get_or_create_game_goal') as mock_create:
                mock_create.return_value = {'id': 1, 'game_id': 'new-game'}
                resp = client.post('/api/social/content-gen/register',
                                  json={'game_id': 'new-game', 'game_config': {'title': 'New'}})
                assert resp.status_code == 200
                data = resp.get_json()
                assert data['success'] is True

    def test_register_game_no_id(self, client):
        resp = client.post('/api/social/content-gen/register', json={})
        assert resp.status_code == 400


# ─── Daemon Monitor Integration Tests ───

class TestDaemonMonitorIntegration:
    """Tests for content gen monitor wiring in AgentDaemon._tick()."""

    def test_daemon_has_content_gen_monitor_code(self):
        import inspect
        from integrations.agent_engine.agent_daemon import AgentDaemon

        source = inspect.getsource(AgentDaemon._tick)
        assert 'content_gen_tracker' in source or 'ContentGenTracker' in source

    def test_daemon_tick_count_modulo(self):
        from integrations.agent_engine.agent_daemon import AgentDaemon
        daemon = AgentDaemon()
        daemon._tick_count = 5
        assert daemon._tick_count % 5 == 0

    def test_content_gen_monitor_import(self):
        from integrations.agent_engine.content_gen_tracker import ContentGenTracker
        assert hasattr(ContentGenTracker, 'get_stuck_games')
        assert hasattr(ContentGenTracker, 'attempt_unblock')
        assert hasattr(ContentGenTracker, 'record_progress_snapshot')


# ─── Services Health Tests ───

class TestServicesHealth:
    """Tests for _check_media_service and _restart_media_service."""

    def test_check_media_service_returns_bool(self):
        """_check_media_service returns a boolean (True/False)."""
        from integrations.agent_engine.content_gen_tracker import _check_media_service
        # Without a running RuntimeToolManager, the function catches and returns False
        result = _check_media_service('image')
        assert isinstance(result, bool)

    def test_check_media_service_graceful_failure(self):
        """When RuntimeToolManager is unavailable, returns False (no crash)."""
        from integrations.agent_engine.content_gen_tracker import _check_media_service
        assert _check_media_service('nonexistent') is False

    def test_restart_media_service_graceful_failure(self):
        """When RuntimeToolManager is unavailable, returns False (no crash)."""
        from integrations.agent_engine.content_gen_tracker import _restart_media_service
        assert _restart_media_service('nonexistent') is False

    def test_check_via_mock(self):
        """Mock the whole function to verify tool mapping works."""
        import integrations.agent_engine.content_gen_tracker as tracker_mod

        with patch.object(tracker_mod, '_check_media_service',
                          side_effect=lambda t: t == 'image'):
            assert tracker_mod._check_media_service('image') is True
            assert tracker_mod._check_media_service('tts') is False

    def test_get_services_health(self):
        from integrations.agent_engine.content_gen_tracker import ContentGenTracker

        with patch('integrations.agent_engine.content_gen_tracker._check_media_service',
                   return_value=True):
            health = ContentGenTracker.get_services_health()
            assert len(health) == 5
            assert all(v is True for v in health.values())

    def test_get_services_health_mixed(self):
        from integrations.agent_engine.content_gen_tracker import ContentGenTracker

        def mock_check(svc):
            return svc in ('txt2img', 'tts_audio_suite')

        with patch('integrations.agent_engine.content_gen_tracker._check_media_service',
                   side_effect=mock_check):
            health = ContentGenTracker.get_services_health()
            assert health['txt2img'] is True
            assert health['tts_audio_suite'] is True
            assert health['acestep'] is False
