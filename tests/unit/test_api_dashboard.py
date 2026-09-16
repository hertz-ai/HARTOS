"""
test_api_dashboard.py - Tests for integrations/social/api_dashboard.py

Tests the Agent Dashboard API — consumed by AgentDashboardPage and AgentHiveView.
Each test verifies a specific frontend contract or data integrity guarantee:

FT: /dashboard/agents returns agent list, /dashboard/health returns watchdog state,
    /dashboard/system returns tier+resources, error handling returns 500 with details.
NFT: Response shape stability (frontend parses specific keys), graceful degradation
     when subsystems unavailable, no sensitive data leakage.
"""
import os
import sys
import json
from unittest.mock import patch, MagicMock

import pytest
from flask import Flask

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config['TESTING'] = True
    from integrations.social.api_dashboard import dashboard_bp
    app.register_blueprint(dashboard_bp)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


# ============================================================
# /api/social/dashboard/health — public, no auth
# ============================================================

class TestDashboardHealth:
    """Health endpoint polled every 5s by AgentDashboardPage."""

    def test_returns_200(self, client):
        resp = client.get('/api/social/dashboard/health')
        assert resp.status_code == 200

    def test_returns_success_true(self, client):
        data = client.get('/api/social/dashboard/health').get_json()
        assert data['success'] is True

    def test_has_watchdog_key(self, client):
        """Frontend reads data.watchdog to show daemon health chip."""
        data = client.get('/api/social/dashboard/health').get_json()
        assert 'watchdog' in data['data']

    def test_has_world_model_key(self, client):
        data = client.get('/api/social/dashboard/health').get_json()
        assert 'world_model' in data['data']

    def test_graceful_without_watchdog(self, client):
        """If watchdog isn't started yet, returns default — not crash."""
        with patch.dict('sys.modules', {'security.node_watchdog': None}):
            resp = client.get('/api/social/dashboard/health')
        assert resp.status_code == 200


# ============================================================
# /api/social/dashboard/agents — requires auth in prod
# ============================================================

class TestDashboardAgents:
    """Agent list endpoint — renders the agent cards in AgentDashboardPage."""

    def test_returns_json(self, client):
        """Must always return JSON — frontend parses it as JSON."""
        mock_db = MagicMock()
        mock_svc = MagicMock()
        mock_svc.get_dashboard.return_value = {'agents': [], 'goals': []}
        mock_mod = MagicMock()
        mock_mod.DashboardService = mock_svc
        mock_models = MagicMock()
        mock_models.get_db.return_value = mock_db
        with patch.dict('sys.modules', {
            'integrations.social.dashboard_service': mock_mod,
            'integrations.social.models': mock_models,
        }):
            resp = client.get('/api/social/dashboard/agents')
        assert resp.content_type.startswith('application/json')

    def test_returns_500_on_service_error(self, client):
        """DB failure must return 500, not crash the Flask worker."""
        mock_db = MagicMock()
        mock_svc = MagicMock()
        mock_svc.get_dashboard.side_effect = Exception("DB fail")
        mock_mod = MagicMock()
        mock_mod.DashboardService = mock_svc
        mock_models = MagicMock()
        mock_models.get_db.return_value = mock_db
        with patch.dict('sys.modules', {
            'integrations.social.dashboard_service': mock_mod,
            'integrations.social.models': mock_models,
        }):
            resp = client.get('/api/social/dashboard/agents')
        assert resp.status_code == 500

    def test_closes_db_always(self, client):
        """DB session leak prevention — close() must be called even on error."""
        mock_db = MagicMock()
        mock_svc = MagicMock()
        mock_svc.get_dashboard.return_value = {}
        mock_mod = MagicMock()
        mock_mod.DashboardService = mock_svc
        mock_models = MagicMock()
        mock_models.get_db.return_value = mock_db
        with patch.dict('sys.modules', {
            'integrations.social.dashboard_service': mock_mod,
            'integrations.social.models': mock_models,
        }):
            client.get('/api/social/dashboard/agents')
        mock_db.close.assert_called_once()


# ============================================================
# /api/social/dashboard/system — system resources
# ============================================================

