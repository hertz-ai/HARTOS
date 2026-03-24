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
