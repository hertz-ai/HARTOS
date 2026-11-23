"""The person's reply stays protected for every later step of their turn,
also after the ROLE-ORDER-GUARD merged it behind a verifier verdict.

WHAT WENT WRONG (live 2026-11-21 12:46:44-12:46:50 IST, speech agent
1794540972213, build f5d2f7b04, frozen_debug.log.old): the child's reply
"I am the child" rode in action 3's seed (Message[13], seat User), right after
a StatusVerifier verdict (Message[12]).  For action 4's request:

  * the seats' limiter had already cut the history's head, so the first user
    turn was a verdict, and _task_turn (the opener's newest turn) was the
    newest verdict, not the person's words;
  * validate_messages merged 12+13 into ONE turn that kept the verdict's name;
  * 12:46:45 "[TRIM] trimmed 20 msg(s) ... budget 4126" dropped it, and
    action 4 (llm_outbound.jsonl row 502, four messages) saved
    name_response='Hi' with the reply nowhere in its request.

THE RULE: _task_turn protects the newest user turn posted from a person seat
(core.constants.PERSON_SEATS) or merged from one.  validate_messages records,
out of band and keyed by the merged text (the wire trim reads the body
re-parsed from JSON), which seats a merged turn came from; it never renames
it, so the verdict is never sent, or read back, as the person's words
(hartos-52's review).

Real code: hartos.helper ToolMessageHandler.validate_messages, the seats'
token limiter, and core.llm_outbound_logger._trim_to_budget, with only the
model's context size pinned.

    python -m pytest tests/unit/test_a_persons_reply_survives_the_wire_trim.py -q --noconftest
"""
import copy
import json
from unittest.mock import MagicMock

import pytest

import core.llm_outbound_logger as lol
from core.constants import (
    AUTOGEN_MESSAGE_TOKEN_BUDGET, AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE)

REPLY = 'I am the child'
LIVE_BUDGET = 4126          # "[TRIM] ... budget 4126" at 12:46:45


def _prose(tag, n_words):
    words = ('the child looked at the toy and the parent wrote what happened '
             'during the practice of names in tamil at home today').split()
    return tag + ': ' + ' '.join(f'{words[i % len(words)]}{i % 97}'
                                 for i in range(n_words))


def _verdict(aid, note):
    return {'role': 'user', 'name': 'StatusVerifier', 'content': json.dumps(
        {'status': 'pending', 'action_id': aid, 'message': _prose(note, 60)})}


def _order(aid, step):
    return {'role': 'user', 'name': 'ChatInstructor',
            'content': (f'Perform this action -> Action #{aid}:{step}'
                        '\n follow these steps: []')}


def _seed():
    """Action 3's reply seed, as _reuse_seed_message posted it at 12:46:06."""
    return {'role': 'user', 'name': 'User', 'content': (
        "Perform this action -> Action #3:send_message_to_user: read the "
        "child's reply to their name in the person's latest message\n\n"
        "This step's question was already delivered to the person and is "
        "waiting for their answer.\nThe question: இங்கே பாரு! (Look here!)\n"
        f"Their words:\n{REPLY}\n follow these steps: []")}


def _call(cid, tool, name=None):
    m = {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': cid, 'type': 'function',
         'function': {'name': tool, 'arguments': '{}'}}]}
    if name:
        m['name'] = name
    return m


def _result(cid, n_words, body=None):
    body = _prose('result ' + cid, n_words) if body is None else body
    return {'role': 'tool', 'name': 'Assistant', 'tool_call_id': cid, 'content': body,
            'tool_responses': [{'tool_call_id': cid, 'role': 'tool', 'content': body}]}


def _live_order():
    """Action 4's ToolMessageHandler input at 12:46:45, seat for seat
    (CONVERSATION STRUCTURE, 28 messages); sizes put the turns after the
    reply over the live budget, as they were."""
    return [
        _verdict(3, 'v0'), {'role': 'user', 'name': 'Helper', 'content': _prose('helper', 40)},
        _call('c1', 'get_user_camera_inp_by_mins'), _result('c1', 300),
        _verdict(3, 'v1'), _order(3, 'send prompt message if no reply found'),
        _call('c2', 'get_data_by_key'), _result('c2', 300),
        _verdict(3, 'v2'), _order(3, 'check the parent text for the reply'),
        _call('c3', 'send_message_to_user', 'Helper'), _result('c3', 300),
        _verdict(3, 'v3'), _seed(),
        _call('c4', 'get_user_id'), _result('c4', 700),
        _verdict(3, 'v4'),
        _call('c5', 'get_chat_history', 'Helper'), _result('c5', 700),
        _call('c6', 'get_saved_metadata'), _result('c6', 700),
        _call('c7', 'get_data_by_key'), _result('c7', 700),
        {'role': 'user', 'name': 'ChatInstructor',
         'content': 'Your tool for this action has already executed and returned its result.'},
        _call('c8', 'send_message_to_user'), _result('c8', 30),
        _verdict(3, 'v5'),
        _order(4, "save_data_in_memory: key 'name_response', the value is what "
                  "the child did when called"),
    ]


