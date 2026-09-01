"""Regression test for the WhatsApp outbound send-field bug (2026-09-01).

Root cause: WhatsAppAdapter.send_message posted {"chatId": ...} but the
embedded Baileys gateway's POST /api/sessions/:id/messages/send route reads
{"to", "text"} and returns 400 "to + text required" without `to`
(integrations/channels/whatsapp/gateway.js:356-363), then does
sock.sendMessage(to, {text}).  So every agent auto-reply routed through
ChannelRegistry._route_to_agent -> adapter.send_message silently 400'd at
the gateway: inbound reached /chat, the agent ran, but the reply never
sent.  This is the outbound half of "connected but the agent never
replies" (the inbound half was fixed 2026-07-21 in test_whatsapp_live_adapter).

Fix: send_message now sends `to` (a JID).  Inbound chat_id already IS a JID
(gateway sets chat.id._serialized = key.remoteJid); a bare number is
normalised to <digits>@s.whatsapp.net exactly like
integrations/social/api_channels.py's whatsapp send helper (:509-513).

These tests pin the payload contract by capturing the JSON body posted to
the gateway.
"""
import asyncio
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.channels.whatsapp_adapter import create_whatsapp_adapter


class _FakeResp:
    """Minimal aiohttp-response-shaped async context manager."""
    status = 200

    async def json(self):
        return {"messageId": "wamid.TEST"}

    async def text(self):
        return ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


def _adapter_with_capture():
    """Adapter whose _session.post captures the posted JSON body."""
    adapter = create_whatsapp_adapter(
        api_url="http://127.0.0.1:3000", account_id="user_1",
    )
    adapter._authenticated = True
    captured = {}

    def _post(url, json=None, **_kw):
        captured["url"] = url
        captured["json"] = json
        return _FakeResp()

    session = MagicMock()
    session.post = _post
    adapter._session = session
    return adapter, captured


def test_send_message_posts_to_jid_for_gateway():
    """The gateway requires `to` (a JID); a reply on an inbound JID chat_id
    must go out with to == that JID (chatId alone is ignored → 400)."""
    adapter, captured = _adapter_with_capture()
    jid = "919876543210@s.whatsapp.net"

    result = asyncio.run(adapter.send_message(jid, "your receipt is ready"))

    assert result.success is True
    assert captured["json"]["to"] == jid
    assert captured["json"]["text"] == "your receipt is ready"
    assert captured["url"].endswith("/api/sessions/user_1/messages/send")


def test_send_message_normalises_bare_number_to_jid():
    """A bare phone number (e.g. a manual send_to_channel) is normalised to
    a JID the same way api_channels.whatsapp send does."""
    adapter, captured = _adapter_with_capture()

    asyncio.run(adapter.send_message("+91 98765 43210", "hi"))

    assert captured["json"]["to"] == "919876543210@s.whatsapp.net"
    assert captured["json"]["text"] == "hi"


def test_group_jid_is_preserved():
    """A group chat_id (@g.us) is already a JID and must pass through as-is."""
    adapter, captured = _adapter_with_capture()
    group = "120363000000000000@g.us"

    asyncio.run(adapter.send_message(group, "hello group"))

    assert captured["json"]["to"] == group
