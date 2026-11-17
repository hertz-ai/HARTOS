"""Trimming keeps the order for the step the pipeline is on.

Live 2026-11-01 on the desktop, goal 'TTS Engine Venv Provisioning'
(daemon_e0f826dc, session ..._18895904180).  02:41:19 step 6 completed;
02:41:20 step 7's order went into the chat as ChatInstructor's
"Perform this action -> Action #7: ...", the last of 22 messages.  It
reached the model for 45 seconds: from 02:42:14 every input left it out
([PROTECTED-KEPT] put back User's message and nothing else), while the
opener -- User's "Perform this action -> Action #6: ..." -- stayed.  The 743
calls of 08:31-10:13 (llm_outbound.jsonl) carried no step 7 at all; the
Assistant kept working step 6 and the StatusVerifier, shown only step 6,
answered action_id 6 twenty-nine times while the pipeline held step 7.

Why: the seats' token limiter keeps the newest 2,500 tokens, and the one
protected set (core.llm_outbound_logger.protected_messages) kept the newest
user turn, the newest turn of the opener (User) and the newest tool result.
Step 7's order came from ChatInstructor, so it stayed protected only until
any later turn arrived.  The opener's order is kept too: it is the only
message holding the person's words (later orders carry none,
reuse_recipe._build_reuse_action_message).

    python -m pytest tests/unit/test_limiters_keep_the_current_order.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.constants import (  # noqa: E402
    ACTION_STEPS_SEPARATOR, AUTOGEN_MESSAGE_TOKEN_BUDGET,
    AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE)
from core.llm_outbound_logger import protected_messages  # noqa: E402
from hartos.helper import history_limiter, token_limiter  # noqa: E402

_TOKENS = dict(max_tokens=AUTOGEN_MESSAGE_TOKEN_BUDGET,
               max_tokens_per_message=AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE,
               min_tokens=0)
_STEP_6 = ('Perform this action -> Action #6:Identify and isolate unhealthy '
           'TTS backend engines')
_STEP_7 = ('Perform this action -> Action #7:Identify and repair unhealthy '
           'TTS backend engines')
_GOAL = 'GOAL: TTS Engine Venv Provisioning, paced, one at a time'


def _words(n, word='alpha'):
    return ' '.join([word] * n)


def _msg(role, name, content, **extra):
    m = {'role': role, 'name': name, 'content': content}
    m.update(extra)
    return m


def _call(call_id, name='execute_windows_or_android_command'):
    return _msg('assistant', 'Assistant', '', tool_calls=[{
        'id': call_id, 'type': 'function',
        'function': {'name': name, 'arguments': '{}'}}])


def _order(head, words=None):
    """An order as _build_reuse_action_message writes it."""
    text = head + (f'\n\n{words}' if words else '')
    return f"{text}{ACTION_STEPS_SEPARATOR}[{{'Import the modules': {{}}}}]"


def _walk_past_step_6():
    """The chat a seat holds once step 7 is on: the step-6 opener and its
    work, step 7's order, then more than the token budget of later turns."""
    return [
        _msg('user', 'User', _order(_STEP_6, _GOAL)),
        _call('c1'),
        _msg('tool', 'Assistant', _words(600), tool_call_id='c1'),
        _msg('user', 'StatusVerifier',
             '{"status": "completed", "action_id": 6, "message": "done"}'),
        _msg('user', 'ChatInstructor', _order(_STEP_7)),
        _msg('user', 'StatusVerifier',
             '{"status": "requires_breakdown", "action_id": 7, "reason": "'
             + _words(900, 'beta') + '"}'),
        _msg('user', 'Helper', _words(900, 'gamma')),
        _call('c2'),
        _msg('tool', 'Assistant', _words(900, 'delta'), tool_call_id='c2'),
        _msg('user', 'ChatInstructor', 'Work on subtask: read tts_router.py'),
    ]


def _orders_in(msgs, head):
    return [m for m in msgs
            if str(m.get('content') or '').startswith(head)]


def test_the_token_limiter_keeps_the_order_of_the_step_on_the_floor():
    """THE LIVE FAILURE: step 7's order was outside every window."""
    chat = _walk_past_step_6()
    kept = token_limiter(**_TOKENS).apply_transform(chat)
    assert _orders_in(kept, _STEP_7), (
        "the seat is asked to work with no order for step 7; it holds only "
        "step 6's")


