"""Connecting a channel from chat or OAuth ends connected, or says why not.

Measured before this change (2026-09-29), driving the real code:
  * The OAuth callback ended "Connection failed" for 7 of 8 OAuth channels:
    register_channel demanded every setup field, and a sign-in returns only
    its token (Slack's catalog also asked for a signing_secret nothing read).
  * "connect whatsapp" in chat never showed a QR: it asked for a phone twice
    and could only mint a pair code.
  * The chat credential form named a `submit_action` no route handled, so
    Connect dropped the typed token.
  * A completed connect said "Adapter will connect on restart"; the OAuth
    callback still sent a "connected" card.

Each test runs the real function (the OAuth blueprint, the register_channel
closure, the WhatsApp link helper compiled from hart_intelligence_entry) and
mocks only the boundaries: the provider token endpoint, the admin config
store, the WhatsApp gateway, the Liquid UI service and live adapter wiring.
"""
import ast
import json
import logging
import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask, g

from core.platform.registry import get_registry, reset_registry
from integrations.channels.base import ChannelStatus
from tests.unit.module_swap import swap_modules

_ENTRY = os.path.join(os.path.dirname(__file__), '..', '..',
                      'hart_intelligence_entry.py')


class _FakeApi:
    def __init__(self):
        self._channels = {}

    def _save_config(self):
        pass


@pytest.fixture()
def lui():
    reset_registry()
    svc = MagicMock()
    svc.agent_ui_update.return_value = True
    get_registry().register('LiquidUIService', lambda: svc)
    yield svc
    reset_registry()


def _cards(svc, kind):
    return [c.args[1] for c in svc.agent_ui_update.call_args_list
            if c.args[1].get('type') == kind]


def _register_fn(user_id='u-1'):
    from integrations.channels.agent_tools import build_channel_tool_closures
    tools = build_channel_tool_closures({'user_id': user_id, 'prompt_id': None})
    return next(t[2] for t in tools if t[0] == 'register_channel')


@contextmanager
def _no_db_no_disk(wired=None):
    """The admin config store and the binding DB are boundaries; so is the
    live adapter wiring (a real channel loop) and its status watcher."""
    wire = MagicMock(return_value=wired or {'success': True})
    watch = MagicMock()
    with patch('integrations.channels.admin.api.get_api',
               return_value=_FakeApi()), \
            patch('integrations.social.models.get_db',
                  side_effect=RuntimeError('no db in this test')), \
            patch('integrations.social.api_channels._wire_live_adapter', wire), \
            patch('integrations.channels.agent_tools._report_when_live', watch):
        yield wire, watch


# ─── register_channel: required fields and going live ───────────────────

