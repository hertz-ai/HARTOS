"""An elided-text pointer never reaches the user.

Owner ruling (relayed 2026-09-27): a pointer "is never echoed to the user as
an answer".  The wire trim puts ``[elided:<id> <n> chars of <kind>]`` in the
copy of a conversation it sends a model; a model can copy one into its own
reply.  Decision (coordinator, sensible default): strip it where a message
leaves for the user -- core.peer_link.crossbar_publish.publish_agent_message
(every send_message_to_user1, CREATE and REUSE, on the desktop) and the /chat
reply (hart_intelligence_entry._chat_reply, the one builder every /chat
return goes through).  Expansion stays mid-turn, through get_data_by_key.

Behavioural: a model reply that echoes a pointer (the stubbed model) goes
through the real publisher and the real _chat_reply (extracted and run with
its collaborators stubbed, as test_consent_fanout_p2 does); what reaches the
user carries no pointer and keeps the rest of the text.
"""
import os
import re
import sys
import textwrap
from unittest.mock import MagicMock, patch

import core.llm_outbound_logger as lol

HARTOS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_ECHO = ('The page lists three items [elided:0123456789ab 11240 chars of a '
         'tool result] and the total is 42.')


def test_the_parser_and_the_stripper_agree():
    assert lol.parse_elided_pointers(_ECHO)
    stripped = lol.strip_elided_pointers(_ECHO)
    assert not lol.parse_elided_pointers(stripped)
    assert stripped == 'The page lists three items and the total is 42.'


def test_a_published_message_carries_no_pointer():
    from core.peer_link import crossbar_publish
    sent = []
    with patch('core.safe_hartos_attr.safe_hartos_attr',
               return_value=lambda topic, payload: sent.append(payload)):
        assert crossbar_publish.publish_agent_message(
            text=_ECHO, user_id='u1', request_id='r1', prompt_id='p1')
    assert sent, 'nothing was published'
    text = sent[0]['text'][0]
    assert not lol.parse_elided_pointers(text), text
    assert 'the total is 42' in text


def _extract_function(src, name):
    m = re.search(r'^def ' + name + r'\(.*?(?=^def |\Z)', src, re.S | re.M)
    return textwrap.dedent(m.group(0)) if m else None


def test_the_chat_reply_carries_no_pointer():
    src = open(os.path.join(HARTOS_ROOT, 'hart_intelligence_entry.py'),
               encoding='utf-8').read()
    fn_src = _extract_function(src, '_chat_reply')
    assert fn_src
    tts = MagicMock()
    ns = {'_tts_synthesize_and_publish': tts,
          'get_memory': MagicMock(return_value=None),
          'app': MagicMock(),
          'jsonify': lambda x: ('JSONIFIED', x)}
    with patch.dict(sys.modules, {
            'integrations.social': MagicMock(),
            'integrations.social.chat_messages': MagicMock(),
            'core.user_lang': MagicMock(get_preferred_lang=lambda: 'en'),
            'flask': MagicMock(has_request_context=lambda: False)}):
        exec(compile(fn_src, '<isolated:_chat_reply>', 'exec'), ns)
        out = ns['_chat_reply']('u1', 'r1', _ECHO, preferred_lang='en')
    body = out[1]
    assert not lol.parse_elided_pointers(body['response']), body['response']
    assert 'the total is 42' in body['response']
    for call in tts.call_args_list:
        for arg in list(call.args) + list(call.kwargs.values()):
            assert not lol.parse_elided_pointers(str(arg)), arg


# ── review of d99b1aa88: central, the hive expert, channel replies ──────

def _central_send(modname):
    import json as _json
    mod = __import__(modname, fromlist=['send_message_to_user1'])
    sent = []

    def fake_post(url, data=None, **kw):
        sent.append(_json.loads(data))
        return MagicMock()

    with patch.object(mod, 'is_bundled', return_value=False), \
            patch.object(mod, 'pooled_post', side_effect=fake_post):
        mod.send_message_to_user1('u1', _ECHO, '', 'p1')
    assert sent, 'nothing was sent'
    return sent[0]['message']


def test_create_central_send_carries_no_pointer():
    text = _central_send('hartos.create_recipe')
    assert not lol.parse_elided_pointers(text), text
    assert 'the total is 42' in text


def test_reuse_central_send_carries_no_pointer():
    text = _central_send('hartos.reuse_recipe')
    assert not lol.parse_elided_pointers(text), text
    assert 'the total is 42' in text


def test_the_hive_expert_publish_carries_no_pointer():
    import threading
    from integrations.agent_engine.speculative_dispatcher import (
        SpeculativeDispatcher)
    published = []
    stub = MagicMock()
    stub._lock = threading.Lock()
    stub._active = {}

    def attr(name):
        if name == 'publish_async':
            return lambda topic, payload: published.append(payload)
        return None

    with patch('core.safe_hartos_attr.safe_hartos_attr', side_effect=attr):
        SpeculativeDispatcher._deliver_expert_response(
            stub, 'u1', 'p1', 's1', _ECHO)
    assert published, 'nothing was published'
    assert not lol.parse_elided_pointers(str(published[0])), published[0]


def test_a_channel_reply_carries_no_pointer():
    from integrations.channels.response.router import ChannelResponseRouter
    router = ChannelResponseRouter.__new__(ChannelResponseRouter)
    seen = []
    router._log_conversation = lambda **kw: seen.append(kw['content'])
    router._async_fan_out = lambda **kw: seen.append(kw['text'])
    router._notify_desktop_wamp = lambda **kw: seen.append(kw['text'])
    router.route_response('u1', _ECHO)
    assert len(seen) == 3
    for text in seen:
        assert not lol.parse_elided_pointers(text), text


def test_the_wire_fallback_leaves_exactly_the_plain_marker():
    """_strip_pointers (elided text could not be saved) must leave the
    plain WIRE_TRIM_MARKER, not the marker plus the pointer's newline."""
    from core.constants import WIRE_TRIM_MARKER
    p = lol.elided_pointer('0123456789ab', 11240, 'a tool result')
    msg = {'role': 'tool', 'content': 'HEAD' + WIRE_TRIM_MARKER + p + '\n' + 'TAIL'}
    assert lol._strip_pointers(msg)['content'] == 'HEAD' + WIRE_TRIM_MARKER + 'TAIL'


def test_source_guard_every_user_bound_send_strips_pointers():
    """test_source_guard_: a function that sends text to a user -- it names
    the user's chat topic (chat_topic_for) or posts to the cloud's
    /autogen_response -- calls strip_elided_pointers itself.  A new send
    that bypasses it fails here."""
    import ast
    import pathlib
    root = pathlib.Path(HARTOS_ROOT)
    offenders = []
    for top in ('hartos', 'core', 'integrations'):
        for path in (root / top).rglob('*.py'):
            try:
                tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
            except SyntaxError:
                continue
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                calls = {(c.func.attr if isinstance(c.func, ast.Attribute)
                          else getattr(c.func, 'id', None))
                         for c in ast.walk(fn) if isinstance(c, ast.Call)}
                consts = [n.value for n in ast.walk(fn)
                          if isinstance(n, ast.Constant) and isinstance(n.value, str)]
                sends = ('chat_topic_for' in calls
                         or any('/autogen_response' in c for c in consts))
                if sends and 'strip_elided_pointers' not in calls:
                    offenders.append('%s:%s' % (path.relative_to(root).as_posix(),
                                                fn.name))
    assert not offenders, offenders
