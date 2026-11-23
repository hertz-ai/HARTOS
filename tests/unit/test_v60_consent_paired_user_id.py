"""v60 adds user_consents.paired_user_id to a desktop that already holds
phones' grants (#306).

The column keeps the account a phone was allowed as (auth.verify_device_jwt
admits that account only).  A desktop upgrading from v59 has device_access
asks and grants written before it existed: the step must add the column
without touching those rows, leave every one of them naming no account (so
the phone is asked about again, never bound on first use), and stamp v60
only once the column is there.

The REAL run_migrations against a REAL sqlite file shaped like a v59
desktop's (its user_consents table as v58 left it), never a source grep.
"""
import os
import sys
from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from integrations.social import migrations  # noqa: E402
from tests.unit.test_device_access_gate import Phone  # noqa: E402

OWNER = 'owner-1'

#: user_consents as a v59 desktop has it (v55 label, v58 reopened_at).
_V59_USER_CONSENTS = """
CREATE TABLE user_consents (
    id VARCHAR(64) NOT NULL PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    agent_id VARCHAR(64),
    consent_type VARCHAR(30) NOT NULL,
    scope VARCHAR(100) NOT NULL,
    granted BOOLEAN NOT NULL,
    granted_at DATETIME,
    revoked_at DATETIME,
    label VARCHAR(100),
    reopened_at DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME,
    CONSTRAINT uq_user_consent UNIQUE (user_id, agent_id, consent_type, scope)
)"""


@pytest.fixture
def v59_desktop(tmp_path, monkeypatch):
    """A v59 desktop's database holding one phone's ask and grant and a
    camera grant, and run_migrations pointed at it."""
    engine = create_engine(
        'sqlite:///' + str(tmp_path / 'hevolve_database.db').replace('\\', '/'))
    phone = Phone(user_id='40021', username='Sathish')
    scope = 'device:' + phone.public_hex
    now = datetime(2026, 11, 1, 9, 0, 0)
    with engine.begin() as conn:
        conn.execute(text(_V59_USER_CONSENTS))
        conn.execute(text(
            "CREATE TABLE social_meta (key TEXT PRIMARY KEY, value TEXT)"))
        conn.execute(text(
            "INSERT INTO social_meta (key, value) VALUES ('schema_version', '59')"))
        rows = [
            ('ask-1', scope, 0, None, 'Sathish', 'device_access'),
            ('grant-1', scope, 1, now, 'Sathish', 'device_access'),
            ('cam-1', '*', 1, now, None, 'camera_capture'),
        ]
        for rid, rscope, granted, granted_at, label, ctype in rows:
            conn.execute(text(
                "INSERT INTO user_consents (id, user_id, agent_id, consent_type, "
                "scope, granted, granted_at, label, created_at, updated_at) "
                "VALUES (:id, :u, NULL, :t, :s, :g, :ga, :l, :c, :c)"),
                {'id': rid, 'u': OWNER, 't': ctype, 's': rscope, 'g': granted,
                 'ga': granted_at, 'l': label, 'c': now})
    monkeypatch.setattr(migrations, 'get_engine', lambda: engine)
    yield engine, phone
    engine.dispose()


def _columns(engine):
    return {c['name'] for c in inspect(engine).get_columns('user_consents')}


def test_v60_adds_the_column_and_keeps_every_grant(v59_desktop):
    engine, _ = v59_desktop
    assert 'paired_user_id' not in _columns(engine)
    migrations.run_migrations()
    assert 'paired_user_id' in _columns(engine)
    assert migrations.get_schema_version(engine) == 60
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, granted, label, paired_user_id FROM user_consents "
            "ORDER BY id")).fetchall()
    assert [tuple(r) for r in rows] == [
        ('ask-1', 0, 'Sathish', None),
        ('cam-1', 1, None, None),
        ('grant-1', 1, 'Sathish', None),
    ]


def test_v60_is_idempotent(v59_desktop):
    engine, _ = v59_desktop
    migrations.run_migrations()
    migrations.run_migrations()
    assert 'paired_user_id' in _columns(engine)
    assert migrations.get_schema_version(engine) == 60
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT COUNT(*) FROM user_consents")).scalar() == 3


def test_a_grant_the_step_carried_over_is_asked_again_not_bound(v59_desktop):
    """The migrated grant names no account, so the phone that holds it is
    asked about again (auth.verify_device_jwt 'pending', reason 'reallow'),
    and the grant row itself is never written with the account it claims."""
    engine, phone = v59_desktop
    migrations.run_migrations()
    from integrations.social.auth import verify_device_jwt
    from integrations.social.models import UserConsent
    db = sessionmaker(bind=engine)()
    try:
        verdict = verify_device_jwt(db, phone.token(), OWNER)
        assert verdict['status'] == 'pending', verdict
        assert verdict['reason'] == 'reallow'
        assert db.get(UserConsent, 'grant-1').paired_user_id is None
    finally:
        db.close()


def test_a_failed_add_is_not_stamped(v59_desktop, monkeypatch):
    """The version is stamped only once the column exists, so a pass that
    could not add it (the desktop's database locked, #71) is retried on the
    next boot instead of being recorded as done."""
    engine, _ = v59_desktop
    monkeypatch.setattr(migrations, '_v60_consent_paired_user_id',
                        lambda _engine: False)
    migrations.run_migrations()
    assert migrations.get_schema_version(engine) == 59
