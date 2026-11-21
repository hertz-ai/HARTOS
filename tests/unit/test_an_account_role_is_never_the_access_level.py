"""A person's account role on central is never this node's access level.

Owner, 2026-11-21: "again in nunba we regressed navigating to admin, it
redirects to social . some issue with auth".

Measured on the owner's desktop (Nunba on :5000, user 10202): the local
/api/social/auth/me answered role "student".  The page reads `role` as the
access level (guest / flat / regional / central); "student" is none of them,
so the signed-in owner counted as signed out and /admin sent him to /social.

Where "student" came from: core.profile_sync copies the owner's profile down
from the central account store at every boot (sync_owner_account) and carried
the account store's own `role` field into the sync item.  The receiver
(SyncEngine._handle_sync_user) wrote any non-privileged role it was given, so
the account store's role replaced the local access level.

The real sync_profile and receiver against an in-memory DB; central is mocked
at fetch_central_profile.  No grep tests.
"""
from __future__ import annotations

import os
import sys

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import profile_sync  # noqa: E402
from integrations.social.models import Base, User, db_session, get_engine  # noqa: E402
from integrations.social.sync_engine import SyncEngine  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.create_all(get_engine())


def _central_says(monkeypatch, profile):
    monkeypatch.setattr(profile_sync, 'fetch_central_profile', lambda cid, **k: profile)


def _role(uid):
    with db_session(commit=False) as db:
        row = db.query(User).filter_by(id=uid).first()
        return row.role if row else None


def _make(uid, role):
    with db_session() as db:
        db.add(User(id=uid, username=uid, display_name=uid, role=role,
                    user_type='human'))


def test_the_payload_from_central_carries_no_role():
    payload = profile_sync.build_user_sync_payload(
        'uuid-a', 10202, {'name': 'Owner', 'role': 'student'})
    assert 'role' not in payload


def test_a_pulled_account_role_never_becomes_the_access_level(monkeypatch):
    _central_says(monkeypatch, {'name': 'Owner', 'role': 'student'})
    assert profile_sync.sync_profile('uuid-new', 10202) is True
    assert _role('uuid-new') == 'flat'


def test_a_pull_repairs_the_role_an_earlier_pull_left(monkeypatch):
    _make('uuid-owner', 'student')
    _central_says(monkeypatch, {'name': 'Owner', 'role': 'student'})
    assert profile_sync.sync_profile('uuid-owner', 10202) is True
    assert _role('uuid-owner') == 'flat'


def test_a_pull_without_a_role_still_repairs_it(monkeypatch):
    _make('uuid-owner2', 'student')
    _central_says(monkeypatch, {'name': 'Owner'})
    assert profile_sync.sync_profile('uuid-owner2', 10202) is True
    assert _role('uuid-owner2') == 'flat'


def test_a_local_central_role_is_kept(monkeypatch):
    _make('uuid-steward', 'central')
    _central_says(monkeypatch, {'name': 'Steward', 'role': 'student'})
    assert profile_sync.sync_profile('uuid-steward', 1) is True
    assert _role('uuid-steward') == 'central'


def test_a_sync_item_still_sets_an_access_level():
    _make('uuid-g', 'flat')
    with db_session() as db:
        SyncEngine._handle_sync_user(db, {'user_id': 'uuid-g', 'username': 'g',
                                          'role': 'guest'})
    assert _role('uuid-g') == 'guest'


def test_a_sync_item_with_another_systems_role_makes_a_flat_user():
    with db_session() as db:
        SyncEngine._handle_sync_user(db, {'user_id': 'uuid-t', 'username': 't',
                                          'role': 'teacher'})
    assert _role('uuid-t') == 'flat'
