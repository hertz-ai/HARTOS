"""Byte-equality + behavior tests for publish_thinking_trace.

Locks the helper output against each historical inline literal
across the 4 caller sites.  Migration commits depend on these
diffs being clean — any divergence in field order, value, or
shape would change the wire bytes and risk breaking a downstream
parser we can't see.

Caller sites:
  1. create_recipe.py:102 publish_agent_thought (autogen GroupChat)
     FULL schema, request_id placeholder '123456', bot_type='Agent'
  2. create_recipe.py:4258 publish_to_crossbar_new_action_start
     FULL schema, request_id placeholder '123456', bot_type='Agent'
  3. hart_intelligence_entry.py:4440 _publish_thinking (URL crawl)
     MINIMAL schema, real request_id, bot_type='Agent'
     NOTE: caller-3 wire field ORDER differs from helper canonical
     (request_id last vs after bot_type).  Order is consumer-
     irrelevant — adapter and frontend access by key — so the
     migration commit accepts the rotation.  Test below uses
     dict-equality for caller 3, json byte-equality for callers
     1/2/4.
  4. integrations/agent_engine/model_bus_service.py:60 ComputeRouter
     MINIMAL schema, request_id or '', bot_type='ComputeRouter'
"""
import json
from unittest.mock import patch

import pytest

from core.peer_link.crossbar_publish import (
    publish_thinking_trace,
    _ZOOM_STUB,
)


# ── Caller 1 + 2: FULL schema, byte-equal ───────────────────────

def _caller_1_inline_literal(content: str, user_id: str) -> str:
    """Reproduces create_recipe.py:102-119 exactly."""
    crossbar_message = {
        "text": [f'{content}'], "priority": 49,
        "action": 'Thinking', "historical_request_id": [],
        "preffered_language": 'en-US',
        "options": [], "newoptions": [], "bot_type": 'Agent',
        "page_image_url": "", "analogy_image_url": '',
        "request_id": "123456",
        "zoom_bounding_box": {
            'top_left': {'x': 0, 'y': 0},
            'top_right': {'x': 0, 'y': 0},
            'bottom_right': {'x': 0, 'y': 0},
            'bottom_left': {'x': 0, 'y': 0},
        },
    }
    return json.dumps(crossbar_message)


def _caller_2_inline_literal(message: str, user_id: str) -> str:
    """Reproduces create_recipe.py:4258-4264 exactly."""
    crossbar_message = {
        "text": [
            "Working on " + message
            + ".\n please evaluate the response i am giving to check if it meets the current action"],
        "priority": 49, "action": 'Thinking', "historical_request_id": [],
        "preffered_language": 'en-US', "options": [], "newoptions": [],
        "bot_type": 'Agent',
        "page_image_url": "", "analogy_image_url": '',
        "request_id": "123456",
        "zoom_bounding_box": {
            'top_left': {'x': 0, 'y': 0}, 'top_right': {'x': 0, 'y': 0},
            'bottom_right': {'x': 0, 'y': 0},
            'bottom_left': {'x': 0, 'y': 0}}}
    return json.dumps(crossbar_message)


# ── Caller 3 + 4: MINIMAL schema ────────────────────────────────

def _caller_3_inline_literal(msg: str, request_id: str, user_id: str) -> str:
    """Reproduces hart_intelligence_entry.py:4440-4445 exactly."""
    return json.dumps({
        "text": [msg], "priority": 49,
        "action": "Thinking", "bot_type": "Agent",
        "historical_request_id": [], "options": [], "newoptions": [],
        "request_id": request_id,
    })


def _caller_4_inline_literal(message: str, request_id: str, user_id: str) -> str:
    """Reproduces model_bus_service.py:60-68 exactly."""
    return json.dumps({
        'text': [message],
        'priority': 49,
        'action': 'Thinking',
        'bot_type': 'ComputeRouter',
        'request_id': request_id or '',
        'historical_request_id': [],
        'options': [], 'newoptions': [],
    })


# ── Capture the envelope the helper would publish ──────────────

def _capture_envelope(**kwargs) -> str:
    """Run publish_thinking_trace, capture the JSON it hands to publish_async."""
    captured = {}

    def _fake_publish_async(topic, payload, *args, **kw):
        captured['topic'] = topic
        captured['payload'] = payload

    def _fake_safe_hartos_attr(name, default=None):
        if name == 'publish_async':
            return _fake_publish_async
        return default

    with patch('core.safe_hartos_attr.safe_hartos_attr', _fake_safe_hartos_attr):
        ok = publish_thinking_trace(**kwargs)

    assert ok is True, "helper must return True when publish_async is reachable"
    return captured['topic'], captured['payload']


# ── Caller 1 byte-equality ─────────────────────────────────────

def test_caller_1_publish_agent_thought_byte_equal():
    content = "Last assistant message about the user's task"
    user_id = "user_alpha"
    inline = _caller_1_inline_literal(content, user_id)
    topic, helper_payload = _capture_envelope(
        text=content, user_id=user_id,
        request_id="123456", bot_type='Agent', full_schema=True,
    )
    assert topic == f'com.hertzai.hevolve.chat.{user_id}'
    assert helper_payload == inline, (
        f"Caller 1 envelope diverged.\n"
        f"  inline:  {inline}\n"
        f"  helper:  {helper_payload}\n"
        f"Migration commit for create_recipe.py:102 would change wire "
        f"bytes — would break any downstream consumer that signature-"
        f"compares the payload."
    )


# ── Caller 2 byte-equality ─────────────────────────────────────

