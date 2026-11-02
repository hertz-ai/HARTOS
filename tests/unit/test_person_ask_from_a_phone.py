"""A phone answers its person's ask on the desktop (person_ask, slice S3).

Owner 2026-10-29: "why is liquid ui not asking the user for clips to be
uploaded".  The phone answers with the token it signs with its own PeerLink
key; the desktop's API gate admits it for a phone the owner allowed
(security.middleware: ``g.auth_source == 'device'``, and a JSON body must
name the token's user).  The approval door names the answerer from that
verified identity (integrations.commerce.approvals.approver_from_request).
Before, the door read only a HARTOS or hive JWT, so an allowed phone's answer
was refused 403 not_yours and the ask stayed open (review of RN 8bf1da4a5,
probes A, B and E).  Off the LAN the same call rides the device link: an
alias row maps the phone's cloud call to this door
(integrations.mobile_adapter.aliases), and the adapter runs it through the
same gate as the device (tests/unit/test_mobile_adapter.py).

Real hart_intelligence_entry app, real gate, real consent rows; the card's
legs are recorded, not sent.

    python -m pytest tests/unit/test_person_ask_from_a_phone.py -q
"""
import os
import sys
import tempfile

os.environ['HEVOLVE_DB_PATH'] = ':memory:'
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from integrations.social.consent_service import ConsentService, device_scope  # noqa: E402
from integrations.social.models import Base, User, db_session, get_engine  # noqa: E402
from tests.unit.test_device_access_gate import Phone  # noqa: E402

LAN = {'REMOTE_ADDR': '192.168.0.164'}
OWNER = '10202'


@pytest.fixture(scope='module')
def hie():
    import logging
    root = logging.getLogger()
    saved = (root.level, list(root.handlers), logging.getLogRecordFactory())
    os.environ.setdefault('HEVOLVE_START_BACKGROUND_SERVICES', '0')
    import hart_intelligence_entry  # noqa: TID251 -- the door under test is on its app
    yield hart_intelligence_entry
    root.setLevel(saved[0])
    for h in list(root.handlers):
        if h not in saved[1]:
            root.removeHandler(h)
    logging.setLogRecordFactory(saved[2])


@pytest.fixture(autouse=True)
def _desktop(monkeypatch):
    monkeypatch.setenv('NUNBA_BUNDLED', '1')
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    for name in ('HEVOLVE_API_KEY', 'HEVOLVE_REQUIRE_AUTH', 'TRUSTED_PROXY', 'NUNBA_CI'):
        monkeypatch.delenv(name, raising=False)
    engine = get_engine()
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


def _allowed(user_id=OWNER):
    """A phone signed in as ``user_id``, its key allowed by the owner."""
    phone = Phone(user_id=user_id, username=f'p{user_id}')
    with db_session() as db:
        ConsentService.grant_consent(db, OWNER, 'device_access',
                                     scope=device_scope(phone.public_hex))
    return phone


@pytest.fixture
def asked(hie, monkeypatch):
    """One open approval ask to the owner, its legs recorded instead of sent."""
    import integrations.agent_engine.liquid_ui_service as lui
    import integrations.social.realtime as rt
    from integrations.agent_engine import person_ask
    monkeypatch.setattr(rt, 'publish_event', lambda *a, **k: None)
    monkeypatch.setattr(lui, 'push_agent_ui', lambda *a, **k: True)
    monkeypatch.setattr(lui, 'withdraw_agent_ui', lambda *a, **k: 0)
    with db_session() as db:
        if not db.query(User).filter_by(id=OWNER).first():
            db.add(User(id=OWNER, username='owner', display_name='Owner',
                        user_type='human', api_token='tok-owner'))
    with person_ask._lock:
        person_ask._asks.clear()
    out = person_ask.ask(OWNER, '342', {'type': 'approval', 'action': 'post_the_reel',
                                        'description': 'Post the reel today?'},
                         prompt_id='342')
    assert out['ok'], out
    yield out['ask_id']
    with person_ask._lock:
        person_ask._asks.clear()


def _answer(hie, monkeypatch, phone, body):
    """POST the phone's answer to the real door from the LAN, its device
    token as the Bearer."""
    monkeypatch.setitem(hie.app.config, 'PROPAGATE_EXCEPTIONS', True)
    secrets = {'security.secrets_manager': MagicMock(
        get_secret=lambda name: os.environ.get(name, ''))}
    with patch.dict(sys.modules, secrets):
        return hie.app.test_client().post(
            '/api/agent/approval', json=body, environ_base=LAN,
            headers={'Authorization': f'Bearer {phone.token()}'})


def test_an_allowed_phone_answers_its_persons_ask(hie, asked, monkeypatch):
    from integrations.agent_engine import person_ask
    r = _answer(hie, monkeypatch, _allowed(), {
        'ask_id': asked, 'answer': {'decision': 'approve'}, 'user_id': OWNER})
    assert r.status_code == 200, r.get_json()
    assert r.get_json() == {'status': 'ok', 'ask_id': asked}
    assert person_ask.ask_state(asked, OWNER) == {
        'status': 'answered', 'answer': {'decision': 'approve'}}


def test_the_phone_names_its_tokens_user_or_the_gate_refuses(hie, asked, monkeypatch):
    """The gate's rule for a device (#51): a JSON body names the token's user.
    The phone sends it; the door still answers as the token's user, never the
    body's (test_person_ask_route.test_a_body_user_id_never_answers_for_the_person)."""
    from integrations.agent_engine import person_ask
    r = _answer(hie, monkeypatch, _allowed(),
                {'ask_id': asked, 'answer': {'decision': 'approve'}})
    assert r.status_code == 403, r.get_json()
    assert person_ask.ask_state(asked, OWNER)['status'] == 'open'


def test_a_phone_signed_in_as_someone_else_cannot_answer(hie, asked, monkeypatch):
    from integrations.agent_engine import person_ask
    other = _allowed(user_id='777')
    r = _answer(hie, monkeypatch, other, {
        'ask_id': asked, 'answer': {'decision': 'approve'}, 'user_id': '777'})
    assert r.status_code == 403 and r.get_json()['reason'] == 'not_yours', r.get_json()
    assert person_ask.ask_state(asked, OWNER)['status'] == 'open'


def test_a_device_verdict_without_its_user_names_nobody(hie):
    """Fail closed: only the gate sets the device verdict, with its payload;
    a verdict without a user answers for nobody, never a body field."""
    from flask import g
    from integrations.commerce.approvals import approver_from_request
    with hie.app.test_request_context('/api/agent/approval', method='POST',
                                      json={'user_id': OWNER}):
        g.auth_source = 'device'
        g.jwt_payload = {}
        from flask import request
        assert approver_from_request(request) is None
        g.jwt_payload = {'user_id': OWNER}
        assert approver_from_request(request) == OWNER


def test_off_the_lan_the_phones_answer_rides_the_device_link(hie):
    """The phone sends its answer as the cloud call it already knows (POST
    https://azurekong.hertzai.com/api/agent/approval, contract.json); this
    desktop claims it and runs its own door."""
    from integrations.mobile_adapter import aliases
    url = f'https://{aliases.CLOUD}/api/agent/approval'
    assert aliases.resolve('POST', url) == '/api/agent/approval'
    rows = aliases.on_host(hie.app)
    assert any(r.path == '/api/agent/approval' for r in rows)
    assert f'POST {aliases.CLOUD} /api/agent/approval {aliases.SHORT_WAIT_S}' in \
        aliases.served(rows)
