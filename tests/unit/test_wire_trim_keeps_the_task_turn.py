"""The wire trim must not drop the user's own input in favour of an agent's.

Measured live 2026-09-25 (installed build, source autogen.reuse, request
livetest_reuse_verify_1790351226, llm_outbound.jsonl.old -- the logged body is
the POST-trim wire body):

    21:17:17  Assistant seat, call 1:  [system, user/User "Summarize: The
              Quibble river ferry carries 83 cyclists ..."]
    21:17:36  Assistant seat, call 2:  [system, assistant, tool,
              user/StatusVerifier '{"status": "pending", ...}']

In an autogen group chat every OTHER agent's message reaches a seat as
role='user', so the StatusVerifier verdict became the newest user message --
the only one ``_trim_to_budget`` protected -- and the user's text, the oldest
droppable message, went first.  The Assistant then asked the user for the text
again and the turn ended with no summary.

The rule these tests pin: the speaker who opened the conversation's user side
(the initiator whose task it is) keeps its newest turn, in addition to the
newest user turn the Qwen template needs.  Behavioural -- they call the real
``_trim_to_budget`` with the budget pinned; no source inspection.
"""
import core.llm_outbound_logger as lol
from core.constants import WIRE_TRIM_SAFETY_MARGIN_TOKENS, WIRE_TRIM_MARKER
from core.token_utils import count_tokens_for_messages

_MAX_TOKENS = 64
_TASK = ('Summarize: The Quibble river ferry carries 83 cyclists daily across '
         'Marrowby. It was built in 1934 from teal steel. Its captain is Pim.')
_VERDICT = ('{"status": "pending", "action": "Receive the input text from the '
            'user.", "action_id": 1}')


def _sys():
    return {'role': 'system', 'content': 'You are the reuse assistant. ' * 20}


def _task(content=_TASK):
    return {'role': 'user', 'name': 'User', 'content': content}


def _verdict():
    return {'role': 'user', 'name': 'StatusVerifier', 'content': _VERDICT}


def _bulk(role, tag):
    m = {'role': role, 'content': ('%s reply ' % tag) + 'x ' * 600}
    if role == 'tool':
        m['tool_call_id'] = 'c_' + tag
    return m


def _trim_so_that_only(keep, messages, monkeypatch):
    """Pin the budget so ``keep`` fits and ``messages`` does not."""
    per_slot = (count_tokens_for_messages(keep, None) + _MAX_TOKENS
                + WIRE_TRIM_SAFETY_MARGIN_TOKENS + 20)
    monkeypatch.setattr(lol, '_get_budget_per_slot', lambda: per_slot)
    body = {'model': 'llama', 'messages': messages, 'max_tokens': _MAX_TOKENS}
    out, n_dropped, _, _, est_after, budget = lol._trim_to_budget(body)
    assert n_dropped > 0, 'fixture failed to force any trimming'
    return out['messages'], est_after, budget


def test_the_live_shape_keeps_the_users_text(monkeypatch):
    """[system, User task, assistant, tool, StatusVerifier verdict]."""
    sys_m, task, verdict = _sys(), _task(), _verdict()
    result = _bulk('tool', 't1')
    msgs = [sys_m, task, _bulk('assistant', 'a1'), result, verdict]
    # The newest tool result is protected too (owner decision 2026-09-26,
    # test_wire_trim_protects_the_newest_tool_result.py), so it is kept.
    out, est_after, budget = _trim_so_that_only(
        [sys_m, task, result, verdict], msgs, monkeypatch)
    contents = [m.get('content') for m in out]
    assert _TASK in contents, (
        "the user's input was dropped while the StatusVerifier verdict was "
        'kept (post-trim roles/names: %r)'
        % [(m.get('role'), m.get('name')) for m in out])
    assert _VERDICT in contents, 'the newest user turn must still be kept'
    assert est_after <= budget
    # Everything else was droppable and was dropped.
    assert [m.get('role') for m in out] == ['system', 'user', 'tool', 'user']