class TestRegisterChannelCompletes:
    def test_whatsapp_needs_nothing_typed_and_is_not_generically_wired(self):
        """The QR links without a phone, so '{}' is a complete WhatsApp
        registration; its adapter is wired by the pairing watcher, never
        by the generic single-credential path."""
        with _no_db_no_disk() as (wire, watch):
            out = _register_fn()('whatsapp', '{}')
        assert 'registered and enabled' in out and 'Missing' not in out
        # How to link depends on the device (the chat handler says which);
        # the binding reply does not promise a QR.
        assert 'QR' not in out
        wire.assert_not_called()
        watch.assert_not_called()

    def test_slack_needs_only_the_bot_token_and_goes_live(self, monkeypatch):
        monkeypatch.setenv('SLACK_APP_TOKEN', 'xapp-1')
        with _no_db_no_disk() as (wire, watch):
            out = _register_fn()('slack', '{"bot_token": "xoxb-1"}')
        assert 'registered and enabled' in out
        wire.assert_called_once_with('slack', 'xoxb-1')
        watch.assert_called_once()
        assert watch.call_args.args[0] == 'slack'

    def test_slack_without_the_server_app_token_names_it(self, monkeypatch):
        """Socket Mode needs the app's xapp- token, an operator setting.
        The user is told which one, not the adapter's bare "returned False"."""
        monkeypatch.delenv('SLACK_APP_TOKEN', raising=False)
        with _no_db_no_disk() as (wire, watch):
            out = _register_fn()('slack', '{"bot_token": "xoxb-1"}')
        assert 'registered and enabled' not in out
        assert 'SLACK_APP_TOKEN' in out
        wire.assert_not_called()
        watch.assert_not_called()

    def test_a_channel_that_cannot_start_says_so(self):
        with _no_db_no_disk({'success': False,
                             'error': 'register_channel returned False'}) as (_w, watch):
            out = _register_fn()('telegram', '{"bot_token": "123:ABC"}')
        assert 'registered and enabled' not in out
        assert 'could not be started' in out and 'returned False' in out
        watch.assert_not_called()

    def test_multi_credential_channel_says_it_is_not_running(self):
        """Live connect passes one credential and boot restore reads one, so
        a channel needing several is saved but not started.  It must not
        claim success or promise a restart that would not bring it up."""
        with _no_db_no_disk() as (wire, _watch):
            out = _register_fn()('mattermost', json.dumps(
                {'server_url': 'https://mm.example', 'access_token': 't'}))
        assert 'registered and enabled' not in out
        assert "can't be started" in out and 'restart' not in out
        wire.assert_not_called()

    def test_the_token_is_kept_where_boot_restore_reads_it(self):
        """restore_persisted_channels reads the binding's metadata_json; a
        chat or OAuth connect that kept the token only in the admin config
        (whose boot reader looks at the top level, not ['config']) never came
        back after a restart."""
        from integrations.social.models import UserChannelBinding
        added = []
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        db.add.side_effect = added.append
        with _no_db_no_disk() as (_wire, _watch), \
                patch('integrations.social.models.get_db', return_value=db):
            _register_fn()('telegram', '{"bot_token": "123:ABC"}')
        (row,) = added
        assert isinstance(row, UserChannelBinding)
        assert row.metadata_json == {'bot_token': '123:ABC'}
        db.commit.assert_called_once()

    def test_an_empty_token_is_still_missing(self):
        with _no_db_no_disk() as (wire, _watch):
            out = _register_fn()('telegram', '{"bot_token": ""}')
        assert "Missing: ['bot_token']" in out
        wire.assert_not_called()


# ─── The OAuth callback ends connected for a sign-in's own fields ───────

def _oauth_app():
    from integrations.channels.oauth_api import oauth_bp
    app = Flask(__name__)
    app.register_blueprint(oauth_bp)
    return app


def _callback(app, channel, token_body):
    from integrations.channels.security import get_oauth_state_manager
    state = get_oauth_state_manager().generate_state(
        user_id='u-1', channel_type=channel)
    resp = MagicMock(status_code=200)
    resp.json.return_value = token_body
    with patch('integrations.channels.oauth_api.requests.post',
               return_value=resp):
        return app.test_client().get(
            f'/api/oauth/{channel}/callback?code=c&state={state}')


