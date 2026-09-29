"""ChannelRegistry._route_to_agent: reply, declined, and failed are distinct.

An agent handler returns one of three things, and the channel must treat each
differently:

  * a real reply       -> sent back to the chat
  * ``None`` (declined) -> nothing sent; e.g. a group message with no bot
                           mention under require_mention_in_groups
  * ``''`` (failed)     -> a short fallback, so the user is never left in
                           silence

The 2026-08-10 empty-reply fix collapsed the last two into "falsy", so the bot
replied "I wasn't able to put together a reply for that one." to every
unmentioned group message -- it spoke on exactly the messages it was configured
to ignore (seen on a real Discord channel 2026-08-12).

These drive the REAL registry on a real event loop with a started adapter;
only the adapter's wire is faked.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from integrations.channels.base import (  # noqa: E402
    ChannelAdapter, ChannelConfig, ChannelStatus, Message, SendResult)
from integrations.channels.registry import ChannelRegistry  # noqa: E402


class _Adapter(ChannelAdapter):
    """Records what, if anything, the registry sent back to the chat."""

    def __init__(self):
        super().__init__(ChannelConfig(token='t'))
        self.sent = []

    @property
    def name(self):
        return 'discord'

    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send_message(self, chat_id, text, reply_to=None, media=None,
                           buttons=None):
        self.sent.append(text)
        return SendResult(success=True, message_id=str(len(self.sent)))

    async def edit_message(self, *a, **k):
        return SendResult(success=True)

    async def delete_message(self, *a, **k):
        return True

    async def send_typing(self, chat_id):
        return None

    async def get_chat_info(self, chat_id):
        return {}


def _route(handler_result):
    registry = ChannelRegistry()
    adapter = _Adapter()
    registry.register(adapter)
    registry.set_agent_handler(lambda message: handler_result)

    async def _run():
        await adapter.start()
        assert adapter.get_status() == ChannelStatus.CONNECTED
        await registry._route_to_agent(Message(
            id='m1', channel='discord', sender_id='s1', chat_id='c1',
            text='hello'))

    asyncio.run(_run())
    return adapter.sent


def test_real_reply_is_sent_once():
    assert _route('pong') == ['pong']


def test_declined_message_stays_silent():
    assert _route(None) == []


def test_empty_reply_gets_a_fallback_not_silence():
    from core.agent_tools import is_user_facing_error
    sent = _route('')
    assert len(sent) == 1
    # The canonical failure sentence, so hive callers never count it as work.
    assert is_user_facing_error(sent[0])


@pytest.mark.parametrize('blank', ['   ', '\n'])
def test_whitespace_reply_counts_as_empty(blank):
    assert len(_route(blank)) == 1
