"""Four hart_intelligence_entry emitters reach the ONE LiquidUIService.

Review of a0ecafe09: the QR-pair emitter, the Connect_Channel OAuth link and
credential form, and Join_External_Room's consent notification looked the
service up with ``ServiceRegistry.get('LiquidUIService')`` -- on the CLASS.
That raises TypeError (missing 'name'), each site swallows it, and the card
is never delivered.  The canonical accessor is
``get_registry().get_or_none(name)`` (core.platform.registry: "callers must
not ... call .get() on the class").

No interpreter here imports hart_intelligence_entry from source, so these
tests compile the REAL function definitions out of the file (AST, no text
edits) and run them in a namespace holding only the module globals they
read (thread_local_data, logger, logging).  Every import inside the
functions is the real module; mocked are the boundaries: the registered
service, the channel registration closure, channel metadata, the OAuth URL
builder, the room-presence gate and the channel adapter.
"""
import ast
import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.platform.registry import get_registry, reset_registry

_ENTRY = os.path.join(os.path.dirname(__file__), '..', '..',
                      'hart_intelligence_entry.py')
_WANTED = ('_wire_qr_pair_emitter', '_handle_connect_channel_tool',
           '_handle_join_external_room_tool', '_start_gateway_qr_pair_push',
           '_user_agent_is_phone')
# Module-level values the functions read.
_WANTED_VALUES = ('_PHONE_USER_AGENT',)


class _ThreadLocal:
    def __init__(self, uid):
        self._uid = uid

    def get_user_id(self):
        return self._uid

    def get_prompt_id(self):
        return 'p-1'

    def get_client_user_agent(self):
        return 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0'


def _load(uid='u-7'):
    src = open(_ENTRY, encoding='utf-8').read()
    tree = ast.parse(src)
    import re
    defs = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in _WANTED]
    assert sorted(d.name for d in defs) == sorted(_WANTED)
    values = [n for n in tree.body if isinstance(n, ast.Assign) and any(
        getattr(t, 'id', None) in _WANTED_VALUES for t in n.targets)]
    ns = {'thread_local_data': _ThreadLocal(uid), 're': re,
          'logger': logging.getLogger('test.entry'), 'logging': logging}
    defs = values + defs
    exec(compile(ast.Module(body=defs, type_ignores=[]), _ENTRY, 'exec'), ns)
    return ns


@pytest.fixture()
def lui():
    reset_registry()
    svc = MagicMock()
    svc.agent_ui_update.return_value = True
    get_registry().register('LiquidUIService', lambda: svc)
    yield svc
    reset_registry()


def _closures(result):
    return [('register_channel', 'desc', lambda ct, cfg: result)]


def test_join_external_room_consent_card_is_delivered(lui):
    ns = _load()
    with patch('integrations.social.room_presence_service.gate',
               return_value=(False, 'Grant room presence first.')):
        out = ns['_handle_join_external_room_tool']('discord 123')
    assert out == 'Grant room presence first.'
    lui.agent_ui_update.assert_called_once()
    args, kwargs = lui.agent_ui_update.call_args
    assert args[1]['type'] == 'notification'
    assert kwargs.get('user_id') == 'u-7'


def test_connect_channel_oauth_link_is_delivered(lui):
    # build_authorize_url takes int(user_id): an OAuth user is a numeric id
    ns = _load('42')
    meta = {'display_name': 'Slack', 'setup_fields': []}
    with patch('integrations.channels.agent_tools.build_channel_tool_closures',
               return_value=_closures('Missing: bot_token')), \
            patch('integrations.channels.metadata.get_channel_metadata',
                  return_value=meta), \
            patch('integrations.channels.metadata.is_oauth_configured',
                  return_value=True), \
            patch('integrations.channels.oauth_api.build_authorize_url',
                  return_value=('https://slack.example/auth', 'st')):
        out = ns['_handle_connect_channel_tool']('slack')
    assert 'click the button I just sent' in out
    args, kwargs = lui.agent_ui_update.call_args
    assert args[1]['type'] == 'oauth_link'
    assert args[1]['authorize_url'] == 'https://slack.example/auth'
    assert kwargs.get('user_id') == '42'


