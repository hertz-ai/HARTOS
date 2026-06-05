"""link-device must return HTTP 201 for a NEW device binding (#106).

Regression from the #97 envelope migration (fb1d108): the call site was
`_ok(binding.to_dict(), 201)`, but the canonical `_ok(data, meta, status)` binds
the positional 201 to `meta`, so a new device link returned HTTP 200 and leaked
`meta: 201` — erasing the created-vs-exists (201 vs 200) distinction. (The
sibling `create_backup` already used `status=201`; only link_device was missed.)

Drives the REAL route through a Flask test client against an in-memory social DB.
No grep tests.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def client_db(monkeypatch):
    monkeypatch.setenv('HEVOLVE_DB_PATH', ':memory:')
    from integrations.social import auth as auth_mod
    auth_mod._jwt_manager = False                      # simple JWT path (no manager)
    from integrations.social import models as models_mod
    models_mod._engine = None
    models_mod._SessionLocal = None
    from integrations.social import migrations
    from integrations.social.models import get_engine, get_db, DeviceBinding
    eng = get_engine()
    migrations.run_migrations()
    DeviceBinding.__table__.create(bind=eng, checkfirst=True)   # ensure table present
    db = get_db()

    from flask import Flask
    from integrations.social.sync_api import sync_bp
    app = Flask(__name__)
    app.register_blueprint(sync_bp)
    try:
        yield app.test_client(), db
    finally:
        try:
            db.close()
        except Exception:
            pass
        try:
            eng.dispose()          # drop the :memory: connection so each test is isolated
        except Exception:
            pass
        models_mod._engine = None
        models_mod._SessionLocal = None


def _seed_user(db):
    from integrations.social.models import User
    u = User(id=str(uuid.uuid4()), username='dev', display_name='Dev',
             email='dev@x.test', password_hash='x:y', user_type='human')
    db.add(u)
    db.commit()
    return u


def _auth_header(u):
    from integrations.social import auth
    return {'Authorization': f'Bearer {auth.generate_jwt(u.id, u.username, "flat")}'}


def test_link_new_device_returns_201(client_db):
    client, db = client_db
    u = _seed_user(db)
    r = client.post('/api/social/sync/link-device',
                    json={'device_id': 'dev-abc', 'platform': 'web'},
                    headers=_auth_header(u))
    assert r.status_code == 201, f"new device link must be 201, got {r.status_code}"
    body = r.get_json()
    assert body['success'] is True
    assert body['data']['device_id'] == 'dev-abc'
    assert body.get('meta') != 201, "the 201 must be the HTTP status, not leaked into meta"


def test_relink_existing_device_returns_200(client_db):
    """Idempotent re-link of the SAME device is an update, not a create → 200,
    proving the 201-vs-200 distinction the positional-arg bug had erased."""
    client, db = client_db
    u = _seed_user(db)
    hdr = _auth_header(u)
    first = client.post('/api/social/sync/link-device',
                        json={'device_id': 'dev-xyz'}, headers=hdr)
    assert first.status_code == 201
    again = client.post('/api/social/sync/link-device',
                        json={'device_id': 'dev-xyz'}, headers=hdr)
    assert again.status_code == 200
