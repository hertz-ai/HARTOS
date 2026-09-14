"""A revoked consent stops passing the consent check, from either writer.

Measured 2026-09-14 on in-memory SQLite with the model the desktop loads
(integrations.social._models_local: sql.models is installed neither in the
HARTOS venv nor in Nunba's python-embed):

    grant, revoke from the privacy page            -> check_consent True
    ask, grant, ConsentService.revoke_consent      -> check_consent True
    grant twice, revoke from the privacy page      -> check_consent True

The privacy page (consent_api.revoke_consent) records a revoke as revoked_at
on the row and leaves granted=True, and check_consent never read revoked_at.
ConsentService.revoke_consent took the first row of the combination, which
after an ask is the pending ask row, so the grant stayed.  grant_consent is
append-only, so two grants for one combination are two rows, and a revoke
that ends only one of them leaves the other passing.

The desktop's live user_consents table already holds a pending
screen_capture ask (~/Documents/Nunba/data/hevolve_database.db,
2026-09-14), so ask-then-grant is the order the first real grant takes.

    python -m pytest tests/unit/test_consent_revoke_is_honoured.py -q
"""
import contextlib
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TOKEN = 'TEST-USER-'
UID = 10
AGENT = '88659566083'


@pytest.fixture
def factory():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from integrations.social.models import UserConsent

    engine = create_engine('sqlite://', poolclass=StaticPool,
                           connect_args={'check_same_thread': False})
    UserConsent.__table__.create(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def client(factory, monkeypatch):
    """The privacy page's own routes, on the same database."""
    from flask import Flask
    from integrations.social import auth as auth_mod
    from integrations.social import consent_api

    def _user(token):
        if not (isinstance(token, str) and token.startswith(_TOKEN)):
            return None, None
        return (SimpleNamespace(id=int(token[len(_TOKEN):]), is_admin=False,
                                is_moderator=False), factory())

    monkeypatch.setattr(auth_mod, '_get_user_from_token', _user)
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(consent_api.consent_bp)
    return app.test_client()


@contextlib.contextmanager
def _db(factory):
    db = factory()
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _page(client, path=''):
    resp = client.post(f'/api/social/consent{path}',
                       json={'consent_type': 'screen_capture', 'scope': '*'},
                       headers={'Authorization': f'Bearer {_TOKEN}{UID}'})
    assert resp.status_code in (200, 201), resp.get_data(as_text=True)
    return resp


def _check(factory, agent_id=None):
    from integrations.social.consent_service import ConsentService
    with _db(factory) as db:
        return ConsentService.check_consent(db, str(UID), 'screen_capture',
                                            agent_id=agent_id)


def _service(factory, method, **kw):
    from integrations.social.consent_service import ConsentService
    with _db(factory) as db:
        return getattr(ConsentService, method)(db, str(UID), 'screen_capture',
                                               **kw)


# ── the privacy page ─────────────────────────────────────────────────────

def test_a_revoke_from_the_privacy_page_ends_the_consent(client, factory):
    _page(client)
    assert _check(factory) is True
    _page(client, '/revoke')
    assert _check(factory) is False, (
        'the privacy page revoked the consent and the check still passes')


def test_a_page_revoke_ends_every_stacked_grant(client, factory):
    """Two Allow clicks are two rows; one Revoke must end both."""
    _page(client)
    _page(client)
    _page(client, '/revoke')
    assert _check(factory) is False, (
        'the older of two grants survived the revoke')


def test_a_page_revoke_ends_a_blanket_grant_for_every_agent(client, factory):
    _page(client)
    assert _check(factory, agent_id=AGENT) is True
    _page(client, '/revoke')
    assert _check(factory, agent_id=AGENT) is False, (
        "a revoked blanket grant still covers an agent (check_consent's "
        'blanket lookup)')


# ── ConsentService ───────────────────────────────────────────────────────

def test_a_revoke_after_an_ask_ends_the_consent(factory):
    _service(factory, 'request_consent')
    _service(factory, 'grant_consent')
    _service(factory, 'revoke_consent')
    assert _check(factory) is False, (
        'revoke_consent marked the pending ask and left the grant active')


def test_a_service_revoke_ends_every_stacked_grant(factory):
    _service(factory, 'grant_consent')
    _service(factory, 'grant_consent')
    _service(factory, 'revoke_consent')
    assert _check(factory) is False, (
        'the older of two grants survived the revoke')


# ── what must not change ─────────────────────────────────────────────────

def test_a_grant_after_a_revoke_passes_again(client, factory):
    _page(client)
    _page(client, '/revoke')
    _page(client)
    assert _check(factory) is True, 'a revoke must not be permanent'


def test_declining_an_ask_still_stops_the_reasks(factory):
    """revoke_consent on an ask nobody granted records the no, as it always
    did, so request_consent stops re-asking."""
    _service(factory, 'request_consent')
    row = _service(factory, 'revoke_consent')
    assert row is not None and row.revoked_at is not None
    with patch('integrations.social.consent_service._emit') as emit:
        _service(factory, 'request_consent')
    emit.assert_not_called()
