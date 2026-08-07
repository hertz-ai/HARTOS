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
import base64
import hashlib
import hmac
import json
import os
import sys
import unittest
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


if __name__ == '__main__':
    unittest.main()
