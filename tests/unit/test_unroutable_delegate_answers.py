"""A substantive request must get an answer, not an apology.

Observed on a real Discord channel 2026-08-12: "i need a help in coding" was
classified is_casual=true, delegate='local', confidence=0.9 -- correctly and
confidently -- and the model wrote a real reply.  But `delegate` is read by
nothing for routing, so the turn could never complete: it burned the full turn
deadline and returned "I couldn't complete that request just now." while the
model's own answer sat unused in group_chat.messages.

That made EVERY substantive request fail (coding, research, anything needing
tools); only chit-chat with delegate 'none' worked.

Two behaviours are pinned here:
  1. an unroutable delegate is answered directly rather than entering a loop
     that provably cannot terminate  (fast path)
  2. anything that still reaches the failure path returns the model's reply
     rather than the generic apology                            (backstop)
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from reuse_recipe import (  # noqa: E402
    _extract_conversational_reply,
    _salvage_assistant_reply,
)

CODING_REPLY = ("I can help with that. What language are you working in, "
                "and what are you trying to build?")


def _msg(**obj):
    return [{'name': 'Assistant', 'content': json.dumps(obj)}]


@pytest.fixture(autouse=True)
def _clean_env():
    os.environ.pop('HEVOLVE_DELEGATE_ROUTING', None)
    yield
    os.environ.pop('HEVOLVE_DELEGATE_ROUTING', None)


# ── 1. the fast path ────────────────────────────────────────────────────────

@pytest.mark.parametrize('delegate', ['local', 'hive'])
def test_unroutable_delegate_is_answered_directly(delegate):
    """The exact live case: delegate has no destination, so answer now."""
    msgs = _msg(reply=CODING_REPLY, delegate=delegate, is_casual=True,
                is_create_agent=False, confidence=0.9)
    assert _extract_conversational_reply(msgs) == CODING_REPLY


def test_delegate_none_still_answered():
    """Chit-chat behaviour is unchanged."""
    msgs = _msg(reply='Hello!', delegate='none', is_casual=True,
                is_create_agent=False)
    assert _extract_conversational_reply(msgs) == 'Hello!'


def test_real_routing_takes_precedence_when_enabled():
    """The interim behaviour must switch off cleanly once routing exists."""
    os.environ['HEVOLVE_DELEGATE_ROUTING'] = '1'
    msgs = _msg(reply=CODING_REPLY, delegate='local', is_casual=True,
                is_create_agent=False)
    assert _extract_conversational_reply(msgs) is None


def test_create_agent_still_falls_through():
    """Agent creation needs its own flow; it must not be short-circuited."""
    msgs = _msg(reply='Sure, creating that now.', delegate='local',
                is_casual=True, is_create_agent=True)
    assert _extract_conversational_reply(msgs) is None


def test_non_casual_still_falls_through():
    msgs = _msg(reply='Working on it.', delegate='local', is_casual=False,
                is_create_agent=False)
    assert _extract_conversational_reply(msgs) is None


def test_template_echo_is_never_returned_even_when_unroutable():
    """The echo guard must survive the loosened delegate rule."""
    msgs = _msg(reply='<your short reply to the user, 1-3 sentences>',
                delegate='local', is_casual=True, is_create_agent=False)
    assert _extract_conversational_reply(msgs) is None


# ── 2. the backstop ─────────────────────────────────────────────────────────

def test_salvage_returns_the_reply_regardless_of_flags():
    """On the failure path, routing no longer matters -- any answer beats none."""
    msgs = _msg(reply=CODING_REPLY, delegate='hive', is_casual=False,
                is_create_agent=True)
    assert _salvage_assistant_reply(msgs) == CODING_REPLY


def test_salvage_skips_a_template_echo_and_keeps_looking():
    """An echo is not an answer; an older genuine reply is better than none."""
    msgs = [
        {'name': 'Assistant', 'content': json.dumps({'reply': 'Earlier real answer'})},
        {'name': 'Assistant', 'content': json.dumps(
            {'reply': '<your short reply to the user, 1-3 sentences>'})},
    ]
    assert _salvage_assistant_reply(msgs) == 'Earlier real answer'


def test_salvage_returns_none_when_there_is_genuinely_nothing():
    """Then, and only then, the caller's apology is the right answer."""
    assert _salvage_assistant_reply([]) is None
    assert _salvage_assistant_reply(
        [{'name': 'Assistant', 'content': 'not json'}]) is None
    assert _salvage_assistant_reply(
        [{'name': 'Assistant', 'content': json.dumps({'reply': '   '})}]) is None


def test_salvage_ignores_non_assistant_speakers():
    msgs = [{'name': 'Helper', 'content': json.dumps({'reply': 'helper text'})}]
    assert _salvage_assistant_reply(msgs) is None
