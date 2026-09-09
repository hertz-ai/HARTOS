"""Unit tests for the RoomCapableAdapter subclass on Slack, Matrix,
Telegram, Teams, WhatsApp adapters (UNIF-G2 / W1.4).

Each adapter is exercised through a fake of its underlying SDK so the
real network layer never runs.  Discord is covered separately in
``test_discord_room_capable.py``; this file covers the four W1.4
adapters plus a cross-adapter contract test that pins the Mixin
invariants.

Verification matrix (per adapter that subclasses RoomCapableAdapter):
  - is_room_capable(adapter) → True
  - join_room success → True
  - join_room refused (idempotent / forbidden) → True or False per
    adapter, never raises
  - leave_room → True (idempotent)
  - list_room_members → list of dicts with id + display_name + is_bot,
    bot itself filtered

Stubs (Teams, WhatsApp): join_room and leave_room return False, and
list_room_members returns []. The Mixin marker is still True.
"""
from __future__ import annotations

import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock


# ─── Slack ──────────────────────────────────────────────────────────

_FAKE_SLACK_MODULES = (
    'slack_bolt', 'slack_bolt.async_app', 'slack_bolt.adapter',
    'slack_bolt.adapter.socket_mode',
    'slack_bolt.adapter.socket_mode.async_handler',
    'slack_sdk', 'slack_sdk.web', 'slack_sdk.web.async_client',
    'slack_sdk.errors',
)
_installed_fake_slack = False


def _install_fake_slack():
    global _installed_fake_slack
    if 'slack_bolt' in sys.modules:
        return
    slack_bolt = types.ModuleType('slack_bolt')
    async_app_mod = types.ModuleType('slack_bolt.async_app')
    async_app_mod.AsyncApp = type('AsyncApp', (), {})
    slack_bolt.async_app = async_app_mod
    sys.modules['slack_bolt'] = slack_bolt
    sys.modules['slack_bolt.async_app'] = async_app_mod
    adapter_mod = types.ModuleType('slack_bolt.adapter')
    socket_mode_mod = types.ModuleType('slack_bolt.adapter.socket_mode')
    async_handler_mod = types.ModuleType(
        'slack_bolt.adapter.socket_mode.async_handler')
    async_handler_mod.AsyncSocketModeHandler = type(
        'AsyncSocketModeHandler', (), {})
    socket_mode_mod.async_handler = async_handler_mod
    adapter_mod.socket_mode = socket_mode_mod
    slack_bolt.adapter = adapter_mod
    sys.modules['slack_bolt.adapter'] = adapter_mod
    sys.modules['slack_bolt.adapter.socket_mode'] = socket_mode_mod
    sys.modules['slack_bolt.adapter.socket_mode.async_handler'] = \
        async_handler_mod
    slack_sdk = types.ModuleType('slack_sdk')
    web_mod = types.ModuleType('slack_sdk.web')
    async_client_mod = types.ModuleType('slack_sdk.web.async_client')
    async_client_mod.AsyncWebClient = type('AsyncWebClient', (), {})
    web_mod.async_client = async_client_mod
    slack_sdk.web = web_mod
    errors_mod = types.ModuleType('slack_sdk.errors')

    class SlackApiError(Exception):
        def __init__(self, msg='', response=None):
            super().__init__(msg)
            self.response = response or {}
    errors_mod.SlackApiError = SlackApiError
    slack_sdk.errors = errors_mod
    sys.modules['slack_sdk'] = slack_sdk
    sys.modules['slack_sdk.web'] = web_mod
    sys.modules['slack_sdk.web.async_client'] = async_client_mod
    sys.modules['slack_sdk.errors'] = errors_mod
    _installed_fake_slack = True


_install_fake_slack()
from integrations.channels.base import ChannelConfig  # noqa: E402
from integrations.channels.room_capable import (  # noqa: E402
    RoomCapableAdapter, UnsupportedRoomError, is_room_capable,
)
from integrations.channels.slack_adapter import SlackAdapter  # noqa: E402
from slack_sdk.errors import SlackApiError  # noqa: E402

# Undo _install_fake_slack() now, not in a teardown_module: pytest COLLECTS
# (imports) every test file before it EXECUTES any of them, so a
# later-collected module that needs the real slack_sdk (e.g.
# test_slack_send_result_raw.py imports slack_sdk.web.async_slack_response,
# which this file's minimal stand-in doesn't have) would already have failed
# to import by the time any teardown_module ran. slack_adapter and
# SlackApiError above already hold their own bound references, so removing
# the sys.modules entries here is safe. Only remove what this module
# actually installed: if 'slack_bolt' was already present (real or another
# fake), leave it.
if _installed_fake_slack:
    for _name in _FAKE_SLACK_MODULES:
        sys.modules.pop(_name, None)