class TestOAuthCallbackCompletes:
    @pytest.fixture(autouse=True)
    def _creds(self, monkeypatch):
        for ch in ('SLACK', 'DISCORD', 'TWITTER'):
            monkeypatch.setenv(f'HARTOS_OAUTH_CLIENT_{ch}', 'id')
            monkeypatch.setenv(f'HARTOS_OAUTH_SECRET_{ch}', 'secret')

    def test_slack_sign_in_ends_connected(self, lui, monkeypatch):
        monkeypatch.setenv('SLACK_APP_TOKEN', 'xapp-1')
        with _no_db_no_disk() as (wire, _watch):
            r = _callback(_oauth_app(), 'slack',
                          {'ok': True, 'bot': {'access_token': 'xoxb-9'}})
        assert r.status_code == 200 and b'Connected.' in r.data
        wire.assert_called_once_with('slack', 'xoxb-9')
        # The "connected" card is the watcher's to send, once the adapter
        # is live; the callback sending one claimed it at token-save time.
        assert _cards(lui, 'channel_connected') == []

    def test_a_sign_in_whose_adapter_cannot_start_fails_with_the_reason(self, lui):
        with _no_db_no_disk({'success': False, 'error': 'no app token'}):
            r = _callback(_oauth_app(), 'discord', {'access_token': 'a'})
        assert r.status_code == 502 and b'Connection failed.' in r.data
        assert b'no app token' in r.data

    def test_twitter_sign_in_fails_naming_what_its_adapter_lacks(self, lui):
        """Twitter's adapter is OAuth 1 (api key/secret, access token and
        secret); the OAuth 2 sign-in returns none of them, so it cannot make
        a working channel.  It must fail naming the fields, not succeed."""
        with _no_db_no_disk() as (wire, _watch):
            r = _callback(_oauth_app(), 'twitter',
                          {'access_token': 'a', 'refresh_token': 'r'})
        assert r.status_code == 502 and b'Connection failed.' in r.data
        assert b'api_key' in r.data and b'access_secret' in r.data
        wire.assert_not_called()


# ─── _report_when_live tells the user how the start ended ───────────────

class TestReportWhenLive:
    def _registry(self, statuses):
        seq = iter(statuses)
        adapter = MagicMock()
        adapter.get_status.side_effect = lambda: next(seq, statuses[-1])
        reg = MagicMock()
        reg.get.return_value = adapter
        return reg

    def test_connected_sends_the_connected_card(self, lui):
        from integrations.channels.agent_tools import _report_when_live
        reg = self._registry([ChannelStatus.CONNECTING, ChannelStatus.CONNECTED])
        with patch('integrations.channels.registry.get_registry', return_value=reg):
            ok = _report_when_live('telegram', {'display_name': 'Telegram'},
                                   'u-1', wait_seconds=2, poll_seconds=0.01)
        assert ok is True
        (card,) = _cards(lui, 'channel_connected')
        assert card['channel'] == 'telegram'
        assert lui.agent_ui_update.call_args.kwargs['user_id'] == 'u-1'

    def test_a_failed_start_is_reported_without_waiting_out_the_clock(self, lui):
        import time
        from integrations.channels.agent_tools import _report_when_live
        reg = self._registry([ChannelStatus.ERROR])
        t0 = time.monotonic()
        with patch('integrations.channels.registry.get_registry', return_value=reg), \
                patch('integrations.social.fleet_command.emit_channel_unhealthy'), \
                patch('integrations.social.models.get_db', return_value=MagicMock()):
            ok = _report_when_live('telegram', {'display_name': 'Telegram'},
                                   'u-1', wait_seconds=30, poll_seconds=0.01)
        assert ok is False and time.monotonic() - t0 < 5
        assert 'start failed' in _cards(lui, 'toast')[0]['text']

    def test_never_connecting_is_a_toast_and_a_fleet_banner(self, lui):
        from integrations.channels.agent_tools import _report_when_live
        reg = self._registry([ChannelStatus.DISCONNECTED])
        fanout = MagicMock()
        with patch('integrations.channels.registry.get_registry', return_value=reg), \
                patch('integrations.social.fleet_command.emit_channel_unhealthy',
                      fanout), \
                patch('integrations.social.models.get_db', return_value=MagicMock()):
            ok = _report_when_live('telegram', {'display_name': 'Telegram'},
                                   'u-1', wait_seconds=0.05, poll_seconds=0.01)
        assert ok is False
        (toast,) = _cards(lui, 'toast')
        assert toast['severity'] == 'error' and 'disconnected' in toast['text']
        assert _cards(lui, 'channel_connected') == []
        assert fanout.call_args.kwargs['channel_type'] == 'telegram'


# ─── The chat connect form's submit reaches register_channel ────────────

def _call_view(view, path, body, *args):
    app = Flask(__name__)
    with app.test_request_context(path, method='POST', json=body):
        g.user_id = 'u-1'
        return view.__wrapped__(*args)


