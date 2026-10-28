"""A cut keeps the CURRENT order, not only the oldest one.

Live 2026-10-28 21:32, desktop agent 54 ("Teach Yourself") answering a Redmi
Pad turn; llm_outbound.jsonl rows 85-106 (the POST-trim wire bodies) and the
elided text saved beside them.  Action 3 names save_data_in_memory.  The
agent never called it: the FABRICATED-COMPLETE gate refused its claimed
completion three times, re-steered, then gave up (143 s).  In the 9 requests
the tool-carrying seats made during action 3:

  * 7 carried "Perform this action -> Action #3" only inside a user turn the
    ROLE-ORDER-GUARD had merged after User's Action #1 order (joined with a
    blank line, User's name kept).  The cut kept that turn's head through the
    FIRST "follow these steps:" -- Action #1's order -- and elided the rest,
    Action #3's order with it.  The model repeated Action #1's calls
    (get_user_id, get_data_by_key) and send_message_to_user.
  * 1 (the Assistant seat, 21:33:27) carried no Action #3 order at all: the
    seats' token limiter dropped ChatInstructor's dispatch before the wire.
    Not covered here: whether the current order joins the protected set is
    the owner's call (2026-10-28: what to protect "should ideally be learnt
    by the auto evolve loop").
  * 1 (21:33:14) carried it, merged after a verdict as that turn's first
    order; the model started on it (get_user_id).

Owner 2026-10-28: "oldest shd be trimmed not latest appended".  Pinned here,
in the one rule both cuts share (core.llm_outbound_logger):
  1. a cut user turn that holds several orders keeps its newest order's
     marker whole, as well as its first one's (the user's words); the older
     text between them is what goes
  2. a turn holding one order is cut exactly as before

    python -m pytest tests/unit/test_wire_trim_keeps_the_newest_order.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import core.llm_outbound_logger as lol  # noqa: E402
from core.constants import (  # noqa: E402
    ACTION_STEPS_SEPARATOR, WIRE_TRIM_MARKER, WIRE_TRIM_SAFETY_MARGIN_TOKENS)

_MAX_TOKENS = 64
_WORDS = 'What is evaporation in one sentence'


def _order(n, what, words=None, n_steps=60):
    """``(head, turn)``: a dispatch as reuse_recipe._build_reuse_action_message
    writes it -- marker, the user's words on the opening turn only, the
    separator, the steps."""
    head = 'Perform this action -> Action #%d:%s' % (n, what)
    if words:
        head += '\n\n' + words
    steps = '[' + ', '.join(
        "{'step %d.%d': {'tool_name': 'get_data_by_key', 'code': ''}}" % (n, i)
        for i in range(n_steps)) + ']'
    return head + ACTION_STEPS_SEPARATOR, head + ACTION_STEPS_SEPARATOR + steps


A1_HEAD, A1 = _order(1, 'Call get_user_id, then get_data_by_key with the key '
                        "teach.<that user id> to read this learner's saved "
                        'progress.', _WORDS)
A2_HEAD, A2 = _order(2, "Write this turn's reply to the learner.")
A3_HEAD, A3 = _order(3, 'Call save_data_in_memory with the key teach.<that '
                        "same user id> to save this learner's updated "
                        'progress after the reply.')


def _verdict(n):
    return ('{"status": "completed", "action": "action %d", "action_id": %d, '
            '"message": "done"}' % (n, n))


_STEER = ('Action 3 is NOT complete: the tool(s) save_data_in_memory did not '
          'produce a real result. @Helper call save_data_in_memory now with '
          'real arguments and report the actual returned result verbatim.')


def _merged(*parts):
    # The ROLE-ORDER-GUARD's coalesce (hartos.helper ToolMessageHandler):
    # consecutive user turns joined by a blank line, the first one's name kept.
    return '\n\n'.join(parts)


def _system():
    return {'role': 'system', 'content': 'You are a helpful tutor. ' * 40}


def _call(cid, name):
    return {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': cid, 'type': 'function',
                            'function': {'name': name, 'arguments': '{}'}}]}


def _result(cid, text):
    return {'role': 'tool', 'tool_call_id': cid, 'content': text}


def _trim(msgs, budget, monkeypatch):
    per_slot = budget + _MAX_TOKENS + WIRE_TRIM_SAFETY_MARGIN_TOKENS
    monkeypatch.setattr(lol, '_get_budget_per_slot', lambda: per_slot)
    out, _, n_cut, _, est_after, got = lol._trim_to_budget(
        {'model': 'llama', 'messages': msgs, 'max_tokens': _MAX_TOKENS})
    assert n_cut > 0, 'fixture failed to force a cut'
    return out['messages'], est_after, got


def _text(msg):
    # A body that carries a pointer gets the pointer explanation appended to
    # its system message; it is not part of the message's own text.
    return str(msg.get('content') or '').split(lol.ELIDED_POINTER_EXPLANATION)[0]


# ── 1. the merged turn keeps its newest order ───────────────────────────

def test_the_live_merged_turn_keeps_the_current_order(monkeypatch):
    """Row 10's shape (Helper seat, 21:33:21): [system, User's turn merged
    from Action #1's order, a verdict, Action #2's order, a verdict and
    Action #3's order, the call and result, the verdict + steer]."""
    user = {'role': 'user', 'name': 'User',
            'content': _merged(A1, _verdict(1), A2, _verdict(2), A3)}
    msgs = [_system(), user, _call('c1', 'get_user_id'), _result('c1', '42'),
            {'role': 'user', 'name': 'StatusVerifier',
             'content': _merged(_verdict(3), _STEER)}]
    out, est_after, budget = _trim(msgs, 900, monkeypatch)
    kept = _text(next(m for m in out if m.get('name') == 'User'))
    assert kept.startswith(A1_HEAD), "the user's words were cut: %r" % kept[:200]
    assert A3_HEAD in kept, (
        'the current order (Action #3) was cut and only the oldest one '
        '(Action #1) survived: %r' % kept)
    assert kept.index(A1_HEAD) < kept.index(A3_HEAD)
    assert A2_HEAD not in kept, 'an older order outlived the cut'
    assert est_after <= budget


