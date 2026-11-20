"""A step's output sent early, in the step before it, is that step's receipt once.

Live 19 Nov 2026 00:40-00:45 IST (source Nunba, local 4B, walks/*_fix1119):
the 4B sent step N's message while step N-1 was running.  N's watermark
(_stamp_action_evidence_watermark: every call id present at N's dispatch)
excluded it, FABRICATED-COMPLETE re-steered N, and the person got the message
twice.  The commit check (lifecycle_hooks._verifier_completion_has_conversation_
evidence) refuses a receipt outside N's own window too, so the gate, the
receipt finder and the commit check must read ONE rule:
lifecycle_hooks.credited_early_send.  Design agreed with hartos-43 (19 Nov
00:52-01:10 IST): output sends only, same turn by the send's own request id,
"the step before" by dispatch position (never action_id - 1), the earlier
step not itself naming the send nor an ask step, CREATE (no request id)
never credited.

    python -m pytest tests/unit/test_an_early_send_is_credited_once.py -q --noconftest
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import hartos.lifecycle_hooks as lh  # noqa: E402
from hartos import reuse_recipe  # noqa: E402

UP = 'early_send_user_1'
TURN = 'req-turn-2'
SEND = 'send_message_to_user'
SAVE = 'save_data_in_memory'
DRAFT = ("Subject: Request for Time Off - This Friday\n\nDear Manager,\n\n"
         "I would like to request this Friday off. My tasks will be covered.\n\n"
         "Best regards")


class _Task:
    def __init__(self, description):
        self.description = description
        self.context = {}


class _Ledger:
    def __init__(self, descriptions):
        self.tasks = {f'action_{i}': _Task(d) for i, d in descriptions.items()}


class _Agent:
    def __init__(self, name, tools):
        self.name = name
        self._function_map = {t: (lambda **kw: None) for t in tools}
        self.llm_config = {'tools': [{'function': {'name': t}} for t in tools]}
        self._oai_messages = {}


class _Chat:
    def __init__(self, messages):
        self.messages = messages
        self.agents = [_Agent('Helper', [SEND, SAVE]), _Agent('Assistant', [])]


class _ReuseTask:
    def __init__(self, texts, seen):
        self._texts = texts
        self.evidence_seen_call_ids = set(seen)

    def get_action(self, idx):
        return self._texts[idx]


def _dispatch(n, text):
    return {'role': 'user', 'name': 'ChatInstructor',
            'content': f'Perform this action -> Action #{n}: {text}'}


def _call(cid, fn, args):
    return {'role': 'assistant', 'name': 'Helper', 'content': None,
            'tool_calls': [{'id': cid, 'type': 'function',
                            'function': {'name': fn, 'arguments': json.dumps(args)}}]}


def _result(cid, body):
    return {'role': 'tool', 'name': 'Assistant', 'content': body,
            'tool_responses': [{'tool_call_id': cid, 'role': 'tool', 'content': body}]}


def _sent(request_id=TURN):
    return f'Message sent successfully to user with request_id: {request_id}'


def _verdict(n):
    return {'role': 'user', 'name': 'StatusVerifier',
            'content': json.dumps({'status': 'completed', 'action_id': n})}


TEXT_1 = 'save_data_in_memory: email_brief'
TEXT_2 = 'send_message_to_user: generate and display plain text email'
TEXT_3 = 'send_message_to_user: ask user if changes are needed'


def _email_log(send_args=None, request_id=TURN, first=None):
    """The live email shape: action 1 saves the brief and ALSO sends the draft
    (step 2's work); then action 2 is dispatched."""
    args = send_args or {'text': DRAFT, 'expects_reply': False}
    return [
        _dispatch(1, first or TEXT_1),           # 0
        _call('c_save', SAVE, {'key': 'email_brief', 'value': 'x'}),  # 1
        _result('c_save', 'Saved email_brief'),  # 2
        _call('c_draft', SEND, args),            # 3
        _result('c_draft', _sent(request_id)),   # 4
        _verdict(1),                             # 5
        _dispatch(2, TEXT_2),                    # 6
        _verdict(2),                             # 7
    ]


@pytest.fixture
def session():
    saved = {k: dict(getattr(lh, k)) for k in ('_ledger_registry',)
             if isinstance(getattr(lh, k, None), dict)}
    lh.register_ledger_for_session(UP, _Ledger({1: TEXT_1, 2: TEXT_2, 3: TEXT_3}))
    yield
    for k, v in saved.items():
        getattr(lh, k).clear()
        getattr(lh, k).update(v)


def _use(log):
    gc = _Chat(log)
    lh.register_groupchat_for_session(UP, gc)
    return gc


def _credited(log, index=4, call_id='c_draft', action_id=2, request_id=TURN,
              is_ask_step=lambda a: False):
    """REUSE's own reading of a no-tool step's written answer, as its three
    readers pass it (_reuse_early_send_terms)."""
    body = log[index]['tool_responses'][0]['content']
    answered = reuse_recipe._reuse_early_send_terms(UP)['answered_in_writing']
    return lh.credited_early_send(UP, log, index, call_id, body, action_id,
                                  request_id=request_id, is_ask_step=is_ask_step,
                                  answered_in_writing=answered)


# ---- the rule ---------------------------------------------------------------

def test_the_live_email_shape_is_credited(session):
    log = _email_log()
    _use(log)
    assert _credited(log) is True


def test_create_passes_no_turn_and_credits_nothing(session):
    log = _email_log()
    _use(log)
    assert _credited(log, request_id=None) is False


def test_a_send_from_another_turn_is_never_credited(session):
    log = _email_log(request_id='req-turn-1')
    _use(log)
    assert _credited(log) is False


def test_an_early_ask_is_not_credited(session):
    """Scope: output sends only. The cafe's early name QUESTION stays out."""
    log = _email_log(send_args={'text': 'What is your name?', 'expects_reply': True})
    _use(log)
    assert _credited(log) is False


def test_the_earlier_step_naming_the_send_takes_it_first(session):
    lh.register_ledger_for_session(
        UP, _Ledger({1: 'send_message_to_user: greet the person', 2: TEXT_2, 3: TEXT_3}))
    log = _email_log(first='send_message_to_user: greet the person')
    _use(log)
    assert _credited(log) is False


def test_an_earlier_step_that_names_no_tool_keeps_its_send(session):
    """rv_6d6e MF1: a step naming no tool is finished by its own send
    (user_output_receipt), so that send is its receipt, never the next
    step's: one send must not complete both while the email never goes out."""
    lh.register_ledger_for_session(
        UP, _Ledger({1: 'Greet the person warmly by name', 2: TEXT_2, 3: TEXT_3}))
    log = _email_log(first='Greet the person warmly by name')
    _use(log)
    assert _credited(log) is False


def test_an_earlier_no_tool_step_with_a_written_answer_leaves_the_send(session):
    """rv_357d N1: an M naming no tool that is finished by its WRITTEN answer
    does not need the send, so the early send is still N's (as on the
    parent).  Refusing it re-steered N and the person got N's message twice."""
    greet = 'Greet the person warmly by name'
    lh.register_ledger_for_session(UP, _Ledger({1: greet, 2: TEXT_2, 3: TEXT_3}))
    log = [
        _dispatch(1, greet),                                            # 0
        {'role': 'assistant', 'name': 'Assistant',
         'content': 'Hello Priya, it is lovely to meet you. Welcome aboard, '
                    'I will help you write your email today.'},         # 1
        _call('c_draft', SEND, {'text': DRAFT, 'expects_reply': False}),  # 2
        _result('c_draft', _sent()),                                     # 3
        _verdict(1),                                                     # 4
        _dispatch(2, TEXT_2),                                            # 5
        _verdict(2),                                                     # 6
    ]
    _use(log)
    assert _credited(log, index=3) is True


GREET = 'Greet the person warmly by name'
GREETING = 'Hello Priya, it is lovely to meet you! Welcome aboard.'


def test_a_note_riding_the_send_call_is_not_the_earlier_steps_answer(session):
    """rv_cc80 F1 (P1): the 4B writes a line beside its send call. REUSE never
    counts such a note as an answer (it narrates the call: all 75 on build 69
    did), so REUSE finishes M on the send and the send stays M's. Crediting it
    to N too finished both steps on one greeting while N's email never went
    out."""
    lh.register_ledger_for_session(UP, _Ledger({1: GREET, 2: TEXT_2, 3: TEXT_3}))
    narrated = _call('c_g', SEND, {'text': GREETING, 'expects_reply': False})
    narrated.update(name='Assistant', content='Let me greet Priya warmly now.')
    log = [
        _dispatch(1, GREET),          # 0
        narrated,                     # 1
        _result('c_g', _sent()),      # 2
        _verdict(1),                  # 3
        _dispatch(2, TEXT_2),         # 4
        _verdict(2),                  # 5
    ]
    _use(log)
    assert _credited(log, index=2, call_id='c_g') is False


def test_an_earlier_turns_answer_is_not_the_earlier_steps_answer(session):
    """rv_cc80 F1 (P2): M wrote its greeting in turn 1 and only SENDS in turn
    2 (the log keeps every turn, clear_history=False). REUSE reads M's answer
    back to M's latest dispatch only, so turn 2's M is finished by its send,
    which stays M's."""
    lh.register_ledger_for_session(UP, _Ledger({1: GREET, 2: TEXT_2, 3: TEXT_3}))
    log = [
        _dispatch(1, GREET),                                              # 0
        {'role': 'assistant', 'name': 'Assistant',
         'content': 'Hello Priya, it is lovely to meet you. Welcome aboard!'},  # 1
        _verdict(1),                                                      # 2
        _dispatch(2, TEXT_2),                                             # 3
        _call('c_old', SEND, {'text': DRAFT, 'expects_reply': False}),    # 4
        _result('c_old', _sent('req-turn-1')),                            # 5
        _verdict(2),                                                      # 6
        _dispatch(1, GREET),                                              # 7  turn 2
        _call('c_g', SEND, {'text': GREETING, 'expects_reply': False}),   # 8
        _result('c_g', _sent()),                                          # 9
        _verdict(1),                                                      # 10
        _dispatch(2, TEXT_2),                                             # 11
        _verdict(2),                                                      # 12
    ]
    _use(log)
    assert _credited(log, index=9, call_id='c_g') is False


def test_an_earlier_steps_status_report_is_not_its_answer(session):
    """REUSE's written answer is also is_written_answer: the Assistant's own
    {"status": ...} report is control JSON, not work, so REUSE finishes M on
    the send and the send stays M's."""
    lh.register_ledger_for_session(UP, _Ledger({1: GREET, 2: TEXT_2, 3: TEXT_3}))
    log = [
        _dispatch(1, GREET),                                              # 0
        {'role': 'assistant', 'name': 'Assistant',
         'content': json.dumps({'status': 'completed', 'action_id': 1})},  # 1
        _call('c_g', SEND, {'text': GREETING, 'expects_reply': False}),   # 2
        _result('c_g', _sent()),                                          # 3
        _verdict(1),                                                      # 4
        _dispatch(2, TEXT_2),                                             # 5
        _verdict(2),                                                      # 6
    ]
    _use(log)
    assert _credited(log, index=3, call_id='c_g') is False


def _greet_shape(kind):
    """(log, send result index, call id, index of M's last verdict) for one
    shape of a no-tool greeting step M followed by the email step N."""
    answer = {'role': 'assistant', 'name': 'Assistant',
              'content': 'Hello Priya, it is lovely to meet you. Welcome aboard, '
                         'I will help you write your email today.'}
    narrated = _call('c_g', SEND, {'text': GREETING, 'expects_reply': False})
    narrated.update(name='Assistant', content='Let me greet Priya warmly now.')
    if kind == 'written_answer':      # rv_357d N1
        return [_dispatch(1, GREET), answer,
                _call('c_g', SEND, {'text': DRAFT, 'expects_reply': False}),
                _result('c_g', _sent()), _verdict(1), _dispatch(2, TEXT_2),
                _verdict(2)], 3, 'c_g', 4
    if kind == 'no_answer':           # rv_6d6e MF1
        return [_dispatch(1, GREET),
                _call('c_g', SEND, {'text': GREETING, 'expects_reply': False}),
                _result('c_g', _sent()), _verdict(1), _dispatch(2, TEXT_2),
                _verdict(2)], 2, 'c_g', 3
    if kind == 'narrated_call':       # rv_cc80 P1
        return [_dispatch(1, GREET), narrated, _result('c_g', _sent()),
                _verdict(1), _dispatch(2, TEXT_2), _verdict(2)], 2, 'c_g', 3
    if kind == 'earlier_turn':        # rv_cc80 P2
        return [_dispatch(1, GREET), answer, _verdict(1), _dispatch(2, TEXT_2),
                _call('c_old', SEND, {'text': DRAFT, 'expects_reply': False}),
                _result('c_old', _sent('req-turn-1')), _verdict(2),
                _dispatch(1, GREET),
                _call('c_g', SEND, {'text': GREETING, 'expects_reply': False}),
                _result('c_g', _sent()), _verdict(1), _dispatch(2, TEXT_2),
                _verdict(2)], 9, 'c_g', 10
    if kind == 'echo':                # hartos-52 point 3
        return [_dispatch(1, GREET),
                _call('c_g', SEND, {'text': DRAFT, 'expects_reply': False}),
                _result('c_g', _sent()),
                {'role': 'assistant', 'name': 'Assistant', 'content': DRAFT},
                _verdict(1), _dispatch(2, TEXT_2), _verdict(2)], 2, 'c_g', 4
    if kind == 'late_text':           # hartos-52 point 4
        return [_dispatch(1, GREET),
                _call('c_g', SEND, {'text': GREETING, 'expects_reply': False}),
                _result('c_g', _sent()), _verdict(1),
                {'role': 'assistant', 'name': 'Assistant',
                 'content': 'That was lovely, Priya. Shall we write the email now?'},
                _dispatch(2, TEXT_2), _verdict(2)], 2, 'c_g', 3
    if kind == 'assistant_status_after_verdict':   # rv_4bc4 F1, shape E3
        return [_dispatch(1, GREET),
                _call('c_g', SEND, {'text': GREETING, 'expects_reply': False}),
                _result('c_g', _sent()), _verdict(1),
                {'role': 'assistant', 'name': 'Assistant',
                 'content': 'Shall we write the email now?'},
                {'role': 'assistant', 'name': 'Assistant',
                 'content': json.dumps({'status': 'completed'})},
                _dispatch(2, TEXT_2), _verdict(2)], 2, 'c_g', 3
    if kind == 'tool_status_in_window':   # hartos-52 shape 11
        return [_dispatch(1, GREET),
                _call('c_s', SAVE, {'key': 'greeted', 'value': 'yes'}),
                _result('c_s', json.dumps({'status': 'success'})), answer,
                _call('c_g', SEND, {'text': DRAFT, 'expects_reply': False}),
                _result('c_g', _sent()), _verdict(1), _dispatch(2, TEXT_2),
                _verdict(2)], 5, 'c_g', 6
    if kind == 'tool_status_after_verdict':   # hartos-52 shape 12
        return [_dispatch(1, GREET),
                _call('c_g', SEND, {'text': GREETING, 'expects_reply': False}),
                _result('c_g', _sent()), _verdict(1),
                {'role': 'assistant', 'name': 'Assistant',
                 'content': 'That was lovely, Priya. Shall we write the email now?'},
                _call('c_s', SAVE, {'key': 'greeted', 'value': 'yes'}),
                _result('c_s', json.dumps({'status': 'ok'})),
                _dispatch(2, TEXT_2), _verdict(2)], 2, 'c_g', 3
    if kind in ('verdict_renamed', 'verdict_nameless'):   # rv_ae7e F1: the #725 sync
        log, send_at, call_id, m_verdict = _greet_shape('written_answer')
        verdict = dict(log[m_verdict])
        if kind == 'verdict_renamed':
            verdict['name'] = 'Assistant'
        else:
            verdict.pop('name', None)
        log[m_verdict] = verdict
        return log, send_at, call_id, m_verdict
    if kind == 'helper_to_user':      # REUSE's reply filter passes it, is_written_answer not
        return [_dispatch(1, GREET),
                {'role': 'assistant', 'name': 'Helper',
                 'content': '@user Hello Priya, it is lovely to meet you!'},
                _call('c_g', SEND, {'text': GREETING, 'expects_reply': False}),
                _result('c_g', _sent()), _verdict(1), _dispatch(2, TEXT_2),
                _verdict(2)], 3, 'c_g', 4
    if kind == 'status_json':
        return [_dispatch(1, GREET),
                {'role': 'assistant', 'name': 'Assistant',
                 'content': json.dumps({'status': 'completed', 'action_id': 1})},
                _call('c_g', SEND, {'text': GREETING, 'expects_reply': False}),
                _result('c_g', _sent()), _verdict(1), _dispatch(2, TEXT_2),
                _verdict(2)], 3, 'c_g', 4
    raise ValueError(kind)


@pytest.fixture
def reuse_turn(monkeypatch):
    """REUSE's per-session state for its own receipt finder, restored after."""
    monkeypatch.setitem(reuse_recipe.request_id_list, UP, TURN)
    monkeypatch.setattr(reuse_recipe, '_reuse_ask_only_step', lambda up, a, gc: False)
    monkeypatch.setitem(reuse_recipe.user_tasks, UP,
                        _ReuseTask([GREET, TEXT_2, TEXT_3], ()))


@pytest.mark.parametrize('kind', ['written_answer', 'no_answer', 'narrated_call',
                                  'earlier_turn', 'echo', 'late_text',
                                  'status_json', 'helper_to_user',
                                  'verdict_renamed', 'verdict_nameless',
                                  'tool_status_in_window',
                                  'tool_status_after_verdict',
                                  'assistant_status_after_verdict'])
def test_the_send_goes_to_the_next_step_exactly_when_reuse_did_not_use_it(
        session, reuse_turn, kind):
    """The rule's whole point (rv_6d6e MF1, rv_357d N1, rv_cc80 F1): one send
    is one step's receipt.  REUSE judged M at M's last verdict; if its receipt
    for M is that send, N must not get it (one greeting would finish both);
    if it is anything else, N gets it (else N is re-steered and its message
    goes out twice)."""
    lh.register_ledger_for_session(UP, _Ledger({1: GREET, 2: TEXT_2, 3: TEXT_3}))
    log, send_at, call_id, m_verdict = _greet_shape(kind)
    m_receipt = reuse_recipe._reuse_completion_evidence(
        UP, 1, _use(log[:m_verdict + 1]))
    m_used_the_send = isinstance(m_receipt, dict) and \
        m_receipt.get('message_index') == send_at
    _use(log)
    assert m_receipt is not None, (kind, 'REUSE must finish M one way or the other')
    assert _credited(log, index=send_at, call_id=call_id) is (not m_used_the_send)


def test_an_echo_of_the_send_leaves_the_send_to_the_next_step(session):
    """Decided (hartos-52 point 3): the 4B repeats the sent text as a plain
    message.  REUSE has no echo rule, so it finishes M on that message, not on
    the send; the send is N's, and refusing it would re-steer N and send the
    email twice.  (That REUSE accepts an echo of another step's text as M's
    work is its own question, outside this rule.)"""
    lh.register_ledger_for_session(UP, _Ledger({1: GREET, 2: TEXT_2, 3: TEXT_3}))
    log, send_at, call_id, _v = _greet_shape('echo')
    _use(log)
    assert _credited(log, index=send_at, call_id=call_id) is True


def test_text_after_the_earlier_steps_verdict_is_not_its_answer(session):
    """hartos-52 point 4: REUSE judged M at its verdict, on the send; text the
    Assistant writes after that verdict was never M's receipt, so it cannot
    hand M's send to N."""
    lh.register_ledger_for_session(UP, _Ledger({1: GREET, 2: TEXT_2, 3: TEXT_3}))
    log, send_at, call_id, _v = _greet_shape('late_text')
    _use(log)
    assert _credited(log, index=send_at, call_id=call_id) is False


def test_a_helpers_note_to_the_person_is_not_the_earlier_steps_answer(session):
    """REUSE's written answer is _reuse_message_is_user_answer AND
    is_written_answer: the Helper's '@user ...' line passes the first (its
    @user lets it through) but is not the Assistant's written work, so REUSE
    finishes M on the send and the send stays M's."""
    lh.register_ledger_for_session(UP, _Ledger({1: GREET, 2: TEXT_2, 3: TEXT_3}))
    log, send_at, call_id, _v = _greet_shape('helper_to_user')
    _use(log)
    assert _credited(log, index=send_at, call_id=call_id) is False


@pytest.mark.parametrize('kind', ['verdict_renamed', 'verdict_nameless'])
def test_the_earlier_steps_verdict_is_found_by_its_content(session, kind):
    """rv_ae7e F1: the #725 sync can hand M's verdict another seat's name, or
    none.  It is still M's verdict by its content ({"status": ...}, the
    reading the gate's _reuse_latest_verdict uses), so the N1 shape still
    leaves the send to N; refusing would re-send N's message."""
    lh.register_ledger_for_session(UP, _Ledger({1: GREET, 2: TEXT_2, 3: TEXT_3}))
    log, send_at, call_id, _v = _greet_shape(kind)
    _use(log)
    assert _credited(log, index=send_at, call_id=call_id) is True


def test_an_assistant_report_after_the_verdict_is_not_when_the_step_was_judged(session):
    """rv_4bc4 F1 (E3): the verifier judged M on its send; the Assistant then
    writes a line and its own {"status": ...}.  That report is not M's
    verdict (the seat's verdict is in the window and wins), so the late line
    is not M's answer and M keeps its send: one greeting must not finish
    both steps."""
    lh.register_ledger_for_session(UP, _Ledger({1: GREET, 2: TEXT_2, 3: TEXT_3}))
    log, send_at, call_id, _v = _greet_shape('assistant_status_after_verdict')
    _use(log)
    assert _credited(log, index=send_at, call_id=call_id) is False


def test_two_unseated_verdicts_leave_the_earlier_step_its_send(session):
    """hartos-52: M's verdict renamed by the #725 sync, then an Assistant
    {"status": ...} report: no seat verdict and two content matches that
    cannot be told apart.  The rule fails closed (M keeps the send) rather
    than take the newest: a wrong credit loses N's output silently, a wrong
    refusal at worst sends it twice.  One content match alone still credits
    (test_the_earlier_steps_verdict_is_found_by_its_content)."""
    lh.register_ledger_for_session(UP, _Ledger({1: GREET, 2: TEXT_2, 3: TEXT_3}))
    log, send_at, call_id, m_verdict = _greet_shape('verdict_renamed')
    log[m_verdict + 1:m_verdict + 1] = [
        {'role': 'assistant', 'name': 'Assistant',
         'content': 'Shall we write the email now?'},
        {'role': 'assistant', 'name': 'Assistant',
         'content': json.dumps({'status': 'completed'})}]
    _use(log)
    assert _credited(log, index=send_at, call_id=call_id) is False


def test_a_tools_status_is_never_the_earlier_steps_verdict(session):
    """hartos-52: tools answer {"status": "ok"} too.  After M's verdict, such
    a result must not become the cut point, or the late text before it would
    count as M's answer and hand M's send to N (REUSE judged M at its real
    verdict, on the send).  And with no verdict at all, a tool's status is
    not one: M keeps its send."""
    lh.register_ledger_for_session(UP, _Ledger({1: GREET, 2: TEXT_2, 3: TEXT_3}))
    log, send_at, call_id, _v = _greet_shape('tool_status_after_verdict')
    _use(log)
    assert _credited(log, index=send_at, call_id=call_id) is False
    log, send_at, call_id, m_verdict = _greet_shape('written_answer')
    del log[m_verdict]
    log[m_verdict:m_verdict] = [
        _call('c_s', SAVE, {'key': 'greeted', 'value': 'yes'}),
        _result('c_s', json.dumps({'status': 'ok'}))]
    _use(log)
    assert _credited(log, index=send_at, call_id=call_id) is False


def test_no_verdict_for_the_earlier_step_keeps_its_send(session):
    """hartos-52: with no verifier message in M's window (a seat name lost, a
    trimmed or resumed log), REUSE's judgment of M cannot be located, so the
    rule fails closed: M keeps the send, even beside a written answer, and
    late text can never hand it to N."""
    lh.register_ledger_for_session(UP, _Ledger({1: GREET, 2: TEXT_2, 3: TEXT_3}))
    log, send_at, call_id, m_verdict = _greet_shape('written_answer')
    del log[m_verdict]                        # M's verdict message is missing
    _use(log)
    assert _credited(log, index=send_at, call_id=call_id) is False


def test_the_next_steps_own_answer_is_not_the_earlier_steps(session):
    """hartos-52 point 1: the window ends at N's dispatch.  N's written answer
    after it must not count as M's (M wrote none: the send is M's)."""
    lh.register_ledger_for_session(UP, _Ledger({1: GREET, 2: TEXT_2, 3: TEXT_3}))
    log = [
        _dispatch(1, GREET),                                              # 0
        _call('c_g', SEND, {'text': GREETING, 'expects_reply': False}),   # 1
        _result('c_g', _sent()),                                          # 2
        _verdict(1),                                                      # 3
        _dispatch(2, TEXT_2),                                             # 4
        {'role': 'assistant', 'name': 'Assistant',
         'content': 'Here is the email draft you asked for, Priya. ' + DRAFT},  # 5
        _verdict(2),                                                      # 6
    ]
    _use(log)
    assert _credited(log, index=2, call_id='c_g') is False


def test_a_send_before_the_next_step_is_posted_stays_with_the_earlier_step(session):
    """hartos-52 point 2: N not dispatched yet, so there is no window end;
    the rule fails closed (no slice, no error) and M keeps its send, even with
    a written answer."""
    lh.register_ledger_for_session(UP, _Ledger({1: GREET, 2: TEXT_2, 3: TEXT_3}))
    log, send_at, call_id, m_verdict = _greet_shape('written_answer')
    log = log[:m_verdict + 1]                 # nothing after M's verdict yet
    _use(log)
    assert _credited(log, index=send_at, call_id=call_id) is False


def test_without_reuses_reader_a_no_tool_step_keeps_its_send(session):
    """A caller that passes no reader (anything but REUSE's three) credits a
    no-tool M nothing, even with a written answer: the parent's behaviour."""
    lh.register_ledger_for_session(UP, _Ledger({1: GREET, 2: TEXT_2, 3: TEXT_3}))
    log = [
        _dispatch(1, GREET),
        {'role': 'assistant', 'name': 'Assistant',
         'content': 'Hello Priya, it is lovely to meet you. Welcome aboard, '
                    'I will help you write your email today.'},
        _call('c_draft', SEND, {'text': DRAFT, 'expects_reply': False}),
        _result('c_draft', _sent()),
        _verdict(1),
        _dispatch(2, TEXT_2),
        _verdict(2),
    ]
    _use(log)
    body = log[3]['tool_responses'][0]['content']
    assert lh.credited_early_send(UP, log, 3, 'c_draft', body, 2, request_id=TURN,
                                  is_ask_step=lambda a: False) is False


def test_an_earlier_ask_step_keeps_its_send(session):
    log = _email_log()
    _use(log)
    assert _credited(log, is_ask_step=lambda a: a == 1) is False


def test_a_step_that_names_no_send_gets_no_send(session):
    lh.register_ledger_for_session(
        UP, _Ledger({1: TEXT_1, 2: 'save_data_in_memory: last_tip', 3: TEXT_3}))
    log = _email_log()
    _use(log)
    assert _credited(log) is False


def test_one_send_is_never_the_receipt_of_two_steps(session):
    log = _email_log() + [_dispatch(3, TEXT_3)]
    _use(log)
    assert _credited(log, action_id=2) is True
    assert _credited(log, action_id=3) is False


def test_the_step_before_is_by_dispatch_order_not_by_number(session):
    """Action 3 ran between 1 and 2 (held step / dependency order): the step
    before 2 is 3, which names no send; action 1 names one, so arithmetic
    (action_id - 1) would refuse."""
    lh.register_ledger_for_session(
        UP, _Ledger({1: 'send_message_to_user: greet', 2: TEXT_2,
                     3: 'save_data_in_memory: brief'}))
    log = [
        _dispatch(1, 'send_message_to_user: greet'),
        _dispatch(3, 'save_data_in_memory: brief'),
        _call('c_draft', SEND, {'text': DRAFT, 'expects_reply': False}),
        _result('c_draft', _sent()),
        _dispatch(2, TEXT_2),
    ]
    _use(log)
    assert _credited(log, index=3) is True


def test_a_resteered_earlier_step_still_owns_its_window(session):
    """A retry dispatch of the same earlier step between the send and N's
    dispatch keeps the send in that step's window."""
    log = [
        _dispatch(1, TEXT_1),
        _call('c_draft', SEND, {'text': DRAFT, 'expects_reply': False}),
        _result('c_draft', _sent()),
        {'role': 'user', 'name': 'ChatInstructor',
         'content': f'[retry: no receipt] Execute Action 1: {TEXT_1}'},
        _dispatch(2, TEXT_2),
    ]
    _use(log)
    assert _credited(log, index=2) is True


def test_a_send_with_another_step_between_is_not_credited(session):
    log = [
        _dispatch(1, TEXT_1),
        _call('c_draft', SEND, {'text': DRAFT, 'expects_reply': False}),
        _result('c_draft', _sent()),
        _dispatch(3, 'save_data_in_memory: brief'),
        _dispatch(2, TEXT_2),
    ]
    lh.register_ledger_for_session(
        UP, _Ledger({1: TEXT_1, 2: TEXT_2, 3: 'save_data_in_memory: brief'}))
    _use(log)
    assert _credited(log, index=2) is False


# ---- the three readers --------------------------------------------------------

@pytest.fixture
def reuse(session, monkeypatch):
    saved_task = reuse_recipe.user_tasks.get(UP)
    saved_rid = reuse_recipe.request_id_list.get(UP)
    reuse_recipe.request_id_list[UP] = TURN
    monkeypatch.setattr(reuse_recipe, '_reuse_ask_only_step', lambda up, a, gc: False)
    yield
    if saved_task is None:
        reuse_recipe.user_tasks.pop(UP, None)
    else:
        reuse_recipe.user_tasks[UP] = saved_task
    if saved_rid is None:
        reuse_recipe.request_id_list.pop(UP, None)
    else:
        reuse_recipe.request_id_list[UP] = saved_rid


def _reuse_task(log, upto=6):
    seen = {c['id'] for m in log[:upto] for c in (m.get('tool_calls') or [])}
    reuse_recipe.user_tasks[UP] = _ReuseTask([TEXT_1, TEXT_2, TEXT_3], seen)


def test_the_gate_credits_the_early_draft(reuse):
    log = _email_log()
    gc = _use(log)
    _reuse_task(log)
    assert reuse_recipe._reuse_fabricated_tools(UP, 2, gc, gc.agents) == []


def test_the_gate_still_holds_a_step_whose_send_never_happened(reuse):
    log = [_dispatch(1, TEXT_1), _call('c_save', SAVE, {'key': 'k', 'value': 'v'}),
           _result('c_save', 'Saved'), _verdict(1), _dispatch(2, TEXT_2), _verdict(2)]
    gc = _use(log)
    _reuse_task(log, upto=4)
    assert reuse_recipe._reuse_fabricated_tools(UP, 2, gc, gc.agents) == [SEND]


def test_the_receipt_finder_cites_the_early_draft(reuse):
    log = _email_log()
    gc = _use(log)
    _reuse_task(log)
    assert reuse_recipe._reuse_completion_evidence(UP, 2, gc) == {
        'message_index': 4, 'kind': 'tool_receipt'}


def test_the_commit_check_accepts_the_credited_receipt_only_with_the_turn(session):
    log = _email_log()
    _use(log)
    verdict = {'status': 'completed', 'action_id': 2,
               'evidence': {'message_index': 4, 'kind': 'tool_receipt'}}
    assert lh._verifier_completion_has_conversation_evidence(
        UP, 2, verdict, request_id=TURN, is_ask_step=lambda a: False) is True
    assert lh._verifier_completion_has_conversation_evidence(UP, 2, verdict) is False


def test_every_other_early_result_stays_out_of_the_window(reuse):
    """Only the credited send moves: action 1's save result is not action 2's,
    even for an action 2 that names the save."""
    lh.register_ledger_for_session(
        UP, _Ledger({1: TEXT_1, 2: 'save_data_in_memory: last_tip', 3: TEXT_3}))
    log = _email_log()
    log[6] = _dispatch(2, 'save_data_in_memory: last_tip')
    gc = _use(log)
    reuse_recipe.user_tasks[UP] = _ReuseTask(
        [TEXT_1, 'save_data_in_memory: last_tip', TEXT_3], {'c_save', 'c_draft'})
    assert reuse_recipe._reuse_fabricated_tools(UP, 2, gc, gc.agents) == [SAVE]


# ---- R15's three surviving mutants, each pinned ------------------------------

def test_a_turn_without_an_id_credits_nothing_even_when_its_receipt_says_none(session):
    """CREATE passes no request id. A send made while the turn had none reads
    'with request_id: None-intermediate', which _receipt_request strips to
    'None' == str(None): only the no-turn guard keeps it out (R15 mutant M01)."""
    log = _email_log(request_id='None-intermediate')
    _use(log)
    assert _credited(log, request_id=None) is False


def test_the_receipt_finder_takes_no_uncredited_early_result(reuse):
    """With no evidence watermark stamped, an earlier step's result that is NOT
    a credited send (here action 1's save, for an action 2 that names the save)
    is never cited by the finder's look back (R15 mutant M11)."""
    lh.register_ledger_for_session(
        UP, _Ledger({1: TEXT_1, 2: 'save_data_in_memory: last_tip', 3: TEXT_3}))
    log = _email_log()
    log[6] = _dispatch(2, 'save_data_in_memory: last_tip')
    gc = _use(log)
    reuse_recipe.user_tasks[UP] = _ReuseTask(
        [TEXT_1, 'save_data_in_memory: last_tip', TEXT_3], set())
    assert reuse_recipe._reuse_completion_evidence(UP, 2, gc) is None


def test_the_receipt_finder_reaches_past_two_resteers_of_the_step_before(reuse):
    """The draft went out in action 1's first attempt; action 1 was then
    re-steered twice before action 2 was dispatched. The look back walks
    through every re-steer of that one step (R15 mutant M12)."""
    retry = {'role': 'user', 'name': 'ChatInstructor',
             'content': f'[retry: no receipt] Execute Action 1: {TEXT_1}'}
    log = [
        _dispatch(1, TEXT_1),                                               # 0
        _call('c_draft', SEND, {'text': DRAFT, 'expects_reply': False}),    # 1
        _result('c_draft', _sent()),                                        # 2
        retry,                                                              # 3
        dict(retry),                                                        # 4
        _dispatch(2, TEXT_2),                                               # 5
        _verdict(2),                                                        # 6
    ]
    gc = _use(log)
    _reuse_task(log, upto=5)
    assert reuse_recipe._reuse_completion_evidence(UP, 2, gc) == {
        'message_index': 2, 'kind': 'tool_receipt'}
