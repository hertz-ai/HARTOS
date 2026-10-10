"""AI-native Liquid UI registry, slice 1 (2026-10-10).

An LLM that composes UI needs three things the A2UI protocol did not give it:
a typed contract per component (28 of 29 specs said every attribute is 'any'),
a verdict it can read when a push is refused (agent_ui_update returned a bare
bool and logged the reason), and a catalogue it can be handed (prompt / JSON
schema) instead of one somebody hand-wrote.

Behavioural: a real LiquidUIService, the security boundary mocked only where a
test is not about it.  The payload fixtures below are copied from the real emit
sites (named in each docstring), so the typed schema is pinned to what is
actually sent, not to what a doc says is sent.

    python -m pytest tests/unit/test_a2ui_ai_native_registry.py --noconftest -p no:capture -q
"""
import json
from unittest.mock import MagicMock, patch

import pytest

import integrations.agent_engine.liquid_ui_service as m
from integrations.agent_engine.liquid_ui_service import (
    COMPONENT_TYPES, LiquidUIService)

TYPED = (
    'card', 'list', 'form', 'chart', 'progress', 'notification', 'approval',
    'code', 'markdown', 'media', 'layout', 'product_card', 'cart', 'checkout',
    'payment_status', 'order_tracking', 'comparison', 'agent_action',
    'navigate', 'meet_copilot', 'qr_pair', 'oauth_link', 'toast',
    'pair_code', 'channel_connected',
)


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv('HEVOLVE_DATA_DIR', str(tmp_path))
    with patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=False), \
            patch('security.immutable_audit_log.get_audit_log',
                  return_value=MagicMock()), \
            patch('core.platform.events.emit_event'):
        yield LiquidUIService(a2ui_enabled=True)


def _issues(name, component):
    entry = COMPONENT_TYPES[name]
    return m.validate_component(name, {'type': name, **component}, entry)


def _errors(issues):
    return [i for i in issues if i['level'] == 'error']


# ── the typed registry ─────────────────────────────────────────────────────

def test_the_common_types_carry_a_typed_contract(svc):
    for name in TYPED:
        entry = COMPONENT_TYPES[name]
        assert isinstance(entry.get('attributes'), dict) and entry['attributes'], name
        spec = svc.get_component_spec(name)
        assert spec['attributes'] == entry['attributes'], name
        assert set(spec['attributes'].values()) != {'any'}, name


def test_every_type_grammar_is_valid_and_required_are_declared():
    for name, entry in COMPONENT_TYPES.items():
        attrs = m._attributes_for(entry)
        if not attrs:
            continue
        for key, spec in attrs.items():
            assert m._attr_grammar_ok(spec), (name, key, spec)
        assert set(entry.get('required') or []) <= set(attrs), name


def test_no_declared_prop_was_dropped_by_typing():
    # 'type' is the component's own key, not an attribute.
    for name in TYPED:
        entry = COMPONENT_TYPES[name]
        assert set(entry['props']) - {'type'} <= set(entry['attributes']), name


def test_every_example_is_valid_against_its_own_schema():
    for name in TYPED:
        entry = COMPONENT_TYPES[name]
        example = entry.get('example')
        assert example, f'{name} has no example'
        assert m.validate_component(
            name, {'type': name, **example}, entry) == [], name


# ── validate_component (pure) ──────────────────────────────────────────────

def test_enum_violation_names_the_allowed_values():
    (issue,) = _errors(_issues('notification', {'severity': 'loud'}))
    assert issue['code'] == 'enum' and issue['path'] == 'severity'
    assert issue['expected'] == 'info|success|warning|error'
    assert issue['got'] == "'loud'"


def test_wrong_type_is_reported_with_expected_and_got():
    (issue,) = _errors(_issues('list', {'items': 'a,b'}))
    assert (issue['code'], issue['expected']) == ('type', 'list')


def test_a_bool_is_not_a_number():
    assert _errors(_issues('progress', {'value': True}))


def test_missing_required_is_an_error_and_none_counts_as_missing():
    (issue,) = _errors(_issues('toast', {}))
    assert issue['code'] == 'required' and issue['path'] == 'message'
    assert _errors(_issues('toast', {'message': None}))