def test_the_person_s_words_still_reach_the_seat():
    """The opener's order is the only message with the person's words."""
    chat = _walk_past_step_6()
    kept = token_limiter(**_TOKENS).apply_transform(chat)
    assert any(_GOAL in str(m.get('content') or '') for m in kept)


def test_the_order_keeps_its_place_before_the_later_turns():
    """Put back where it was, so the seat reads it as the newest order and
    the work after it as that step's."""
    chat = _walk_past_step_6()
    kept = token_limiter(**_TOKENS).apply_transform(chat)
    names = [m.get('name') for m in kept]
    orders = _orders_in(kept, _STEP_7)
    assert orders, "step 7's order is not in the window at all"
    at_7 = kept.index(orders[0])
    assert at_7 < len(kept) - 1
    assert names[-1] == 'ChatInstructor'
    assert str(kept[-1]['content']).startswith('Work on subtask')


def test_the_history_limiter_keeps_it_too():
    chat = _walk_past_step_6()
    kept = history_limiter(max_messages=4).apply_transform(chat)
    assert _orders_in(kept, _STEP_7)


def test_a_create_order_is_an_order():
    """CREATE posts "Execute Action N:" from ChatInstructor after the
    person's own words (lifecycle_hooks.dispatch_action_id reads both)."""
    chat = [
        _msg('user', 'User', 'Plan my week around three gym sessions'),
        _msg('user', 'ChatInstructor', 'Execute Action 1: list the free slots'),
        _msg('user', 'StatusVerifier', '{"status": "completed", "action_id": 1}'),
        _msg('user', 'ChatInstructor', 'Execute Action 2: book the sessions'),
        _msg('user', 'StatusVerifier', '{"status": "pending", "action_id": 2}'),
    ]
    got = protected_messages(chat)
    assert chat[3] in got
    assert chat[1] not in got
    assert chat[0] in got


def test_a_quoted_order_is_not_an_order():
    """Only a LEADING marker dispatches (lifecycle_hooks._DISPATCH_MARKER):
    a turn that quotes an older order does not take the current one's place."""
    chat = _walk_past_step_6()[:5] + [
        _msg('user', 'Helper', 'Earlier you said: ' + _order(_STEP_6))]
    got = protected_messages(chat)
    assert chat[4] in got


def test_an_order_from_an_earlier_run_is_not_this_runs_order():
    """A turn seeded from shared history (_from_shared) belongs to an earlier
    run; lifecycle_hooks.latest_dispatch_before skips it the same way."""
    chat = [
        _msg('user', 'User', _order(_STEP_6, _GOAL), _from_shared=True),
        _msg('user', 'User', 'Why are my voices silent?'),
        _msg('user', 'StatusVerifier', '{"status": "pending"}'),
    ]
    assert chat[0] not in protected_messages(chat)


def test_the_model_repeating_an_order_does_not_replace_it():
    """Orders reach a seat as role='user' turns; an Assistant reply that opens
    by repeating an older order is the model's text, not an order."""
    chat = _walk_past_step_6()[:5] + [
        _msg('assistant', 'Assistant', _order(_STEP_6) + ' Starting now.'),
        _msg('user', 'StatusVerifier', '{"status": "pending", "action_id": 7}')]
    assert chat[4] in protected_messages(chat)


# ── when the kept set alone is over the budget ──────────────────────────
# Review of 2f407f3de (claude-autostart-71): the opener (the live TTS goal
# text is ~577 words), the current order, the newest tool result and the
# newest user turn can together exceed the window.  Then the cuts decide,
# and the current order must keep its leading marker.

def _oversized_walk():
    steps = '[' + ', '.join("{'step 7.%d': {'tool_name': '', 'code': ''}}" % i
                            for i in range(120)) + ']'
    return [
        {'role': 'system', 'content': 'You are a helpful assistant. ' * 40},
        _msg('user', 'User', _order(_STEP_6, _GOAL + ' ' + _words(577, 'goal'))),
        _call('c1'),
        _msg('tool', 'Assistant', _words(1500, 'delta'), tool_call_id='c1'),
        _msg('user', 'ChatInstructor', _STEP_7 + ACTION_STEPS_SEPARATOR + steps),
        # Enough after the order that autogen's own window ends before it (the
        # message that crosses the budget is cut to fit, not dropped, so it
        # takes three full ones), and only the protected set brings it back
        # (review of 86dc21b59: with the order second-newest the limiter test
        # could not fail).
        _msg('user', 'Helper', _words(1500, 'gamma')),
        _msg('user', 'Helper', _words(1500, 'epsilon')),
        _msg('user', 'StatusVerifier',
             '{"status": "pending", "action_id": 7, "message": "'
             + _words(1500, 'beta') + '"}'),
    ]


