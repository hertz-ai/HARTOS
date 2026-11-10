"""A phone answers its owner's ask on the desktop (person_ask, slice S3),
and nothing else through the approval door.

Owner 2026-10-29: "why is liquid ui not asking the user for clips to be
uploaded".  The phone answers with the token it signs with its own PeerLink
key.  The ask door checks that token itself
(integrations.social.auth.allowed_device_owner, on verify_device_jwt, the
API gate's own check) and takes the phone as this desktop's owner when the
owner allowed its key and it signed as them; any other phone names nobody.

Review of ec468c821 (REJECTED):
  C1  the door read the gate's verdict off flask.g, and on a desktop Nunba
      runs this node's routes in-process (routes/hartos_backend_adapter
      _dispatcher: test_client(), the Authorization header only, from this
      machine), so the verdict never reached the door: 403 not_yours.
  C2  a phone was whichever user its token named, on every branch of the
      door: an allowed phone signed as anyone approved their payments and
      store drafts.
  M1  the same rule let it read their mandates (GET /api/commerce/mandates).

Real hart_intelligence_entry app, real gate, real consent rows; the card's
legs and the drop event are recorded, not sent.

    python -m pytest tests/unit/test_person_ask_from_a_phone.py -q
"""
import os
import sys
import tempfile

os.environ['HEVOLVE_DB_PATH'] = ':memory:'
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from integrations.social.consent_service import ConsentService, device_scope  # noqa: E402
from integrations.social.models import (  # noqa: E402
    Base, User, UserConsent, db_session, get_engine)
from tests.unit.module_swap import swap_modules  # noqa: E402
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
    # A consent row's event-bus leg goes out to crossbar on a worker thread
    # (hart_intelligence_entry's HTTP transport); recorded nowhere, so no
    # publish is left in flight when the session ends (review of ec468c821).
    import core.platform.events as events
    monkeypatch.setattr(events, 'emit_event', lambda *a, **k: None)
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
def legs(hie, monkeypatch):
    """The card and the realtime events (the drop included), recorded instead
    of sent: nothing is left publishing when a test ends."""
    import integrations.agent_engine.liquid_ui_service as lui
    import integrations.social.realtime as rt
    from integrations.agent_engine import person_ask
    sent = {'events': []}
    monkeypatch.setattr(rt, 'publish_event', lambda *a, **k: None)
    monkeypatch.setattr(rt, 'on_notification',
                        lambda user_id, data, *a, **k: sent['events'].append((user_id, data)))
    monkeypatch.setattr(lui, 'push_agent_ui', lambda *a, **k: True)
    monkeypatch.setattr(lui, 'withdraw_agent_ui', lambda *a, **k: 0)
    with db_session() as db:
        for uid in (OWNER, '777'):
            if not db.query(User).filter_by(id=uid).first():
                db.add(User(id=uid, username=f'u{uid}', display_name=f'U{uid}',
                            user_type='human', api_token=f'tok-{uid}'))
    with person_ask._lock:
        person_ask._asks.clear()
    yield sent
    with person_ask._lock:
        person_ask._asks.clear()


def _ask(user_id=OWNER):
    from integrations.agent_engine import person_ask
    out = person_ask.ask(user_id, '342', {'type': 'approval', 'action': 'post_the_reel',
                                          'description': 'Post the reel today?'},
                         prompt_id='342')
    assert out['ok'], out
    return out['ask_id']


def _state(ask_id, user_id=OWNER):
    from integrations.agent_engine import person_ask
    return person_ask.ask_state(ask_id, user_id)['status']


def _call(hie, monkeypatch, phone, body=None, *, method='POST',
          path='/api/agent/approval', environ=LAN):
    """The phone's call to the real app with its device token as the Bearer:
    from the LAN by default; ``environ=None`` is Nunba's in-process hop
    (routes/hartos_backend_adapter _dispatcher: test_client() from this
    machine, the Authorization header and the JSON body only)."""
    monkeypatch.setitem(hie.app.config, 'PROPAGATE_EXCEPTIONS', True)
    secrets = {'security.secrets_manager': MagicMock(
        get_secret=lambda name: os.environ.get(name, ''))}
    kwargs = {'headers': {'Authorization': f'Bearer {phone.token()}'}}
    if body is not None:
        kwargs['json'] = body
    if environ is not None:
        kwargs['environ_base'] = environ
    with swap_modules(secrets):
        return hie.app.test_client().open(path, method=method, **kwargs)


