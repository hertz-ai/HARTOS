"""A node reaches a person whose app is closed, through Firebase.

Owner, 23 Nov 2026: "when the app is not open the central or desktop flat
node shd be able to communicate with user via Firebase".  Measured:

  * the registry answers ``GET /get_fcm_token/<id>`` with a bare token string
    (Hevolve_Database crud.get_fcm_token), and the parse took only a dict, so
    every token pull read as "no token";
  * the bus counted a Crossbar publish as delivered the moment the worker took
    it, so hand_push_to_central said True for a post that failed (or went to
    a stub publisher that answered {"ok": True} and sent nothing);
  * a consent ask went data-only, which HyperOS drops for a stopped app, and
    no push named the screen its tap should open;
  * the drop of an answered ask was never handed to central's relay, although
    central carries it;
  * the direct send went to one token per person, so the owner's tablet and
    phone took the push from each other.

The contract with the phone (cointinue, 23 Nov 2026): data keys kind, ask_id,
request_id, prompt_id, node_id, all strings; android.notification.click_action
OPEN_PERSON_ASK / OPEN_CONSENT_ASK; android.notification.tag the ask's id.

    python -m pytest tests/unit/test_a_push_reaches_a_closed_app.py -q --noconftest
"""
import os
import sys
import textwrap
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import fcm_sync  # noqa: E402
from core.peer_link import message_bus  # noqa: E402

OWNER = '9003054371'


# ── 1. the registry's answer ────────────────────────────────────────────────

@pytest.mark.parametrize('payload, token', [
    ({'fcm_token': 'tok-a'}, 'tok-a'),
    ({'token': ' tok-b '}, 'tok-b'),
    ('tok-c', 'tok-c'),                 # what crud.get_fcm_token returns
    (' tok-d\n', 'tok-d'),
    ('', None),
    ('   ', None),
    (None, None),
    ({'detail': 'user not Found'}, None),
])
def test_the_registry_answer_is_read_in_every_shape_it_comes_in(payload, token):
    assert fcm_sync.parse_fcm_token_response(payload) == token


def test_a_device_list_is_read_once_per_token_in_order():
    assert fcm_sync.parse_fcm_tokens_response(
        ['phone', ' tablet ', 'phone', '', None, 7]) == ['phone', 'tablet']
    assert fcm_sync.parse_fcm_tokens_response('only') == ['only']
    assert fcm_sync.parse_fcm_tokens_response({'fcm_token': 'one'}) == ['one']
    assert fcm_sync.parse_fcm_tokens_response(None) == []
    assert fcm_sync.parse_fcm_tokens_response({'detail': 'user not Found'}) == []


def test_the_pull_asks_the_registry_for_every_device_of_the_person():
    resp = MagicMock(status_code=200)
    resp.json.return_value = ['phone', 'tablet']
    with patch('requests.get', return_value=resp) as get:
        assert fcm_sync.fetch_central_fcm_tokens(OWNER) == ['phone', 'tablet']
    url = get.call_args.args[0]
    assert url.endswith(f'/get_fcm_token/{OWNER}?devices=1')


def test_an_older_registry_that_answers_one_bare_token_still_counts():
    resp = MagicMock(status_code=200)
    resp.json.return_value = 'phone'
    with patch('requests.get', return_value=resp):
        assert fcm_sync.fetch_central_fcm_tokens(OWNER) == ['phone']


# ── 3. the message the phone gets ───────────────────────────────────────────

def _built(title, body, data):
    return fcm_sync.build_fcm_v1_message('tok', title, body, data=data)['message']


def test_a_person_ask_keeps_its_banner_and_names_the_screen_its_tap_opens():
    m = _built('Your agent asks', 'Which supplier?',
               {'type': 'ui_ask', 'request_id': 'ask-3', 'ask_id': 'ask-3',
                'prompt_id': 77, 'node_id': 'a1b2c3d4e5f60718'})
    assert m['notification'] == {'title': 'Your agent asks', 'body': 'Which supplier?'}
    assert m['android']['notification'] == {
        'tag': 'ask-3', 'click_action': 'com.hertzai.hevolve.OPEN_PERSON_ASK'}
    assert m['android']['priority'] == 'high'
    d = m['data']
    assert d['kind'] == 'ui_ask'
    assert (d['ask_id'], d['prompt_id'], d['node_id']) == ('ask-3', '77', 'a1b2c3d4e5f60718')
    assert all(isinstance(v, str) for v in d.values())


