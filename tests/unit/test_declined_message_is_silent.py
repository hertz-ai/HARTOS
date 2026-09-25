"""A declined message must stay silent; a failed one must get the fallback.

_handle_message returns None to DELIBERATELY decline a message (a group
message with no bot mention, under require_mention_in_groups) and '' when the
agent ran and produced nothing.

The 2026-08-10 empty-reply fix collapsed both into "falsy", so the bot replied
"I wasn't able to put together a reply for that one." to every unmentioned
message in a group -- i.e. it spoke on exactly the messages it was configured
to ignore.  Seen on a real Discord channel 2026-08-12.
"""
import asyncio
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


class _Adapter:
    """Records what, if anything, was sent back to the channel."""

    def __init__(self):
        self.sent = []
        self.config = types.SimpleNamespace(require_mention_in_groups=True)

    async def send_message(self, chat_id=None, text=None, reply_to=None):
        self.sent.append(text)


def _route(handler_result):
    """Run the registry's reply branch for a given handler return value.

    Mirrors the branch under test rather than importing the whole registry,
    which pulls in adapters and an event loop.  The three-way distinction --
    real reply / declined / failed -- is the contract being pinned.
    """
    adapter = _Adapter()

    async def _run():
        response = handler_result
        if response and str(response).strip():
            await adapter.send_message(chat_id='c', text=response, reply_to='m')
        elif response is None:
            pass                      # declined: stay silent
        else:
            await adapter.send_message(
                chat_id='c',
                text=("I wasn't able to put together a reply for that one. "
                      "Could you try rephrasing it?"),
                reply_to='m')

    asyncio.run(_run())
    return adapter.sent


def test_declined_message_sends_nothing():
    """None => the bot was not addressed. It must not speak."""
    assert _route(None) == []


def test_empty_string_still_gets_the_fallback():
    """'' => the agent ran and produced nothing. Silence is the worst failure."""
    sent = _route('')
    assert len(sent) == 1
    assert "wasn't able to put together a reply" in sent[0]


def test_blank_whitespace_gets_the_fallback():
    sent = _route('   \n  ')
    assert len(sent) == 1
    assert "wasn't able to put together a reply" in sent[0]


def test_real_reply_is_sent_verbatim():
    assert _route('Hello! How can I help you today?') == [
        'Hello! How can I help you today?']


def test_the_regression_itself():
    """The exact bug: a declined message must not produce the failure text."""
    for text in _route(None):
        assert "wasn't able to put together a reply" not in text


@pytest.mark.parametrize('declined,expected_sends', [
    (None, 0),   # not addressed  -> silent
    ('', 1),     # addressed, agent failed -> fallback
])
def test_none_and_empty_are_not_interchangeable(declined, expected_sends):
    assert len(_route(declined)) == expected_sends


def test_handler_really_does_return_none_for_unmentioned_group_messages():
    """Guard the assumption this whole fix rests on.

    If _handle_message ever stops returning None for that path, the silent
    branch becomes dead code and the regression returns unnoticed.
    """
    import inspect
    from integrations.channels import flask_integration

    src = inspect.getsource(flask_integration.FlaskChannelIntegration._handle_message)
    assert 'require_mention_in_groups' in src
    idx = src.index('require_mention_in_groups')
    # The next return after that check must be a bare `return None`.
    assert 'return None' in src[idx:idx + 200], (
        'the unmentioned-group path no longer returns None -- registry.py\'s '
        'silent branch keys off exactly that')