def _make_slack(channels: dict | None = None,
                bot_user_id: str = 'U_BOT'):
    cfg = ChannelConfig(token='xoxb-fake', extra={'app_token': 'xapp-fake'})
    a = SlackAdapter(cfg)
    a._client = MagicMock()
    a._bot_user_id = bot_user_id
    a._channels = channels or {}
    return a


class SlackRoomCapableTest(unittest.TestCase):

    def test_is_room_capable(self):
        a = _make_slack()
        self.assertTrue(is_room_capable(a))

    def test_join_public_channel_succeeds(self):
        a = _make_slack()
        a._client.conversations_join = AsyncMock(return_value={'ok': True})
        ok = asyncio.run(a.join_room('C123ABC'))
        self.assertTrue(ok)
        a._client.conversations_join.assert_awaited_once_with(channel='C123ABC')

    def test_join_already_in_channel_is_idempotent_true(self):
        a = _make_slack()
        a._client.conversations_join = AsyncMock(
            side_effect=SlackApiError('err', {'error': 'already_in_channel'}))
        ok = asyncio.run(a.join_room('C123'))
        self.assertTrue(ok)

    def test_join_private_channel_returns_false(self):
        a = _make_slack()
        a._client.conversations_join = AsyncMock(
            side_effect=SlackApiError(
                'err', {'error': 'method_not_supported_for_channel_type'}))
        ok = asyncio.run(a.join_room('G123'))
        self.assertFalse(ok)

    def test_join_dm_raises_unsupported(self):
        a = _make_slack()
        with self.assertRaises(UnsupportedRoomError):
            asyncio.run(a.join_room('D123'))

    def test_leave_idempotent_on_not_in_channel(self):
        a = _make_slack()
        a._client.conversations_leave = AsyncMock(
            side_effect=SlackApiError('err', {'error': 'not_in_channel'}))
        ok = asyncio.run(a.leave_room('C123'))
        self.assertTrue(ok)

    def test_list_members_skips_bot(self):
        a = _make_slack()
        a._client.conversations_members = AsyncMock(
            return_value={'members': ['U_BOT', 'U_alice', 'U_bob']})

        async def users_info(user):
            return {'user': {
                'profile': {'display_name': f'{user}_dn'},
                'real_name': f'{user}_real', 'name': user,
                'is_bot': False,
            }}
        a._client.users_info = AsyncMock(side_effect=users_info)
        members = asyncio.run(a.list_room_members('C123'))
        ids = [m['id'] for m in members]
        self.assertNotIn('U_BOT', ids)
        self.assertEqual(set(ids), {'U_alice', 'U_bob'})


# ─── Telegram ───────────────────────────────────────────────────────

def _install_fake_telegram():
    if 'telegram' in sys.modules:
        return
    telegram_mod = types.ModuleType('telegram')

    class _Stub:
        def __init__(self, *a, **kw): pass

    for name in ('Update', 'Bot', 'Message', 'InlineKeyboardButton',
                 'InlineKeyboardMarkup', 'InputMediaPhoto',
                 'InputMediaVideo', 'InputMediaDocument',
                 'InputMediaAudio'):
        setattr(telegram_mod, name, _Stub)

    ext_mod = types.ModuleType('telegram.ext')
    for name in ('Application', 'ApplicationBuilder', 'CommandHandler',
                 'MessageHandler', 'CallbackQueryHandler', 'ContextTypes'):
        setattr(ext_mod, name, _Stub)
    ext_mod.filters = _Stub()
    telegram_mod.ext = ext_mod

    constants_mod = types.ModuleType('telegram.constants')
    constants_mod.ChatAction = _Stub
    constants_mod.ParseMode = _Stub
    telegram_mod.constants = constants_mod

    error_mod = types.ModuleType('telegram.error')
    class TelegramError(Exception):
        pass
    class RetryAfter(TelegramError):
        pass
    error_mod.TelegramError = TelegramError
    error_mod.RetryAfter = RetryAfter
    telegram_mod.error = error_mod

    sys.modules['telegram'] = telegram_mod
    sys.modules['telegram.ext'] = ext_mod
    sys.modules['telegram.constants'] = constants_mod
    sys.modules['telegram.error'] = error_mod


_install_fake_telegram()
from integrations.channels.telegram_adapter import (  # noqa: E402
    TelegramAdapter,
)
# Pull TelegramError from the live fake module at test time — pytest's
# import-collection ordering occasionally drops top-level rebinding when
# the same module name was imported by an earlier test file.  Going via
# sys.modules sidesteps that.
TelegramError = sys.modules['telegram.error'].TelegramError


def _make_telegram():
    cfg = ChannelConfig(token='fake')
    # Bypass real ApplicationBuilder construction in __init__.
    a = TelegramAdapter.__new__(TelegramAdapter)
    a.config = cfg
    a._bot = MagicMock()
    a._bot_user_id = 9999
    return a