def test_unknown_prop_is_a_warning_with_a_close_match_hint():
    issues = _issues('notification', {'message': 'x', 'mesage': 'y'})
    (issue,) = [i for i in issues if i['code'] == 'unknown_prop']
    assert issue['level'] == 'warning' and "'message'" in issue['hint']
    assert not _errors(issues)


def test_the_measured_toast_text_drift_gets_an_alias_hint():
    issues = _issues('toast', {'text': 'Discord disconnected.'})
    (unknown,) = [i for i in issues if i['code'] == 'unknown_prop']
    assert unknown['path'] == 'text' and "'message'" in unknown['hint']


def test_envelope_and_underscore_keys_are_not_unknown_props():
    issues = _issues('notification', {
        '_ts': 1.0, '_agent_id': 'a', '_spec': {}, 'msg_id': 'x',
        'agent_name': 'N', 'agent_id': 'a', 'user_id': 'u', 'timestamp': 1})
    assert issues == []


def test_a_malformed_entry_or_component_degrades_instead_of_raising():
    # A custom-types file is hand-editable; a push must not die on its shape.
    entry = {'props': ['a'], 'attributes': {'a': 7, 'b': 'str'},
             'required': 'a', 'aliases': ['x']}
    assert m.validate_component(
        't', {'type': 't', 1: 'int key', 'a': 'v', 'b': 3, 'x': 1}, entry
    ) == [
        {'path': 'b', 'code': 'type', 'level': 'error', 'expected': 'str',
         'got': '3', 'hint': "'b' must be str"},
        {'path': 'x', 'code': 'unknown_prop', 'level': 'warning',
         'expected': None, 'got': '1',
         'hint': 't declares: a, b'}]


# A custom-types file is hand-editable.  Every reader of an entry goes through
# ONE normaliser, so none of them can be the one that still trusts the file.
HAND_EDITED = [
    {'props': ['r'], 'attributes': {'r': 7}},                       # int type
    {'props': ['r'], 'attributes': {'r': ['a']}},                   # list type
    {'props': ['r'], 'attributes': {'r': 'integer'}},               # not in grammar
    {'props': ['r'], 'attributes': {'r': 'str'}, 'required': 'r'},  # not a list
    {'props': ['r'], 'attributes': {'r': 'str'}, 'required': [['r']]},
    {'props': ['r'], 'attributes': {'r': 'str'}, 'doc': 5, 'aliases': 3},
    {'props': 'abc'},                                               # not a list
    {'props': [['r']]},                                             # unhashable
]


@pytest.mark.parametrize('entry', HAND_EDITED)
def test_every_catalogue_reader_survives_a_hand_edited_custom_entry(svc, entry):
    svc._custom_component_types = {'ringb': entry}
    assert 'ringb' in svc.component_prompt(['ringb'])
    json.dumps(svc.component_json_schema('ringb'))
    verdict = svc.agent_ui_compose('a', {'type': 'ringb', 'r': 1}, strict=True)
    assert verdict['type'] == 'ringb'


def test_a_type_the_grammar_does_not_define_is_not_checked():
    # 'integer' is not a type this grammar knows; guessing would refuse every
    # value, so it is read as 'any'.
    entry = {'props': ['r'], 'attributes': {'r': 'integer'}}
    assert m.validate_component('t', {'type': 't', 'r': 'anything'}, entry) == []


def test_an_untyped_custom_type_only_gets_unknown_prop_warnings():
    entry = {'props': ['radius'], 'spec': {'attributes': {'radius': 'any'}}}
    issues = m.validate_component('ring', {'type': 'ring', 'radius': 'huge',
                                           'colour': 'red'}, entry)
    assert [i['code'] for i in issues] == ['unknown_prop']


# ── real emitters conform ──────────────────────────────────────────────────