@pytest.fixture
def handler(monkeypatch):
    import hartos.helper as helper
    monkeypatch.setattr(helper, 'current_app', MagicMock())
    return helper.ToolMessageHandler(user_tasks=None, user_prompt=None)


def _wire_trim(messages, monkeypatch):
    """The request body as the wire trim sees it: re-parsed from JSON."""
    monkeypatch.setattr(lol, '_get_budget_per_slot', lambda: 8192)
    monkeypatch.setattr(lol, '_message_budget', lambda *a, **k: LIVE_BUDGET)
    body = json.loads(json.dumps({'model': 'llama', 'max_tokens': 2048,
                                  'messages': [{'role': 'system',
                                                'content': 'You are the reuse assistant.'}]
                                  + messages}))
    out, n_dropped, _, est_before, est_after, budget = lol._trim_to_budget(body)
    assert n_dropped > 0 and est_before > budget, 'fixture failed to force a trim'
    return out['messages'], est_after, budget


def test_the_live_order_keeps_the_persons_reply_through_the_wire_trim(handler, monkeypatch):
    merged = handler.validate_messages(copy.deepcopy(_live_order()))
    holder = [m for m in merged if REPLY in str(m.get('content'))]
    assert len(holder) == 1 and holder[0]['name'] == 'StatusVerifier', [
        (m.get('role'), m.get('name')) for m in merged]
    out, est_after, budget = _wire_trim(merged, monkeypatch)
    assert any(REPLY in str(m.get('content')) for m in out), (
        "the person's reply was trimmed from the next step's request "
        '(post-trim seats: %r)' % [(m.get('role'), m.get('name')) for m in out])
    assert est_after <= budget


def test_the_persons_reply_survives_the_seats_2500_token_window():
    """The seats' limiter, before any merge: the reply's own seat is User and
    the opener is a verdict."""
    from hartos.helper import token_limiter
    out = token_limiter(max_tokens=AUTOGEN_MESSAGE_TOKEN_BUDGET,
                        max_tokens_per_message=AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE,
                        min_tokens=0).apply_transform(_live_order())
    kept = [m for m in out if REPLY in str(m.get('content'))]
    assert kept and kept[0].get('name') == 'User', [
        (m.get('role'), m.get('name')) for m in out]


def test_a_merged_verdict_and_reply_is_never_the_persons_answer(handler):
    """The merge keeps the verdict's seat, so the seat readers never take the
    verdict's text for the person's answer; the same reply on its own seat is
    their answer (the reader is live)."""
    import hartos.lifecycle_hooks as lh
    asked = [_order(3, 'ask the parent what the child did'),
             {'role': 'assistant', 'content': '', 'tool_calls': [
                 {'id': 's1', 'type': 'function', 'function': {
                     'name': 'send_message_to_user',
                     'arguments': json.dumps({'text': 'What did the child do?',
                                              'expects_reply': True})}}]},
             _result('s1', 0, 'Message sent successfully to user with request_id: r1'),
             _verdict(3, 'v9'),
             {'role': 'user', 'name': 'UserProxy', 'content': REPLY}]
    assert lh.window_question(asked, 3, [REPLY]) is None
    merged = handler.validate_messages(copy.deepcopy(asked))
    holder = [m for m in merged if REPLY in str(m.get('content'))]
    assert len(holder) == 1 and holder[0]['name'] == 'StatusVerifier', merged
    assert lh.window_question(merged, 3, [REPLY]) is not None


def test_bodies_with_no_person_seat_keep_the_openers_rule(monkeypatch):
    """No regression: a named body with no person seat protects its opener's
    newest turn, as before."""
    msgs = [{'role': 'user', 'name': 'Admin', 'content': 'task one'},
            {'role': 'assistant', 'content': 'ok'},
            {'role': 'user', 'name': 'Admin', 'content': 'task two'},
            {'role': 'user', 'name': 'Critic', 'content': 'looks fine'}]
    assert lol._task_turn(msgs) is msgs[2]