class TestConnectFormSubmit:
    def test_every_field_reaches_register_channel_for_this_user(self):
        from integrations.social import api_channels
        seen = []

        def fake_register(ch, cfg):
            seen.append((ch, json.loads(cfg)))
            return 'Mattermost registered and enabled! Auth: x.'

        tools = [('register_channel', 'd', fake_register)]
        with patch('integrations.channels.agent_tools.build_channel_tool_closures',
                   return_value=tools) as build:
            resp = _call_view(api_channels.channel_connect_submit,
                              '/api/social/channels/mattermost/connect',
                              {'server_url': 'https://mm', 'access_token': 't'},
                              'mattermost')
        assert build.call_args.args[0]['user_id'] == 'u-1'
        assert seen == [('mattermost', {'server_url': 'https://mm',
                                        'access_token': 't'})]
        assert resp.get_json()['success'] is True

    def test_an_incomplete_form_is_an_error_not_a_success(self):
        from integrations.social import api_channels
        tools = [('register_channel', 'd',
                  lambda ch, cfg: "Slack registered with partial config. "
                                  "Missing: ['bot_token'].")]
        with patch('integrations.channels.agent_tools.build_channel_tool_closures',
                   return_value=tools):
            resp, status = _call_view(api_channels.channel_connect_submit,
                                      '/api/social/channels/slack/connect',
                                      {}, 'slack')
        assert status == 400 and 'Missing' in resp.get_json()['error']


# ─── WhatsApp from chat: a QR, refreshed until scanned ──────────────────

_DESKTOP_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/126.0 Safari/537.36')
_PHONE_UA = ('Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 '
             '(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36')

_HELPER_NAMES = ('_PHONE_USER_AGENT', '_user_agent_is_phone',
                 '_start_gateway_qr_pair_push')


def _load_link_helper(ensure_live, user_agent=_DESKTOP_UA, name=None):
    """The real helper (and its device check) compiled out of the entry
    module; ``name`` picks which to return."""
    import re
    tree = ast.parse(open(_ENTRY, encoding='utf-8').read())
    defs = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in _HELPER_NAMES)
            or (isinstance(n, ast.Assign) and any(
                getattr(t, 'id', None) in _HELPER_NAMES for t in n.targets))]
    thread_local = SimpleNamespace(get_user_id=lambda: 'u-7',
                                   get_prompt_id=lambda: 'p-1',
                                   get_client_user_agent=lambda: user_agent)
    ns = {'thread_local_data': thread_local, 'logging': logging, 're': re,
          'logger': logging.getLogger('test.entry'),
          '_ensure_whatsapp_live_adapter': ensure_live}
    exec(compile(ast.Module(body=defs, type_ignores=[]), _ENTRY, 'exec'), ns)
    return ns[name or '_start_gateway_qr_pair_push']


class _InlineThread:
    """Runs the watcher on start() so the test sees its whole life."""
    def __init__(self, target=None, **_kw):
        self._target = target

    def start(self):
        self._target()


def _status(**kw):
    return kw


