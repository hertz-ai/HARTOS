"""Unit tests for Discord adapter ``RoomCapableAdapter`` implementation
(UNIF-G2 / W1.1).

discord.py is not in the standard CI install set, so these tests inject
a faked ``discord`` module via ``sys.modules`` before importing
``discord_adapter``.  The faked module exposes only the symbols
``DiscordAdapter`` actually touches: ``Intents``, ``Embed``, ``File``,
``Message``, ``DMChannel``, ``VoiceChannel``, ``LoginFailure``,
``Forbidden``, ``HTTPException``, ``NotFound``, ``ClientException``,
``opus.OpusNotLoaded``, ``ext.commands.Bot``, ``ui.View``, ``ui.Button``,
``ButtonStyle.link``, ``ButtonStyle.primary``.

The tests cover:
  - ``is_room_capable`` returns True for a DiscordAdapter
  - ``join_room`` text channel → True + recorded in ``_joined_text_rooms``
  - ``join_room`` voice channel → calls ``connect`` + caches client
  - ``join_room`` voice idempotent (no double-connect)
  - ``join_room`` DM → raises ``UnsupportedRoomError``
  - ``join_room`` channel not found → False
  - ``leave_room`` voice → disconnects + drops client
  - ``leave_room`` text → discards from set
  - ``leave_room`` unknown → False
  - ``list_room_members`` skips the bot itself + returns id/display_name
"""
from __future__ import annotations

import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock


def _install_fake_discord():
    """Build and register a faked ``discord`` module tree just rich
    enough for ``discord_adapter`` to import + instantiate.
    """
    if 'discord' in sys.modules:
        return  # already faked / installed

    discord_mod = types.ModuleType('discord')

    class Intents:
        @staticmethod
        def default():
            inst = Intents()
            inst.message_content = False
            inst.members = False
            inst.dm_messages = False
            inst.guild_messages = False
            inst.voice_states = False
            return inst

    class _BaseError(Exception):
        pass

    class Forbidden(_BaseError):
        pass

    class NotFound(_BaseError):
        pass

    class ClientException(_BaseError):
        pass

    class LoginFailure(_BaseError):
        pass

    class HTTPException(_BaseError):
        def __init__(self, *args, status=None, retry_after=None, **kwargs):
            super().__init__(*args)
            self.status = status
            self.retry_after = retry_after

    class _ChannelMarker:
        """Base marker for channel kinds; tests subclass to attach data."""
        pass

    class DMChannel(_ChannelMarker):
        pass

    class VoiceChannel(_ChannelMarker):
        pass

    class TextChannel(_ChannelMarker):
        pass

    discord_mod.Intents = Intents
    discord_mod.Forbidden = Forbidden
    discord_mod.NotFound = NotFound
    discord_mod.ClientException = ClientException
    discord_mod.LoginFailure = LoginFailure
    discord_mod.HTTPException = HTTPException
    discord_mod.DMChannel = DMChannel
    discord_mod.VoiceChannel = VoiceChannel
    discord_mod.TextChannel = TextChannel
    discord_mod.Embed = type('Embed', (), {'__init__': lambda self, **kw: None})
    discord_mod.File = type('File', (), {'__init__': lambda self, *a, **kw: None})
    discord_mod.Message = type('Message', (), {})
    discord_mod.ButtonStyle = types.SimpleNamespace(link='link', primary='primary')

    opus_mod = types.ModuleType('discord.opus')
    class OpusNotLoaded(_BaseError):
        pass
    opus_mod.OpusNotLoaded = OpusNotLoaded
    discord_mod.opus = opus_mod

    ui_mod = types.ModuleType('discord.ui')
    ui_mod.View = type('View', (), {
        '__init__': lambda self: setattr(self, '_items', []),
        'add_item': lambda self, item: self._items.append(item),
    })
    ui_mod.Button = type('Button', (), {'__init__': lambda self, **kw: None})
    discord_mod.ui = ui_mod

    ext_mod = types.ModuleType('discord.ext')
    commands_mod = types.ModuleType('discord.ext.commands')

    class Bot:
        def __init__(self, command_prefix='!', intents=None, **kwargs):
            self.command_prefix = command_prefix
            self.intents = intents
            self.user = MagicMock(id=999, name='HiveAgentBot',
                                  discriminator='0001')
            self._channels = {}
            # event registration is a no-op for tests
            self.event = lambda fn: fn

        def get_channel(self, cid):
            return self._channels.get(cid)

        async def fetch_channel(self, cid):
            ch = self._channels.get(cid)
            if ch is None:
                raise NotFound("not found")
            return ch

        async def close(self):
            pass

        async def start(self, token):
            pass

    commands_mod.Bot = Bot
    ext_mod.commands = commands_mod

    sys.modules['discord'] = discord_mod
    sys.modules['discord.opus'] = opus_mod
    sys.modules['discord.ui'] = ui_mod
    sys.modules['discord.ext'] = ext_mod
    sys.modules['discord.ext.commands'] = commands_mod


# Install BEFORE import of discord_adapter
_install_fake_discord()

