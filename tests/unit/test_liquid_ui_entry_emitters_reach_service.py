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
           '_handle_join_external_room_tool', '_start_gateway_qr_pair_push')


class _ThreadLocal:
    def __init__(self, uid):
        self._uid = uid

    def get_user_id(self):
        return self._uid

    def get_prompt_id(self):
        return 'p-1'


def _load(uid='u-7'):
    src = open(_ENTRY, encoding='utf-8').read()
    tree = ast.parse(src)
    defs = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in _WANTED]
    assert sorted(d.name for d in defs) == sorted(_WANTED)
    ns = {'thread_local_data': _ThreadLocal(uid),
          'logger': logging.getLogger('test.entry'), 'logging': logging}
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
def test_pair_code_phone_form_names_the_user_or_none(lui, monkeypatch, uid,
                                                     expected):
    """F5: with no thread-local user the card must pass user_id=None so
    agent_ui_update resolves the owner, never route to a user 'system'."""
    ns = _load(uid)
    monkeypatch.setenv('HEVOLVE_WHATSAPP_PHONE', '')
    with patch('integrations.social.models.db_session',
               side_effect=RuntimeError('no db')):
        ns['_start_gateway_qr_pair_push']('whatsapp',
                                          {'display_name': 'WhatsApp'})
    args, kwargs = lui.agent_ui_update.call_args
    assert args[1]['type'] == 'form'
    assert kwargs['user_id'] == expected


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
