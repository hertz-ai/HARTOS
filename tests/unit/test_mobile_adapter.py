"""A phone's cloud API call answered by its own desktop over the device link
(integrations.mobile_adapter).

The phone sends the call it would send the cloud as an ``api_request`` on
'dispatch': method, the cloud URL, the device token, headers and body.  A row
in aliases.ALIASES maps that cloud call to the desktop's own route; the
adapter runs the route in-process through the desktop's API gate as a remote
caller, so the gate's device rules hold (the owner's grant, the body's
user_id = the token's).  A call no row covers is ``not_here`` at once and the
phone sends it to the cloud.

Real handshake, real receive loop, real gate (security.middleware), real
consent rows; the host app is a small Flask app standing in for Nunba's.
"""
import base64
import json
import os
import sys
import threading
import types
from unittest.mock import MagicMock, patch

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest  # noqa: E402
from flask import Flask, g, jsonify, request  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.peer_link.channels import device_may_receive, device_may_send  # noqa: E402
from core.peer_link.link import LinkState, PeerLink, TrustLevel  # noqa: E402
from core.peer_link.link_manager import get_link_manager  # noqa: E402
from integrations.mobile_adapter import adapter, aliases  # noqa: E402
from security.middleware import _apply_api_auth  # noqa: E402
from tests.unit.test_device_access_gate import Phone  # noqa: E402
from tests.unit.test_desktop_socket_gate import _BOOT_STEPS  # noqa: E402
from tests.unit.test_peer_link_device_links import (  # noqa: E402,F401
    _accept, _allow, _desktop, _hello, _install_real_verifier, phone,
)

PHONE_USER = '40021'


class _Socket:
    """Feeds frames to the real receive loop, then holds the socket open
    until the link has answered (or a short wait passes), then drops."""

    def __init__(self, frames, expect_reply=True):
        self.frames = [json.dumps(f) for f in frames]
        self.sent = []
        self.replied = threading.Event()
        self.expect_reply = expect_reply

    def recv(self, timeout=None):
        if self.frames:
            return self.frames.pop(0)
        self.replied.wait(5 if self.expect_reply else 0.3)
        raise ConnectionResetError('done')

    def send(self, data):
        frame = json.loads(data.decode('utf-8') if isinstance(data, bytes) else data)
        self.sent.append(frame)
        if frame.get('re'):
            self.replied.set()

    def close(self):
        pass


def _serve(link, frames, expect_reply=True):
    """Run the link's real receive loop over ``frames``; return the frames
    the desktop sent back."""
    sock = _Socket(frames, expect_reply)
    link._ws = sock
    link._state = LinkState.CONNECTED
    link._receive_loop()
    return sock.sent

CLOUD = 'azurekong.hertzai.com'
ROW = aliases.Alias('POST', CLOUD, '/db/getprompt_userid', '/prompts/mine')
PREFIX = aliases.Alias('GET', 'mailer.hertzai.com', '/api/v1/books/', '/books/')


def _host_app():
    app = Flask('desktop')

    @app.route('/prompts/mine', methods=['POST'])
    def mine():
        return jsonify({'user': (getattr(g, 'jwt_payload', None) or {}).get('user_id'),
                        'auth': getattr(g, 'auth_source', None),
                        'asked': request.get_json(silent=True)})

    @app.route('/books/<book_id>/pages')
    def pages(book_id):
        return jsonify({'book': book_id, 'page': request.args.get('page')})

    @app.route('/big', methods=['POST'])
    def big():
        return 'x' * 2048
    _apply_api_auth(app)
    return app


@pytest.fixture(autouse=True)
def _no_host_app_after():
    yield
    adapter.set_host_app(None)