def test_the_newest_order_is_kept_whatever_the_room(monkeypatch):
    """Like the words: kept whole even when the room is smaller, and the
    request reported over budget, never cut through the order."""
    user = {'role': 'user', 'name': 'User',
            'content': _merged(A1, _verdict(1), A2, _verdict(2), A3)}
    out, _, _ = _trim([_system(), user], 120, monkeypatch)
    kept = _text(next(m for m in out if m.get('name') == 'User'))
    assert kept.startswith(A1_HEAD) and A3_HEAD in kept, kept


def test_the_newest_end_still_closes_the_turn(monkeypatch):
    """The tail is still the turn's newest text, after the newest order."""
    user = {'role': 'user', 'name': 'User',
            'content': _merged(A1, _verdict(1), A2, _verdict(2), A3) + 'LAST'}
    out, _, _ = _trim([_system(), user], 900, monkeypatch)
    kept = _text(next(m for m in out if m.get('name') == 'User'))
    assert kept.endswith('LAST'), kept[-120:]
    assert kept.index(A3_HEAD) < kept.rindex('LAST')


# ── 2. one order: the cut is the one it always was ──────────────────────

def test_a_turn_with_one_order_is_cut_as_before():
    """keep_head_cut is the shared cut; with one order it keeps the head
    and the tail and nothing else."""
    turn = A1 + 'LAST'
    keep = lol.must_keep_head(turn, 'user')
    assert keep == len(A1_HEAD)
    assert (lol.keep_head_cut(turn, keep, 40, WIRE_TRIM_MARKER)
            == turn[:keep] + WIRE_TRIM_MARKER + turn[-40:])


def test_an_order_already_inside_the_tail_is_not_kept_twice():
    turn = _merged(A1, _verdict(1), A3)
    keep = lol.must_keep_head(turn, 'user')
    tail = len(turn) - turn.index(A3_HEAD) + 10
    out = lol.keep_head_cut(turn, keep, tail, WIRE_TRIM_MARKER)
    assert out.count(A3_HEAD) == 1
    assert out == turn[:keep] + WIRE_TRIM_MARKER + turn[-tail:]


def test_the_shared_cut_keeps_both_orders():
    """keep_head_cut is the ONE cut the wire trim and the seats' token
    limiter make of a dispatch turn; the newest order survives it."""
    turn = _merged(A1, _verdict(1), A2, _verdict(2), A3) + 'LAST'
    keep = lol.must_keep_head(turn, 'user')
    out = lol.keep_head_cut(turn, keep, 0, WIRE_TRIM_MARKER)
    assert out.startswith(A1_HEAD) and A3_HEAD in out, out
    assert A2_HEAD not in out
    assert len(out) < len(turn)


def test_the_prefix_is_the_one_the_producer_writes():
    """reuse_recipe keeps its own literal (its extract-and-exec tests need
    its constants literal, as with _REUSE_AGENT_MENTIONS); pinned equal to
    the one the trim reads, so a reworded dispatch cannot leave the trim
    looking for the old marker."""
    import ast
    from core.constants import ACTION_DISPATCH_PREFIX
    path = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos',
                        'reuse_recipe.py')
    tree = ast.parse(open(path, encoding='utf-8').read())
    found = [ast.literal_eval(node.value) for node in ast.walk(tree)
             if isinstance(node, ast.Assign)
             and any(getattr(t, 'id', None) == '_REUSE_ACTION_MESSAGE_PREFIX'
                     for t in node.targets)]
    assert found == [ACTION_DISPATCH_PREFIX]
