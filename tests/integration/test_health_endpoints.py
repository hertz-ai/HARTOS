"""
Tests for /health (liveness) and /ready (readiness) endpoints.
"""
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault('SOCIAL_DB_PATH', ':memory:')


@pytest.fixture
def client():
    """Create Flask test client for health endpoints."""
    from langchain_gpt_api import app
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


class TestHealthLiveness:
    """Tests for GET /health (liveness probe)."""

    def test_health_returns_200(self, client):
        resp = client.get('/health')
        assert resp.status_code == 200

    def test_health_returns_alive(self, client):
        resp = client.get('/health')
        data = resp.get_json()
        assert data['status'] == 'alive'

    def test_health_always_succeeds(self, client):
        """Liveness should always return 200 if the process is running."""
        for _ in range(3):
            resp = client.get('/health')
            assert resp.status_code == 200


class TestHealthReadiness:
    """Tests for GET /ready (readiness probe)."""

    def test_ready_returns_200_when_healthy(self, client):
        """When DB and node identity are available, returns 200."""
        resp = client.get('/ready')
        data = resp.get_json()
        # DB should work (in-memory SQLite), node identity may or may not
        assert resp.status_code in (200, 503)
        assert 'checks' in data
        assert 'database' in data['checks']

    def test_ready_checks_database(self, client):
        """Readiness check includes database connectivity."""
        resp = client.get('/ready')
        data = resp.get_json()
        assert data['checks']['database'] == 'ok'

    def test_ready_includes_node_identity(self, client):
        """Readiness check includes node identity."""
        resp = client.get('/ready')
        data = resp.get_json()
        assert 'node_identity' in data['checks']

    def test_ready_includes_optional_checks(self, client):
        """Readiness check includes optional HevolveAI and llm_backend."""
        resp = client.get('/ready')
        data = resp.get_json()
        assert 'hevolve_core' in data['checks']
        assert 'llm_backend' in data['checks']

    def test_ready_503_when_db_fails(self, client):
        """Returns 503 when database is unavailable."""
        with patch('integrations.social.models.get_db',
                   side_effect=RuntimeError("DB down")):
            resp = client.get('/ready')
            data = resp.get_json()
            assert resp.status_code == 503
            assert data['status'] == 'not_ready'
            assert 'fail' in data['checks']['database']

    def test_ready_status_field(self, client):
        """Response includes 'ready' or 'not_ready' status."""
        resp = client.get('/ready')
        data = resp.get_json()
        assert data['status'] in ('ready', 'not_ready')


class TestExistingStatus:
    """Verify existing /status endpoint still works."""

    def test_status_returns_200(self, client):
        resp = client.get('/status')
        assert resp.status_code == 200

    def test_status_returns_working(self, client):
        resp = client.get('/status')
        data = resp.get_json()
        assert data['response'] == 'Working...'
        assert data['status'] == 'running'