def test_connect_channel_credential_form_is_delivered(lui):
    ns = _load()
    meta = {'display_name': 'Telegram',
            'setup_fields': [{'key': 'bot_token', 'label': 'Bot token'},
                             {'key': 'api_url', 'auto': True}]}
    with patch('integrations.channels.agent_tools.build_channel_tool_closures',
               return_value=_closures('Missing: bot_token')), \
            patch('integrations.channels.metadata.get_channel_metadata',
                  return_value=meta), \
            patch('integrations.channels.metadata.is_oauth_configured',
                  return_value=False):
        ns['_handle_connect_channel_tool']('telegram')
    args, kwargs = lui.agent_ui_update.call_args
    assert args[1]['type'] == 'form'
    assert [f['name'] for f in args[1]['fields']] == ['bot_token']
    # The renderer POSTs to `action`; a form without one dropped the token.
    assert args[1]['action'] == '/api/social/channels/telegram/connect'
    assert kwargs.get('user_id') == 'u-7'


def test_qr_pair_card_is_delivered_when_the_adapter_emits_a_qr(lui):
    ns = _load()
    adapter = MagicMock()
    adapter.disconnect = AsyncMock()
    adapter.connect = AsyncMock()
    chan_reg = MagicMock()
    chan_reg.get.return_value = adapter
    with patch('integrations.channels.registry.get_registry',
               return_value=chan_reg):
        ns['_wire_qr_pair_emitter']('whatsapp', {'display_name': 'WhatsApp'})
    adapter.set_qr_callback.assert_called_once()
    emit = adapter.set_qr_callback.call_args.args[0]
    emit('QRDATA')
    args, kwargs = lui.agent_ui_update.call_args
    assert args[1]['type'] == 'qr_pair' and args[1]['qr'] == 'QRDATA'
    assert kwargs.get('user_id') == 'u-7'


@pytest.mark.parametrize('uid,expected', [('u-7', 'u-7'), (None, None)])
def test_whatsapp_link_failure_names_the_user_or_none(lui, monkeypatch, uid,
                                                      expected):
    """F5: with no thread-local user the card must pass user_id=None so
    agent_ui_update resolves the owner, never route to a user 'system'.

    On a desktop with no number WhatsApp links by QR, so there is no phone
    form; a gateway that cannot be reached is the card the user sees, and it
    must be addressed the same way."""
    ns = _load(uid)
    monkeypatch.setenv('HEVOLVE_WHATSAPP_PHONE', '')
    # The ONE gateway client answers (None, 503) when it cannot connect.
    with patch('integrations.social.api_channels._proxy_gateway',
               return_value=(None, 503)):
        ns['_start_gateway_qr_pair_push']('whatsapp',
                                          {'display_name': 'WhatsApp'})
    args, kwargs = lui.agent_ui_update.call_args
    assert args[1]['type'] == 'toast' and args[1]['severity'] == 'error'
    assert 'WhatsApp' in args[1]['text']
    assert kwargs['user_id'] == expected


@pytest.mark.parametrize('text,phone', [
    ('whatsapp', None),
    ('whatsapp +91 90030 54371', '919003054371'),
    ('whatsapp 919003054371', '919003054371'),
    ('whatsapp {"phone": "+91 90030 54371"}', '919003054371'),
    ('whatsapp 12345', None),            # too short to be a phone number
])
def test_connect_whatsapp_uses_a_number_only_when_given(lui, text, phone):
    """A number in the chat input asks for a pair code (the way in for
    someone whose only device is the phone); the name alone shows the QR.
    It used to accept only the JSON form, so '+91 ...' silently meant QR."""
    ns = _load()
    link = MagicMock()
    ns['_start_gateway_qr_pair_push'] = link
    meta = {'display_name': 'WhatsApp', 'auth_method': 'gateway_qr',
            'setup_fields': []}
    with patch('integrations.channels.agent_tools.build_channel_tool_closures',
               return_value=_closures('WhatsApp registered and enabled!')), \
            patch('integrations.channels.metadata.get_channel_metadata',
                  return_value=meta):
        ns['_handle_connect_channel_tool'](text)
    assert link.call_args.kwargs == {'phone': phone}


