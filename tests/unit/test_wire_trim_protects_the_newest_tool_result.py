"""The newest tool result is protected like the task turn: both keep content.

Review of 9ddc8b92d, probed: ``[system, User task 7.5k, assistant
tool_calls, StatusVerifier, tool 12k]`` under pressure.  The tool result was
the one unprotected message, so the cut pass took it first and could floor it
at 64 tokens while the task stayed whole: the verifier and the assistant were
left to judge a crawl by a few rows of its output.

Owner decision (delegated 2026-09-26, "use sensible defaults without creating
more friction"): the newest role='tool' message joins the protected set in
``_trim_to_budget``.  No new mechanism: the existing protected pass cuts the
larger protected message first, each only as far as the others at their
current size require, so the task and the result both keep real content.
Its tool_calls message stays with it (``_drop_units``).

Behavioural: the real ``_trim_to_budget`` with only the budget pinned.
"""
import core.llm_outbound_logger as lol
from core.constants import WIRE_TRIM_SAFETY_MARGIN_TOKENS, WIRE_TRIM_MARKER
from core.token_utils import count_tokens_for_text

_MAX_TOKENS = 64
_FLOOR_TOKENS = 64


def _shape(task_words, rows):
    sys_m = {'role': 'system', 'content': 'sys ' * 50}
    task = {'role': 'user', 'name': 'User',
            'content': 'TASKHEAD ' + 'task ' * task_words + ' TASKTAIL'}
    call = {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': 'c1', 'type': 'function',
                            'function': {'name': 'crawl', 'arguments': '{}'}}]}
    verdict = {'role': 'user', 'name': 'StatusVerifier',
               'content': '{"status":"pending"}'}
    result = {'role': 'tool', 'tool_call_id': 'c1',
              'content': 'PAGEHEAD ' + 'row ' * rows + ' PAGETAIL'}
    return sys_m, task, call, verdict, result


def _trim(msgs, budget, monkeypatch):
    per_slot = budget + _MAX_TOKENS + WIRE_TRIM_SAFETY_MARGIN_TOKENS
    monkeypatch.setattr(lol, '_get_budget_per_slot', lambda: per_slot)
    out, _, _, _, est_after, got_budget = lol._trim_to_budget(
        {'model': 'llama', 'messages': msgs, 'max_tokens': _MAX_TOKENS})
    return out['messages'], est_after, got_budget


def _tokens(m):
    return count_tokens_for_text(m['content'], 'llama')


def test_the_reviewed_shape_keeps_real_content_in_both(monkeypatch):
    sys_m, task, call, verdict, result = _shape(1500, 3000)
    out, est_after, budget = _trim([sys_m, task, call, verdict, result],
                                   2000, monkeypatch)
    kept_task = next(m for m in out if m.get('name') == 'User')
    kept_result = next(m for m in out if m.get('role') == 'tool')
    assert call in out, 'the call must stay with its result'
    # The larger (the result) is cut; the task is not cut at all when
    # cutting the result is enough.
    assert kept_task['content'] == task['content']
    assert WIRE_TRIM_MARKER in kept_result['content']
    assert kept_result['content'].endswith('PAGETAIL')
    # Real content, not the floor: several times the 64-token floor.
    assert _tokens(kept_result) > 4 * _FLOOR_TOKENS, _tokens(kept_result)
    assert est_after <= budget


def test_a_larger_task_is_cut_before_the_result_is_floored(monkeypatch):
    """Task 3,000 words, result 1,500 rows, the result newest.  Cut first
    because it was unprotected, the result went to the 64-token floor while
    the task stayed whole.  Protected, the larger (the task) is cut first."""
    sys_m, task, call, verdict, result = _shape(3000, 1500)
    out, est_after, budget = _trim([sys_m, task, call, verdict, result],
                                   2500, monkeypatch)
    kept_task = next(m for m in out if m.get('name') == 'User')
    kept_result = next(m for m in out if m.get('role') == 'tool')
    assert call in out
    assert kept_result['content'] == result['content'], (
        'the result was cut (%d tok) although cutting the larger task fits'
        % _tokens(kept_result))
    assert WIRE_TRIM_MARKER in kept_task['content']
    assert _tokens(kept_task) > 4 * _FLOOR_TOKENS, _tokens(kept_task)
    assert est_after <= budget


def test_a_result_that_is_not_the_newest_message_is_kept(monkeypatch):
    """[system, task, call, result, StatusVerifier]: the verdict is newest,
    the result sits before it.  Unprotected, the result and its call were
    dropped whole; the verifier then judged a crawl it could not see."""
    sys_m, task, call, verdict, result = _shape(200, 3000)
    out, est_after, budget = _trim([sys_m, task, call, result, verdict],
                                   1500, monkeypatch)
    kept_result = [m for m in out if m.get('role') == 'tool']
    assert kept_result, 'the newest tool result was dropped'
    assert call in out
    assert kept_result[0]['content'].endswith('PAGETAIL')
    assert _tokens(kept_result[0]) > 4 * _FLOOR_TOKENS
    assert est_after <= budget


def test_an_older_tool_result_is_still_droppable(monkeypatch):
    """Only the NEWEST result is protected; an earlier crawl and its call go
    first, together."""
    sys_m, task, call, verdict, result = _shape(50, 200)
    old_call = {'role': 'assistant', 'content': 'first',
                'tool_calls': [{'id': 'c0', 'type': 'function',
                                'function': {'name': 'crawl',
                                             'arguments': '{}'}}]}
    old_result = {'role': 'tool', 'tool_call_id': 'c0',
                  'content': 'OLD ' + 'old ' * 1500}
    msgs = [sys_m, old_call, old_result, task, call, verdict, result]
    out, est_after, budget = _trim(msgs, 900, monkeypatch)
    assert old_call not in out and old_result not in out
    assert result in out and call in out
    assert est_after <= budget
