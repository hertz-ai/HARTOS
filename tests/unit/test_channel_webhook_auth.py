"""The inbound-channel webhook must not be an open door into agent dispatch.

WHY THIS EXISTS
───────────────
PR #106 added `POST /channels/webhook/<channel_type>`, which hands the request
body to the channel adapter's `handle_webhook`, which parses it and dispatches
it to the agent as a user message. That route is public by nature — Meta, LINE
and Viber dial it from their own infrastructure and cannot hold a HART
credential.

The adapters cannot be trusted to authenticate it. Their own check is

    if signature and not self.verify_signature(...):   # messenger_adapter.py
        return

which SKIPS verification entirely when the header is absent, and zalo's
`handle_webhook(self, body: Dict)` takes no signature parameter at all. So
before this gate, an unsigned POST with arbitrary JSON was parsed and
dispatched to the agent as a genuine user message.

THE CONTRACT
────────────
Two accepted proofs, and nothing else gets through:

  1. Kong authenticated the caller — HEVOLVE_TRUST_KONG=true and Kong's
     X-Consumer-* stamp present. Same flag integrations/social/auth.py already
     uses, so there is ONE answer to "did the gateway vouch for this caller".
  2. A valid provider HMAC over the raw body, hex (Meta) or base64 (LINE).

Anything else is 401 BEFORE the adapter is touched.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

BODY = json.dumps({"object": "page", "entry": [{"messaging": [{"x": 1}]}]})
SECRET = "test-app-secret"


def _client(with_adapter=True):
    """A Flask app carrying ONLY the channel webhook route."""
    from flask import Flask
    from integrations.channels.flask_integration import FlaskChannelIntegration

    app = Flask(__name__)
    app.config['TESTING'] = True

    mgr = FlaskChannelIntegration.__new__(FlaskChannelIntegration)     # no __init__ side effects
    mgr.registry = MagicMock()
    mgr._loop = MagicMock() if with_adapter else None
    adapter = MagicMock()

    async def _hw(*a, **k):
        return None
    adapter.handle_webhook = _hw
    mgr.registry.get.return_value = adapter if with_adapter else None

    mgr.register_webhook_routes(app)
    return app.test_client(), adapter


def _sig(secret, body, algo=hashlib.sha256, b64=False):
    mac = hmac.new(secret.encode(), body.encode(), algo)
    return base64.b64encode(mac.digest()).decode() if b64 else mac.hexdigest()


class WebhookRefusesUnauthenticatedCallers(unittest.TestCase):

    def setUp(self):
        for k in list(os.environ):
            if 'MESSENGER' in k or k == 'HEVOLVE_TRUST_KONG':
                os.environ.pop(k, None)

    def test_an_UNSIGNED_post_is_rejected(self):
        """The exact injection: arbitrary JSON, no signature, no Kong."""
        c, adapter = _client()
        r = c.post('/channels/webhook/messenger', data=BODY,
                   content_type='application/json')
        self.assertEqual(401, r.status_code,
                         "an unsigned webhook reached the adapter — this is "
                         "unauthenticated injection into agent dispatch")

    def test_a_WRONG_signature_is_rejected(self):
        os.environ['MESSENGER_APP_SECRET'] = SECRET
        c, _ = _client()
        r = c.post('/channels/webhook/messenger', data=BODY,
                   content_type='application/json',
                   headers={'X-Hub-Signature-256': 'sha256=' + 'de' * 32})
        self.assertEqual(401, r.status_code)

    def test_a_VALID_meta_hex_signature_is_accepted(self):
        os.environ['MESSENGER_APP_SECRET'] = SECRET
        c, _ = _client()
        r = c.post('/channels/webhook/messenger', data=BODY,
                   content_type='application/json',
                   headers={'X-Hub-Signature-256':
                            'sha256=' + _sig(SECRET, BODY)})
        self.assertNotEqual(401, r.status_code,
                            "a correctly-signed provider webhook was refused")

    def test_a_VALID_line_base64_signature_is_accepted(self):
        """LINE sends base64, not hex — both must work."""
        os.environ['LINE_CHANNEL_SECRET'] = SECRET
        c, _ = _client()
        r = c.post('/channels/webhook/line', data=BODY,
                   content_type='application/json',
                   headers={'X-Line-Signature': _sig(SECRET, BODY, b64=True)})
        self.assertNotEqual(401, r.status_code)
        os.environ.pop('LINE_CHANNEL_SECRET', None)

    def test_a_KONG_authenticated_consumer_is_accepted(self):
        """Kong fronted the call and vouched for the caller."""
        os.environ['HEVOLVE_TRUST_KONG'] = 'true'
        c, _ = _client()
        r = c.post('/channels/webhook/messenger', data=BODY,
                   content_type='application/json',
                   headers={'X-Consumer-ID': 'kong-consumer-abc'})
        self.assertNotEqual(401, r.status_code,
                            "Kong stamped the caller and it was still refused")

    def test_kong_headers_are_IGNORED_when_the_trust_flag_is_off(self):
        """Otherwise anyone could forge X-Consumer-ID and walk in."""
        os.environ.pop('HEVOLVE_TRUST_KONG', None)
        c, _ = _client()
        r = c.post('/channels/webhook/messenger', data=BODY,
                   content_type='application/json',
                   headers={'X-Consumer-ID': 'forged'})
        self.assertEqual(401, r.status_code,
                         "X-Consumer-ID was trusted without HEVOLVE_TRUST_KONG "
                         "— a forged header would authenticate any caller")

    def test_rejection_happens_BEFORE_the_adapter_is_touched(self):
        """A 401 must not have already dispatched to the agent."""
        c, adapter = _client()
        called = {}

        async def _spy(*a, **k):
            called['yes'] = True
        adapter.handle_webhook = _spy
        c.post('/channels/webhook/messenger', data=BODY,
               content_type='application/json')
        self.assertNotIn('yes', called,
                         "the adapter ran despite the request being refused")


class TheGetHandshakeStillWorks(unittest.TestCase):
    """The gate must not break Meta's subscribe verification."""

    def test_correct_verify_token_echoes_the_challenge(self):
        os.environ['MESSENGER_VERIFY_TOKEN'] = 'vt-123'
        c, _ = _client()
        r = c.get('/channels/webhook/messenger?hub.mode=subscribe'
                  '&hub.verify_token=vt-123&hub.challenge=echo-me')
        self.assertEqual(200, r.status_code)
        self.assertEqual('echo-me', r.get_data(as_text=True))
        os.environ.pop('MESSENGER_VERIFY_TOKEN', None)

    def test_wrong_verify_token_is_refused(self):
        os.environ['MESSENGER_VERIFY_TOKEN'] = 'vt-123'
        c, _ = _client()
        r = c.get('/channels/webhook/messenger?hub.mode=subscribe'
                  '&hub.verify_token=WRONG&hub.challenge=echo-me')
        self.assertEqual(403, r.status_code)
        os.environ.pop('MESSENGER_VERIFY_TOKEN', None)