from integrations.channels.base import ChannelConfig  # noqa: E402
from integrations.channels.discord_adapter import (  # noqa: E402
    DiscordAdapter,
)
from integrations.channels.room_capable import (  # noqa: E402
    UnsupportedRoomError, is_room_capable,
)


def _make_adapter(channels: dict | None = None) -> DiscordAdapter:
    """Build a DiscordAdapter with a fake bot whose channel registry
    is the supplied dict (id -> channel-like object).
    """
    cfg = ChannelConfig(token='fake-token')
    a = DiscordAdapter(cfg)
    a._bot_user_id = 999  # match the fake Bot.user.id
    if channels:
        a._bot._channels = channels
    return a


class DiscordRoomCapableTest(unittest.TestCase):

    def test_adapter_is_room_capable(self):
        a = _make_adapter()
        self.assertTrue(is_room_capable(a))

    def test_join_text_channel_caches_presence(self):
        import discord
        ch = discord.TextChannel()
        ch.id = 100
        ch.members = []
        a = _make_adapter({100: ch})
        ok = asyncio.run(a.join_room('100', 'co_pilot'))
        self.assertTrue(ok)
        self.assertIn(100, a._joined_text_rooms)
        self.assertNotIn(100, a._voice_clients)

    def test_join_voice_channel_connects(self):
        import discord
        vc = discord.VoiceChannel()
        vc.id = 200
        client_mock = MagicMock()
        client_mock.is_connected.return_value = True
        client_mock.disconnect = AsyncMock()
        vc.connect = AsyncMock(return_value=client_mock)
        a = _make_adapter({200: vc})
        ok = asyncio.run(a.join_room('200', 'note_taker'))
        self.assertTrue(ok)
        self.assertIn(200, a._voice_clients)
        vc.connect.assert_awaited_once()

    def test_join_voice_idempotent(self):
        import discord
        vc = discord.VoiceChannel()
        vc.id = 200
        client_mock = MagicMock()
        client_mock.is_connected.return_value = True
        vc.connect = AsyncMock(return_value=client_mock)
        a = _make_adapter({200: vc})

        ok1 = asyncio.run(a.join_room('200'))
        ok2 = asyncio.run(a.join_room('200'))
        self.assertTrue(ok1)
        self.assertTrue(ok2)
        # connect should be called exactly once across two joins
        self.assertEqual(vc.connect.await_count, 1)

    def test_join_dm_raises_unsupported(self):
        import discord
        dm = discord.DMChannel()
        dm.id = 300
        a = _make_adapter({300: dm})
        with self.assertRaises(UnsupportedRoomError):
            asyncio.run(a.join_room('300'))

    def test_join_unknown_returns_false(self):
        a = _make_adapter({})  # no channels registered
        ok = asyncio.run(a.join_room('404'))
        self.assertFalse(ok)

    def test_leave_voice_disconnects(self):
        import discord
        vc = discord.VoiceChannel()
        vc.id = 200
        client_mock = MagicMock()
        client_mock.is_connected.return_value = True
        client_mock.disconnect = AsyncMock()
        vc.connect = AsyncMock(return_value=client_mock)
        a = _make_adapter({200: vc})

        asyncio.run(a.join_room('200'))
        self.assertIn(200, a._voice_clients)
        ok = asyncio.run(a.leave_room('200'))
        self.assertTrue(ok)
        self.assertNotIn(200, a._voice_clients)
        client_mock.disconnect.assert_awaited_once()

    def test_leave_text_clears_presence(self):
        import discord
        ch = discord.TextChannel()
        ch.id = 100
        ch.members = []
        a = _make_adapter({100: ch})
        asyncio.run(a.join_room('100'))
        self.assertIn(100, a._joined_text_rooms)
        ok = asyncio.run(a.leave_room('100'))
        self.assertTrue(ok)
        self.assertNotIn(100, a._joined_text_rooms)

    def test_leave_unknown_returns_false(self):
        a = _make_adapter({})
        ok = asyncio.run(a.leave_room('999'))
        self.assertFalse(ok)

    def test_list_members_skips_bot(self):
        import discord
        bot_member = MagicMock(id=999, display_name='HiveBot', name='HiveBot',
                               bot=True)
        alice = MagicMock(id=1, display_name='Alice', name='alice', bot=False)
        bobbot = MagicMock(id=2, display_name='Bobbot', name='bobbot',
                           bot=True)
        ch = discord.TextChannel()
        ch.id = 100
        ch.members = [bot_member, alice, bobbot]
        a = _make_adapter({100: ch})
        members = asyncio.run(a.list_room_members('100'))
        ids = [m['id'] for m in members]
        self.assertNotIn('999', ids)  # bot itself filtered
        self.assertIn('1', ids)
        self.assertIn('2', ids)
        # display_name + is_bot present
        alice_dict = next(m for m in members if m['id'] == '1')
        self.assertEqual(alice_dict['display_name'], 'Alice')
        self.assertFalse(alice_dict['is_bot'])
        bob_dict = next(m for m in members if m['id'] == '2')
        self.assertTrue(bob_dict['is_bot'])

    def test_list_members_unknown_returns_empty(self):
        a = _make_adapter({})
        result = asyncio.run(a.list_room_members('999'))
        self.assertEqual(result, [])


if __name__ == '__main__':
    unittest.main()