REAL = {
    # hart_intelligence_entry._wire_qr_pair_emitter
    'qr_pair': {'channel': 'whatsapp', 'title': 'Scan to connect WhatsApp',
                'help': 'Open WhatsApp on your phone.', 'qr': '2@abc'},
    # hart_intelligence_entry gateway_qr pair-code push
    'pair_code': {'channel': 'whatsapp', 'channel_type': 'whatsapp',
                  'display_name': 'WhatsApp', 'color': '#25D366',
                  'icon': 'whatsapp', 'code': 'ABCD1234', 'expires_in': 60,
                  'clipboard_payload': 'ABCD1234',
                  'deeplink': 'whatsapp://settings/linked-devices',
                  'notification_id': 7, 'instructions': 'Paste the code.'},
    # hart_intelligence_entry Connect_Channel OAuth fork
    'oauth_link': {'channel': 'google', 'channel_type': 'google',
                   'display_name': 'Google', 'color': '#6c63ff',
                   'icon': 'link', 'url': 'https://x/auth',
                   'authorize_url': 'https://x/auth', 'provider': 'Google',
                   'title': 'Sign in to Google', 'external_url': None,
                   'cta_label': 'Connect with Google'},
    # integrations/channels/agent_tools channel_connected
    'channel_connected': {'channel': 'discord', 'channel_type': 'discord',
                          'display_name': 'Discord', 'color': '#00e89d',
                          'icon': 'discord', 'message': 'Discord connected.'},
    # hart_intelligence_entry Connect_Channel paste-form
    'form': {'title': 'Connect Telegram', 'channel': 'telegram',
             'fields': [{'name': 'bot_token', 'label': 'Bot token',
                         'secret': True, 'type': 'text', 'default': None}],
             'external_url': None, 'submit_label': 'Connect',
             'action': '/api/social/channels/telegram/connect'},
    # model_orchestrator capability-ready card
    'notification': {'title': 'Capability Ready', 'message': 'Qwen is ready',
                     'severity': 'success'},
    # core/agent_tools game sound: the approval card
    'approval': {'agent_id': '42', 'action': 'game_sound:tetris:win',
                 'description': 'New win sound.',
                 'options': ['Keep it', 'Compose another']},
    # core/agent_tools game sound: the audio card
    'media': {'agent_id': '42', 'media_type': 'audio', 'src': '/a.wav',
              'controls': True, 'alt': 'win sound', 'title': 'win sound'},
    # integrations/social/agent_voice_bridge._emit_meet_copilot
    'meet_copilot': {'call_id': 'c1', 'platform': 'livekit', 'room_id': 'r1',
                     'state': 'live', 'transcript_lines': [], 'decisions': [],
                     'action_items': [], 'participants': [],
                     'agent_role': 'co_pilot'},
}


@pytest.mark.parametrize('name', sorted(REAL))
def test_what_the_emit_sites_really_send_has_no_schema_errors(name):
    issues = _issues(name, REAL[name])
    assert _errors(issues) == [], issues


def test_the_channel_failure_toast_is_flagged_not_hidden():
    # integrations/channels/agent_tools: the toast sends `text`; AgentOverlay's
    # NotificationCard reads message/content, so the reason never shows.
    issues = _issues('toast', {'severity': 'error', 'channel': 'discord',
                               'channel_type': 'discord',
                               'text': "Discord couldn't connect."})
    assert {i['code'] for i in issues} == {'unknown_prop', 'required'}


# ── agent_ui_compose: a verdict the model can read ─────────────────────────

def test_a_good_push_is_ok_valid_and_stored(svc):
    v = svc.agent_ui_compose('a', {'type': 'notification', 'message': 'hi'})
    assert v['ok'] is True and v['valid'] is True and v['refused'] is None
    assert v['issues'] == [] and v['type'] == 'notification'
    assert svc._agent_components['a'][-1]['message'] == 'hi'


def test_unknown_type_is_refused_with_a_did_you_mean(svc):
    v = svc.agent_ui_compose('a', {'type': 'notificaton'})
    assert v['ok'] is False and v['refused'] == 'unknown_type'
    assert 'notification' in v['hint']
    assert svc._agent_components == {}


def test_disabled_halted_and_not_an_object_are_named(svc):
    assert LiquidUIService(a2ui_enabled=False).agent_ui_compose(
        'a', {'type': 'card'})['refused'] == 'disabled'
    with patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=True):
        assert svc.agent_ui_compose('a', {'type': 'card'})['refused'] == 'hive_halted'
    assert svc.agent_ui_compose('a', 'oops')['refused'] == 'not_an_object'


def test_xss_refusal_names_the_offending_path(svc):
    v = svc.agent_ui_compose('a', {'type': 'list', 'items': ['ok',
                                   {'text': '<script>x()</script>'}]})
    assert v['refused'] == 'unsafe_content'
    assert 'items[1].text' in v['hint']
    assert svc._agent_components == {}


def test_rate_cap_is_named(svc):
    with patch.object(m.time, 'monotonic', return_value=1_000.0):
        verdicts = [svc.agent_ui_compose('f', {'type': 'card'})
                    for _ in range(25)]
    assert verdicts[0]['ok'] is True
    assert verdicts[-1]['refused'] == 'rate_capped'


