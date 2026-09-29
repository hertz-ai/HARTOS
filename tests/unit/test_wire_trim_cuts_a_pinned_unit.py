"""What the drop keeps for a protected tool result can still be cut.

be96f2510 protected the newest tool result, and 05806ae09 keeps a tool call
and its results together.  Together they pinned the whole unit -- the
assistant message carrying the tool_calls and every sibling result -- while
the cut pass shrank only the protected messages and the newest one.  The
pinned mass could be neither dropped nor cut.  Review of be96f2510
(hartos-5e scratchpad be_probe.py), real _trim_to_budget at per_slot 12288,
budget 7424:

  * [sys, task, call with 40k-char arguments, result, verdict]:
    the parent commit sends 721 tokens; be96f2510 sends 20,243 (over).
  * [sys, task, call, 3 parallel ~16k-char results, verdict]:
    the parent sends 721; be96f2510 sends 8,307 (over).

Both are llama-server context overflows.  The rule pinned here: every
message the drop keeps only because its unit holds a protected message is a
cut candidate too, cut before the protected ones; a call's arguments are cut
inside a strict JSON object (llama.cpp 500s on arguments that are not JSON);
the unit stays paired.
"""
import json

import core.llm_outbound_logger as lol
from core.constants import WIRE_TRIM_MARKER
from hartos.helper import is_wire_json

_PER_SLOT = 12288


def _trim(msgs, monkeypatch, max_tokens=2048):
    monkeypatch.setattr(lol, '_get_budget_per_slot', lambda: _PER_SLOT)
    out, _, _, _, est_after, budget = lol._trim_to_budget(
        {'model': 'llama', 'messages': msgs, 'max_tokens': max_tokens})
    return out['messages'], est_after, budget


def _paired(msgs):
    announced = set()
    for m in msgs:
        for tc in (m.get('tool_calls') or []):
            announced.add(tc['id'])
        if m.get('role') == 'tool' and m.get('tool_call_id') not in announced:
            return False
    return True


def test_a_call_with_huge_arguments_is_cut_to_fit(monkeypatch):
    sys_m = {'role': 'system', 'content': 'sys ' * 500}
    task = {'role': 'user', 'name': 'User', 'content': 'task ' * 200}
    call = {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': 'c1', 'type': 'function',
                            'function': {'name': 'write_file',
                                         'arguments': json.dumps(
                                             {'body': 'x ' * 20000})}}]}
    result = {'role': 'tool', 'tool_call_id': 'c1', 'content': 'row ' * 500}
    verdict = {'role': 'user', 'name': 'StatusVerifier',
               'content': '{"status":"pending"}'}
    out, est_after, budget = _trim([sys_m, task, call, result, verdict],
                                   monkeypatch)
    assert est_after <= budget, (est_after, budget)
    assert _paired(out)
    kept_call = next(m for m in out if m.get('tool_calls'))
    args = kept_call['tool_calls'][0]['function']['arguments']
    assert is_wire_json(args) and isinstance(json.loads(args), dict), args[:200]
    kept_text = json.loads(args)['trimmed_arguments']
    assert WIRE_TRIM_MARKER in kept_text
    assert kept_text.startswith('{"body": "x x')
    assert kept_call['tool_calls'][0]['function']['name'] == 'write_file'


def test_parallel_results_are_cut_to_fit(monkeypatch):
    sys_m = {'role': 'system', 'content': 'sys ' * 500}
    task = {'role': 'user', 'name': 'User', 'content': 'task ' * 200}
    call = {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': 'c%d' % i, 'type': 'function',
                            'function': {'name': 'crawl', 'arguments': '{}'}}
                           for i in range(3)]}
    results = [{'role': 'tool', 'tool_call_id': 'c%d' % i,
                'content': ('R%d ' % i) + 'row ' * 4000} for i in range(3)]
    verdict = {'role': 'user', 'name': 'StatusVerifier',
               'content': '{"status":"pending"}'}
    out, est_after, budget = _trim([sys_m, task, call] + results + [verdict],
                                   monkeypatch)
    assert est_after <= budget, (est_after, budget)
    assert _paired(out)
    kept = [m for m in out if m.get('role') == 'tool']
    assert [m['tool_call_id'] for m in kept] == ['c0', 'c1', 'c2']
    for m in kept:
        assert m['content'].startswith('R'), m['content'][:40]
    # The newest (protected) result keeps at least as much as its siblings.
    assert len(kept[-1]['content']) >= max(len(m['content']) for m in kept[:-1])