class _DrivesTheLinkHelper:
    """Runs the real WhatsApp link helper against a scripted gateway."""

    def _run(self, statuses, phone=None, pair_body=None,
             user_agent=_DESKTOP_UA, profile_phone=None, notify_error=None,
             profile_attr='phone'):
        """Drive the real helper.  ``profile_phone`` is the number on the
        user's profile row (None: the profile read fails); ``notify_error``
        makes the phone push raise."""
        ensure_live = MagicMock(return_value={'success': True})
        link = _load_link_helper(ensure_live, user_agent=user_agent)
        self.link = link
        registered = []
        tools = [('register_channel', 'd',
                  lambda ch, cfg: registered.append((ch, cfg)) or 'ok')]
        posts = []
        seq = iter(statuses)

        def gateway(method, path, **kw):
            if method == 'POST':
                posts.append(path)
                return (pair_body or {'success': True}), 200
            return next(seq, statuses[-1]), 200

        # The fleet bus is a boundary too: its Crossbar leg imports
        # hartos.crossbar_server when autobahn is installed (as on CI), and
        # that import starts reuse_recipe's scheduler thread, which the
        # inline Thread above would run forever.
        self.bus = MagicMock()
        with patch('integrations.social.api_channels._proxy_gateway',
                   side_effect=gateway), \
                patch('threading.Thread', _InlineThread), \
                patch('time.sleep'), \
                patch('integrations.channels.agent_tools.build_channel_tool_closures',
                      return_value=tools), \
                patch('integrations.social.models.db_session',
                      side_effect=self._db_session(profile_phone, profile_attr)), \
                patch('integrations.social.services.NotificationService.create',
                      side_effect=notify_error), \
                patch('core.peer_link.message_bus.get_message_bus',
                      return_value=self.bus):
            self.how = link('whatsapp', {'display_name': 'WhatsApp'},
                            phone=phone)
        return posts, registered, ensure_live

    @staticmethod
    def _db_session(profile_phone, profile_attr='phone'):
        """The user store as the helper reads it: a profile row carrying
        ``profile_phone``, or a store that cannot be read."""
        if profile_phone is None:
            return RuntimeError('no db')

        @contextmanager
        def session(*_a, **_k):
            db = MagicMock()
            db.query.return_value.filter_by.return_value.first.return_value = (
                SimpleNamespace(**{profile_attr: profile_phone}))
            yield db
        return session

class TestWhatsAppLinksByQR(_DrivesTheLinkHelper):

    def test_no_number_shows_each_fresh_qr_then_connects(self, lui, monkeypatch):
        monkeypatch.delenv('HEVOLVE_WHATSAPP_PHONE', raising=False)
        posts, registered, ensure_live = self._run([
            _status(qr='QR-A'), _status(qr='QR-A'), _status(qr='QR-B'),
            _status(authenticated=True),
        ])
        qrs = _cards(lui, 'qr_pair')
        assert [c['qr'] for c in qrs] == ['QR-A', 'QR-B']
        assert len({c['msg_id'] for c in qrs}) == 2
        assert _cards(lui, 'form') == []          # no phone question
        assert not any('request-pair-code' in u for u in posts)
        assert registered == [('whatsapp', '{}')]
        ensure_live.assert_called_once()
        assert _cards(lui, 'channel_connected')[0]['channel'] == 'whatsapp'
        assert lui.agent_ui_update.call_args.kwargs['user_id'] == 'u-7'

    def test_a_number_asks_for_a_pair_code_and_shows_no_qr(self, lui, monkeypatch):
        monkeypatch.delenv('HEVOLVE_WHATSAPP_PHONE', raising=False)
        posts, _reg, _live = self._run(
            [_status(qr='QR-A'), _status(authenticated=True)],
            phone='+91 90000 00000', pair_body={'code': 'ABCD1234'})
        assert any('request-pair-code' in u for u in posts)
        assert _cards(lui, 'pair_code')[0]['code'] == 'ABCD1234'
        assert _cards(lui, 'qr_pair') == []
        # The iOS companion gets the same code over the fleet bus.
        topic, cmd = self.bus.publish.call_args.args
        assert topic == 'fleet.command.user' and cmd['code'] == 'ABCD1234'

    def test_a_newer_connect_supersedes_the_older_watcher(self, lui, monkeypatch):
        """Two watchers on one session would both announce "connected" (and
        both time out).  A new attempt retires the running one."""
        monkeypatch.delenv('HEVOLVE_WHATSAPP_PHONE', raising=False)
        ensure_live = MagicMock(return_value={'success': True})
        link = _load_link_helper(ensure_live)
        state = {'polls': 0}

        def gateway(method, path, **kw):
            if method == 'POST':
                return {'success': True}, 200
            state['polls'] += 1
            if state['polls'] == 1:
                # While the first watcher polls, the user connects again.
                link.__dict__['attempts'][path.split('/')[3]] = object()
            return {'qr': 'QR-A'}, 200

        with patch('integrations.social.api_channels._proxy_gateway',
                   side_effect=gateway), \
                patch('threading.Thread', _InlineThread), patch('time.sleep'):
            link('whatsapp', {'display_name': 'WhatsApp'})
        assert state['polls'] == 1          # it stopped at the next check
        assert _cards(lui, 'toast') == []   # and did not time out loudly
        ensure_live.assert_not_called()

    def test_an_unscanned_code_expires_loudly(self, lui, monkeypatch):
        monkeypatch.delenv('HEVOLVE_WHATSAPP_PHONE', raising=False)
        clock = iter(range(0, 10_000, 100))
        with patch('time.time', side_effect=lambda: next(clock)):
            self._run([_status(qr='QR-A')] * 5)
        toast = _cards(lui, 'toast')[-1]
        assert toast['severity'] == 'error' and 'expired' in toast['text']
        assert 'whatsapp' not in str(self.link.__dict__.get('attempts'))
        assert self.link.__dict__['attempts'] == {}


