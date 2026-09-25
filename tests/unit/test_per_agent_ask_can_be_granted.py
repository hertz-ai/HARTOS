"""A per-agent consent that was asked can then be granted.

request_consent files a pending row on UNIQUE(user_id, agent_id,
consent_type, scope). SQL treats only NULL as distinct, so with a non-NULL
agent_id the grant's append collided with the ask: the person could be asked
"may this agent message you" and their yes could never be recorded.
grant_consent now promotes a never-answered ask in place.

The promotion landed in a34e6489f (a sweep commit that picked up in-flight
work); 077b27332 carries only its no_autoflush wrapper and the account of it.
In the owner-tasked review (2026-09-23) no HARTOS test pinned the promotion.
With the promotion removed, the first test fails with IntegrityError on the
UNIQUE key, which is the defect a person would hit.

    python -m pytest tests/unit/test_per_agent_ask_can_be_granted.py -q
"""
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

UID = '10'
AGENT = '88659566083'


@pytest.fixture
def db():
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


def _rows(db, agent_id):
    from integrations.social.models import UserConsent
    return db.query(UserConsent).filter(
        UserConsent.user_id == UID,
        UserConsent.agent_id == agent_id,
        UserConsent.consent_type == 'agent_contact').all()


def test_a_per_agent_ask_is_granted_on_the_ask(db):
    from integrations.social.consent_service import ConsentService

    ConsentService.request_consent(db, UID, 'agent_contact', agent_id=AGENT)
    db.commit()
    ConsentService.grant_consent(db, UID, 'agent_contact', agent_id=AGENT)
    db.commit()

    rows = _rows(db, AGENT)
    assert len(rows) == 1, 'the answer must land ON the ask, not beside it'
    assert rows[0].granted is True
    assert rows[0].granted_at is not None
    assert ConsentService.check_consent(
        db, UID, 'agent_contact', agent_id=AGENT) is True


def test_a_blanket_ask_still_appends_its_grant(db):
    """agent_id NULL keeps acd11f55's append-only history: two rows."""
    from integrations.social.consent_service import ConsentService

    ConsentService.request_consent(db, UID, 'agent_contact')
    db.commit()
    ConsentService.grant_consent(db, UID, 'agent_contact')
    db.commit()

    rows = _rows(db, None)
    assert len(rows) == 2
    assert sorted(bool(r.granted_at) for r in rows) == [False, True]
