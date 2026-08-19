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
        [{'name': 'Assistant', 'content': '   '}]) is None
    assert _salvage_assistant_reply(
        [{'name': 'Assistant', 'content': json.dumps({'reply': '   '})}]) is None


def test_salvage_ignores_non_assistant_speakers():
    msgs = [{'name': 'Helper', 'content': json.dumps({'reply': 'helper text'})}]
    assert _salvage_assistant_reply(msgs) is None


# ── 3. plain-prose salvage (2026-08-19) ─────────────────────────────────────
# The recipe/task-execution GroupChat's own persona replies are NOT
# JSON-wrapped -- confirmed live on a real REUSE-loop abort (agent 8888,
# "what is the weather in chennai today"): the model produced a good,
# honestly-hedged plain-prose answer twice, and the loop still discarded it
# for the generic apology because retrieve_json() cannot parse prose.

WEATHER_REPLY = ("I can't access real-time data right now, but based on "
                  "Chennai's typical climate it's likely warm and humid "
                  "today, with a chance of scattered light showers.")


def test_salvage_returns_plain_prose_reply():
    """A non-JSON Assistant message is salvaged from its raw content."""
    msgs = [{'name': 'Assistant', 'content': WEATHER_REPLY}]
    assert _salvage_assistant_reply(msgs) == WEATHER_REPLY


def test_salvage_skips_a_plain_prose_template_echo():
    """The echo guard applies to the plain-prose path too."""
    msgs = [{'name': 'Assistant',
             'content': '<your short reply to the user, 1-3 sentences>'}]
    assert _salvage_assistant_reply(msgs) is None


def test_salvage_prefers_the_json_reply_field_over_raw_json_text():
    """A message that IS a valid JSON envelope is read from 'reply', never
    from its own raw (JSON-shaped) text -- the plain-prose path is a
    fallback for when JSON parsing fails, not an alternate read of the
    same message."""
    msgs = [{'name': 'Assistant',
             'content': json.dumps({'reply': CODING_REPLY, 'delegate': 'local'})}]
    assert _salvage_assistant_reply(msgs) == CODING_REPLY


def test_salvage_prefers_more_recent_plain_prose_over_older_json():
    """Scan order (most recent first) is unchanged by adding the fallback."""
    msgs = [
        {'name': 'Assistant', 'content': json.dumps({'reply': 'Older JSON answer'})},
        {'name': 'Assistant', 'content': 'Newer plain-prose answer'},
    ]
    assert _salvage_assistant_reply(msgs) == 'Newer plain-prose answer'


# ── 4. unverified-claim caveat (2026-08-19) ─────────────────────────────────
# Confirmed live: agent 8888, "what is the latest news in chennai politics
# today" -- the Assistant stated a specific, WRONG headline before any tool
# had run. A subtask then dispatched a real tool (a genuine successful
# crawl4ai_crawl fetch) trying to verify it, but the turn deadline hit before
# the Assistant got another turn to answer from that result -- so the
# premature guess is what got salvaged, presented as flat fact.

NEWS_REPLY = "The big story today is CM E. Vaiko's health scare."
CAVEAT = "couldn't fully verify"


def test_salvage_adds_caveat_when_tool_ran_after_the_reply():
    """A tool call AFTER the candidate reply, with no later Assistant
    message using its result, means the reply was never confirmed."""
    msgs = [
        {'name': 'Assistant', 'content': NEWS_REPLY},
        {'name': 'Helper', 'role': 'assistant',
         'tool_calls': [{'id': 'x', 'function': {'name': 'crawl4ai_crawl'}}]},
        {'name': 'Assistant', 'role': 'tool',
         'tool_responses': [{'tool_call_id': 'x', 'content': 'real page data'}]},
    ]
    result = _salvage_assistant_reply(msgs)
    assert result.startswith(NEWS_REPLY)
    assert CAVEAT in result


def test_salvage_no_caveat_when_no_tool_activity_follows():
    """The common case (2026-08-12 coding example, 2026-08-19 weather
    example) -- no tool ran after the reply, so it's presented plainly,
    unchanged from before this fix."""
    msgs = [{'name': 'Assistant', 'content': NEWS_REPLY}]
    assert _salvage_assistant_reply(msgs) == NEWS_REPLY


def test_salvage_no_caveat_when_a_later_assistant_message_used_the_tool_result():
    """If the Assistant DID get a later turn, that later message is the one
    salvaged (scan order is unchanged) -- and IT has no tool activity after
    it, so no caveat is needed; the caveat only guards a reply that was
    itself superseded by an unconsumed tool call."""
    msgs = [
        {'name': 'Assistant', 'content': 'Older, since-updated guess.'},
        {'name': 'Helper', 'role': 'assistant',
         'tool_calls': [{'id': 'x', 'function': {'name': 'crawl4ai_crawl'}}]},
        {'name': 'Assistant', 'role': 'tool',
         'tool_responses': [{'tool_call_id': 'x', 'content': 'real page data'}]},
        {'name': 'Assistant', 'content': 'Final answer grounded in the fetch.'},
    ]
    assert _salvage_assistant_reply(msgs) == 'Final answer grounded in the fetch.'


def test_salvage_never_returns_a_tool_response_as_the_reply():
    """A tool's own response is attributed name='Assistant' too (the tool
    call was Assistant's), role='tool' -- confirmed live in a real
    transcript. Without the role check, a raw scraped-page dump could be
    salvaged as if the model itself had said it."""
    msgs = [
        {'name': 'Assistant', 'content': 'A real earlier answer.'},
        {'name': 'Helper', 'role': 'assistant',
         'tool_calls': [{'id': 'x', 'function': {'name': 'crawl4ai_crawl'}}]},
        {'name': 'Assistant', 'role': 'tool',
         'tool_responses': [{'tool_call_id': 'x',
                              'content': '--- huge raw scraped page dump ---'}]},
    ]
    result = _salvage_assistant_reply(msgs)
    assert 'huge raw scraped page dump' not in result
    assert result.startswith('A real earlier answer.')