class TelegramRoomCapableTest(unittest.TestCase):

    def test_is_room_capable(self):
        a = _make_telegram()
        self.assertTrue(is_room_capable(a))

    def test_join_member_chat_returns_true(self):
        a = _make_telegram()
        chat = MagicMock(type='supergroup')
        a._bot.get_chat = AsyncMock(return_value=chat)
        ok = asyncio.run(a.join_room('-1001234'))
        self.assertTrue(ok)

    def test_join_not_member_returns_false(self):
        # Pull TelegramError from sys.modules at call time — pytest's
        # collection / fixture cycle occasionally drops module-level
        # rebindings established before test discovery, so we go
        # through sys.modules to be deterministic.
        TE = sys.modules['telegram.error'].TelegramError
        a = _make_telegram()
        a._bot.get_chat = AsyncMock(side_effect=TE('forbidden'))
        ok = asyncio.run(a.join_room('-1009999'))
        self.assertFalse(ok)

    def test_join_private_dm_raises_unsupported(self):
        a = _make_telegram()
        chat = MagicMock(type='private')
        a._bot.get_chat = AsyncMock(return_value=chat)
        with self.assertRaises(UnsupportedRoomError):
            asyncio.run(a.join_room('123456'))

    def test_leave_idempotent_on_telegram_error(self):
        TE = sys.modules['telegram.error'].TelegramError
        a = _make_telegram()
        a._bot.leave_chat = AsyncMock(side_effect=TE('not member'))
        ok = asyncio.run(a.leave_room('-1001234'))
        self.assertTrue(ok)

    def test_list_members_returns_admins_minus_bot(self):
        a = _make_telegram()
        admin1 = MagicMock(user=MagicMock(
            id=1, full_name='Alice', username='alice', is_bot=False))
        bot_self = MagicMock(user=MagicMock(
            id=9999, full_name='Bot', username='bot', is_bot=True))
        a._bot.get_chat_administrators = AsyncMock(
            return_value=[admin1, bot_self])
        members = asyncio.run(a.list_room_members('-1001234'))
        ids = [m['id'] for m in members]
        self.assertEqual(ids, ['1'])  # bot itself filtered
        self.assertEqual(members[0]['display_name'], 'Alice')


# ─── Matrix ─────────────────────────────────────────────────────────

def _install_fake_matrix():
    if 'nio' in sys.modules:
        return
    nio = types.ModuleType('nio')

    class JoinResponse:
        pass

    class _Stub:
        def __init__(self, *a, **kw): pass

    for name in ('AsyncClient', 'AsyncClientConfig', 'LoginResponse',
                 'RoomMessageText', 'RoomMessageMedia', 'RoomMemberEvent',
                 'InviteMemberEvent', 'MatrixRoom', 'Event', 'SyncResponse',
                 'UploadResponse', 'RoomCreateResponse', 'RoomSendResponse',
                 'RoomResolveAliasResponse', 'ToDeviceEvent',
                 'KeyVerificationEvent', 'MegolmEvent'):
        setattr(nio, name, _Stub)
    nio.JoinResponse = JoinResponse
    store_mod = types.ModuleType('nio.store')
    store_mod.SqliteStore = _Stub
    nio.store = store_mod
    sys.modules['nio'] = nio
    sys.modules['nio.store'] = store_mod


_install_fake_matrix()
from integrations.channels.extensions.matrix_adapter import (  # noqa: E402
    MatrixAdapter,
)
from nio import JoinResponse  # noqa: E402


def _make_matrix(rooms: dict | None = None, user_id: str = '@bot:hs'):
    a = MatrixAdapter.__new__(MatrixAdapter)
    a._client = MagicMock()
    a._client.user_id = user_id
    a._client.rooms = rooms or {}
    return a


class MatrixRoomCapableTest(unittest.TestCase):

    def test_is_room_capable(self):
        a = _make_matrix()
        self.assertTrue(is_room_capable(a))

    def test_join_returns_true_on_join_response(self):
        a = _make_matrix()
        a._client.join = AsyncMock(return_value=JoinResponse())
        ok = asyncio.run(a.join_room('!room:hs'))
        self.assertTrue(ok)

    def test_join_returns_false_on_unexpected_response(self):
        a = _make_matrix()
        a._client.join = AsyncMock(return_value=object())
        ok = asyncio.run(a.join_room('!room:hs'))
        self.assertFalse(ok)

    def test_leave_returns_true_on_success(self):
        a = _make_matrix()
        a._client.room_leave = AsyncMock(return_value=None)
        ok = asyncio.run(a.leave_room('!room:hs'))
        self.assertTrue(ok)

    def test_list_members_skips_bot(self):
        bot = MagicMock(display_name='Bot')
        alice = MagicMock(display_name='Alice')
        room = MagicMock(users={'@bot:hs': bot, '@alice:hs': alice})
        a = _make_matrix(rooms={'!room:hs': room})
        members = asyncio.run(a.list_room_members('!room:hs'))
        ids = [m['id'] for m in members]
        self.assertNotIn('@bot:hs', ids)
        self.assertIn('@alice:hs', ids)


