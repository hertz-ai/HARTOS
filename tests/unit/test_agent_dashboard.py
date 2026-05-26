"""Tests for Agent Dashboard - truth-grounded unified view."""
import os
import sys
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# Ensure project root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# In-memory SQLite for tests
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from integrations.social.models import Base, AgentGoal, CodingGoal, User
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture(scope='session')
def engine():
    eng = create_engine('sqlite://', echo=False)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture(scope='session')
def Session(engine):
    return sessionmaker(bind=engine)


@pytest.fixture
def db(Session):
    session = Session()
    yield session
    session.rollback()
    session.close()


class TestDashboardService:
    """Dashboard service unit tests."""

    def test_empty_dashboard(self, db):
        from integrations.social.dashboard_service import DashboardService
        data = DashboardService.get_dashboard(db)
        assert 'timestamp' in data
        assert 'node_health' in data
        assert 'world_model' in data
        assert 'agents' in data
        assert 'summary' in data
        assert isinstance(data['agents'], list)

    def test_world_model_section_unavailable(self, db):
        """When HevolveAI is not running, world_model shows unavailable."""
        from integrations.social.dashboard_service import DashboardService
        data = DashboardService.get_dashboard(db)
        wm = data['world_model']
        # HevolveAI is not running in tests → healthy=False
        assert wm.get('healthy') is False or 'error' in wm

    def test_agent_goal_appears(self, db):
        from integrations.social.dashboard_service import DashboardService
        goal = AgentGoal(
            goal_type='marketing', title='Test campaign',
            status='active', spark_budget=200, spark_spent=50,
        )
        db.add(goal)
        db.flush()

        data = DashboardService.get_dashboard(db)
        marketing_goals = [a for a in data['agents']
                           if a['type'] == 'marketing_goal']
        assert len(marketing_goals) >= 1
        assert marketing_goals[0]['name'] == 'Test campaign'
        assert marketing_goals[0]['metrics']['spark_spent'] == 50
        assert marketing_goals[0]['metrics']['spark_budget'] == 200

    def test_stalled_goal_detection(self, db):
        from integrations.social.dashboard_service import DashboardService
        goal = AgentGoal(
            goal_type='marketing', title='Stalled goal',
            status='active', spark_budget=100,
            last_dispatched_at=datetime.utcnow() - timedelta(minutes=5),
        )
        db.add(goal)
        db.flush()

        data = DashboardService.get_dashboard(db)
        stalled = [a for a in data['agents'] if a['name'] == 'Stalled goal']
        assert len(stalled) == 1
        assert stalled[0]['status'] == 'stalled'

    def test_idle_goal_detection(self, db):
        from integrations.social.dashboard_service import DashboardService
        goal = AgentGoal(
            goal_type='analytics', title='Never dispatched',
            status='active', spark_budget=100,
            last_dispatched_at=None,
        )
        db.add(goal)
        db.flush()

        data = DashboardService.get_dashboard(db)
        idle = [a for a in data['agents'] if a['name'] == 'Never dispatched']
        assert len(idle) == 1
        assert idle[0]['status'] == 'idle'

    def test_completed_goal_appears(self, db):
        from integrations.social.dashboard_service import DashboardService
        goal = AgentGoal(
            goal_type='marketing', title='Done campaign',
            status='completed', spark_budget=200, spark_spent=200,
        )
        db.add(goal)
        db.flush()

        data = DashboardService.get_dashboard(db)
        done = [a for a in data['agents'] if a['name'] == 'Done campaign']
        assert len(done) == 1
        assert done[0]['status'] == 'completed'

    def test_trained_agent_appears(self, db):
        from integrations.social.dashboard_service import DashboardService
        agent = User(username='bot_agent', email='bot@test.com',
                     user_type='agent')
        db.add(agent)
        db.flush()

        data = DashboardService.get_dashboard(db)
        trained = [a for a in data['agents'] if a['type'] == 'trained_agent']
        assert len(trained) >= 1

    def test_daemon_status_without_watchdog(self, db):
        from integrations.social.dashboard_service import DashboardService
        with patch('security.node_watchdog.get_watchdog', return_value=None):
            data = DashboardService.get_dashboard(db)
        daemons = [a for a in data['agents'] if a['type'] == 'daemon']
        assert len(daemons) >= 5  # gossip, runtime_monitor, sync, agent, coding
        for d in daemons:
            assert d['status'] == 'unknown'

    def test_daemon_status_with_watchdog(self, db):
        from integrations.social.dashboard_service import DashboardService
        mock_wd = MagicMock()
        mock_wd.get_health.return_value = {
            'watchdog': 'healthy',
            'uptime_seconds': 100,
            'threads': {
                'gossip': {
                    'status': 'healthy',
                    'last_heartbeat_iso': '2026-02-10T12:00:00+00:00',
                    'restart_count': 0,
                    'last_heartbeat_age_s': 2.0,
                },
                'agent_daemon': {
                    'status': 'frozen',
                    'last_heartbeat_iso': '2026-02-10T11:50:00+00:00',
                    'restart_count': 1,
                    'last_heartbeat_age_s': 600.0,
                },
            },
            'restart_log': [],
        }
        with patch('security.node_watchdog.get_watchdog', return_value=mock_wd):
            data = DashboardService.get_dashboard(db)
        daemons = {d['name']: d for d in data['agents'] if d['type'] == 'daemon'}
        assert daemons['gossip']['status'] == 'healthy'
        assert daemons['agent_daemon']['status'] == 'frozen'