def test_a_consent_ask_now_has_a_banner_that_opens_the_consent_card():
    """HyperOS drops a data-only push to a stopped app, so the consent ask
    carries a notification block, and the tap opens ConsentAskActivity."""
    m = _built('An agent needs your OK', 'Enter the password',
               {'type': 'consent_prompt', 'request_id': 'row-7',
                'consent_type': 'credential', 'node_id': 'a1b2'})
    assert m['notification'] == {'title': 'An agent needs your OK',
                                 'body': 'Enter the password'}
    assert m['android']['notification'] == {
        'tag': 'row-7', 'click_action': 'com.hertzai.hevolve.OPEN_CONSENT_ASK'}
    assert m['data']['kind'] == 'consent_prompt'
    assert m['data']['request_id'] == 'row-7'


def test_the_drop_stays_silent_and_names_the_tag_of_the_ask_it_drops():
    m = _built('', '', {'type': 'ask.answered', 'request_id': 'ask-3',
                        'ask_id': 'ask-3'})
    assert 'notification' not in m
    assert 'notification' not in m['android']
    assert m['data']['kind'] == 'ask.answered'
    asked = _built('T', 'B', {'type': 'ui_ask', 'request_id': 'ask-3',
                              'ask_id': 'ask-3'})
    assert m['data']['request_id'] == asked['android']['notification']['tag']


def test_a_push_of_another_kind_names_no_screen():
    m = _built('HART', 'You have a new notification', {'topic': 't', 'msg_id': 'm'})
    assert 'notification' not in m['android']
    assert 'kind' not in m['data']


# ── 4. the drop goes to central's relay too ────────────────────────────────

def test_central_carries_the_drop_of_an_answered_ask():
    assert fcm_sync.relay_carries('', '', {'type': 'ask.answered', 'ask_id': 'a'})
    assert not fcm_sync.relay_carries('', '', {'type': 'other'})


# ── 2. delivered means posted ───────────────────────────────────────────────

def _resolved(value=None, error=None):
    f = Future()
    if error is not None:
        f.set_exception(error)
    else:
        f.set_result(value)
    return f


@pytest.fixture
def bus():
    message_bus.reset_message_bus()
    b = message_bus.get_message_bus()
    yield b
    message_bus.reset_message_bus()


@pytest.mark.parametrize('outcome, delivered', [
    (lambda: _resolved(True), True),
    (lambda: _resolved(False), False),                 # the post failed
    (lambda: _resolved(error=OSError('refused')), False),
    (lambda: Future(), False),                         # still queued
])
def test_a_crossbar_publish_counts_as_delivered_only_once_its_post_succeeded(
        bus, outcome, delivered):
    bus.set_http_transport(lambda topic, payload: outcome())
    msg_id = bus.publish('task.confirmation', {'request_id': 'r'},
                         user_id=OWNER, skip_sse=True, skip_peerlink=True)
    assert bus.crossbar_delivered(msg_id, timeout=0.05) is delivered
    assert bus.get_stats()['delivered_crossbar'] == (1 if delivered else 0)


def test_a_transport_that_raises_is_not_delivered(bus):
    def down(topic, payload):
        raise ConnectionError('no router')
    bus.set_http_transport(down)
    msg_id = bus.publish('task.confirmation', {'request_id': 'r'}, user_id=OWNER,
                         skip_sse=True, skip_peerlink=True)
    assert bus.crossbar_delivered(msg_id) is False
    assert bus.get_stats()['delivered_crossbar'] == 0


def test_an_unknown_message_was_not_delivered(bus):
    assert bus.crossbar_delivered('never-published') is False


def _handed(bus, outcome, data=None):
    bus.set_http_transport(lambda topic, payload: outcome)
    with patch.object(fcm_sync, 'resolve_central_id', return_value=None):
        return fcm_sync.hand_push_to_central(OWNER, 'T', 'B', data)


def test_a_relayed_push_whose_post_failed_is_reported_as_not_handed(bus):
    assert _handed(bus, _resolved(False)) is False


def test_a_relayed_push_whose_post_succeeded_is_reported_as_handed(bus):
    assert _handed(bus, _resolved(True)) is True