def _yes(ask_id, user_id=OWNER):
    return {'ask_id': ask_id, 'answer': {'decision': 'approve'}, 'user_id': user_id}


def test_an_allowed_phone_answers_its_owners_ask(hie, legs, monkeypatch):
    asked = _ask()
    r = _call(hie, monkeypatch, _allowed(), _yes(asked))
    assert r.status_code == 200, r.get_json()
    assert r.get_json() == {'status': 'ok', 'ask_id': asked}
    from integrations.agent_engine import person_ask
    assert person_ask.ask_state(asked, OWNER) == {
        'status': 'answered', 'answer': {'decision': 'approve'}}
    assert (OWNER, {'type': 'ask.answered', 'ask_id': asked,
                    'msg_id': f'{asked}.answered'}) in legs['events']


def test_through_nunbas_in_process_hop_the_phone_still_answers(hie, legs, monkeypatch):
    """C1: on a desktop the call reaches this door through Nunba's dispatcher,
    as this machine and with nothing the gate set on g.  The door checks the
    phone's own token, so the answer lands (403 not_yours on ec468c821)."""
    asked = _ask()
    r = _call(hie, monkeypatch, _allowed(), _yes(asked), environ=None)
    assert r.status_code == 200, r.get_json()
    assert _state(asked) == 'answered'


def test_the_phone_names_its_tokens_user_or_the_gate_refuses(hie, legs, monkeypatch):
    """The gate's rule for a device (#51): a JSON body names the token's user."""
    asked = _ask()
    r = _call(hie, monkeypatch, _allowed(),
              {'ask_id': asked, 'answer': {'decision': 'approve'}})
    assert r.status_code == 403, r.get_json()
    assert _state(asked) == 'open'


@pytest.mark.parametrize('environ', [LAN, None], ids=['lan', 'nunba_hop'])
def test_a_phone_signed_in_as_someone_else_answers_nobodys_ask(hie, legs, monkeypatch,
                                                               environ):
    """C2: the token's user_id is whatever the phone signs.  A phone the owner
    allowed speaks for the owner alone: signed as 777 it answers neither the
    owner's ask nor 777's (on ec468c821 it answered 777's, 200)."""
    mine, theirs = _ask(), _ask('777')
    other = _allowed(user_id='777')
    for asked in (mine, theirs):
        r = _call(hie, monkeypatch, other, _yes(asked, '777'), environ=environ)
        assert r.status_code == 403 and r.get_json()['reason'] == 'not_yours', r.get_json()
    assert _state(mine) == 'open' and _state(theirs, '777') == 'open'


@pytest.mark.parametrize('environ', [LAN, None], ids=['lan', 'nunba_hop'])
def test_an_allowed_phone_approves_no_payment(hie, legs, monkeypatch, environ):
    """C2: a payment card's approver is a signed-in person; the owner's own
    phone names nobody there, and nothing is decided."""
    import integrations.ap2.ap2_mandate as ap2
    decided = []
    monkeypatch.setattr(ap2, 'decide_payment',
                        lambda *a, **k: decided.append(a) or ({'status': 'approved'}, 200))
    r = _call(hie, monkeypatch, _allowed(), {
        'agent_id': '342', 'action': 'ap2_pay:pay-1', 'decision': 'approve',
        'user_id': OWNER}, environ=environ)
    assert r.status_code == 401, r.get_json()
    assert r.get_json()['reason'] == 'sign in to answer this request'
    assert decided == []


def test_an_allowed_phone_reads_no_mandate(hie, legs, monkeypatch):
    """M1: GET carries no body, so the gate's body rule never ran; the
    mandate route's reader names nobody for a phone."""
    r = _call(hie, monkeypatch, _allowed(), method='GET',
              path='/api/commerce/mandates/m-1')
    assert r.status_code == 401, r.get_data(as_text=True)


def test_the_door_never_files_a_phones_ask(hie, legs):
    """allowed_device_owner only reads: a phone the owner has not answered
    about names nobody and no consent row is written (the gate files the
    one ask, paced, on the phone's first call)."""
    from integrations.social.auth import allowed_device_owner
    stranger = Phone(user_id=OWNER, username='stranger')
    assert allowed_device_owner(stranger.token()) is None
    with db_session() as db:
        assert db.query(UserConsent).count() == 0
    allowed = _allowed()
    assert allowed_device_owner(allowed.token()) == OWNER
    assert allowed_device_owner('not-a-token') is None
    assert allowed_device_owner('') is None


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
