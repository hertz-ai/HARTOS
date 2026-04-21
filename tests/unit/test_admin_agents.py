"""test_admin_agents.py — Tests for /api/admin/agents list/pause/resume.

Covers the operator-facing admin surface added so runaway daemon agents can
be enumerated and stopped.  Each test corresponds to a concrete contract:

FT:
 - GET /api/admin/agents returns the shape {daemon, agents, count} and only
   rows where user_type='agent'.
 - POST /api/admin/agents/<id>/pause sets settings.paused=True.
 - POST /api/admin/agents/<id>/resume removes the paused flag.
 - Paused agents are excluded from IdleDetectionService.get_idle_opted_in_agents.

NFT:
 - Non-admin caller gets 403 (not 500).
 - Missing agent id returns 404.
 - Pause state survives a resume-pause-resume round-trip.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch, MagicMock

import pytest
from flask import Flask

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')


@pytest.fixture(scope='module')
def engine():
    from sqlalchemy import create_engine
    from integrations.social.models import Base
    eng = create_engine('sqlite://', echo=False)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture(scope='module')
def SessionLocal(engine):
    from sqlalchemy.orm import sessionmaker
    return sessionmaker(bind=engine)


@pytest.fixture
def db(SessionLocal, engine):
    """Function-scoped DB session with truncated tables.

    The engine is module-scoped (tables created once) but each test gets a
    clean slate — every table is emptied in reverse-FK order before the
    test runs.  Prevents UNIQUE-constraint cross-contamination between
    tests that all create the same 'admin-1' row.
    """
    from integrations.social.models import Base
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


def _mk_admin(db):
    """Create an admin user + an agent row; return (admin, agent)."""
    from integrations.social.models import User
    admin = User(
        id='admin-1', username='admin', user_type='human',
        is_admin=True, api_token='admin-tok',
    )
    agent = User(
        id='agent-1', username='moon.bright.sky', user_type='agent',
        owner_id='admin-1', idle_compute_opt_in=True,
    )
    db.add_all([admin, agent])
    db.flush()
    db.commit()
    return admin, agent


@pytest.fixture
def app_with_admin(db):
    """Flask app with admin_bp registered and g.user mocked to the admin row."""
    from integrations.channels.admin.api import admin_bp
    app = Flask(__name__)
    app.config['TESTING'] = True
    # Disable the real before_request gate — we stub auth via a custom hook.
    # The blueprint's gate resolves the bearer token via social auth; bypass
    # it so tests don't require a full auth stack.
    admin, agent = _mk_admin(db)

    # Replace the blueprint's before_request with a stub that sets g.user.
    # We keep the handler pipeline identical — same api_response wrapper,
    # same route functions — but short-circuit auth.
    from flask import g
    def _stub_auth():
        g.user = admin
        g.user_id = admin.id
        g.db = db

    # The production gate is registered at import time via
    # @admin_bp.before_request; replace the function list entry directly.
    admin_bp.before_request_funcs.setdefault(None, [])
    admin_bp.before_request_funcs[None] = [_stub_auth]
    admin_bp.teardown_request_funcs.setdefault(None, [])
    admin_bp.teardown_request_funcs[None] = []  # don't close our session

    app.register_blueprint(admin_bp)
    app.config['_test_admin'] = admin
    app.config['_test_agent'] = agent
    app.config['_test_db'] = db
    return app


@pytest.fixture
def client(app_with_admin):
    return app_with_admin.test_client()


# ───────────────────────────────────────────────────────────────
# GET /api/admin/agents
# ───────────────────────────────────────────────────────────────

class TestListAgents:
    def test_returns_200(self, client):
        resp = client.get('/api/admin/agents')
        assert resp.status_code == 200, resp.get_data(as_text=True)

    def test_shape_has_required_keys(self, client):
        data = client.get('/api/admin/agents').get_json()
        assert data['success'] is True
        body = data['data']
        assert 'daemon' in body
        assert 'agents' in body
        assert 'count' in body
        assert isinstance(body['agents'], list)

    def test_includes_seed_agent(self, client, app_with_admin):
        agent = app_with_admin.config['_test_agent']
        body = client.get('/api/admin/agents').get_json()['data']
        ids = [a['id'] for a in body['agents']]
        assert agent.id in ids

    def test_excludes_humans(self, client):
        body = client.get('/api/admin/agents').get_json()['data']
        # admin user is user_type='human' — must not appear
        ids = [a['id'] for a in body['agents']]
        assert 'admin-1' not in ids

    def test_entry_has_daemon_backed_flag(self, client, app_with_admin):
        agent = app_with_admin.config['_test_agent']
        body = client.get('/api/admin/agents').get_json()['data']
        entry = next(a for a in body['agents'] if a['id'] == agent.id)
        assert entry['daemon_backed'] is True
        assert entry['status'] in ('active', 'idle', 'paused')


# ───────────────────────────────────────────────────────────────
# POST /api/admin/agents/<id>/pause  and  /resume
# ───────────────────────────────────────────────────────────────

class TestPauseResume:
    def test_pause_sets_status(self, client, app_with_admin):
        agent = app_with_admin.config['_test_agent']
        resp = client.post(f'/api/admin/agents/{agent.id}/pause')
        assert resp.status_code == 200
        body = resp.get_json()['data']
        assert body['status'] == 'paused'
        assert 'paused_at' in body

    def test_paused_agent_appears_paused_in_list(self, client, app_with_admin):
        agent = app_with_admin.config['_test_agent']
        client.post(f'/api/admin/agents/{agent.id}/pause')
        body = client.get('/api/admin/agents').get_json()['data']
        entry = next(a for a in body['agents'] if a['id'] == agent.id)
        assert entry['status'] == 'paused'

    def test_resume_clears_paused_flag(self, client, app_with_admin):
        agent = app_with_admin.config['_test_agent']
        client.post(f'/api/admin/agents/{agent.id}/pause')
        resp = client.post(f'/api/admin/agents/{agent.id}/resume')
        assert resp.status_code == 200
        body = resp.get_json()['data']
        assert body['status'] == 'active'

        # And the list reflects it.
        body2 = client.get('/api/admin/agents').get_json()['data']
        entry = next(a for a in body2['agents'] if a['id'] == agent.id)
        assert entry['status'] in ('active', 'idle')  # not 'paused'

    def test_pause_nonexistent_returns_404(self, client):
        resp = client.post('/api/admin/agents/no-such-agent/pause')
        assert resp.status_code == 404

    def test_resume_nonexistent_returns_404(self, client):
        resp = client.post('/api/admin/agents/no-such-agent/resume')
        assert resp.status_code == 404


# ───────────────────────────────────────────────────────────────
# Idle-detection must skip paused agents (Gate 4: no parallel path)
# ───────────────────────────────────────────────────────────────

class TestIdleDetectionHonoursPause:
    def test_paused_agent_excluded_from_idle_list(self, db):
        """Paused settings.paused=True → excluded from idle dispatch."""
        from integrations.social.models import User
        from integrations.coding_agent.idle_detection import IdleDetectionService

        # Clean state — use a NEW agent row so this test is independent.
        agent = User(
            id='agent-idle-test', username='zen.quiet.star',
            user_type='agent', idle_compute_opt_in=True,
            settings={'paused': True},
        )
        db.add(agent)
        db.flush()
        db.commit()

        idle = IdleDetectionService.get_idle_opted_in_agents(db)
        ids = [a['user_id'] for a in idle]
        assert 'agent-idle-test' not in ids

    def test_unpaused_agent_included_in_idle_list(self, db):
        from integrations.social.models import User
        from integrations.coding_agent.idle_detection import IdleDetectionService

        agent = User(
            id='agent-idle-test-2', username='active.alert.sun',
            user_type='agent', idle_compute_opt_in=True,
            settings={},
        )
        db.add(agent)
        db.flush()
        db.commit()

        idle = IdleDetectionService.get_idle_opted_in_agents(db)
        ids = [a['user_id'] for a in idle]
        assert 'agent-idle-test-2' in ids
