"""The wire trim's cut pass only ever cuts messages the drop kept.

``_trim_to_budget`` drops messages first, then cuts the content of the ones
the drop may not remove: the protected turns (the newest user message and
the task turn) and the newest message.  The drop skips exactly those, so
every message the cut pass looks for is still in the list when it looks.

Until the review of 9ddc8b92d the cut pass carried an ``if p_idx is None:
continue  # dropped above`` branch for a candidate that had gone missing.
Nothing reaches it: the review's instrumented probe hit it 0 times over
3,000 random bodies, and so did this corpus with the branch replaced by a
raise.  The branch was removed; a candidate that ever does go missing now
raises instead of being skipped in silence, and this test is what would see
it.

The same corpus pins that the trim leaves no tool result without the call
it answers (see test_wire_trim_keeps_tool_calls_with_results.py).

Behavioural: the real ``_trim_to_budget`` on seeded random bodies (system or
not, named and unnamed user turns, assistant replies, tool calls and their
results), with only the per-slot budget pinned.
"""
import random

import core.llm_outbound_logger as lol
from core.token_utils import count_tokens_for_messages

_SPEAKERS = ('User', 'Assistant', 'StatusVerifier')


def _random_body(rnd):
    msgs = []
    if rnd.random() < 0.8:
        msgs.append({'role': 'system', 'content': 'sys ' * rnd.randint(1, 200)})
    open_calls = []
    for j in range(rnd.randint(1, 8)):
        text = 'w%d ' % j + 'w ' * rnd.randint(1, 700)
        kind = rnd.choice(('user', 'named', 'assistant', 'call', 'tool'))
        if kind == 'user':
            msgs.append({'role': 'user', 'content': text})
        elif kind == 'named':
            msgs.append({'role': 'user', 'name': rnd.choice(_SPEAKERS),
                         'content': text})
        elif kind == 'assistant':
            msgs.append({'role': 'assistant', 'content': text})
        elif kind == 'call':
            ids = ['c%d_%d' % (j, k) for k in range(rnd.randint(1, 2))]
            open_calls.extend(ids)
            msgs.append({'role': 'assistant', 'content': '',
                         'tool_calls': [
                             {'id': i, 'type': 'function',
                              'function': {'name': 'crawl',
                                           'arguments': '{"url": "u"}'}}
                             for i in ids]})
        else:
            call_id = (open_calls.pop(0) if open_calls and rnd.random() < 0.8
                       else 'unannounced_%d' % j)
            msgs.append({'role': 'tool', 'tool_call_id': call_id,
                         'content': text})
    return msgs


def _unanswered_results(messages):
    """tool_call_ids of role='tool' messages no earlier message announced."""
    announced, orphans = set(), set()
    for m in messages:
        for tc in (m.get('tool_calls') or []):
            announced.add(tc['id'])
        if m['role'] == 'tool' and m['tool_call_id'] not in announced:
            orphans.add(m['tool_call_id'])
    return orphans


def test_the_cut_pass_never_misses_a_message(monkeypatch):
    rnd = random.Random(20260926)
    trimmed = 0
    for _ in range(250):
        msgs = _random_body(rnd)
        newest_role = msgs[-1]['role']
        per_slot = rnd.randint(900, 2400)
        monkeypatch.setattr(lol, '_get_budget_per_slot', lambda: per_slot)
        out, n_dropped, n_cut, est_before, est_after, budget = \
            lol._trim_to_budget({'model': 'llama', 'messages': msgs,
                                 'max_tokens': 64})
        kept = out['messages']
        if n_dropped or n_cut:
            trimmed += 1
        assert est_after == count_tokens_for_messages(kept, 'llama')
        assert kept[-1]['role'] == newest_role, 'the newest message was dropped'
        assert any(m['role'] == 'user' for m in kept), 'no user turn left'
        # No tool result loses the call it answers to the trim (a result
        # that had no call on the way in is not the trim's doing).
        assert _unanswered_results(kept) <= _unanswered_results(msgs), (
            [m.get('tool_call_id') or m['role'] for m in kept])
    # The corpus has to exercise the trim, not just the early return.
    assert trimmed > 100, trimmed
