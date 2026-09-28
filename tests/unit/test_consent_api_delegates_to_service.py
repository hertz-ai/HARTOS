"""Parallel-path fix #4: the JWT consent UI surface
(``consent_api.grant_consent``) INSERTED the ``UserConsent`` row inline, so
UI-driven grants left NO immutable-audit entry, emitted NO ``consent.granted``
event, skipped ``public_exposure`` up-sync, and accepted arbitrary
``consent_type`` strings — drifting from the canonical
``ConsentService.grant_consent`` that internal callers use.

This fix delegates the grant WRITE to the service. Tests:
  1. the canonical grant writes an audit entry + emits an event + validates type
  2. ``consent_api.grant_consent`` delegates to it (no inline ``UserConsent`` insert)
"""
import re
from pathlib import Path
import pytest

from integrations.social import consent_service as cs


@pytest.fixture
def db():
    """A real session: the grant's broadcast waits for its commit, which a
    stand-in object without one could never deliver."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from integrations.social.models import UserConsent

    engine = create_engine('sqlite://', poolclass=StaticPool,
                           connect_args={'check_same_thread': False})
    UserConsent.__table__.create(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def test_service_grant_audits_emits_and_validates(monkeypatch, db):
    audits, emits = [], []
    monkeypatch.setattr(cs, '_audit', lambda *a, **k: audits.append((a, k)))
    monkeypatch.setattr(cs, '_emit', lambda topic, data: emits.append(topic))

    row = cs.ConsentService.grant_consent(db, 'user-1', 'data_access', '*')
    db.commit()
    assert row.consent_type == 'data_access'
    assert audits, "grant must write an immutable-audit entry"
    assert 'consent.granted' in emits, "grant must emit consent.granted"

    # Unknown type is now rejected (the UI inline path used to accept anything).
    with pytest.raises(ValueError):
        cs.ConsentService.grant_consent(db, 'user-1', 'not_a_real_type', '*')


def test_consent_api_grant_delegates_to_service():
    src = (Path(cs.__file__).resolve().parent / 'consent_api.py').read_text(encoding='utf-8')
    grant = re.search(r"def grant_consent\(\):.*?(?=\n@|\ndef |\Z)", src, re.DOTALL)
    assert grant, "grant_consent not found"
    body = grant.group(0)
    assert 'ConsentService.grant_consent' in body, "API grant must delegate to the service"
    assert 'UserConsent(' not in body, "API grant must not insert UserConsent inline"