def test_the_drop_is_handed_to_central_with_the_ask_it_names(bus):
    sent = []
    bus.set_http_transport(lambda topic, payload: sent.append((topic, payload))
                           or _resolved(True))
    with patch.object(fcm_sync, 'resolve_central_id', return_value=None):
        ok = fcm_sync.hand_push_to_central(
            OWNER, '', '', {'type': 'ask.answered', 'request_id': 'ask-3',
                            'ask_id': 'ask-3'})
    assert ok is True
    import json
    (topic, payload), = sent
    assert topic == 'com.hertzai.hevolve.confirmation'
    push_data = json.loads(payload)['push_data']
    assert push_data['type'] == 'ask.answered' and push_data['ask_id'] == 'ask-3'


# ── 2. a stub publisher refuses loudly ─────────────────────────────────────

@pytest.fixture
def stub_on_path(tmp_path, monkeypatch):
    pkg = tmp_path / 'crossbarhttp'
    pkg.mkdir()
    (pkg / '__init__.py').write_text(textwrap.dedent('''
        class Client:
            def __init__(self, url=None, *args, **kwargs):
                self.url = url
            def publish(self, *a, **k): return {"ok": True}
    '''))
    saved = {k: v for k, v in sys.modules.items() if k.split('.')[0] == 'crossbarhttp'}
    for k in saved:
        del sys.modules[k]
    monkeypatch.syspath_prepend(str(tmp_path))
    yield pkg
    for k in [k for k in sys.modules if k.split('.')[0] == 'crossbarhttp']:
        del sys.modules[k]
    sys.modules.update(saved)


def test_a_stub_crossbarhttp_is_refused_at_import_with_its_reason(stub_on_path):
    from core.wamp_url import publish_client
    with pytest.raises(ImportError) as e:
        publish_client('http://127.0.0.1:9/publish')
    assert 'crossbarhttp3' in str(e.value)
    assert str(stub_on_path) in str(e.value)


def test_the_real_publisher_raises_on_a_failed_post():
    pytest.importorskip('crossbarhttp.crossbarhttp')
    from core.wamp_url import publish_client
    client = publish_client('http://127.0.0.1:9/publish', timeout=0.5)
    with pytest.raises(Exception):
        client.publish('com.hertzai.hevolve.confirmation', '{}')


# ── 7. every device of the person ──────────────────────────────────────────

def _direct(tokens, answers):
    """send_fcm_push with a credential: ``answers`` maps a token to the
    (status, body) Firebase gives it."""
    posted, dropped = [], []

    class R:
        def __init__(self, status, body):
            self.status_code, self._body, self.text = status, body, str(body)

        def json(self):
            return self._body

    def post(url, headers=None, json=None, timeout=None):
        tok = json['message']['token']
        posted.append(tok)
        return R(*answers[tok])

    with patch.object(fcm_sync, '_fcm_credential', return_value=('acc', 'proj')), \
            patch.object(fcm_sync, 'device_tokens', return_value=list(tokens)), \
            patch.object(fcm_sync, 'drop_fcm_token', side_effect=dropped.append), \
            patch.object(fcm_sync, 'hand_push_to_central', return_value=False) as h, \
            patch('requests.post', side_effect=post):
        ok = fcm_sync.send_fcm_push(OWNER, 'T', 'B', data={'type': 'ui_ask',
                                                          'ask_id': 'a'})
    return ok, posted, dropped, h


_UNREGISTERED = (404, {'error': {'code': 404, 'status': 'NOT_FOUND',
                                 'message': 'Requested entity was not found.',
                                 'details': [{'errorCode': 'UNREGISTERED'}]}})
_NOT_A_TOKEN = (400, {'error': {'code': 400, 'status': 'INVALID_ARGUMENT',
                                'message': 'The registration token is not a valid '
                                           'FCM registration token',
                                'details': [{'errorCode': 'INVALID_ARGUMENT'}]}})
_BAD_PAYLOAD = (400, {'error': {'code': 400, 'status': 'INVALID_ARGUMENT',
                                'message': 'Invalid value at message.data',
                                'details': [{'errorCode': 'INVALID_ARGUMENT'}]}})