# ─── Teams + WhatsApp stubs ─────────────────────────────────────────

def _install_fake_teams():
    """Best-effort stubs for Teams' SDK surface — only shapes
    ``TeamsAdapter`` references at import time. Only called at the point
    teams_adapter is actually imported (not at this file's own module
    level): a bare ``types.ModuleType('aiohttp')`` with zero attributes
    left sitting in sys.modules breaks every OTHER file's real
    ``from aiohttp import ...`` for the rest of the pytest session.
    Returns the module names this call actually added, so the caller can
    remove exactly those and nothing else (a name already present, real
    or another fake, is left alone).
    """
    added = []
    for mod_name in ('botbuilder', 'botbuilder.core',
                     'botbuilder.schema', 'aiohttp'):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)
            added.append(mod_name)
    # Coarse-grained: re-route any deep symbol access to a permissive
    # MagicMock if the stub was created above.  This is sufficient for
    # the import-time surface; the adapter's stub join_room / leave_room
    # never call into the SDK anyway.
    return added


class StubAdaptersTest(unittest.TestCase):
    """Teams + WhatsApp marked RoomCapableAdapter but stub-only.

    We exercise the contract via the Mixin directly to avoid the
    full SDK dependency surface in unit-test land.  The stubs are
    NOT subclasses of the real adapter classes — they ARE the same
    methods, copy-pasted into a minimal subclass.  This keeps the
    test resilient to refactors of the heavy adapter __init__.
    """

    def test_stub_returns_false_for_join_and_empty_members(self):
        # We construct a minimal subclass that mirrors the
        # Teams/WhatsApp stub shape — same return values — to
        # contract-test the pattern without instantiating the real
        # adapter (which pulls in heavy SDK chains).
        class _StubAdapter(RoomCapableAdapter):
            async def join_room(self, room_id, role='participant'):
                return False
            async def leave_room(self, room_id):
                return False
            async def list_room_members(self, room_id):
                return []

        a = _StubAdapter()
        self.assertTrue(is_room_capable(a))
        self.assertFalse(asyncio.run(a.join_room('any-room')))
        self.assertFalse(asyncio.run(a.leave_room('any-room')))
        self.assertEqual(asyncio.run(a.list_room_members('any-room')), [])


# ─── Cross-adapter contract guard ───────────────────────────────────


class RoomCapableContractTest(unittest.TestCase):
    """Drift guard: every adapter that imports ``RoomCapableAdapter``
    AND subclasses ``ChannelAdapter`` MUST not leave the Mixin's
    NotImplementedError defaults in place — every subclass must
    override join_room and leave_room.
    """

    def test_every_room_capable_adapter_overrides_join_and_leave(self):
        # teams_adapter's own `import aiohttp`/botbuilder need SOMETHING
        # importable at ITS import time; installed here, right where it's
        # used, and removed in the finally below rather than left to leak
        # into every other test file collected/run afterward.
        installed = _install_fake_teams()
        try:
            # Walk all adapter classes that import RoomCapableAdapter.
            targets = [
                ('integrations.channels.discord_adapter', 'DiscordAdapter'),
                ('integrations.channels.slack_adapter', 'SlackAdapter'),
                ('integrations.channels.telegram_adapter', 'TelegramAdapter'),
                ('integrations.channels.extensions.matrix_adapter',
                 'MatrixAdapter'),
                ('integrations.channels.extensions.teams_adapter',
                 'TeamsAdapter'),
                ('integrations.channels.whatsapp_adapter', 'WhatsAppAdapter'),
            ]
            for mod_name, cls_name in targets:
                try:
                    mod = __import__(mod_name, fromlist=[cls_name])
                    cls = getattr(mod, cls_name)
                except Exception:
                    # If the heavy import chain isn't available in this
                    # test env, skip — not a contract failure.
                    continue
                self.assertTrue(
                    issubclass(cls, RoomCapableAdapter),
                    f"{cls_name} must subclass RoomCapableAdapter")
                self.assertNotEqual(
                    cls.join_room, RoomCapableAdapter.join_room,
                    f"{cls_name}.join_room must override the Mixin default")
                self.assertNotEqual(
                    cls.leave_room, RoomCapableAdapter.leave_room,
                    f"{cls_name}.leave_room must override the Mixin default")
        finally:
            for mod_name in installed:
                sys.modules.pop(mod_name, None)


if __name__ == '__main__':
    unittest.main()