SEND_BODY = json.dumps({"channel": "telegram", "chat_id": "victim-999",
                        "text": "you have won a prize"})


class _RealLoop:
    """A genuine asyncio loop on a background thread, so the route's real
    ``asyncio.run_coroutine_threadsafe`` bridge is exercised end to end
    (the registry is the only thing mocked)."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


def _send_client(loop=None, send_impl=None, calls=None):
    """A Flask app carrying the REAL ``/channels/send`` route.

    ``init_channels`` registers the actual route (closing over the integration
    instance); we build that instance with a light ``__init__`` so the route's
    two boundaries — ``integration._loop`` and
    ``integration.registry.send_to_channel`` — are the only mocked surface.
    Returns (test_client, integration, calls) where ``calls`` records every
    (channel, chat_id, text) actually relayed.
    """
    from flask import Flask
    from integrations.channels import flask_integration as fi

    if calls is None:
        calls = []
    if send_impl is None:
        async def send_impl(channel, chat_id, text, **kw):
            calls.append((channel, chat_id, text))
            return SimpleNamespace(success=True, message_id="mid-1", error=None)

    def _light_init(self, *a, **k):
        self._loop = loop
        self.registry = MagicMock()
        self.registry.send_to_channel = send_impl

    app = Flask(__name__)
    app.config['TESTING'] = True
    with patch.object(fi.FlaskChannelIntegration, '__init__', _light_init):
        integration = fi.init_channels(app)
    # init_channels used the patched __init__; make the boundary explicit.
    integration._loop = loop
    integration.registry.send_to_channel = send_impl
    return app.test_client(), integration, calls


def _clear_send_env():
    for k in ('NUNBA_BUNDLED', 'HEVOLVE_API_KEY', 'HEVOLVE_TRUST_KONG'):
        os.environ.pop(k, None)


class RegisterStatusRoutesOnExistingIntegration(unittest.TestCase):
    """standalone main() reaches channels via get_channel_integration()
    (get-or-create the module singleton), never init_channels() — so
    GET /channels/status and POST /channels/send, which used to be wired up
    ONLY inside init_channels()'s closure, 404'd on that boot path even
    though the integration itself was live (same "standalone launcher never
    did X" gap as web-channel registration and the webhook routes).

    register_status_routes(app, integration) fixes it by wiring those two
    routes onto an EXISTING integration instance. The property that matters:
    it must NOT go through init_channels()'s constructor path, which would
    build a second, separate FlaskChannelIntegration and silently orphan
    whatever the passed-in one already had running.
    """

    def test_status_route_reflects_the_passed_in_integration(self):
        from flask import Flask
        from integrations.channels import flask_integration as fi

        app = Flask(__name__)
        app.config['TESTING'] = True

        integration = fi.FlaskChannelIntegration.__new__(fi.FlaskChannelIntegration)
        integration.get_status = lambda: {'discord': 'connected'}

        fi.register_status_routes(app, integration)
        resp = app.test_client().get('/channels/status')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {'discord': 'connected'})

    def test_does_not_touch_the_module_singleton(self):
        """The whole point: this must be safe to call on an integration a
        caller already holds (e.g. get_channel_integration()'s return
        value), without init_channels()'s side effect of overwriting
        integrations.channels.flask_integration._integration."""
        from flask import Flask
        from integrations.channels import flask_integration as fi

        sentinel = object()
        fi._integration = sentinel
        try:
            app = Flask(__name__)
            other_integration = fi.FlaskChannelIntegration.__new__(
                fi.FlaskChannelIntegration)
            other_integration.get_status = lambda: {}

            fi.register_status_routes(app, other_integration)

            self.assertIs(
                fi._integration, sentinel,
                "register_status_routes must not replace the module "
                "singleton — that would orphan whatever it already had "
                "running, the exact bug this function exists to avoid.")
        finally:
            fi._integration = None

    def test_noop_when_app_is_none(self):
        from integrations.channels import flask_integration as fi

        integration = fi.FlaskChannelIntegration.__new__(fi.FlaskChannelIntegration)
        fi.register_status_routes(None, integration)  # must not raise


class ChannelSendRefusesUnauthenticatedCallers(unittest.TestCase):
    """POST /channels/send is an outbound relay: unauthenticated it lets any
    reachable caller send a message through any registered channel to any
    chat_id. Unlike /channels/webhook it shipped with NO gate and sits in
    neither middleware protected tuple, so it was public on every tier."""

    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ('NUNBA_BUNDLED', 'HEVOLVE_API_KEY',
                                 'HEVOLVE_TRUST_KONG')}
        _clear_send_env()
        self._rl = _RealLoop()

    def tearDown(self):
        self._rl.stop()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_an_UNAUTHENTICATED_send_is_rejected_and_relays_nothing(self):
        """The exact abuse: valid outbound payload, no credential of any kind.
        Must 401 AND never reach the channel registry."""
        c, _integ, calls = _send_client(loop=self._rl.loop)
        r = c.post('/channels/send', data=SEND_BODY,
                   content_type='application/json')
        self.assertEqual(401, r.status_code,
                         "an unauthenticated caller relayed an outbound "
                         "message — spoof/spam relay into any channel")
        self.assertEqual([], calls,
                         "the message was sent to the channel despite the "
                         "request being unauthenticated")

    def test_a_valid_API_KEY_is_accepted_and_relays(self):
        os.environ['HEVOLVE_API_KEY'] = 'super-secret-key'
        c, _integ, calls = _send_client(loop=self._rl.loop)
        r = c.post('/channels/send', data=SEND_BODY,
                   content_type='application/json',
                   headers={'X-API-Key': 'super-secret-key'})
        self.assertNotEqual(401, r.status_code,
                            "a correctly keyed operator call was refused")
        self.assertEqual([('telegram', 'victim-999', 'you have won a prize')],
                         calls)

    def test_a_WRONG_API_KEY_is_rejected(self):
        os.environ['HEVOLVE_API_KEY'] = 'super-secret-key'
        c, _integ, calls = _send_client(loop=self._rl.loop)
        r = c.post('/channels/send', data=SEND_BODY,
                   content_type='application/json',
                   headers={'X-API-Key': 'not-the-key'})
        self.assertEqual(401, r.status_code)
        self.assertEqual([], calls)

    def test_a_valid_BEARER_jwt_is_accepted(self):
        """Reuses the canonical integrations.social.auth.decode_jwt verifier."""
        with patch('integrations.social.auth.decode_jwt',
                   return_value={'user_id': '10077', 'scope': 'local'}):
            c, _integ, calls = _send_client(loop=self._rl.loop)
            r = c.post('/channels/send', data=SEND_BODY,
                       content_type='application/json',
                       headers={'Authorization': 'Bearer good.jwt.token'})
        self.assertNotEqual(401, r.status_code)
        self.assertEqual(1, len(calls))

    def test_a_GARBAGE_bearer_is_rejected(self):
        """No patch — the real decoder returns {} for a non-JWT string."""
        c, _integ, calls = _send_client(loop=self._rl.loop)
        r = c.post('/channels/send', data=SEND_BODY,
                   content_type='application/json',
                   headers={'Authorization': 'Bearer not-a-real-jwt'})
        self.assertEqual(401, r.status_code)
        self.assertEqual([], calls)

    def test_a_KONG_consumer_is_accepted_when_trusted(self):
        os.environ['HEVOLVE_TRUST_KONG'] = 'true'
        c, _integ, calls = _send_client(loop=self._rl.loop)
        r = c.post('/channels/send', data=SEND_BODY,
                   content_type='application/json',
                   headers={'X-Consumer-ID': 'kong-consumer-1'})
        self.assertNotEqual(401, r.status_code)
        self.assertEqual(1, len(calls))

    def test_kong_headers_are_IGNORED_when_the_trust_flag_is_off(self):
        """Otherwise anyone could forge X-Consumer-ID and relay at will."""
        c, _integ, calls = _send_client(loop=self._rl.loop)
        r = c.post('/channels/send', data=SEND_BODY,
                   content_type='application/json',
                   headers={'X-Consumer-ID': 'forged'})
        self.assertEqual(401, r.status_code,
                         "a forged X-Consumer-ID authenticated without "
                         "HEVOLVE_TRUST_KONG")
        self.assertEqual([], calls)

    def test_bundled_desktop_is_trusted(self):
        """NUNBA_BUNDLED is the single-user in-process desktop — the same
        mode the security middleware early-returns for. It must keep working
        with no credential (the desktop UI drives this route directly)."""
        os.environ['NUNBA_BUNDLED'] = '1'
        c, _integ, calls = _send_client(loop=self._rl.loop)
        r = c.post('/channels/send', data=SEND_BODY,
                   content_type='application/json')
        self.assertNotEqual(401, r.status_code,
                            "bundled single-user desktop was locked out")
        self.assertEqual(1, len(calls))


class ChannelSendBehaviour(unittest.TestCase):
    """The route's non-auth contract: validation, the async bridge, the
    not-running degrade path, and result pass-through. Exercised under the
    bundled trust flag so auth is out of the way."""

    def setUp(self):
        self._saved = os.environ.get('NUNBA_BUNDLED')
        os.environ['NUNBA_BUNDLED'] = '1'       # bypass auth for logic tests
        self._rl = None

    def tearDown(self):
        if self._rl is not None:
            self._rl.stop()
        if self._saved is None:
            os.environ.pop('NUNBA_BUNDLED', None)
        else:
            os.environ['NUNBA_BUNDLED'] = self._saved

    def test_missing_all_required_fields_is_400(self):
        self._rl = _RealLoop()
        c, _integ, calls = _send_client(loop=self._rl.loop)
        r = c.post('/channels/send', data=json.dumps({}),
                   content_type='application/json')
        self.assertEqual(400, r.status_code)
        self.assertEqual([], calls)

    def test_missing_one_field_is_400(self):
        self._rl = _RealLoop()
        c, _integ, calls = _send_client(loop=self._rl.loop)
        r = c.post('/channels/send',
                   data=json.dumps({"channel": "telegram", "chat_id": "5"}),
                   content_type='application/json')  # no text
        self.assertEqual(400, r.status_code)
        self.assertEqual([], calls)

    def test_empty_string_text_is_treated_as_missing(self):
        """all([...]) makes an empty string falsy — an empty message must not
        be relayed as a valid send."""
        self._rl = _RealLoop()
        c, _integ, calls = _send_client(loop=self._rl.loop)
        r = c.post('/channels/send',
                   data=json.dumps({"channel": "telegram",
                                    "chat_id": "5", "text": ""}),
                   content_type='application/json')
        self.assertEqual(400, r.status_code)
        self.assertEqual([], calls)

    def test_channels_not_running_is_503(self):
        """No event loop → the adapters aren't up; degrade to 503, don't crash."""
        c, _integ, calls = _send_client(loop=None)   # _loop is None
        r = c.post('/channels/send', data=SEND_BODY,
                   content_type='application/json')
        self.assertEqual(503, r.status_code)
        self.assertEqual([], calls)

    def test_happy_path_relays_exact_args_and_returns_result(self):
        self._rl = _RealLoop()
        c, _integ, calls = _send_client(loop=self._rl.loop)
        r = c.post('/channels/send', data=SEND_BODY,
                   content_type='application/json')
        self.assertEqual(200, r.status_code)
        body = r.get_json()
        self.assertTrue(body['success'])
        self.assertEqual('mid-1', body['message_id'])
        self.assertIsNone(body['error'])
        # The security-relevant observable: the caller-supplied channel +
        # chat_id + text are exactly what got relayed.
        self.assertEqual(
            [('telegram', 'victim-999', 'you have won a prize')], calls)

    def test_a_failed_send_result_is_passed_through(self):
        self._rl = _RealLoop()

        async def _fail(channel, chat_id, text, **kw):
            return SimpleNamespace(success=False, message_id=None,
                                   error="unknown channel")
        c, _integ, _calls = _send_client(loop=self._rl.loop, send_impl=_fail)
        r = c.post('/channels/send', data=SEND_BODY,
                   content_type='application/json')
        self.assertEqual(200, r.status_code)
        body = r.get_json()
        self.assertFalse(body['success'])
        self.assertEqual("unknown channel", body['error'])


if __name__ == '__main__':
    unittest.main()