class TestDashboardPriority:
    """Priority ordering tests."""

    def test_priority_ordering(self, db):
        from integrations.social.dashboard_service import DashboardService
        # Create goals with different statuses
        active = AgentGoal(goal_type='marketing', title='Active',
                           status='active', spark_budget=100,
                           last_dispatched_at=datetime.utcnow())
        completed = AgentGoal(goal_type='marketing', title='Completed',
                              status='completed', spark_budget=100)
        db.add_all([active, completed])
        db.flush()

        data = DashboardService.get_dashboard(db)
        # Active should be before completed
        names = [a['name'] for a in data['agents']]
        if 'Active' in names and 'Completed' in names:
            assert names.index('Active') < names.index('Completed')

    def test_frozen_daemon_high_priority(self):
        from integrations.social.dashboard_service import DashboardService
        frozen = {'type': 'daemon', 'status': 'frozen', 'metrics': {}}
        healthy = {'type': 'daemon', 'status': 'healthy', 'metrics': {}}
        assert DashboardService._compute_priority(frozen) > \
               DashboardService._compute_priority(healthy)

    def test_stalled_lower_than_active(self):
        from integrations.social.dashboard_service import DashboardService
        active = {'type': 'marketing_goal', 'status': 'active', 'metrics': {}}
        stalled = {'type': 'marketing_goal', 'status': 'stalled', 'metrics': {}}
        assert DashboardService._compute_priority(active) > \
               DashboardService._compute_priority(stalled)

    def test_spark_budget_tiebreak(self):
        from integrations.social.dashboard_service import DashboardService
        rich = {'type': 'marketing_goal', 'status': 'active',
                'metrics': {'spark_budget': 500, 'spark_spent': 0}}
        poor = {'type': 'marketing_goal', 'status': 'active',
                'metrics': {'spark_budget': 50, 'spark_spent': 0}}
        assert DashboardService._compute_priority(rich) > \
               DashboardService._compute_priority(poor)

    def test_summary_counts(self, db):
        from integrations.social.dashboard_service import DashboardService
        data = DashboardService.get_dashboard(db)
        assert data['summary']['total'] == len(data['agents'])
        # Verify by_type counts add up
        total_by_type = sum(data['summary']['by_type'].values())
        assert total_by_type == data['summary']['total']
