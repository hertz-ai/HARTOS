"""Sales and outreach messages reach the channel they are meant for.

Read 2026-10-21: the sales journey and the outreach tools looked the
adapter up with ``registry.get_adapter`` (ChannelRegistry has ``get``) and
scheduled the send on ``registry._loop`` (ChannelRegistry has no loop; the
loop is owned by FlaskChannelIntegration, tests/unit/test_channel_loop_owner).
So a journey step on Telegram or Discord never went out, an outreach email
never used the person's own email channel, the channel tool answered
"Message queued for delivery" for a message nobody queued, and the reply
hook that moves a prospect who answered never attached.

These tests run the real ChannelRegistry, the real FlaskChannelIntegration
over a running event loop, the real channel tools, the real journey engine
and the real outreach functions.  The adapters are ChannelAdapter
subclasses that record what they were asked to send, standing in for the
platforms.
"""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from integrations.channels.base import (
    ChannelAdapter, ChannelConfig, ChannelStatus, Message, SendResult,
)


class _Adapter(ChannelAdapter):
    """A connected channel that records each message it is asked to send."""

    def __init__(self, channel_name):
        super().__init__(ChannelConfig())
        self._channel_name = channel_name
        self._running = True
        self.status = ChannelStatus.CONNECTED
        self.sent = []

    @property
    def name(self):
        return self._channel_name

    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send_message(self, chat_id, text, reply_to=None, media=None, buttons=None):
        self.sent.append((chat_id, text))
        return SendResult(success=True, message_id='m1')

    async def edit_message(self, chat_id, message_id, text, buttons=None):
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id, message_id):
        return True

    async def send_typing(self, chat_id):
        return None

    async def get_chat_info(self, chat_id):
        return None


class _EmailAdapter(_Adapter):
    """The email channel: also sends a full email, as EmailAdapter does."""

    def __init__(self):
        super().__init__('email')
        self.emails = []

    async def send_email(self, to, subject, body, cc=None, bcc=None,
                         attachments=None, html_body=None):
        self.emails.append((to, subject, body, html_body))
        return SendResult(success=True, message_id='e1')


@pytest.fixture
def channels(monkeypatch):
    """A running channel integration over a fresh registry, installed as
    the process's own (get_registry / get_channel_integration)."""
    from integrations.channels import flask_integration
    from integrations.channels import registry as registry_mod
    from integrations.channels.registry import ChannelRegistry

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    registry = ChannelRegistry()
    integration = flask_integration.FlaskChannelIntegration.__new__(
        flask_integration.FlaskChannelIntegration)
    integration.registry = registry
    integration._loop = loop
    integration._thread = None
    monkeypatch.setattr(registry_mod, '_registry', registry)
    monkeypatch.setattr(flask_integration, '_integration', integration)
    yield SimpleNamespace(registry=registry, loop=loop, integration=integration)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


def _channel_tool(name, user_id=1):
    from integrations.channels.agent_tools import build_channel_tool_closures
    return next(t[2] for t in build_channel_tool_closures({'user_id': user_id})
                if t[0] == name)


# ── The sales journey ──────────────────────────────────────────────

def test_a_journey_step_reaches_the_prospects_chat_on_another_channel(channels):
    from integrations.agent_engine.journey_engine import JourneyEngine
    telegram = _Adapter('telegram')
    channels.registry.register(telegram)
    prospect = {'id': 'p1', 'email': 'ana@shop.example',
                'channel_ids': {'telegram': 'tg-ana'}}

    result = JourneyEngine().send_via_channel(prospect, 'hello Ana', 'telegram')

    assert telegram.sent == [('tg-ana', 'hello Ana')]
    assert result == {'success': True, 'channel': 'telegram'}


def test_a_journey_step_with_no_chat_for_that_channel_sends_nothing(channels):
    from integrations.agent_engine.journey_engine import JourneyEngine
    telegram = _Adapter('telegram')
    channels.registry.register(telegram)
    prospect = {'id': 'p1', 'email': 'ana@shop.example', 'channel_ids': {}}

    result = JourneyEngine().send_via_channel(prospect, 'hello Ana', 'telegram')

    assert telegram.sent == []
    assert result['success'] is False


# ── Outreach email ─────────────────────────────────────────────────

def test_outreach_email_goes_out_on_the_persons_email_channel(channels, monkeypatch):
    from core import http_pool
    from integrations.agent_engine import outreach_crm_tools
    mailer_calls = []
    monkeypatch.setattr(http_pool, 'pooled_post',
                        lambda *a, **k: mailer_calls.append((a, k)))
    email = _EmailAdapter()
    channels.registry.register(email)

    result = outreach_crm_tools._send_email(
        'ana@shop.example', 'Your store on McGroce', '<p>Hello Ana</p>')

    assert email.emails == [('ana@shop.example', 'Your store on McGroce',
                             'Hello Ana', '<p>Hello Ana</p>')]
    assert result['success'] is True and result['via'] == 'email_channel'
    assert mailer_calls == []


# ── A prospect's reply ─────────────────────────────────────────────

def _reply_from(sender, subject, text):
    """The Message EmailAdapter._process_email builds for an inbound email."""
    return Message(id='<reply-1@shop.example>', channel='email',
                   sender_id=sender, sender_name=sender, chat_id='thread-1',
                   text=text, raw={'subject': subject,
                                   'message_id': '<reply-1@shop.example>'})


def test_the_reply_hook_attaches_once_the_email_channel_is_up(channels, monkeypatch):
    """Boot wires the hook before channels are restored, so the outreach
    tick attaches it once the email channel exists, and only once."""
    from integrations.agent_engine import outreach_crm_tools
    replies = []
    monkeypatch.setattr(outreach_crm_tools, 'handle_inbound_email',
                        lambda *a, **k: replies.append(a))
    monkeypatch.setattr(outreach_crm_tools, '_check_pending_followups_impl',
                        lambda: '{"actions": [], "checked_at": "now"}')

    outreach_crm_tools.register_reply_handler()      # at boot: no email channel yet
    email = _EmailAdapter()
    channels.registry.register(email)
    outreach_crm_tools.check_pending_followups_daemon()
    outreach_crm_tools.check_pending_followups_daemon()

    asyncio.run_coroutine_threadsafe(
        email._dispatch_message(_reply_from(
            'ana@shop.example', 'Re: Your store on McGroce', 'Yes, tell me more')),
        channels.loop).result(timeout=5)

    assert replies == [('ana@shop.example', 'Re: Your store on McGroce',
                        'Yes, tell me more', '<reply-1@shop.example>')]


# ── The channel tools ──────────────────────────────────────────────

def test_send_to_channel_delivers_and_says_so(channels):
    telegram = _Adapter('telegram')
    channels.registry.register(telegram)

    out = _channel_tool('send_to_channel')('telegram', 'Your order is ready', 'tg-1')

    assert telegram.sent == [('tg-1', 'Your order is ready')]
    assert 'sent' in out.lower() and 'queued' not in out.lower(), out


def test_send_to_channel_never_claims_a_queue_that_does_not_exist(channels):
    channels.registry.register(_Adapter('telegram'))
    channels.integration._loop = None

    out = _channel_tool('send_to_channel')('telegram', 'Your order is ready', 'tg-1')

    assert 'queued' not in out.lower(), out
    assert 'not sent' in out.lower(), out