@pytest.mark.parametrize('how, says', [
    ('pair_code', 'linking code'),
    ('ask_phone', 'Enter your WhatsApp number'),
    ('qr', 'Scan the QR code'),
    (None, "couldn't start"),
])
def test_the_reply_names_the_way_the_link_was_started(lui, how, says):
    """The reply tells the user what to do with what they were shown; it
    used to say "scan the QR" whatever the device, including on the phone
    that would have to scan it."""
    ns = _load()
    ns['_start_gateway_qr_pair_push'] = MagicMock(return_value=how)
    meta = {'display_name': 'WhatsApp', 'auth_method': 'gateway_qr',
            'setup_fields': []}
    with patch('integrations.channels.agent_tools.build_channel_tool_closures',
               return_value=_closures('WhatsApp registered and enabled! '
                                      'Link it from your phone to finish.')), \
            patch('integrations.channels.metadata.get_channel_metadata',
                  return_value=meta):
        out = ns['_handle_connect_channel_tool']('whatsapp')
    assert says in out
    if how != 'qr':
        assert 'Scan the QR' not in out


def test_no_thread_local_user_passes_none_not_system(lui):
    """F5 on the QR-pair and Connect_Channel form sites."""
    ns = _load(None)
    adapter = MagicMock()
    adapter.disconnect = AsyncMock()
    adapter.connect = AsyncMock()
    chan_reg = MagicMock()
    chan_reg.get.return_value = adapter
    with patch('integrations.channels.registry.get_registry',
               return_value=chan_reg):
        ns['_wire_qr_pair_emitter']('whatsapp', {'display_name': 'WhatsApp'})
    adapter.set_qr_callback.call_args.args[0]('QR')
    assert lui.agent_ui_update.call_args.kwargs['user_id'] is None

    lui.agent_ui_update.reset_mock()
    meta = {'display_name': 'Telegram',
            'setup_fields': [{'key': 'bot_token'}]}
    with patch('integrations.channels.agent_tools.build_channel_tool_closures',
               return_value=_closures('Missing: bot_token')),             patch('integrations.channels.metadata.get_channel_metadata',
                  return_value=meta),             patch('integrations.channels.metadata.is_oauth_configured',
                  return_value=False):
        ns['_handle_connect_channel_tool']('telegram')
    assert lui.agent_ui_update.call_args.kwargs['user_id'] is None


def test_no_service_registered_is_a_quiet_no_op():
    """On HART OS the backend registers no LiquidUIService: the handlers
    still return their text and do not raise."""
    reset_registry()
    try:
        ns = _load()
        with patch('integrations.social.room_presence_service.gate',
                   return_value=(False, 'nope')):
            assert ns['_handle_join_external_room_tool']('discord 1') == 'nope'
    finally:
        reset_registry()


def test_source_guard_no_class_call_lookup_of_liquid_ui():
    """DRY guard: the class-call shape can only ever raise, so a second copy
    of it anywhere in the entry is a silent card drop."""
    tree = ast.parse(open(_ENTRY, encoding='utf-8').read())
    bad = [n.lineno for n in ast.walk(tree)
           if isinstance(n, ast.Call)
           and isinstance(n.func, ast.Attribute) and n.func.attr == 'get'
           and isinstance(n.func.value, ast.Name)
           and n.func.value.id == 'ServiceRegistry'
           and n.args and isinstance(n.args[0], ast.Constant)
           and n.args[0].value == 'LiquidUIService']
    assert bad == [], 'ServiceRegistry.get on the class at lines %s' % bad