class TestWhatsAppLinksTheWayThisDeviceCan(_DrivesTheLinkHelper):
    """Review of #130: QR-always broke the one-tap flow on the phone.  A
    phone cannot scan a QR it is showing, and before #130 a number on file
    sent the pair code straight to the phone's clipboard.  The method now
    follows the number on file and the device."""

    def test_on_a_phone_with_no_number_the_number_is_asked(self, lui,
                                                           monkeypatch):
        monkeypatch.delenv('HEVOLVE_WHATSAPP_PHONE', raising=False)
        posts, registered, _live = self._run([_status(qr='QR-A')],
                                            user_agent=_PHONE_UA)
        assert self.how == 'ask_phone'
        (form,) = _cards(lui, 'form')
        assert [f['name'] for f in form['fields']] == ['phone']
        assert form['action'] == '/api/social/channels/whatsapp/connect-pair-code'
        assert _cards(lui, 'qr_pair') == []
        assert posts == []                  # no gateway session, no QR socket
        assert self.link.__dict__.get('attempts', {}) == {}

    def test_a_number_on_file_sends_the_code_to_the_phone(self, lui,
                                                          monkeypatch):
        # The pre-#130 one-tap flow: profile number -> pair code on the
        # phone's clipboard (consent.* push) and in chat; no QR, no form.
        monkeypatch.delenv('HEVOLVE_WHATSAPP_PHONE', raising=False)
        for ua in (_PHONE_UA, _DESKTOP_UA):
            lui.reset_mock()
            posts, _reg, _live = self._run(
                [_status(qr='QR-A'), _status(authenticated=True)],
                user_agent=ua, profile_phone='+91 90030 54371',
                pair_body={'code': 'WXYZ1234'})
            assert self.how == 'pair_code', ua
            assert any('request-pair-code' in u for u in posts), ua
            assert _cards(lui, 'pair_code')[0]['code'] == 'WXYZ1234', ua
            assert _cards(lui, 'qr_pair') == [] and _cards(lui, 'form') == []

    def test_the_hevolve_database_field_name_is_read_too(self, lui,
                                                         monkeypatch):
        # Hevolve_Database's user record calls the field ``phone_number``.
        monkeypatch.delenv('HEVOLVE_WHATSAPP_PHONE', raising=False)
        posts, _reg, _live = self._run(
            [_status(qr='QR-A'), _status(authenticated=True)],
            profile_phone='+91 90030 54371', profile_attr='phone_number',
            pair_body={'code': 'WXYZ1234'})
        assert self.how == 'pair_code'
        assert any('request-pair-code' in u for u in posts)

    def test_on_a_desktop_with_no_number_the_qr_is_shown(self, lui,
                                                         monkeypatch):
        monkeypatch.delenv('HEVOLVE_WHATSAPP_PHONE', raising=False)
        self._run([_status(qr='QR-A'), _status(authenticated=True)],
                 user_agent=_DESKTOP_UA, profile_phone='')
        assert self.how == 'qr'
        assert [c['qr'] for c in _cards(lui, 'qr_pair')] == ['QR-A']
        assert _cards(lui, 'form') == []
        assert self.link.__dict__['attempts'] == {}   # retired on connect

    def test_a_failed_phone_push_is_a_warning(self, lui, monkeypatch, caplog):
        monkeypatch.delenv('HEVOLVE_WHATSAPP_PHONE', raising=False)
        with caplog.at_level(logging.WARNING):
            self._run([_status(authenticated=True)], phone='919003054371',
                     pair_body={'code': 'WXYZ1234'},
                     notify_error=RuntimeError('db locked'))
        assert any('not pushed' in r.getMessage() and r.levelno == logging.WARNING
                   for r in caplog.records)
        assert _cards(lui, 'pair_code')[0]['code'] == 'WXYZ1234'

    def test_a_refused_pair_code_retires_the_attempt(self, lui, monkeypatch):
        monkeypatch.delenv('HEVOLVE_WHATSAPP_PHONE', raising=False)
        self._run([_status(qr='QR-A')], phone='919003054371',
                 pair_body={'error': 'rate limited'})
        assert self.how is None
        assert 'rate limited' in _cards(lui, 'toast')[0]['text']
        assert self.link.__dict__['attempts'] == {}