@pytest.fixture
def desktop_app(monkeypatch):
    monkeypatch.setenv('NUNBA_BUNDLED', '1')
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    for name in ('HEVOLVE_API_KEY', 'TRUSTED_PROXY', 'NUNBA_CI'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(aliases, 'ALIASES', (ROW, PREFIX))
    app = _host_app()
    secrets = {'security.secrets_manager': types.SimpleNamespace(get_secret=lambda name: '')}
    with patch.dict(sys.modules, secrets):
        assert adapter.install(app)
        yield app
    adapter.set_host_app(None)


def _device_link(phone):
    _allow(phone)
    _install_real_verifier()
    return _accept(_hello(phone, phone.token()))


def _api(phone, url, method='POST', body=None, token=None):
    raw = json.dumps(body).encode() if body is not None else b''
    return {'type': 'api_request', 'method': method, 'url': url,
            'device_token': phone.token() if token is None else token,
            'headers': {'Content-Type': 'application/json'},
            'body_b64': base64.b64encode(raw).decode()}


def _ask(link, d, rid='a-1', expect_reply=True):
    sent = _serve(link, [{'ch': 'dispatch', 'id': rid, 'rq': 1, 'd': d}], expect_reply)
    return [f['d'] for f in sent if f.get('re') == rid]


def _body(reply):
    return json.loads(base64.b64decode(reply['body_b64']))


def test_a_phone_may_send_its_api_requests_on_dispatch_and_nothing_else_there():
    """One door from a phone to its desktop: an api_request, as a request.
    The chat_request door it replaced is shut, and device_control (embedded
    nodes act on it on this channel) never opens to a phone."""
    assert device_may_send('dispatch', {'type': 'api_request'})
    assert not device_may_send('dispatch', {'type': 'chat_request'})
    assert not device_may_send('dispatch', {'type': 'device_control'})
    assert not device_may_send('dispatch', {'type': 'agent_task'})
    assert not device_may_send('dispatch', {})
    assert not device_may_send('dispatch', b'binary')
    assert not device_may_send('dispatch')
    assert not device_may_receive('dispatch')
    assert device_may_send('control')
    assert not device_may_send('compute', {'type': 'api_request'})


def test_a_phones_chat_request_starts_nothing_and_gets_no_reply(desktop_app, phone):
    link = _device_link(phone)
    turns = []
    get_link_manager().register_channel_handler('dispatch', lambda ch, d, p: turns.append(d) or {'x': 1})
    frame = {'type': 'chat_request', 'payload': {'text': 'teach me fractions', 'prompt_id': 54}}
    assert _ask(link, frame, expect_reply=False) == []
    assert turns == []


def test_a_phones_cloud_call_runs_the_desktops_own_route_as_the_phones_user(desktop_app, phone):
    link = _device_link(phone)
    replies = _ask(link, _api(phone, f'https://{CLOUD}/db/getprompt_userid',
                              body={'user_id': PHONE_USER, 'page': 1}))
    assert len(replies) == 1
    reply = replies[0]
    assert reply['type'] == 'api_reply' and reply['status'] == 200
    assert reply['content_type'].startswith('application/json')
    assert _body(reply) == {'user': PHONE_USER, 'auth': 'device',
                            'asked': {'user_id': PHONE_USER, 'page': 1}}


def test_a_prefix_row_carries_the_rest_of_the_path_and_the_query(desktop_app, phone):
    link = _device_link(phone)
    reply = _ask(link, _api(phone, 'https://mailer.hertzai.com/api/v1/books/b7/pages?page=3',
                            method='GET'))[0]
    assert reply['status'] == 200
    assert _body(reply) == {'book': 'b7', 'page': '3'}


def test_a_call_no_row_covers_is_not_here_at_once(desktop_app, phone):
    link = _device_link(phone)
    for url in (f'https://{CLOUD}/db/create_prompt',
                'https://elsewhere.example/db/getprompt_userid',
                'https://mailer.hertzai.com/api/v1/books/../../admin/x'):
        assert _ask(link, _api(phone, url, body={'user_id': PHONE_USER})) == [
            {'type': 'api_reply', 'not_here': True}]


def test_the_gate_still_decides_a_body_naming_another_user_is_refused(desktop_app, phone):
    link = _device_link(phone)
    reply = _ask(link, _api(phone, f'https://{CLOUD}/db/getprompt_userid',
                            body={'user_id': '99999'}))[0]
    assert reply['status'] == 403


def test_another_users_token_on_this_link_is_refused(desktop_app, phone):
    link = _device_link(phone)
    other = Phone(user_id='50050', username='Other')
    _allow(other)
    reply = _ask(link, _api(phone, f'https://{CLOUD}/db/getprompt_userid',
                            body={'user_id': '50050'}, token=other.token()))[0]
    assert reply['status'] == 403


def test_a_body_or_a_reply_too_big_for_the_link_stays_on_the_cloud(desktop_app, phone, monkeypatch):
    monkeypatch.setattr(aliases, 'ALIASES', (ROW, aliases.Alias('POST', CLOUD, '/big', '/big')))
    assert adapter.install(desktop_app)
    monkeypatch.setattr(adapter, 'MAX_BODY_BYTES', 1024)
    link = _device_link(phone)
    assert _ask(link, _api(phone, f'https://{CLOUD}/db/getprompt_userid',
                           body={'user_id': PHONE_USER, 'pad': 'y' * 2000})) == [
        {'type': 'api_reply', 'not_here': True}]
    assert _ask(link, _api(phone, f'https://{CLOUD}/big', body={'user_id': PHONE_USER}),
                rid='a-2') == [{'type': 'api_reply', 'not_here': True}]


def test_a_nodes_api_request_is_not_answered(desktop_app):
    node = PeerLink('node-7', '10.0.0.7:6777', TrustLevel.PEER)
    node._state = LinkState.CONNECTED
    get_link_manager()._links['node-7'] = node
    for channel, handlers in get_link_manager()._channel_handlers.items():
        for h in handlers:
            node.on_message(channel, h)
    d = {'type': 'api_request', 'method': 'POST', 'url': f'https://{CLOUD}/db/getprompt_userid'}
    assert _ask(node, d, expect_reply=False) == []


def test_a_row_whose_route_this_desktop_lacks_is_neither_claimed_nor_answered(
        desktop_app, phone, monkeypatch):
    """Standalone HARTOS has none of Nunba's routes: a row naming one is not
    in the handshake, and a phone that sends the call anyway hears
    not_here at once instead of a 404 from this desktop."""
    absent = aliases.Alias('POST', CLOUD, '/chat/teachme2', '/chat/teachme2')
    wrong_method = aliases.Alias('GET', CLOUD, '/db/getprompt_userid', '/prompts/mine')
    no_prefix = aliases.Alias('GET', 'mailer.hertzai.com', '/api/v1/shelves/', '/shelves/')
    monkeypatch.setattr(aliases, 'ALIASES', (ROW, absent, wrong_method, no_prefix, PREFIX))
    assert adapter.install(desktop_app)
    assert PeerLink._get_local_capabilities()['mobile_paths'] == [
        f'POST {CLOUD} /db/getprompt_userid 15', 'GET mailer.hertzai.com /api/v1/books/ 15']
    link = _device_link(phone)
    assert _ask(link, _api(phone, f'https://{CLOUD}/chat/teachme2',
                           body={'user_id': PHONE_USER})) == [
        {'type': 'api_reply', 'not_here': True}]
    assert _ask(link, _api(phone, f'https://{CLOUD}/db/getprompt_userid', method='GET'),
                rid='a-2') == [{'type': 'api_reply', 'not_here': True}]
    assert _ask(link, _api(phone, f'https://{CLOUD}/db/getprompt_userid',
                           body={'user_id': PHONE_USER}), rid='a-3')[0]['status'] == 200


def test_boot_claims_a_route_the_consumer_registered_during_boot(monkeypatch):
    """Nunba's routes are on the app by the time the adapter is installed:
    a row for a route registered in a boot route step is claimed."""
    import hartos.hartos_bootstrap as hb
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    monkeypatch.setattr(hb, '_BOOTSTRAP_DONE', hb._BOOTSTRAP_DONE)
    monkeypatch.setattr(aliases, 'ALIASES', (
        aliases.Alias('POST', CLOUD, '/chat/teachme2', '/chat/teachme2'),))

    def consumer_routes(app, cfg):
        app.add_url_rule('/chat/teachme2', 'teachme2', lambda: {'ok': True},
                         methods=['POST'])

    stubs = {s: MagicMock() for s in _BOOT_STEPS
             if s not in ('_run_consumer_hook', '_install_mobile_adapter')}
    secrets = {'security.secrets_manager': types.SimpleNamespace(get_secret=lambda name: '')}
    with patch.dict(sys.modules, secrets), patch.multiple(hb, **stubs),             patch.object(hb, '_run_consumer_hook', side_effect=consumer_routes):
        hb._run_bootstrap(Flask('nunba'), {})
    caps = PeerLink._get_local_capabilities()
    assert caps['mobile_paths'] == [f'POST {CLOUD} /chat/teachme2 15']
    assert 'api_request' in caps['device_requests']


def test_central_answers_no_phone_api_call(monkeypatch):
    import hartos.hartos_bootstrap as hb
    monkeypatch.setattr(aliases, 'ALIASES', (ROW,))
    with patch('security.key_delegation.get_node_tier', return_value='central'):
        hb._install_mobile_adapter(_host_app())
    caps = PeerLink._get_local_capabilities()
    assert 'mobile_paths' not in caps
    assert 'api_request' not in caps.get('device_requests', [])


def test_a_phones_call_is_a_foreground_turn_on_the_desktop(monkeypatch, phone):
    """Background agents yield to a turn the desktop's foreground rule counts
    (core.foreground.mark_view, the wrapper Nunba's teach / custom-bot routes
    carry).  The rule asks dispatch.is_genuine_user_request about the
    request's id; the stand-in check below reads the id the way
    hart_intelligence_entry's registered check does (header, then body).  The
    phone's teach body names no request_id, and its turn must still count."""
    from core import foreground
    from integrations.agent_engine.dispatch import is_genuine_user_request
    monkeypatch.setenv('NUNBA_BUNDLED', '1')
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    for name in ('HEVOLVE_API_KEY', 'TRUSTED_PROXY', 'NUNBA_CI'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(aliases, 'ALIASES', (
        aliases.Alias('POST', CLOUD, '/chat/teachme2', '/chat/teachme2', aliases.CHAT_WAIT_S),))
    app = Flask('nunba')
    seen = []

    def teach():
        seen.append(foreground.foreground_active())
        return jsonify({'text': 'ok'})
    app.add_url_rule('/chat/teachme2', 'teachme2', foreground.mark_view(teach), methods=['POST'])
    _apply_api_auth(app)

    def genuine():
        rid = (request.headers.get('X-HARTOS-Request-ID')
               or (request.get_json(silent=True) or {}).get('request_id'))
        return is_genuine_user_request(rid)
    monkeypatch.setattr(foreground, '_genuine_check', genuine)
    secrets = {'security.secrets_manager': types.SimpleNamespace(get_secret=lambda name: '')}
    with patch.dict(sys.modules, secrets):
        assert adapter.install(app)
        link = _device_link(phone)
        body = {'text': ['teach me fractions'], 'user_id': int(PHONE_USER), 'conversation_id': 'msg-1'}
        reply = _ask(link, _api(phone, f'https://{CLOUD}/chat/teachme2', body=body))[0]
    assert reply['status'] == 200
    assert seen == [True]


def test_the_handshake_names_what_this_desktop_answers(desktop_app):
    caps = PeerLink._get_local_capabilities()
    assert 'api_request' in caps['device_requests']
    assert caps['mobile_paths'] == [f'POST {CLOUD} /db/getprompt_userid 15',
                                    'GET mailer.hertzai.com /api/v1/books/ 15']


def test_a_phones_teach_and_custom_bot_turns_run_the_desktops_own_routes(monkeypatch, phone):
    """The shipped rows: the phone's Teach Yourself and custom-bot calls run
    this desktop's routes for central's contract, as the phone's user, the
    body as sent; the handshake names them with a chat turn's wait."""
    monkeypatch.setenv('NUNBA_BUNDLED', '1')
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    for name in ('HEVOLVE_API_KEY', 'TRUSTED_PROXY', 'NUNBA_CI'):
        monkeypatch.delenv(name, raising=False)
    app = Flask('nunba')
    seen = []

    def turn(kind):
        def view():
            seen.append((kind, request.get_json(), getattr(g, 'auth_source', None)))
            return jsonify({'text': f'{kind} answer', 'request_id': 'r-1'})
        return view
    app.add_url_rule('/chat/teachme2', 'teachme2', turn('teach'), methods=['POST'])
    app.add_url_rule('/chat/custom_gpt', 'custom_gpt', turn('custom'), methods=['POST'])
    _apply_api_auth(app)
    secrets = {'security.secrets_manager': types.SimpleNamespace(get_secret=lambda name: '')}
    with patch.dict(sys.modules, secrets):
        assert adapter.install(app)
        assert PeerLink._get_local_capabilities()['mobile_paths'] == [
            f'POST {CLOUD} /chat/teachme2 150', f'POST {CLOUD} /chat/custom_gpt 150']
        link = _device_link(phone)
        teach = {'text': ['teach me fractions'], 'user_id': int(PHONE_USER), 'teacher_avatar_id': 7}
        reply = _ask(link, _api(phone, f'https://{CLOUD}/chat/teachme2', body=teach))[0]
        assert reply['status'] == 200 and _body(reply) == {'text': 'teach answer', 'request_id': 'r-1'}
        custom = {'text': 'hi', 'user_id': int(PHONE_USER), 'prompt_id': 60834540771}
        reply = _ask(link, _api(phone, f'https://{CLOUD}/chat/custom_gpt', body=custom), rid='a-2')[0]
        assert reply['status'] == 200 and _body(reply)['text'] == 'custom answer'
    assert seen == [('teach', teach, 'device'), ('custom', custom, 'device')]


def test_a_phone_past_its_request_slots_hears_busy_at_once(desktop_app, phone):
    """N1: a device request past the link's slots is answered 'busy' at
    once -- the phone goes to the cloud -- never dropped unanswered."""
    link = _device_link(phone)
    link._request_slots = threading.BoundedSemaphore(1)
    link._request_slots.acquire()
    replies = _ask(link, _api(phone, f'https://{CLOUD}/db/getprompt_userid',
                              body={'user_id': PHONE_USER}))
    assert replies == [{'type': 'busy'}]


def test_a_node_past_its_request_slots_still_hears_nothing(desktop_app):
    """'busy' is a phone's: a node's callers read any reply as an answer and
    keep their own timeout and fallback, so a node past its slots is dropped
    as before."""
    node = PeerLink('node-7', '10.0.0.7:6777', TrustLevel.PEER)
    node._request_slots = threading.BoundedSemaphore(1)
    node._request_slots.acquire()
    node.on_message('dispatch', lambda ch, data, peer: {'ok': True})
    assert _ask(node, {'type': 'agent_task'}, expect_reply=False) == []


def test_an_encoded_dot_dot_never_walks_out_of_a_prefix_row(desktop_app, phone, monkeypatch):
    """The desktop decodes the path before it routes, so '%2e%2e' is '..' to
    the route a prefix row reaches: an encoded walk out of the prefix is
    not_here like a plain one, and the route never runs."""
    files = aliases.Alias('GET', 'mailer.hertzai.com', '/api/v1/files/', '/files/')
    got = []

    @desktop_app.route('/files/<path:rest>')
    def file_view(rest):
        got.append(rest)
        return jsonify({'rest': rest})
    monkeypatch.setattr(aliases, 'ALIASES', (ROW, files))
    assert adapter.install(desktop_app)
    link = _device_link(phone)
    for n, rest in enumerate(('a/%2e%2e/%2e%2e/admin', '%2E%2E/x', '..%2fadmin',
                              'a/%2e/b', 'a%2f%2fb', '..%5cadmin')):
        assert _ask(link, _api(phone, f'https://mailer.hertzai.com/api/v1/files/{rest}',
                               method='GET'), rid=f'w-{n}') == [
            {'type': 'api_reply', 'not_here': True}], rest
    assert got == []
    reply = _ask(link, _api(phone, 'https://mailer.hertzai.com/api/v1/files/a/b.txt',
                            method='GET'), rid='ok')[0]
    assert reply['status'] == 200 and _body(reply) == {'rest': 'a/b.txt'}


def test_each_phone_has_its_own_rate_budget_on_the_desktop(desktop_app, phone, monkeypatch):
    """A limiter keyed by the caller's address charges each phone its own
    budget, as each phone's own address did over HTTP: one phone past its
    limit leaves another of the same person's phones answered."""
    from security import rate_limiter_redis
    monkeypatch.setattr(rate_limiter_redis.RedisRateLimiter, '_init_redis', lambda self: None)
    monkeypatch.setattr(rate_limiter_redis, '_limiter', rate_limiter_redis.RedisRateLimiter())
    limit, _ = rate_limiter_redis.RedisRateLimiter.LIMITS['shell_power']

    @desktop_app.route('/power', methods=['POST'])
    @rate_limiter_redis.rate_limit('shell_power')
    def power():
        return jsonify({'ok': True})
    monkeypatch.setattr(aliases, 'ALIASES', (ROW, aliases.Alias('POST', CLOUD, '/power', '/power')))
    assert adapter.install(desktop_app)
    tablet = Phone(user_id=PHONE_USER, username='Sathish')

    def call(device, link, rid):
        return _ask(link, _api(device, f'https://{CLOUD}/power',
                               body={'user_id': PHONE_USER}), rid=rid)[0]['status']
    first = _device_link(phone)
    assert [call(phone, first, f'p-{n}') for n in range(limit + 1)] == [200] * limit + [429]
    second = _device_link(tablet)
    assert call(tablet, second, 't-0') == 200
