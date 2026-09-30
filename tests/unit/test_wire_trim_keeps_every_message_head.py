"""A message the wire trim shortens keeps its head: the middle is elided.

Live 2026-09-27, installed build (HARTOS 62649bc1), REUSE probe
liveprobe_reuse_1, llm_outbound.jsonl: the dispatch turn reads
``Perform this action -> Action #1:get_self_build_status\\n\\nWhat is the
current self-build status?\\n follow these steps: [...]`` -- marker, then the
user's words, then the steps.  ``_truncate_msg_content`` cut from the HEAD,
so the first call went out as ``...[truncated head]...\\n: ''}}, {'get_data_
by_key(...`` : the marker and the words were the part removed, the steps the
part kept, and the reply was off-topic.  6 of the 77 calls carried the turn
head-cut this way.

The rule pinned here: whatever the trim shortens -- a user turn, a tool
result, the system message -- keeps its head and its tail, and the
``WIRE_TRIM_MARKER`` sits where the middle was.  Behavioural: the real
``_trim_to_budget`` with only the budget pinned.
"""
import core.llm_outbound_logger as lol
from core.constants import WIRE_TRIM_SAFETY_MARGIN_TOKENS, WIRE_TRIM_MARKER

_MAX_TOKENS = 64
_HEAD = ('Perform this action -> Action #1:get_self_build_status\n\n'
         'What is the current self-build status?\n follow these steps: ')
_STEPS = ''.join("{'get_data_by_key({\"key\":\"os.builds.k%d\"})': "
                 "{'tool_name': 'get_data_by_key', 'code': ''}}, " % i
                 for i in range(90))
_TAIL = "{'save_data_in_memory': 'LAST STEP'}]"


def _trim(msgs, budget, monkeypatch):
    per_slot = budget + _MAX_TOKENS + WIRE_TRIM_SAFETY_MARGIN_TOKENS
    monkeypatch.setattr(lol, '_get_budget_per_slot', lambda: per_slot)
    out, _, n_cut, _, est_after, got = lol._trim_to_budget(
        {'model': 'llama', 'messages': msgs, 'max_tokens': _MAX_TOKENS})
    assert n_cut > 0, 'fixture failed to force a cut'
    return out['messages'], est_after, got


def _assert_head_and_tail(text, head, tail):
    # A body that carries a pointer gets the pointer explanation appended to
    # its system message (test_wire_trim_elided_pointers.py); it is not part
    # of the message's own text.
    text = text.split(lol.ELIDED_POINTER_EXPLANATION)[0]
    assert text.startswith(head), 'the head was cut: %r' % text[:120]
    assert text.endswith(tail), 'the tail was cut: %r' % text[-120:]
    assert WIRE_TRIM_MARKER in text


def test_the_live_dispatch_turn_keeps_the_marker_and_the_words(monkeypatch):
    sys_m = {'role': 'system', 'content': 'You are the reuse assistant. ' * 40}
    turn = {'role': 'user', 'name': 'User', 'content': _HEAD + _STEPS + _TAIL}
    out, est_after, budget = _trim([sys_m, turn], 900, monkeypatch)
    kept = next(m for m in out if m.get('name') == 'User')
    _assert_head_and_tail(kept['content'], _HEAD, _TAIL)
    assert est_after <= budget


def test_a_cut_tool_result_keeps_its_head(monkeypatch):
    task = {'role': 'user', 'name': 'User', 'content': 'crawl it'}
    call = {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': 'c1', 'type': 'function',
                            'function': {'name': 'crawl', 'arguments': '{}'}}]}
    result = {'role': 'tool', 'tool_call_id': 'c1',
              'content': 'PAGEHEAD ' + 'row ' * 3000 + ' PAGETAIL'}
    out, est_after, budget = _trim(
        [{'role': 'system', 'content': 'sys'}, task, call, result],
        700, monkeypatch)
    kept = next(m for m in out if m.get('role') == 'tool')
    _assert_head_and_tail(kept['content'], 'PAGEHEAD', 'PAGETAIL')
    assert est_after <= budget


def test_a_cut_system_message_keeps_its_head(monkeypatch):
    sys_m = {'role': 'system',
             'content': 'PERSONA HEAD. ' + 'wisdom ' * 4000 + ' RECIPE TAIL.'}
    out, est_after, budget = _trim(
        [sys_m, {'role': 'user', 'content': 'go'}], 700, monkeypatch)
    _assert_head_and_tail(out[0]['content'], 'PERSONA HEAD.', 'RECIPE TAIL.')
    assert est_after <= budget


# ── review of 111c458b0 ─────────────────────────────────────────────────
# probe_111.py: on the live seat shape the User turn was cut to ~250 chars at
# every budget from 150 to 2000 tokens -- sized first (largest) against a
# 3000-char tool result at full size, floored at 64 tokens, and a fixed
# half/half split lost the middle of any words longer than ~120 chars.

