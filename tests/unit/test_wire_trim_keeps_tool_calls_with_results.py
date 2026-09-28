"""The wire trim keeps a tool call and its results together, or drops both.

Review of 9ddc8b92d, probed: ``[system, User task 7.5k, assistant
tool_calls, StatusVerifier, tool 12k]`` trimmed to ``[system, user, user,
tool(cut)]``.  The drop removed the assistant message that carried the
tool_calls and kept its result, because the result was the newest message.

Measured against the live llama-server (b10330, Qwen3.5-4B template,
/apply-template and a max_tokens=1 completion, 2026-09-26): the server
accepts that body with HTTP 200 and renders the result as a bare
``<tool_response>`` user turn with no ``<tool_call>`` before it, so the
model reads an answer to a call it never sees -- which tool, which
arguments.  The kept-together body renders the ``<tool_call>`` block.  A
hosted OpenAI-style endpoint was not measured here.

The rule these tests pin, inside the one trimmer (``_trim_to_budget``): an
assistant message carrying tool_calls and the role='tool' messages that
answer it (matched by ``tool_call_id``) are one unit.  The drop removes the
whole unit or none of it, and a unit holding the newest message is kept.
Behavioural: the real ``_trim_to_budget`` with only the budget pinned.
"""
import core.llm_outbound_logger as lol
from core.constants import WIRE_TRIM_SAFETY_MARGIN_TOKENS, WIRE_TRIM_MARKER
from core.token_utils import count_tokens_for_messages

_MAX_TOKENS = 64


def _sys():
    return {'role': 'system', 'content': 'sys ' * 50}


def _call(*ids):
    return {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': i, 'type': 'function',
                            'function': {'name': 'crawl', 'arguments': '{}'}}
                           for i in ids]}


def _result(call_id, content):
    return {'role': 'tool', 'tool_call_id': call_id, 'content': content}


def _trim(messages, per_slot, monkeypatch):
    monkeypatch.setattr(lol, '_get_budget_per_slot', lambda: per_slot)
    out, n_dropped, _, _, est_after, budget = lol._trim_to_budget(
        {'model': 'llama', 'messages': messages, 'max_tokens': _MAX_TOKENS})
    return out['messages'], n_dropped, est_after, budget


def _per_slot_fitting(keep):
    return (count_tokens_for_messages(keep, None) + _MAX_TOKENS
            + WIRE_TRIM_SAFETY_MARGIN_TOKENS + 20)


def _orphans(messages):
    """role='tool' messages whose call no earlier kept message announced."""
    announced, orphans = set(), []
    for m in messages:
        for tc in (m.get('tool_calls') or []):
            announced.add(tc['id'])
        if m.get('role') == 'tool' and m.get('tool_call_id') not in announced:
            orphans.append(m)
    return orphans


def test_the_reviewed_shape_keeps_the_call_its_newest_result_answers(
        monkeypatch):
    """The probed body and budget: the result is the newest message, so its
    call stays with it and the result is what gets cut."""
    task = {'role': 'user', 'name': 'User',
            'content': 'TASKHEAD ' + 'task ' * 1500 + ' TASKTAIL'}
    verdict = {'role': 'user', 'name': 'StatusVerifier',
               'content': '{"status":"pending"}'}
    call = _call('c1')
    result = _result('c1', 'PAGEHEAD ' + 'row ' * 3000 + ' PAGETAIL')
    msgs = [_sys(), task, call, verdict, result]
    out, _, est_after, budget = _trim(
        msgs, 2000 + _MAX_TOKENS + WIRE_TRIM_SAFETY_MARGIN_TOKENS, monkeypatch)
    assert _orphans(out) == [], (
        'a tool result was kept without the call it answers: %r'
        % [(m.get('role'), m.get('name')) for m in out])
    assert call in out
    assert out[-1]['role'] == 'tool'
    assert WIRE_TRIM_MARKER in out[-1]['content']
    assert out[-1]['content'].endswith('PAGETAIL')
    assert est_after <= budget


def test_a_droppable_call_goes_with_all_of_its_results(monkeypatch):
    """A parallel call (two ids) and both answers are dropped as one: no
    result outlives the call, and no call is left unanswered."""
    task = {'role': 'user', 'name': 'User', 'content': 'crawl both pages'}
    call = _call('a', 'b')
    res_a = _result('a', 'A ' + 'x ' * 600)
    res_b = _result('b', 'B ' + 'y ' * 600)
    reply = {'role': 'assistant', 'content': 'both crawled'}
    # A later call and result: the newest tool result is protected, so the
    # pair under test must be an earlier one.
    later_call, later_res = _call('c'), _result('c', 'C done')
    newest = {'role': 'user', 'name': 'User', 'content': 'now summarise'}
    msgs = [_sys(), task, call, res_a, res_b, reply, later_call, later_res,
            newest]
    keep = [_sys(), res_b, reply, later_call, later_res, newest]
    out, n_dropped, est_after, budget = _trim(
        msgs, _per_slot_fitting(keep), monkeypatch)
    assert call not in out and res_a not in out and res_b not in out, (
        'the call and its results must leave together: %r'
        % [m.get('tool_call_id') or m.get('role') for m in out])
    assert n_dropped >= 3
    assert est_after <= budget


def test_a_reused_call_id_pairs_with_the_nearest_earlier_call(monkeypatch):
    """Ids repeat across turns (a model that numbers its calls from 1 every
    time).  Dropping the old call takes only its own result; the newer call
    and its answer stay."""
    old_call = dict(_call('c1'), content='first try')
    old_res = _result('c1', 'OLD ' + 'o ' * 800)
    new_call, new_res = _call('c1'), _result('c1', 'NEW result')
    task = {'role': 'user', 'name': 'User', 'content': 'crawl it again'}
    msgs = [_sys(), old_call, old_res, task, new_call, new_res]
    keep = [_sys(), task, new_call, new_res]
    out, _, est_after, budget = _trim(msgs, _per_slot_fitting(keep),
                                      monkeypatch)
    assert old_call not in out and old_res not in out
    assert out[-2:] == [new_call, new_res]
    assert _orphans(out) == []
    assert est_after <= budget


def test_a_result_with_no_announcing_call_is_still_droppable(monkeypatch):
    """A tool message no assistant announced has no unit; it drops alone,
    as before (the shape the older trim tests use)."""
    task = {'role': 'user', 'name': 'User', 'content': 'go'}
    stray = _result('never_announced', 'S ' + 's ' * 800)
    call, res = _call('c9'), _result('c9', 'newest result')
    reply = {'role': 'assistant', 'content': 'done'}
    msgs = [_sys(), task, stray, call, res, reply]
    keep = [_sys(), task, call, res, reply]
    out, n_dropped, _, _ = _trim(msgs, _per_slot_fitting(keep), monkeypatch)
    assert stray not in out
    assert n_dropped == 1