def test_caller_2_publish_to_crossbar_new_action_start_byte_equal():
    message = "fetch user profile"
    user_id = "user_beta"
    expected_text = (
        "Working on " + message
        + ".\n please evaluate the response i am giving to check if it meets the current action")
    inline = _caller_2_inline_literal(message, user_id)
    topic, helper_payload = _capture_envelope(
        text=expected_text, user_id=user_id,
        request_id="123456", bot_type='Agent', full_schema=True,
    )
    assert topic == f'com.hertzai.hevolve.chat.{user_id}'
    assert helper_payload == inline


# ── Caller 3 dict-equality (order rotation expected) ───────────

def test_caller_3_publish_thinking_dict_equal():
    msg = "Crawling https://example.com..."
    user_id = "user_gamma"
    request_id = "req-12345"
    inline = json.loads(_caller_3_inline_literal(msg, request_id, user_id))
    topic, helper_payload = _capture_envelope(
        text=msg, user_id=user_id,
        request_id=request_id, bot_type='Agent', full_schema=False,
    )
    helper_dict = json.loads(helper_payload)
    assert helper_dict == inline, (
        "Caller 3 envelope semantics diverged.  Field order may differ "
        "from the inline literal (helper uses caller-4 canonical order); "
        "VALUES must match exactly."
    )
    assert topic == f'com.hertzai.hevolve.chat.{user_id}'


# ── Caller 4 byte-equality ─────────────────────────────────────

def test_caller_4_compute_router_byte_equal():
    message = "Routing locally..."
    user_id = "user_delta"
    request_id = "req-67890"
    inline = _caller_4_inline_literal(message, request_id, user_id)
    topic, helper_payload = _capture_envelope(
        text=message, user_id=user_id,
        request_id=(request_id or ''), bot_type='ComputeRouter',
        full_schema=False,
    )
    assert topic == f'com.hertzai.hevolve.chat.{user_id}'
    assert helper_payload == inline, (
        f"Caller 4 envelope diverged.\n"
        f"  inline:  {inline}\n"
        f"  helper:  {helper_payload}"
    )


# ── Caller 4 with empty request_id (the `request_id or ''` defensive coerce) ──

def test_caller_4_empty_request_id_coerces_to_empty_string():
    message = "Routing locally..."
    user_id = "user_epsilon"
    inline = _caller_4_inline_literal(message, '', user_id)
    topic, helper_payload = _capture_envelope(
        text=message, user_id=user_id,
        request_id='', bot_type='ComputeRouter',
        full_schema=False,
    )
    assert helper_payload == inline


# ── Filter-passthrough ─────────────────────────────────────────

def test_envelope_passes_capture_thinking_filter():
    """Both shapes must satisfy the Nunba adapter's filter:
    msg.get('priority') == 49 and msg.get('action') == 'Thinking'."""
    for kwargs in [
        dict(text='x', user_id='u', full_schema=False),
        dict(text='x', user_id='u', full_schema=True),
    ]:
        _, payload = _capture_envelope(**kwargs)
        msg = json.loads(payload)
        assert msg.get('priority') == 49
        assert msg.get('action') == 'Thinking'


# ── Type coercion ──────────────────────────────────────────────

def test_non_string_text_is_str_coerced():
    """autogen taps occasionally pass dict / list as content;
    the f-string in the original literal coerced to str() repr.
    Helper preserves that behavior."""
    payload_dict = {'role': 'assistant', 'content': 'oh hi'}
    _, payload = _capture_envelope(
        text=payload_dict, user_id='u', full_schema=False,
    )
    msg = json.loads(payload)
    assert msg['text'] == [str(payload_dict)], (
        "Non-str text must be coerced via str() to match the legacy "
        "f-string behavior; otherwise json.dumps would either embed "
        "the dict (changing the wire schema from list-of-string to "
        "list-of-object) or raise TypeError on non-serialisable values"
    )


# ── Empty user_id is a no-op ───────────────────────────────────

def test_empty_user_id_returns_false_no_publish():
    """user_id is required for per-user topic routing.  Empty must
    return False without invoking publish_async."""
    publish_calls = []

    def _record(name, default=None):
        if name == 'publish_async':
            return lambda *a, **kw: publish_calls.append((a, kw))
        return default

    with patch('core.safe_hartos_attr.safe_hartos_attr', _record):
        for empty in ('', None):
            ok = publish_thinking_trace(text='x', user_id=empty)
            assert ok is False
    assert publish_calls == [], (
        "publish_async must NOT be invoked when user_id is falsy")


# ── Publisher unresolvable (worker-thread cold path) ───────────

def test_unresolvable_publish_async_returns_false():
    """When safe_hartos_attr returns None (HARTOS loader still
    initialising, or worker thread before facade is ready), the
    helper returns False instead of raising."""
    with patch('core.safe_hartos_attr.safe_hartos_attr',
               lambda name, default=None: None):
        ok = publish_thinking_trace(text='x', user_id='u')
    assert ok is False


# ── Zoom stub immutability marker ──────────────────────────────

def test_zoom_stub_keys_match_inline_shape():
    """Defensive check on the zoom_bounding_box stub — its corners
    must be the four cardinal names with x:0,y:0 each, otherwise
    the FULL schema byte-equality tests above would silently
    break for callers that key on this field."""
    assert set(_ZOOM_STUB.keys()) == {
        'top_left', 'top_right', 'bottom_right', 'bottom_left'}
    for corner in _ZOOM_STUB.values():
        assert corner == {'x': 0, 'y': 0}