def test_the_limiter_keeps_the_order_s_marker_when_it_must_cut_it():
    from autogen.agentchat.contrib.capabilities import transforms as ag
    walk = _oversized_walk()
    assert not [m for m in ag.MessageTokenLimiter(**_TOKENS).apply_transform(walk)
                if m.get('name') == 'ChatInstructor'], (
        "fixture: autogen's own window already holds the order")
    kept = token_limiter(**_TOKENS).apply_transform(walk)
    orders = [m for m in kept if m.get('name') == 'ChatInstructor']
    assert orders, "step 7's order is not in the window"
    assert str(orders[0]['content']).startswith(_STEP_7 + ACTION_STEPS_SEPARATOR)


def test_the_wire_trim_keeps_the_order_s_marker_when_it_must_cut_it(monkeypatch):
    import core.llm_outbound_logger as lol
    from core.constants import WIRE_TRIM_SAFETY_MARGIN_TOKENS
    max_tokens = 64
    monkeypatch.setattr(lol, '_get_budget_per_slot',
                        lambda: 900 + max_tokens + WIRE_TRIM_SAFETY_MARGIN_TOKENS)
    out, _, n_cut, _, _, _ = lol._trim_to_budget(
        {'model': 'llama', 'messages': _oversized_walk(),
         'max_tokens': max_tokens})
    assert n_cut > 0, 'fixture failed to force a cut'
    text = {m.get('name'): str(m.get('content') or '') for m in out['messages']}
    assert text.get('ChatInstructor', '').startswith(
        _STEP_7 + ACTION_STEPS_SEPARATOR), text.get('ChatInstructor', '')[:200]
    assert text.get('User', '').startswith(_STEP_6), "the opener's head was cut"
    assert _GOAL in text.get('User', ''), "the person's words were cut"


def test_without_hartos_the_set_is_what_it_was(monkeypatch):
    """core can run where hartos.lifecycle_hooks cannot be imported (review of
    2f407f3de, finding 3): the trim then protects what it did before and does
    not raise on every call."""
    import sys
    monkeypatch.setitem(sys.modules, 'hartos.lifecycle_hooks', None)
    chat = _walk_past_step_6()
    got = protected_messages(chat)
    assert chat[0] in got, 'the task turn is no longer protected'
    assert not _orders_in(got, _STEP_7)


def test_an_older_hartos_is_said_once_as_an_error(monkeypatch, caplog):
    """A core file hot-swapped over an install whose lifecycle_hooks has no
    latest_dispatch (claude-autostart-71, 2026-11-01: the installed build
    has dispatch_action_id and latest_dispatch_before only).  The trim keeps
    working, and the lost protection is an ERROR the first time, not a quiet
    warning that would make a live proof read as "the fix does not work"."""
    import logging
    import sys
    import types
    import core.llm_outbound_logger as lol
    older = types.ModuleType('hartos.lifecycle_hooks')
    older.dispatch_action_id = lambda content: None
    monkeypatch.setitem(sys.modules, 'hartos.lifecycle_hooks', older)
    monkeypatch.setattr(lol, '_order_walk_missing_logged', False)
    from core.constants import WIRE_TRIM_SAFETY_MARGIN_TOKENS
    monkeypatch.setattr(lol, '_get_budget_per_slot',
                        lambda: 900 + 64 + WIRE_TRIM_SAFETY_MARGIN_TOKENS)
    chat = _walk_past_step_6()
    with caplog.at_level(logging.ERROR, logger='llm_outbound'):
        got = protected_messages(chat)
        protected_messages(chat)
        # and the wire trim, which reads the set, still trims
        out = lol._trim_to_budget({'model': 'llama', 'messages': _oversized_walk(),
                                   'max_tokens': 64})[0]
    assert chat[0] in got, 'the task turn is no longer protected'
    assert not _orders_in(got, _STEP_7)
    assert out['messages'], 'the wire trim returned no messages'
    errors = [r for r in caplog.records
              if r.levelno == logging.ERROR and 'latest_dispatch' in r.getMessage()]
    assert len(errors) == 1, [r.getMessage() for r in caplog.records]