def test_two_devices_of_one_person_both_get_the_push():
    ok, posted, dropped, h = _direct(['phone', 'tablet'],
                                     {'phone': (200, {}), 'tablet': (200, {})})
    assert ok is True
    assert posted == ['phone', 'tablet']
    assert dropped == []
    h.assert_not_called()


def test_a_token_firebase_no_longer_knows_is_removed_and_the_rest_still_get_it():
    ok, posted, dropped, _ = _direct(['stale', 'phone', 'junk'],
                                     {'stale': _UNREGISTERED, 'phone': (200, {}),
                                      'junk': _NOT_A_TOKEN})
    assert ok is True
    assert posted == ['stale', 'phone', 'junk']
    assert dropped == ['stale', 'junk']


def test_a_refused_payload_never_removes_a_good_token():
    ok, _, dropped, h = _direct(['phone'], {'phone': _BAD_PAYLOAD})
    assert ok is False
    assert dropped == []
    h.assert_called_once()           # nothing went direct: the relay is tried


def test_the_device_list_comes_from_the_registry_and_falls_back_to_the_cache():
    with patch.object(fcm_sync, 'resolve_central_id', return_value=None), \
            patch.object(fcm_sync, 'fetch_central_fcm_tokens',
                         return_value=['phone', 'tablet']), \
            patch.object(fcm_sync, 'store_local_fcm_tokens') as store:
        assert fcm_sync.device_tokens('uuid-1') == ['phone', 'tablet']
    store.assert_called_once()
    with patch.object(fcm_sync, 'resolve_central_id', return_value=None), \
            patch.object(fcm_sync, 'fetch_central_fcm_tokens', return_value=[]), \
            patch.object(fcm_sync, 'get_local_fcm_tokens', return_value=['cached']):
        assert fcm_sync.device_tokens('uuid-1') == ['cached']


def test_a_list_fetched_moments_ago_serves_the_next_push_without_a_round_trip():
    from integrations.social.models import Base, get_engine
    Base.metadata.create_all(get_engine())
    fetched = []
    with patch.object(fcm_sync, 'resolve_central_id', return_value=None), \
            patch.object(fcm_sync, 'fetch_central_fcm_tokens',
                         side_effect=lambda rid: fetched.append(rid) or ['phone', 'tablet']):
        assert fcm_sync.device_tokens('uuid-f') == ['phone', 'tablet']
        assert fcm_sync.device_tokens('uuid-f') == ['phone', 'tablet']
        assert fetched == ['uuid-f']                  # one round-trip
        with patch.object(fcm_sync, 'DEVICE_LIST_FRESH_S', 0):
            fcm_sync.device_tokens('uuid-f')
        assert fetched == ['uuid-f', 'uuid-f']        # stale: asked again


def test_the_local_cache_keeps_every_device_and_forgets_a_dropped_one():
    from sqlalchemy import text
    from integrations.social.models import Base, db_session, get_engine
    Base.metadata.create_all(get_engine())
    assert fcm_sync.store_local_fcm_tokens('uuid-c', ['phone', 'tablet', 'phone'])
    assert fcm_sync.get_local_fcm_tokens('uuid-c') == ['phone', 'tablet']
    assert fcm_sync.get_local_fcm_token('uuid-c') == 'phone'
    # a profile pull brings the per-user slot's one token: it goes first and
    # the other devices stay
    assert fcm_sync.store_local_fcm_token('uuid-c', 'new-phone')
    assert fcm_sync.get_local_fcm_tokens('uuid-c') == ['new-phone', 'phone', 'tablet']
    fcm_sync._forget_local_token('tablet')
    assert fcm_sync.get_local_fcm_tokens('uuid-c') == ['new-phone', 'phone']
    # a row an older build cached holds one bare token
    with db_session() as db:
        db.execute(text(fcm_sync._UPSERT), {'u': 'uuid-old', 't': 'bare', 's': ''})
    assert fcm_sync.get_local_fcm_tokens('uuid-old') == ['bare']


def test_a_token_is_dropped_at_the_registry_and_from_the_local_cache():
    with patch('requests.post') as post, \
            patch.object(fcm_sync, '_forget_local_token') as forget:
        fcm_sync.drop_fcm_token('stale')
    assert post.call_args.args[0].endswith('/delete_fcm_token')
    assert post.call_args.kwargs['json'] == {'token': 'stale'}
    forget.assert_called_once_with('stale')
