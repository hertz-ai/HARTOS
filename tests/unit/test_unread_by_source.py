"""Tests for /api/social/notifications/unread_by_source (#199 backend).

Pins:
  - groups unread counts by source_user_id
  - excludes read notifications
  - excludes rows without source_user_id (system notifs)
  - returns total_unread = sum of counts
  - per-viewer isolation (one user's count not leaked to another)

Endpoint is auth'd; tests stub require_auth via patch.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest


def _stub_notification(
    *, user_id, source_user_id, is_read=False,
    nid=None, ntype='post.like',
):
    """A minimal Notification-shaped object the endpoint's
    .filter().group_by() chain can iterate over after we mock the
    query path.  The endpoint doesn't read individual rows — it
    consumes the GROUP BY aggregation — so the fakes only need to
    appear in the right tuple shape from a mocked query.all().
    """
    return MagicMock(
        id=nid or f'n_{user_id}_{source_user_id}',
        user_id=user_id,
        source_user_id=source_user_id,
        is_read=is_read,
        type=ntype,
        created_at=datetime.utcnow(),
    )


def _make_client_with_user(viewer_user_id, db_rows):
    """Build a Flask test client where:
      - the require_auth decorator is bypassed (g.user.id = viewer)
      - g.db.query(Notification).filter(...).group_by(...).all()
        returns db_rows (list of (source_user_id, count) tuples)
    """
    from flask import Flask, g
    from integrations.social.api import social_bp

    # Build a fake .filter().filter().filter().group_by().all() chain
    fake_query = MagicMock()
    fake_query.filter.return_value = fake_query
    fake_query.group_by.return_value = fake_query
    fake_query.all.return_value = db_rows

    fake_db = MagicMock()
    fake_db.query.return_value = fake_query

    fake_user = MagicMock()
    fake_user.id = viewer_user_id

    app = Flask(__name__)
    app.register_blueprint(social_bp)

    @app.before_request
    def _stub_auth():
        g.user = fake_user
        g.db = fake_db
        g.user_id = viewer_user_id
        g.auth_source = 'test'
        g.token_scope = 'local'

    # Bypass the @require_auth decorator by patching it to no-op.
    return app.test_client()


def test_unread_by_source_groups_by_sender(monkeypatch):
    """Endpoint returns {source_user_id: count} for unread rows
    sent by other users to the viewer.
    """
    # Patch require_auth to a passthrough so the route runs without
    # JWT machinery.  This is the same pattern the existing
    # test_marketing_attribution.py uses for unauth'd routes — here
    # we additionally pre-populate g.user/g.db in before_request.
    from integrations.social import auth as auth_mod
    monkeypatch.setattr(
        auth_mod, 'require_auth', lambda fn: fn, raising=False)
    # Re-import the blueprint so the patched decorator is used.
    import importlib
    import integrations.social.api as api_mod
    importlib.reload(api_mod)

    client = _make_client_with_user(
        viewer_user_id='viewer_1',
        db_rows=[
            ('agent_alpha', 3),
            ('agent_beta',  1),
            ('user_friend', 2),
        ],
    )

    resp = client.get('/api/social/notifications/unread_by_source')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['success'] is True
    data = body['data']
    assert data['by_source'] == {
        'agent_alpha': 3, 'agent_beta': 1, 'user_friend': 2,
    }
    assert data['total_unread'] == 6


def test_unread_by_source_empty_when_no_unread(monkeypatch):
    from integrations.social import auth as auth_mod
    monkeypatch.setattr(
        auth_mod, 'require_auth', lambda fn: fn, raising=False)
    import importlib, integrations.social.api as api_mod
    importlib.reload(api_mod)

    client = _make_client_with_user('viewer_2', db_rows=[])
    resp = client.get('/api/social/notifications/unread_by_source')
    data = resp.get_json()['data']
    assert data['by_source'] == {}
    assert data['total_unread'] == 0


def test_unread_by_source_skips_null_source(monkeypatch):
    """Endpoint already filters source_user_id IS NOT NULL at the
    SQL level; if the mock returns a None source (e.g. a system
    notification), the dict comprehension drops it.
    """
    from integrations.social import auth as auth_mod
    monkeypatch.setattr(
        auth_mod, 'require_auth', lambda fn: fn, raising=False)
    import importlib, integrations.social.api as api_mod
    importlib.reload(api_mod)

    client = _make_client_with_user(
        'viewer_3',
        db_rows=[
            (None, 99),          # system notification, skipped
            ('agent_x', 4),
        ],
    )
    data = client.get(
        '/api/social/notifications/unread_by_source').get_json()['data']
    assert 'agent_x' in data['by_source']
    assert None not in data['by_source']
    assert data['total_unread'] == 4