# ── an order merged behind a verdict, on the wire ───────────────────────
# Live 2026-11-01 13:45-13:48, agent 54 R11 (fix-all's rid
# msg_1793520815330_r11p3, on 2f407f3de): the ROLE-ORDER-GUARD (hartos.helper
# ToolMessageHandler) joined StatusVerifier's verdict and ChatInstructor's
# step-2 order into one user turn with a blank line, so the order no longer
# led its turn.  The wire trim kept the step-1 order and dropped the joined
# turn; action 2 re-ran its tools 12 times and gave up (task #270 F2).

_STEP_2 = "Perform this action -> Action #2:Read the learner's book page"


def _merged_walk():
    return [
        {'role': 'system', 'content': 'You are a tutor. ' * 40},
        _msg('user', 'User', _order('Perform this action -> Action #1:Load the '
                                    "learner's progress", "Let's continue my lesson.")),
        _call('c1', 'get_data_by_key'),
        _msg('tool', 'Assistant', _words(400, 'progress'), tool_call_id='c1'),
        _msg('user', 'StatusVerifier',
             '{"status": "completed", "action_id": 1}\n\n' + _order(_STEP_2)),
        _call('c2', 'read_book_page'),
        _msg('tool', 'Assistant', _words(1500, 'page'), tool_call_id='c2'),
        _msg('user', 'StatusVerifier', '{"status": "pending", "action_id": 2, '
             '"message": "' + _words(600, 'beta') + '"}'),
    ]


def test_an_order_merged_behind_a_verdict_is_the_current_order():
    walk = _merged_walk()
    assert walk[4] in protected_messages(walk)


def test_an_order_merged_behind_a_verdict_survives_the_wire_trim(monkeypatch):
    import core.llm_outbound_logger as lol
    from core.constants import WIRE_TRIM_SAFETY_MARGIN_TOKENS
    monkeypatch.setattr(lol, '_get_budget_per_slot',
                        lambda: 900 + 64 + WIRE_TRIM_SAFETY_MARGIN_TOKENS)
    out, _, n_cut, _, _, _ = lol._trim_to_budget(
        {'model': 'llama', 'messages': _merged_walk(), 'max_tokens': 64})
    assert n_cut > 0, 'fixture failed to force a cut'
    texts = [str(m.get('content') or '') for m in out['messages']
             if m.get('role') == 'user']
    assert any(_STEP_2 in t for t in texts), (
        "the wire trim dropped step 2's order: %r" % [t[:80] for t in texts])


def test_a_hartos_without_joined_still_protects_a_leading_order(monkeypatch, caplog):
    """Review of d7dee5cc5 (finding 1): core over a lifecycle_hooks whose
    latest_dispatch predates ``joined`` raised TypeError on every
    over-budget trim.  Now the leading order stays protected, the joined
    one is not, and that is one ERROR, not a crash."""
    import logging
    import sys
    import types
    import core.llm_outbound_logger as lol
    from hartos.lifecycle_hooks import dispatch_action_id

    def latest_dispatch(messages, before=None, user_turns_only=False):
        for m in reversed(messages[:before]):
            if (isinstance(m, dict) and not m.get('_from_shared')
                    and (not user_turns_only or m.get('role') == 'user')
                    and dispatch_action_id(m.get('content')) is not None):
                return m
        return None

    older = types.ModuleType('hartos.lifecycle_hooks')
    older.latest_dispatch = latest_dispatch
    monkeypatch.setitem(sys.modules, 'hartos.lifecycle_hooks', older)
    monkeypatch.setattr(lol, '_order_join_missing_logged', False, raising=False)
    chat = _walk_past_step_6()
    walk = _merged_walk()
    with caplog.at_level(logging.ERROR, logger='llm_outbound'):
        got = protected_messages(chat)
        joined = protected_messages(walk)
    assert chat[4] in got, 'the leading step-7 order lost its protection'
    assert walk[4] not in joined
    errors = [r for r in caplog.records
              if r.levelno == logging.ERROR and "'joined'" in r.getMessage()]
    assert len(errors) == 1, [r.getMessage() for r in caplog.records]
