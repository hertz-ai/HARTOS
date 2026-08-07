"""P2 chat thread parity — behavioural tests.

Mirrors test_consent_fanout_p0/p1.py discipline: real imports +
mocks + call + assert observable behaviour.

Run:
    pytest tests/unit/test_consent_fanout_p2.py -v --noconftest
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# DERIVED, never hardcoded: an absolute dev-box path makes these guards pass on
# exactly one machine and fail everywhere else (they did, in CI). Siblings live
# beside their own parent -- PycharmProjects/ for desktop+server, StudioProjects/
# for mobile -- and are genuinely absent on a runner that checked out only
# HARTOS, so tests that need one SKIP rather than fail.
HARTOS_ROOT = str(Path(__file__).resolve().parents[2])
IOS_ROOT = str(Path(HARTOS_ROOT).parents[1] / 'StudioProjects' / 'Nunba-Companion-iOS')
_needs_ios = pytest.mark.skipif(
    not Path(IOS_ROOT).is_dir(),
    reason='sibling repo Nunba-Companion-iOS not checked out',
)


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _extract_function(src: str, name: str) -> str | None:
    """Pull a top-level Python function body out of a module source
    string.  Stops at the next top-level def / class / decorator so
    we don't accidentally drag the next route's @decorator stack in
    (which would be a syntax error when exec-compiled in isolation).
    Mirrors the helper in test_consent_fanout_p0.py."""
    m = re.search(
        rf'(?ms)^def {re.escape(name)}\([\s\S]+?(?=^def [a-zA-Z_]|^class [a-zA-Z_]|^@[a-zA-Z_]|\Z)',
        src,
    )
    return textwrap.dedent(m.group(0)) if m else None


# ─── P2-S4 (server): _chat_reply threads channel_type to persist ─────
def test_p2s4_chat_reply_passes_channel_type_to_persist():
    """When _chat_reply receives channel_type='interview' (via
    payload kwarg OR the Flask request body), it must forward it to
    chat_messages.persist_and_publish_async on BOTH user-turn and
    assistant-turn persists.  Behavioural: extract _chat_reply,
    execute with mocks, verify the calls."""
    src = _read(f'{HARTOS_ROOT}/hart_intelligence_entry.py')
    fn_src = _extract_function(src, '_chat_reply')
    assert fn_src, "Could not extract _chat_reply from hart_intelligence_entry.py"

    chat_messages_stub = MagicMock()
    chat_messages_stub.persist_and_publish_async = MagicMock()
    # Simulate the lazy `from integrations.social import chat_messages`
    # the function does inside its try block.
    fake_pkg = MagicMock()
    fake_pkg.chat_messages = chat_messages_stub

    ns = {
        '_tts_synthesize_and_publish': MagicMock(),
        'get_memory': MagicMock(return_value=None),
        'app': MagicMock(),
        'jsonify': lambda x: ('JSONIFIED', x),
        # _chat_reply imports HumanMessage/AIMessage inside the
        # try-save_context block, but with get_memory=None it short-
        # circuits before reaching them, so we don't need real ones.
    }
    with patch.dict(sys.modules, {
        'integrations.social': fake_pkg,
        'integrations.social.chat_messages': chat_messages_stub,
        'core.user_lang': MagicMock(get_preferred_lang=lambda: 'en'),
        # _chat_reply imports HumanMessage/AIMessage inside the
        # try-save_context block — provide a stub module just in case.
        'langchain_classic': MagicMock(),
        'langchain_classic.schema': MagicMock(),
        'langchain_classic.schema.messages': MagicMock(
            HumanMessage=MagicMock, AIMessage=MagicMock),
        # Flask context unavailable in unit test → has_request_context
        # returns False, so _chat_reply falls back to payload kwarg.
        'flask': MagicMock(has_request_context=lambda: False),
    }):
        exec(compile(fn_src, '<isolated:_chat_reply>', 'exec'), ns)
        chat_reply = ns['_chat_reply']
        chat_reply(
            user_id='user-1',
            request_id='req-1',
            response_text='Because the experiment shows X.',
            user_prompt='Why did you decide X?',
            channel_type='interview',
            preferred_lang='en',
        )

    # Two persist calls (user + assistant) — both with channel_type='interview'
    calls = chat_messages_stub.persist_and_publish_async.call_args_list
    assert len(calls) == 2, (
        f"P2-S4: expected exactly 2 persist calls (user + assistant), "
        f"got {len(calls)}"
    )
    roles = sorted(c.args[1] for c in calls)
    assert roles == ['assistant', 'user'], (
        f"P2-S4: expected roles ['user', 'assistant'] (sorted to "
        f"{['assistant', 'user']}), got {roles}"
    )
    for c in calls:
        assert c.kwargs.get('channel_type') == 'interview', (
            f"P2-S4: persist call missing channel_type='interview'. "
            f"role={c.args[1]!r} kwargs={c.kwargs}"
        )


def test_p2s4_chat_reply_defaults_channel_type_to_chat():
    """When no channel_type is supplied (and no Flask context), the
    persist defaults to 'chat' — preserves pre-sweep behaviour for
    every existing caller of _chat_reply."""
    src = _read(f'{HARTOS_ROOT}/hart_intelligence_entry.py')
    fn_src = _extract_function(src, '_chat_reply')
    chat_messages_stub = MagicMock()
    ns = {
        '_tts_synthesize_and_publish': MagicMock(),
        'get_memory': MagicMock(return_value=None),
        'app': MagicMock(),
        'jsonify': lambda x: x,
    }
    with patch.dict(sys.modules, {
        'integrations.social': MagicMock(chat_messages=chat_messages_stub),
        'integrations.social.chat_messages': chat_messages_stub,
        'core.user_lang': MagicMock(get_preferred_lang=lambda: 'en'),
        'langchain_classic': MagicMock(),
        'langchain_classic.schema': MagicMock(),
        'langchain_classic.schema.messages': MagicMock(),
        'flask': MagicMock(has_request_context=lambda: False),
    }):
        exec(compile(fn_src, '<isolated:_chat_reply>', 'exec'), ns)
        ns['_chat_reply'](
            user_id='user-1',
            request_id='req-1',
            response_text='hi',
            user_prompt='hello',
        )
    for c in chat_messages_stub.persist_and_publish_async.call_args_list:
        assert c.kwargs.get('channel_type') == 'chat', (
            f"P2-S4 regression: default channel_type changed from "
            f"'chat'.  kwargs={c.kwargs}"
        )


# ─── P2-S4 (interview route): channel_type='interview' in POST body ──
def test_p2s4_interview_agent_posts_with_channel_type():
    """Extract interview_agent, run it with mocked pooled_post + g.db,
    assert the POST body includes channel_type='interview' and
    request_id=post_id."""
    src = _read(f'{HARTOS_ROOT}/integrations/social/api_tracker.py')
    fn_src = _extract_function(src, 'interview_agent')
    assert fn_src, "Could not extract interview_agent"

    captured = {}

    def fake_pooled_post(url, json=None, timeout=None):  # noqa: A002
        captured['url'] = url
        captured['json'] = json
        captured['timeout'] = timeout
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {'response': 'Because the data shows X.'}
        return r

    fake_goal = MagicMock(
        owner_id='user-7', prompt_id='p-3', title='Cohort Study')
    fake_request = MagicMock()
    fake_request.get_json.return_value = {'question': 'Why X?'}
    fake_g = MagicMock(db=MagicMock())

    ns = {
        '_err': lambda msg, code: ('ERR', msg, code),
        '_ok': lambda data: ('OK', data),
        '_get_goal_for_post': MagicMock(return_value=fake_goal),
        'request': fake_request,
        'g': fake_g,
        'logger': MagicMock(),
        'require_auth': lambda fn: fn,  # decorator passthrough
        'tracker_bp': MagicMock(route=lambda *a, **k: (lambda fn: fn)),
    }
    with patch.dict(sys.modules, {
        'core.http_pool': MagicMock(pooled_post=fake_pooled_post),
        'core.port_registry': MagicMock(get_port=lambda name: 6777),
    }):
        exec(compile(fn_src, '<isolated:interview_agent>', 'exec'), ns)
        result = ns['interview_agent']('post-99')

    assert captured.get('url', '').endswith('/chat'), (
        f"P2-S4: interview must POST to /chat, got {captured.get('url')}"
    )
    body = captured.get('json') or {}
    assert body.get('channel_type') == 'interview', (
        f"P2-S4: POST body must carry channel_type='interview' but "
        f"got {body.get('channel_type')!r}"
    )
    assert body.get('request_id') == 'post-99', (
        f"P2-S4: POST body must carry request_id=post_id so the "
        f"interview turns thread together, got {body.get('request_id')!r}"
    )


# ─── P2-S5 (iOS): chat.new subscription wired ────────────────────────
@_needs_ios
def test_p2s5_ios_subscribes_to_chat_new():
    """Source guard, NOT a behavioural test.  Honest label: Swift
    unit-tests need XCTest infrastructure that's not callable from
    Python.  Behavioural verification happens in the iOS XCTest
    pass (Nunba-Companion-iOS/ios/NunbaCompanionTests) and the
    P2-LIVE manual probe.  We assert here only that the wire is
    present in source so it can't silently disappear on a merge."""
    path = (f'{IOS_ROOT}/ios/NunbaCompanion/Modules/'
            'AutobahnConnectionManager.swift')
    if not os.path.exists(path):
        pytest.skip(f"AutobahnConnectionManager.swift not present at {path}")
    src = _read(path)
    assert 'com.hertzai.hevolve.chat.new.' in src, (
        "P2-S5: AutobahnConnectionManager must subscribe to "
        "com.hertzai.hevolve.chat.new.{userId} so iOS sees the same "
        "chat fanout web/Android already receive."
    )
    assert 'ChatNewEventEmitter' in src, (
        "P2-S5: the chat.new subscription must forward payloads to "
        "ChatNewEventEmitter (the JS bridge) — otherwise rows reach "
        "the WAMP subscriber but never the RN chat surface."
    )


@_needs_ios
def test_p2s5_ios_chat_new_emitter_exists():
    """Source guard for the new emitter file's RN bridge contract."""
    swift_path = (f'{IOS_ROOT}/ios/NunbaCompanion/Modules/'
                  'ChatNewEventEmitter.swift')
    obj_path = (f'{IOS_ROOT}/ios/NunbaCompanion/Modules/'
                'ChatNewEventEmitter.m')
    if not (os.path.exists(swift_path) and os.path.exists(obj_path)):
        pytest.skip("ChatNewEventEmitter files not present")
    swift = _read(swift_path)
    obj = _read(obj_path)
    assert '@objc(ChatNewEventEmitter)' in swift, (
        "P2-S5: Swift class must declare @objc(ChatNewEventEmitter) "
        "so the Obj-C bridge can find it."
    )
    assert '\"chatNew\"' in swift or "'chatNew'" in swift, (
        "P2-S5: emitter must register 'chatNew' as a supportedEvent "
        "for the JS-side DeviceEventEmitter listener."
    )
    assert 'RCT_EXTERN_MODULE(ChatNewEventEmitter' in obj, (
        "P2-S5: Obj-C bridge must EXTERN_MODULE ChatNewEventEmitter."
    )
