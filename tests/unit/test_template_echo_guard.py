"""Guard against the assistant parroting its own prompt skeleton.

The dispatcher prompt shows the model the JSON shape to emit using
angle-bracketed placeholders.  A 4B model sometimes echoes that skeleton
verbatim.  Observed live 2026-08-12 on the first turn after a cold start;
it reached _extract_conversational_reply as well-formed JSON with
is_casual set, and was rejected only because the echoed `delegate` field
happened to parse to garbage.  A cleaner echo would have been delivered to
the user as the answer.

The literal strings below are copied from that run's log, not invented.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reuse_recipe import _is_template_echo  # noqa: E402


# Exactly what the model emitted on 2026-08-12 (scratchpad log line 1695).
LIVE_ECHO = '<your short reply to the user, 1-3 sentences>'


@pytest.mark.parametrize('reply', [
    LIVE_ECHO,
    '  ' + LIVE_ECHO + '  ',                       # whitespace-padded
    '<channel name or empty string>',
    '<ISO 639-1 code or empty string>',
    '<short context if user wants invite link, or empty>',
    '<why you chose this delegate value>',
])
def test_placeholder_echoes_are_rejected(reply):
    assert _is_template_echo(reply) is True


@pytest.mark.parametrize('reply', [
    'Hello! How can I help you today?',
    "Hi there — good to meet you.",
    # Genuine replies that merely CONTAIN angle brackets must survive.
    'Use <div> for a block element and <span> for inline.',
    'The condition is a < b, so <b> wins.',
    'Try `if x < 10:` and see <output> in the console.',
    '<not closed',
    'closed>',
    '',
    '   ',
])
def test_real_replies_are_not_rejected(reply):
    assert _is_template_echo(reply) is False


def test_none_is_safe():
    assert _is_template_echo(None) is False


def test_nested_brackets_are_not_treated_as_a_placeholder():
    # A single placeholder has no inner angle brackets; markup often does.
    assert _is_template_echo('<a href="x"><b>hi</b></a>') is False
