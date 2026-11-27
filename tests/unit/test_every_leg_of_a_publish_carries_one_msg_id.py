"""Every leg of one publish carries the same msg_id; two publishes, two ids.

RN review, 2026-11-23 (a follow-up must-fix): the Crossbar leg of an agent
message carried no msg_id.  hart_intelligence_entry.publish_async handed the
payload to MessageBus.publish, which puts its id on a COPY for its own legs,
and then sent the payload itself to Crossbar.  A client drops a message it
already has by its msg_id, else by its request id; with CREATE's shared
fallback request id ('<user>_<prompt>-intermediate', create_recipe.py) a phone
that got only the Crossbar leg took a repeated reminder for the one before it
and hid it.

publish_async now puts ONE msg_id into the payload before the bus sees it, so
the bus's local leg, its SSE leg, the desktop SSE leg and the Crossbar leg
all carry it.  A payload that names itself keeps its own id.

Harness of test_egress_one_rule.py: the real publish_async source (its module
cannot be imported in a unit test), the real MessageBus and EventBus, the
Crossbar HTTP client and the SSE broker recorded.

    python -m pytest tests/unit/test_every_leg_of_a_publish_carries_one_msg_id.py -q --noconftest
"""
import json
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.platform import events as ev  # noqa: E402
from tests.unit.test_egress_one_rule import (  # noqa: E402,F401
    _Client, _no_link_manager, _publish_async, legs)

TOPIC = 'com.hertzai.hevolve.chat.u-1'


@pytest.fixture()
def seen(legs, monkeypatch):
    """What each leg carried: the bus's local subscribers (its EventBus echo),
    every SSE broadcast of the message (the bus's leg and publish_async's
    own) and the Crossbar HTTP leg.  Not the task.confirmation record the bus
    also publishes: that is a tracking row about the message, under the bus's
    own id, not a copy a client shows."""
    got = {'local': [], 'sse': [], 'client': _Client(), 'pending': [], 'ebus': legs.ebus}
    legs.ebus.on('bus.chat.response', lambda t, d: got['local'].append(dict(d)))
    # The bus's EventBus echo is emitted on a background thread
    # (emit_event's default, EventBus.emit_async), so it reaches its
    # listeners after the other legs have gone: it runs once the publish
    # returns.  (test_egress_one_rule's harness emits inline, which let the
    # EventBus stamp its own id into the payload before the other legs, a
    # timing production does not have, nor a node with no EventBus.)
    monkeypatch.setattr(ev, 'emit_event',
                        lambda t, d=None, async_=True: got['pending'].append((t, d)))

    def sse(evt, data, user_id=None):
        if evt != 'task.confirmation':
            got['sse'].append(dict(data))
        return True
    monkeypatch.setattr(ev, 'broadcast_sse_safe', sse)
    return got


def _run(seen, message):
    with _no_link_manager():
        _publish_async(seen['client'])(TOPIC, message)
    for topic, data in seen['pending']:          # the background echo, after
        seen['ebus'].emit(topic, data)
    seen['pending'].clear()


def _publish(seen, message):
    _run(seen, message)
    return [json.loads(p) for _t, p in seen['client'].published]


def _send(text, **extra):
    return {'text': [text], 'request_id': '10202_342-intermediate', 'prompt_id': '342',
            'bot_type': 'Custom GPT', **extra}


def test_every_leg_of_one_publish_carries_the_same_msg_id(seen):
    crossbar = _publish(seen, _send('Time for your practice!'))
    ids = ({m.get('msg_id') for m in seen['local']}
           | {m.get('msg_id') for m in seen['sse']}
           | {m.get('msg_id') for m in crossbar})
    assert seen['local'] and seen['sse'] and crossbar, seen
    assert len(ids) == 1 and None not in ids and '' not in ids, ids


def test_two_publishes_of_the_same_words_carry_two_ids(seen):
    first = _publish(seen, _send('Time for your practice!'))[-1]['msg_id']
    second = _publish(seen, _send('Time for your practice!'))[-1]['msg_id']
    assert first and second and first != second


def test_a_payload_that_names_itself_keeps_its_id(seen):
    crossbar = _publish(seen, _send('Hello', msg_id='row-7f3a'))
    assert crossbar[-1]['msg_id'] == 'row-7f3a'
    assert {m.get('msg_id') for m in seen['local'] + seen['sse']} == {'row-7f3a'}


def test_a_json_string_keeps_its_crossbar_bytes(seen):
    """The user's own topic goes byte-identical (test_egress_one_rule); the
    bus and SSE legs of the same message still carry one id."""
    raw = json.dumps(_send('Hello'))
    _run(seen, raw)
    assert seen['client'].published == [(TOPIC, raw)]
    ids = {m.get('msg_id') for m in seen['local'] + seen['sse']}
    assert len(ids) == 1 and None not in ids