# ── review of the f97b6bed8 end state (rvtrim_probe2.py) ──────────────────

def _write_call(content, body_text):
    return {'role': 'assistant', 'content': content,
            'tool_calls': [{'id': 'c0', 'type': 'function', 'function': {
                'name': 'write_file',
                'arguments': json.dumps({'path': 'x.py', 'content': body_text})}}]}


def _write_shape(content, body_text):
    sys_m = {'role': 'system', 'content': 'sys ' * 500}
    task = {'role': 'user', 'name': 'User', 'content': 'task ' * 200}
    verdict = {'role': 'user', 'name': 'StatusVerifier',
               'content': '{"status":"pending"}'}
    call = _write_call(content, body_text)
    res = {'role': 'tool', 'tool_call_id': 'c0', 'content': 'written'}
    return [sys_m, task, call, res, verdict]


def _kept_args(out):
    return [tc for m in out for tc in (m.get('tool_calls') or [])][0][
        'function']['arguments']


def test_a_call_with_text_content_has_its_arguments_cut_too(monkeypatch):
    """The cut touched a call's arguments only when it had no text: 40k
    arguments plus "Writing the file." ended at 20,416 tokens against 7,424."""
    out, est_after, budget = _trim(
        _write_shape('Writing the file.', 'print(1)\n' * 4000), monkeypatch)
    assert est_after <= budget, (est_after, budget)
    args = _kept_args(out)
    assert is_wire_json(args) and isinstance(json.loads(args), dict)
    assert _paired(out)


def test_quote_dense_arguments_are_cut_to_fit(monkeypatch):
    """Sized before json.dumps escaping, quote-, backslash- and emoji-dense
    arguments stayed 10k-19k tokens against 7,424."""
    # Quote + backslash, literal backslash escapes, emoji: each is escaped
    # to more characters by json.dumps than it holds.
    for dense in ('"q" \\ ' * 6000, '\\n\\t' * 8000, '\U0001F600 ' * 6000):
        with_budget = _trim(_write_shape(None, dense), monkeypatch)
        out, est_after, budget = with_budget
        assert est_after <= budget, (dense[:10], est_after, budget)
        args = _kept_args(out)
        assert is_wire_json(args) and isinstance(json.loads(args), dict)


def test_a_call_whose_arguments_fit_their_share_is_left_as_written(
        monkeypatch):
    """The pre-check in _truncate_tool_call_arguments: of two parallel calls,
    only the one over its share is cut; the other keeps its arguments
    byte-identical, not wrapped in trimmed_arguments."""
    sys_m = {'role': 'system', 'content': 'sys ' * 500}
    task = {'role': 'user', 'name': 'User', 'content': 'task ' * 200}
    small = json.dumps({'path': 'a.txt'})
    call = {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'c0', 'type': 'function', 'function': {
            'name': 'write', 'arguments': json.dumps({'body': 'x ' * 20000})}},
        {'id': 'c1', 'type': 'function', 'function': {
            'name': 'read', 'arguments': small}}]}
    res = [{'role': 'tool', 'tool_call_id': 'c0', 'content': 'ok'},
           {'role': 'tool', 'tool_call_id': 'c1', 'content': 'read'}]
    verdict = {'role': 'user', 'name': 'StatusVerifier',
               'content': '{"status":"pending"}'}
    out, est_after, budget = _trim([sys_m, task, call] + res + [verdict],
                                   monkeypatch)
    assert est_after <= budget
    kept = [tc for m in out for tc in (m.get('tool_calls') or [])]
    assert kept[1]['function']['arguments'] == small
    assert 'trimmed_arguments' in kept[0]['function']['arguments']