def test_the_initiators_NEWEST_turn_is_kept_not_its_first(monkeypatch):
    """A carried-over history: the old task is stale, the new one is the
    input.  Protecting "the first user message" would keep the wrong one."""
    sys_m, verdict = _sys(), _verdict()
    old = _task('Summarize: an older text about a bridge. ' + 'y ' * 400)
    new = _task()
    result = _bulk('tool', 't1')
    msgs = [sys_m, old, _bulk('assistant', 'a0'), new,
            _bulk('assistant', 'a1'), result, verdict]
    out, est_after, budget = _trim_so_that_only(
        [sys_m, new, result, verdict], msgs, monkeypatch)
    contents = [m.get('content') for m in out]
    assert _TASK in contents, 'the current user input was dropped'
    assert old['content'] not in contents, (
        'the stale earlier task was kept instead of being dropped first')
    assert est_after <= budget


def test_an_opener_that_is_an_assistant_turn_still_finds_the_user(monkeypatch):
    """The 21:20:16 live shape: the seat's own previous reply opens the
    history, the user's new message follows, then the verdict."""
    sys_m, task, verdict = _sys(), _task(), _verdict()
    msgs = [sys_m, _bulk('assistant', 'a0'), task,
            _bulk('assistant', 'a1'), _bulk('tool', 't1'), verdict]
    out, _, _ = _trim_so_that_only([sys_m, task, verdict], msgs, monkeypatch)
    assert _TASK in [m.get('content') for m in out]


def test_unnamed_bodies_are_unchanged(monkeypatch):
    """No speaker names (langchain / raw SDK): only the newest user turn is
    protected, exactly as before -- older user turns stay droppable."""
    sys_m = _sys()
    old_user = {'role': 'user', 'content': 'old question ' + 'q ' * 400}
    newest = {'role': 'user', 'content': 'the current question'}
    msgs = [sys_m, old_user, _bulk('assistant', 'a1'), newest]
    out, _, _ = _trim_so_that_only([sys_m, newest], msgs, monkeypatch)
    assert out == [sys_m, newest]


def _two_large_protected(task_words, anchor_words):
    """[system, user/User task, assistant, user/Assistant result, assistant]:
    the StatusVerifier seat's shape, where the newest user turn is the
    Assistant's result (the anchor) and the task is the user's input."""
    sys_m = _sys()
    task = _task('TASKHEAD ' + 'task ' * task_words + ' TASKTAIL')
    anchor = {'role': 'user', 'name': 'Assistant',
              'content': 'RESULTHEAD ' + 'result ' * anchor_words
                         + ' RESULTTAIL'}
    msgs = [sys_m, task, _bulk('assistant', 'a1'), anchor,
            {'role': 'assistant', 'content': 'ok'}]
    return sys_m, task, anchor, msgs


def test_only_the_larger_protected_message_is_cut_when_that_suffices(
        monkeypatch):
    """Review of bac8f91c4, measured with a probe: the anchor was truncated
    FIRST, its room computed with the big task still at full size, so it fell
    to the 64-token floor -- then the task was cut anyway and budget was left
    unused.  The verifier lost the head of the result it had to check.  When
    cutting only the larger message fits, the smaller must stay whole."""
    sys_m, task, anchor, msgs = _two_large_protected(3000, 400)
    # Room for the whole anchor plus a slice of the task, not the whole task.
    keep = [sys_m, _task('task ' * 200), anchor,
            {'role': 'assistant', 'content': 'ok'}]
    out, est_after, budget = _trim_so_that_only(keep, msgs, monkeypatch)
    kept_anchor = [m for m in out if m.get('name') == 'Assistant']
    kept_task = [m for m in out if m.get('name') == 'User']
    assert kept_anchor and kept_anchor[0]['content'] == anchor['content'], (
        'the result the verifier must check was cut although cutting only '
        'the larger task turn fits')
    assert kept_task and WIRE_TRIM_MARKER in kept_task[0]['content']
    assert est_after <= budget