def _seat(words):
    from core.constants import ACTION_STEPS_SEPARATOR
    steps = ''.join("{'get_data_by_key({\"key\":\"os.builds.k%d\"})': "
                    "{'tool_name': 'get_data_by_key', 'code': ''}}, " % i
                    for i in range(200))
    head = ('Perform this action -> Action #1:get_self_build_status\n\n'
            + words)
    turn = {'role': 'user', 'name': 'User',
            'content': head + ACTION_STEPS_SEPARATOR + steps + 'LAST'}
    call = {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': 'c1', 'type': 'function',
                            'function': {'name': 'f', 'arguments': '{}'}}]}
    res = {'role': 'tool', 'tool_call_id': 'c1',
           'content': 'RESULT ' + 'x ' * 1500}
    verdict = {'role': 'user', 'name': 'StatusVerifier',
               'content': 'verdict ' * 200}
    sys_m = {'role': 'system', 'content': 'You are the reuse assistant. ' * 40}
    return head, [sys_m, turn, call, res, verdict]


def _trim_any(msgs, budget, monkeypatch):
    per_slot = budget + _MAX_TOKENS + WIRE_TRIM_SAFETY_MARGIN_TOKENS
    monkeypatch.setattr(lol, '_get_budget_per_slot', lambda: per_slot)
    out, _, _, _, est_after, got = lol._trim_to_budget(
        {'model': 'llama', 'messages': msgs, 'max_tokens': _MAX_TOKENS})
    return out['messages'], est_after, got


def test_the_users_words_survive_whole_at_every_budget(monkeypatch):
    for n_words in (8, 40, 120):
        words = ' '.join('word%d' % i for i in range(n_words)) + '.'
        for budget in (2000, 900, 400):
            head, msgs = _seat(words)
            out, est_after, got = _trim_any(msgs, budget, monkeypatch)
            user = next(m for m in out if m.get('name') == 'User')
            assert user['content'].startswith(head), (
                'words=%d budget=%d: %r' % (n_words, budget,
                                            user['content'][:200]))
            assert WIRE_TRIM_MARKER in user['content']
            # The newest tool result shares the cut: it is kept, not dropped.
            assert any(m.get('role') == 'tool' for m in out)


def test_the_words_hold_when_the_room_is_smaller_than_them(monkeypatch):
    """The words are longer than the room the budget leaves: they are kept
    and the request is reported over budget, never cut through the words."""
    words = ' '.join('word%d' % i for i in range(600)) + '.'
    head, msgs = _seat(words)
    out, _, _ = _trim_any(msgs, 150, monkeypatch)
    user = next(m for m in out if m.get('name') == 'User')
    assert user['content'].startswith(head)


def test_a_multipart_message_is_not_sent_twice(monkeypatch):
    """Two text parts are joined to measure them; the cut replaced the first
    part and kept the second, so the second part's text went out twice."""
    parts = [{'type': 'text', 'text': 'PART-ONE ' + 'a ' * 3000},
             {'type': 'image_url', 'image_url': {'url': 'data:x'}},
             {'type': 'text', 'text': 'PART-TWO ' + 'b ' * 3000 + 'END'}]
    msgs = [{'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': parts}]
    out, est_after, got = _trim(msgs, 700, monkeypatch)
    content = out[-1]['content']
    texts = [p['text'] for p in content if p.get('type') == 'text']
    assert len(texts) == 1, len(texts)
    assert texts[0].startswith('PART-ONE') and texts[0].endswith('END')
    assert any(p.get('type') == 'image_url' for p in content)
    assert est_after <= got


def test_dense_text_is_cut_to_fit(monkeypatch):
    """JSON-dense content runs well under 3.5 chars/token; sizing its cut at
    3.5 left the message over its room and the request over budget."""
    dense = ''.join('{"k%d":[%d,%d]},' % (i, i, i * 7) for i in range(3000))
    msgs = [{'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'HEAD ' + dense + ' TAIL'}]
    out, est_after, got = _trim(msgs, 700, monkeypatch)
    assert est_after <= got, (est_after, got)
    _assert_head_and_tail(out[-1]['content'], 'HEAD', 'TAIL')


def test_a_system_prompt_holding_the_separator_is_still_cut(monkeypatch):
    """Review of f97b6bed8: the separator was honoured in any message, so a
    system prompt that contained it kept everything before it whole.  Only a
    user turn is a dispatch turn."""
    from core.constants import ACTION_STEPS_SEPARATOR
    sys_text = ('PERSONA ' + 'rule ' * 4000 + ACTION_STEPS_SEPARATOR
                + 'recipe ' * 50 + 'TAIL')
    out, est_after, budget = _trim_any(
        [{'role': 'system', 'content': sys_text},
         {'role': 'user', 'content': 'go'}], 700, monkeypatch)
    assert est_after <= budget, (est_after, budget)
    assert WIRE_TRIM_MARKER in out[0]['content']