def test_lenient_push_reports_schema_issues_but_still_delivers(svc):
    v = svc.agent_ui_compose('a', {'type': 'notification',
                                   'severity': 'loud', 'message': 'hi'})
    assert v['ok'] is True and v['valid'] is False
    assert v['issues'][0]['code'] == 'enum'
    assert svc._agent_components['a']


def test_strict_push_refuses_a_schema_error_and_stores_nothing(svc):
    v = svc.agent_ui_compose('a', {'type': 'notification',
                                   'severity': 'loud'}, strict=True)
    assert v['ok'] is False and v['refused'] == 'invalid'
    assert v['issues'][0]['code'] == 'enum'
    assert svc._agent_components == {}


def test_strict_push_lets_warnings_through(svc):
    v = svc.agent_ui_compose('a', {'type': 'notification', 'message': 'x',
                                   'mesage': 'y'}, strict=True)
    assert v['ok'] is True and v['valid'] is True
    assert v['issues'][0]['level'] == 'warning'


def test_agent_ui_update_stays_a_bool_with_its_old_behaviour(svc):
    # The 17 production callers rely on this exact contract.
    assert svc.agent_ui_update('a', {'type': 'card', 'title': 't'}) is True
    assert svc.agent_ui_update(
        'a', {'type': 'notification', 'severity': 'loud'}) is True
    assert svc.agent_ui_update('a', {'type': 'no_such'}) is False


# ── the catalogue an LLM is handed ─────────────────────────────────────────

def test_prompt_lists_types_with_required_marks_and_enums(svc):
    text = svc.component_prompt()
    assert 'toast(message!: str' in text
    assert 'severity: info|success|warning|error' in text
    assert text.count('\n- ') >= len(TYPED)


def test_prompt_can_be_narrowed_to_some_types(svc):
    text = svc.component_prompt(['toast', 'cart'])
    assert 'toast(' in text and 'cart(' in text and 'media(' not in text


def test_json_schema_is_serialisable_and_carries_enum_and_required(svc):
    schema = svc.component_json_schema('toast')
    json.dumps(schema)
    assert schema['properties']['severity']['enum'] == [
        'info', 'success', 'warning', 'error']
    assert schema['properties']['type'] == {'const': 'toast'}
    assert 'message' in schema['required'] and 'type' in schema['required']
    assert svc.component_json_schema('no_such') is None


# ── runtime-registered types can be typed too ──────────────────────────────

def _register(svc, **spec):
    base = {'attributes': {'radius': 'number', 'shape': 'round|square'},
            'required': ['radius'], 'doc': 'A ring.',
            'example': {'radius': 3, 'shape': 'round'}}
    base.update(spec)
    return svc.register_component_type('composer', 'ring', base)


def test_a_typed_custom_type_is_validated_and_catalogued(svc):
    assert _register(svc)['status'] == 'registered'
    assert svc.get_component_spec('ring')['attributes']['radius'] == 'number'
    assert 'ring(radius!: number' in svc.component_prompt()
    bad = svc.agent_ui_compose('composer', {'type': 'ring', 'radius': 'big'},
                               strict=True)
    assert bad['refused'] == 'invalid' and bad['issues'][0]['code'] == 'type'
    assert svc.agent_ui_compose(
        'composer', {'type': 'ring', 'radius': 3}, strict=True)['ok'] is True


def test_a_typed_custom_type_survives_a_reload(svc, tmp_path):
    _register(svc)
    again = LiquidUIService(a2ui_enabled=True)
    assert again.get_component_spec('ring')['attributes']['shape'] == 'round|square'
    assert again.agent_ui_compose(
        'composer', {'type': 'ring'}, strict=True)['refused'] == 'invalid'


@pytest.mark.parametrize('spec', [
    {'attributes': {'radius': 'integer'}},                    # not in grammar
    {'attributes': {'bad name': 'str'}},                      # not an identifier
    {'required': ['nope']},                                   # not an attribute
    {'example': {'radius': 'big'}},                           # contradicts itself
])
def test_a_contradictory_custom_spec_is_refused_with_a_reason(svc, spec):
    res = _register(svc, **spec)
    assert 'error' in res and 'ring' not in svc._custom_component_types