class TestDeviceFromUserAgent:
    @pytest.mark.parametrize('ua, phone', [
        (_PHONE_UA, True),
        ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) '
         'AppleWebKit/605.1.15 Mobile/15E148', True),
        ('Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15',
         True),
        ('okhttp/4.12.0', True),                       # RN on Android
        ('Hevolve/3 CFNetwork/1494.0.7 Darwin/23.4.0', True),  # RN on iOS
        (_DESKTOP_UA, False),
        ('Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 '
         '(KHTML, like Gecko) Version/17.5 Safari/605.1.15', False),
        ('python-requests/2.32.5', False),             # Nunba's server side
        ('', False), (None, False),
    ])
    def test_phone_or_not(self, ua, phone):
        is_phone = _load_link_helper(MagicMock(), name='_user_agent_is_phone')
        assert is_phone(ua) is phone


class TestUnhealthyBannerNeedsAnOwner:
    def test_no_owner_sends_no_banner(self, lui, caplog):
        from integrations.channels.agent_tools import _report_when_live
        adapter = MagicMock()
        adapter.get_status.return_value = ChannelStatus.ERROR
        reg = MagicMock()
        reg.get.return_value = adapter
        fanout = MagicMock()
        with patch('integrations.channels.registry.get_registry',
                   return_value=reg), \
                patch('integrations.social.fleet_command.emit_channel_unhealthy',
                      fanout), \
                caplog.at_level(logging.WARNING):
            assert _report_when_live('telegram', {'display_name': 'Telegram'},
                                     None, wait_seconds=1,
                                     poll_seconds=0.01) is False
        fanout.assert_not_called()
        assert any('no owner' in r.getMessage() for r in caplog.records)


class TestPairCodeRouteKeepsTheNumberToThisRequest:
    def test_number_is_passed_not_written_to_the_process_env(self, monkeypatch):
        from integrations.social import api_channels
        monkeypatch.delenv('HEVOLVE_WHATSAPP_PHONE', raising=False)
        link = MagicMock()
        fake_entry = SimpleNamespace(_start_gateway_qr_pair_push=link)
        with swap_modules({'hart_intelligence_entry': fake_entry}):
            resp = _call_view(api_channels.channel_connect_pair_code_submit,
                              '/api/social/channels/whatsapp/connect-pair-code',
                              {'phone': '+91 90000 00000'}, 'whatsapp')
        assert resp.get_json()['success'] is True
        assert link.call_args.kwargs == {'phone': '919000000000', 'owner': 'u-1'}
        assert 'HEVOLVE_WHATSAPP_PHONE' not in os.environ