class TestDashboardSystem:
    """System dashboard — shows tier, CPU, RAM, disk in the admin panel."""

    def test_returns_200(self, client):
        mock_db = MagicMock()
        mock_models = MagicMock()
        mock_models.get_db.return_value = mock_db
        with patch.dict('sys.modules', {'integrations.social.models': mock_models}):
            resp = client.get('/api/social/dashboard/system')
        assert resp.status_code == 200

    def test_has_deployment_mode(self, client):
        mock_db = MagicMock()
        mock_models = MagicMock()
        mock_models.get_db.return_value = mock_db
        with patch.dict('sys.modules', {'integrations.social.models': mock_models}):
            data = client.get('/api/social/dashboard/system').get_json()
        result = data.get('data', data)
        assert 'deployment_mode' in result


# ============================================================
# /api/social/node/capabilities — public
# ============================================================

class TestNodeCapabilities:
    """Node capabilities — part of the HART OS equilibrium system."""

    def test_returns_200_with_capabilities(self, client):
        mock_caps = MagicMock()
        mock_caps.to_dict.return_value = {'tier': 'standard', 'gpu': False}
        mock_mod = MagicMock()
        mock_mod.get_capabilities.return_value = mock_caps
        with patch.dict('sys.modules', {'security.system_requirements': mock_mod}):
            resp = client.get('/api/social/node/capabilities')
        assert resp.status_code == 200

    def test_returns_503_when_not_ready(self, client):
        """Before system check completes, must return 503 — frontend shows loading."""
        mock_mod = MagicMock()
        mock_mod.get_capabilities.return_value = None
        with patch.dict('sys.modules', {'security.system_requirements': mock_mod}):
            resp = client.get('/api/social/node/capabilities')
        assert resp.status_code == 503
class TestDashboardHealthCarriesTheGovernor:
    """The governor's mode decides whether ANY background work runs.

    _proactive_check_tasks returns immediately unless MODE_IDLE, and the
    dispatch yield gate closes below a 0.3 throttle (ACTIVE is 0.05). So when
    a node quietly does nothing, the mode is the first thing worth reading.

    Until now nothing served it. On the box 2026-09-07 the agent daemon logged
    "yield gate has blocked on 'governor_throttle' for 12204s" every 30s -- the
    entire boot -- and working out why took ten passes of inference from
    outside the process, because the value itself was exposed nowhere. These
    pin the one call that answers it.
    """

    def test_health_reports_the_governor_mode(self, client):
        gov = MagicMock()
        gov.get_stats.return_value = {
            'mode': 'active', 'throttle': 0.05,
            'cpu_total': 0.71, 'cpu_own': 0.27, 'cpu_external': 0.44,
        }
        with patch('core.resource_governor.get_governor', return_value=gov):
            r = client.get('/api/social/dashboard/health')
        assert r.status_code == 200
        g = json.loads(r.data)['data']['governor']
        assert g['mode'] == 'active'
        assert g['throttle'] == 0.05

    def test_it_carries_the_attribution_that_explains_the_mode(self, client):
        """mode alone says WHAT; the cpu split says WHY. ACTIVE is reached
        either because a user is present or because EXTERNAL cpu crossed the
        backoff line, and only the split tells them apart."""
        gov = MagicMock()
        gov.get_stats.return_value = {
            'mode': 'idle', 'throttle': 1.0,
            'cpu_total': 0.30, 'cpu_own': 0.25, 'cpu_external': 0.05,
        }
        with patch('core.resource_governor.get_governor', return_value=gov):
            r = client.get('/api/social/dashboard/health')
        g = json.loads(r.data)['data']['governor']
        for k in ('cpu_total', 'cpu_own', 'cpu_external'):
            assert k in g, 'the attribution must ride along, not just the mode'

    def test_a_governor_that_never_started_says_so(self, client):
        with patch('core.resource_governor.get_governor', return_value=None):
            r = client.get('/api/social/dashboard/health')
        assert json.loads(r.data)['data']['governor']['mode'] == 'not_started'

    def test_a_raising_governor_degrades_like_its_neighbours(self, client):
        """The watchdog and world-model blocks above never take the endpoint
        down; neither may this one. A health endpoint that 500s is useless
        exactly when it is needed."""
        with patch('core.resource_governor.get_governor',
                   side_effect=RuntimeError('boom')):
            r = client.get('/api/social/dashboard/health')
        assert r.status_code == 200
        assert json.loads(r.data)['data']['governor']['mode'] == 'unavailable'
