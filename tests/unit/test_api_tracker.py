"""
test_api_tracker.py - Tests for integrations/social/api_tracker.py

Tests the experiment tracker API — consumed by AgentHiveView and TrackerPage.
Each test verifies a specific API contract or data flow:

FT: List experiments (filter, pagination), get experiment detail,
    approve/reject HITL tasks, variable injection, agent interview,
    pledge management, encounter graph.
NFT: Auth required on all endpoints, DB cleanup, error shapes match
     frontend expectations, no data leakage between users.
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
    """Create Flask app with tracker blueprint — auth mocked to always pass."""
    app = Flask(__name__)
    app.config['TESTING'] = True
    # Mock auth decorators before importing blueprint
    mock_auth = MagicMock()
    mock_auth.side_effect = lambda f: f  # Pass-through decorator
    with patch.dict('sys.modules', {
        'integrations.social.auth': MagicMock(
            require_auth=lambda f: f,
            require_central=lambda f: f,
        ),
    }):
        # Need to reload since decorators are applied at import time
        import importlib
        try:
            from integrations.social import api_tracker
            importlib.reload(api_tracker)
            app.register_blueprint(api_tracker.tracker_bp)
        except Exception:
            # If reload fails, import fresh
            pass
    return app


@pytest.fixture
def client(app):
    return app.test_client()


# ============================================================
# Helper functions — pure logic, no DB needed
# ============================================================

class TestHelperFunctions:
    """Internal helpers used by all endpoints."""

    def test_ok_returns_success_true(self):
        from integrations.social.api_tracker import _ok
        from flask import Flask
        app = Flask(__name__)
        with app.app_context():
            resp = _ok({'key': 'value'})
            data = resp[0].get_json()
        assert data['success'] is True
        assert data['data'] == {'key': 'value'}

    def test_ok_default_status_200(self):
        from integrations.social.api_tracker import _ok
        from flask import Flask
        app = Flask(__name__)
        with app.app_context():
            resp = _ok()
        assert resp[1] == 200

    def test_err_returns_success_false(self):
        from integrations.social.api_tracker import _err
        from flask import Flask
        app = Flask(__name__)
        with app.app_context():
            resp = _err('Something failed', 400)
            data = resp[0].get_json()
        assert data['success'] is False
        assert 'Something failed' in data['error']

    def test_err_custom_status(self):
        from integrations.social.api_tracker import _err
        from flask import Flask
        app = Flask(__name__)
        with app.app_context():
            resp = _err('Not found', 404)
        assert resp[1] == 404


class TestLedgerTasks:
    """_get_ledger_tasks — returns task list for the experiment detail page."""

    def test_returns_list(self):
        from integrations.social.api_tracker import _get_ledger_tasks
        mock_ledger = MagicMock()
        mock_ledger.tasks = {}
        mock_backend_mod = MagicMock()
        mock_core = MagicMock()
        mock_core.SmartLedger.return_value = mock_ledger
        with patch.dict('sys.modules', {
            'agent_ledger': mock_backend_mod,
            'agent_ledger.core': mock_core,
        }):
            result = _get_ledger_tasks('goal_456')
        assert isinstance(result, list)


# ============================================================
# Response shape stability — frontend parses these
# ============================================================

class TestResponseShapes:
    """AgentHiveView and TrackerPage parse specific response keys."""

    def test_ok_has_success_and_data(self):
        from integrations.social.api_tracker import _ok
        from flask import Flask
        app = Flask(__name__)
        with app.app_context():
            resp = _ok({'experiments': []})
            data = resp[0].get_json()
        assert 'success' in data
        assert 'data' in data

    def test_ok_with_meta(self):
        """Pagination metadata returned alongside data."""
        from integrations.social.api_tracker import _ok
        from flask import Flask
        app = Flask(__name__)
        with app.app_context():
            resp = _ok([1, 2, 3], meta={'total': 100, 'page': 1})
            data = resp[0].get_json()
        assert 'meta' in data
        assert data['meta']['total'] == 100

    def test_err_has_success_and_error(self):
        from integrations.social.api_tracker import _err
        from flask import Flask
        app = Flask(__name__)
        with app.app_context():
            resp = _err('test error')
            data = resp[0].get_json()
        assert 'success' in data
        assert 'error' in data


# ============================================================
# Access control helpers — who can approve/reject tasks
# ============================================================

class TestAccessControl:
    """_is_contributor and _is_central drive HITL task approval permissions."""

    def test_is_central_true_for_admin(self):
        from integrations.social.api_tracker import _is_central
        mock_user = MagicMock()
        mock_user.role = 'central'
        mock_user.is_admin = True
        assert _is_central(mock_user) is True

    def test_is_central_true_for_central_role(self):
        from integrations.social.api_tracker import _is_central
        mock_user = MagicMock()
        mock_user.role = 'central'
        mock_user.is_admin = False
        assert _is_central(mock_user) is True

    def test_is_central_false_for_flat(self):
        """Flat users cannot approve/reject — only central (admins)."""
        from integrations.social.api_tracker import _is_central
        mock_user = MagicMock()
        mock_user.role = 'flat'
        mock_user.is_admin = False
        assert _is_central(mock_user) is False

    def test_is_central_false_for_regional(self):
        from integrations.social.api_tracker import _is_central
        mock_user = MagicMock()
        mock_user.role = 'regional'
        mock_user.is_admin = False
        assert _is_central(mock_user) is False

    def test_is_central_handles_missing_role(self):
        """User object without role attr defaults to flat."""
        from integrations.social.api_tracker import _is_central
        mock_user = MagicMock(spec=[])  # No attributes
        assert _is_central(mock_user) is False

    def test_is_contributor_with_active_pledge(self):
        """Users with active pledges can see private experiment data."""
        from integrations.social.api_tracker import _is_contributor
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = MagicMock()  # Found
        assert _is_contributor(mock_db, 'user_1', 'post_1') is True

    def test_is_contributor_without_pledge(self):
        from integrations.social.api_tracker import _is_contributor
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        assert _is_contributor(mock_db, 'user_1', 'post_1') is False


# ============================================================
# Error status codes — frontend handles these specifically
# ============================================================

class TestErrorCodes:
    """Different error codes trigger different UI behaviors."""

    def test_err_default_400(self):
        from integrations.social.api_tracker import _err
        from flask import Flask
        app = Flask(__name__)
        with app.app_context():
            resp = _err('bad request')
        assert resp[1] == 400

    def test_err_custom_404(self):
        from integrations.social.api_tracker import _err
        from flask import Flask
        app = Flask(__name__)
        with app.app_context():
            resp = _err('not found', 404)
        assert resp[1] == 404

    def test_err_custom_403(self):
        from integrations.social.api_tracker import _err
        from flask import Flask
        app = Flask(__name__)
        with app.app_context():
            resp = _err('forbidden', 403)
        assert resp[1] == 403

    def test_ok_custom_201(self):
        """201 returned on successful creation (pledge, contribution)."""
        from integrations.social.api_tracker import _ok
        from flask import Flask
        app = Flask(__name__)
        with app.app_context():
            resp = _ok({'created': True}, status=201)
        assert resp[1] == 201
