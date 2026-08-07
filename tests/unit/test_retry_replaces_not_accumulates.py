"""#485 follow-up — a retry must REPLACE its predecessor, not stack on it.

THE BUG: every remedy site in create_recipe's main loop re-issues its prompt
with ``clear_history=False``.  Autogen re-sends the whole message list on
each call, so N attempts left N copies in context and cost O(N^2) tokens
over the loop.  Measured in _remedy_replay_exceeded's own docstring: 982 of
1088 calls re-sent an already-sent payload, worst payload 222x.  Observed
live 2026-08-05: 14,330 copies of "Finish what you started, Do not go into
loop and do not repeat same thing in different way" — an anti-repetition
instruction repeated into the context window.

THE FIX (send_retry): drop earlier messages carrying the same retry tag,
then send the new one tagged.  History holds AT MOST ONE retry per tag, so
token cost is flat in the attempt count.  Retrying stays unlimited; only
the accumulation stops.

DISCRIMINATION: test_ten_retries_leave_one_copy fails against the pre-fix
code path (a direct initiate_chat with clear_history=False), which leaves
10 copies.  The helper is new, so import-level tests are trivially red —
the meaningful assertion is the COUNT, which is what regressed.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class _FakeGroupChat:
    """Mimics the one attribute send_retry touches: a live messages list."""

    def __init__(self):
        self.messages = []


class _FakeSender:
    """Stands in for chat_instructor / agents_object['helper'].

    initiate_chat appends to the SAME list autogen would, which is what
    makes accumulation visible.
    """

    def __init__(self, group_chat):
        self._gc = group_chat
        self.calls = 0

    def initiate_chat(self, recipient=None, message=None, clear_history=False,
                      silent=False):
        self.calls += 1
        assert clear_history is False, (
            "send_retry must preserve history — clearing it would drop the "
            "real conversation, not just the retry")
        self._gc.messages.append({'role': 'user', 'name': 'Helper',
                                  'content': message})
        return {'ok': True}


def _mod():
    try:
        import create_recipe
        return create_recipe
    except Exception as e:                                   # pragma: no cover
        pytest.skip(f"create_recipe unavailable: {e}")


def test_ten_retries_leave_one_copy():
    """FAILS PRE-FIX: plain initiate_chat leaves 10 copies, not 1."""
    cr = _mod()
    gc = _FakeGroupChat()
    sender = _FakeSender(gc)

    for i in range(10):
        cr.send_retry(gc, sender, object(), 'nojson-3',
                      f'Finish what you started, continue action 3 (try {i})')

    marker = cr._retry_marker('nojson-3')
    copies = [m for m in gc.messages if marker in m['content']]
    assert len(copies) == 1, (
        f"expected exactly 1 retry in history, found {len(copies)} — "
        "the retry is accumulating again")
    assert sender.calls == 10, "every attempt must still be SENT; only the "
    # newest wins
    assert 'try 9' in copies[0]['content']


def test_token_cost_is_flat_not_quadratic():
    """The point of the fix: context size must not grow with attempts."""
    cr = _mod()
    gc = _FakeGroupChat()
    sender = _FakeSender(gc)

    sizes = []
    for i in range(20):
        cr.send_retry(gc, sender, object(), 'recipe-1', 'X' * 500)
        sizes.append(sum(len(m['content']) for m in gc.messages))

    assert sizes[0] == sizes[-1], (
        f"context grew from {sizes[0]} to {sizes[-1]} chars over 20 retries — "
        "still accumulating")


def test_real_conversation_is_never_dropped():
    """Only same-tag retries are removed.  Losing real turns would be a far
    worse bug than the one being fixed."""
    cr = _mod()
    gc = _FakeGroupChat()
    sender = _FakeSender(gc)

    gc.messages.append({'role': 'user', 'name': 'User', 'content': 'hello'})
    gc.messages.append({'role': 'assistant', 'name': 'Assistant',
                        'content': 'working on it'})

    for _ in range(5):
        cr.send_retry(gc, sender, object(), 'claim-2', 'claim rejected')

    contents = [m['content'] for m in gc.messages]
    assert 'hello' in contents
    assert 'working on it' in contents
    assert len(gc.messages) == 3, f"expected 2 real + 1 retry, got {contents}"


def test_tags_do_not_clear_each_other():
    """A pending recipe request must survive while a claim is retried —
    otherwise fixing accumulation would break concurrent remedies."""
    cr = _mod()
    gc = _FakeGroupChat()
    sender = _FakeSender(gc)

    cr.send_retry(gc, sender, object(), 'recipe-1', 'save a recipe')
    for _ in range(4):
        cr.send_retry(gc, sender, object(), 'claim-1', 'claim rejected')

    assert any(cr._retry_marker('recipe-1') in m['content'] for m in gc.messages)
    assert len([m for m in gc.messages
                if cr._retry_marker('claim-1') in m['content']]) == 1
    assert len(gc.messages) == 2


def test_mutates_message_list_in_place():
    """autogen holds a reference to group_chat.messages; rebinding it would
    leave the manager writing to a detached list — the stale-reference shape
    that caused #621."""
    cr = _mod()
    gc = _FakeGroupChat()
    held = gc.messages          # what autogen would have captured
    sender = _FakeSender(gc)

    for _ in range(3):
        cr.send_retry(gc, sender, object(), 'nojson-1', 'retry')

    assert gc.messages is held, "messages list was rebound, not mutated"
    assert len(held) == 1


def test_empty_history_is_safe():
    cr = _mod()
    gc = _FakeGroupChat()
    sender = _FakeSender(gc)
    cr.send_retry(gc, sender, object(), 'recipe-9', 'first attempt')
    assert len(gc.messages) == 1


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