def test_the_mirror_case_keeps_the_smaller_task_whole(monkeypatch):
    """Same rule the other way round, so no iteration order can pass both."""
    sys_m, task, anchor, msgs = _two_large_protected(400, 3000)
    keep = [sys_m, task, {'role': 'user', 'name': 'Assistant',
                          'content': 'result ' * 200},
            {'role': 'assistant', 'content': 'ok'}]
    out, est_after, budget = _trim_so_that_only(keep, msgs, monkeypatch)
    kept_task = [m for m in out if m.get('name') == 'User']
    kept_anchor = [m for m in out if m.get('name') == 'Assistant']
    assert kept_task and kept_task[0]['content'] == task['content']
    assert kept_anchor and WIRE_TRIM_MARKER in kept_anchor[0]['content']
    assert est_after <= budget


def test_an_oversized_task_turn_is_truncated_not_left_over_budget(monkeypatch):
    """Protecting the task must not make the trim unable to fit: a task that
    alone exceeds the budget is cut, with the marker, as the
    newest-user anchor already is."""
    sys_m, verdict = _sys(), _verdict()
    huge = _task('HEAD ' + 'word ' * 3000 + ' TAIL 83 cyclists')
    result = _bulk('tool', 't1')
    msgs = [sys_m, huge, _bulk('assistant', 'a1'), result, verdict]
    out, est_after, budget = _trim_so_that_only(
        [sys_m, _task(), result, verdict], msgs, monkeypatch)
    kept = [m for m in out if m.get('name') == 'User']
    assert len(kept) == 1, 'the task turn must survive, truncated'
    assert WIRE_TRIM_MARKER in kept[0]['content']
    assert kept[0]['content'].endswith('TAIL 83 cyclists')
    assert _VERDICT in [m.get('content') for m in out]
    assert est_after <= budget


def test_an_anchor_that_is_the_newest_message_is_kept_whole(monkeypatch):
    """Review of 520c95e28, probed: in the StatusVerifier seat the anchor
    (the Assistant's result) is messages[-1].  It was cut first, in a
    separate step sized against the still-full task, and floored while the
    task was cut anyway.  The newest message is now in the largest-first
    pass: cutting only the larger task must leave the result whole."""
    sys_m = _sys()
    task = _task('TASKHEAD ' + 'task ' * 3000 + ' TASKTAIL')
    anchor = {'role': 'user', 'name': 'Assistant',
              'content': 'RESULTHEAD ' + 'result ' * 400 + ' RESULTTAIL'}
    msgs = [sys_m, task, _bulk('assistant', 'a1'), anchor]
    keep = [sys_m, _task('task ' * 200), anchor]
    out, est_after, budget = _trim_so_that_only(keep, msgs, monkeypatch)
    kept_anchor = [m for m in out if m.get('name') == 'Assistant']
    kept_task = [m for m in out if m.get('name') == 'User']
    assert kept_anchor and kept_anchor[0]['content'] == anchor['content'], (
        'the newest message was cut although cutting the larger task fits')
    assert kept_task and WIRE_TRIM_MARKER in kept_task[0]['content']
    assert est_after <= budget


def test_an_unprotected_oversized_newest_message_is_still_truncated(
        monkeypatch):
    """The step the pass replaced existed for this: a newest message that is
    alone too big (an assistant reply, protected by nothing) is cut, not
    left over budget."""
    sys_m = _sys()
    newest = {'role': 'assistant',
              'content': 'HEAD ' + 'word ' * 3000 + ' TAIL'}
    msgs = [sys_m, _bulk('assistant', 'a0'), {'role': 'user', 'content': 'q'},
            newest]
    out, est_after, budget = _trim_so_that_only(
        [sys_m, {'role': 'user', 'content': 'q'},
         {'role': 'assistant', 'content': 'word ' * 100}], msgs, monkeypatch)
    assert WIRE_TRIM_MARKER in out[-1]['content']
    assert out[-1]['content'].endswith('TAIL')
    assert est_after <= budget
