"""The seats' context limiters keep the user's task turn.

Live 2026-09-27, installed build (HARTOS 62649bc1), REUSE probe
liveprobe_reuse_1 (prompt 10456131443): the user's words reached 4 of 77 LLM
calls.  In frozen_debug.log, 99 of the 113 ToolMessageHandler inputs between
16:58 and 17:06 -- the messages AFTER the seats' limiters, BEFORE the wire --
held no message from User, from 16:59:45 on.  The wire trim cannot keep what
it is not given.

The cause, measured below on autogen's own class: MessageTokenLimiter keeps
the newest messages until AUTOGEN_MESSAGE_TOKEN_BUDGET (2500) and discards
every older one, and the task turn is the oldest message of the seat's
buffer.  (The history limiter was not it: 17-43 messages against a limit of
50.)  Once a REUSE turn has ~3,000 tokens of verdicts and instructions after
the task, every seat loses it.

The rule pinned here: hartos.helper's limiters put back every message of
``core.llm_outbound_logger.protected_messages`` -- the same set the wire trim
never drops -- in its place, the token limiter bounding it to the
per-message cap like any other message.  Everything else is autogen's
window, unchanged.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from autogen.agentchat.contrib.capabilities import transforms as ag  # noqa: E402

from core.constants import (  # noqa: E402
    AUTOGEN_HISTORY_LIMIT, AUTOGEN_MESSAGE_TOKEN_BUDGET,
    AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE)
from hartos.helper import history_limiter, token_limiter  # noqa: E402

_TOKENS = dict(max_tokens=AUTOGEN_MESSAGE_TOKEN_BUDGET,
               max_tokens_per_message=AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE,
               min_tokens=0)
_WORDS = 'What is the current self-build status?'
_TASK = ('Perform this action -> Action #1:get_self_build_status\n\n' + _WORDS
         + '\n follow these steps: ' + ', '.join(
             "{'get_data_by_key({\"key\":\"os.builds.k%d\"})': "
             "{'tool_name': 'get_data_by_key', 'code': ''}}" % i
             for i in range(20)))


def _prose(tag, n_chars):
    text = ('%s: the verifier checked the step and found it pending. ' % tag)
    return (text * (n_chars // len(text) + 1))[:n_chars]


def _live_seat_buffer():
    """The StatusVerifier seat's buffer, message sizes (chars) as logged at
    16:59:45: the task, then verdicts and instructions."""
    sizes = [('assistant', 'StatusVerifier', 2937), ('user', 'ChatInstructor', 1163),
             ('assistant', 'StatusVerifier', 392), ('user', 'Helper', 112),
             ('assistant', 'StatusVerifier', 274), ('user', 'ChatInstructor', 783),
             ('assistant', 'StatusVerifier', 2523), ('user', 'ChatInstructor', 2669),
             ('assistant', 'StatusVerifier', 536), ('user', 'Helper', 1906)]
    return ([{'role': 'user', 'name': 'User', 'content': _TASK}]
            + [{'role': r, 'name': n, 'content': _prose(n, c)} for r, n, c in sizes])


def test_autogens_token_limiter_drops_the_task_turn():
    """The measured cause.  If this fails, the fixture no longer reproduces
    the live buffer (or autogen changed)."""
    out = ag.MessageTokenLimiter(**_TOKENS).apply_transform(_live_seat_buffer())
    assert not any(m.get('name') == 'User' for m in out)


def test_the_token_limiter_keeps_the_task_turn_with_the_words():
    msgs = _live_seat_buffer()
    out = token_limiter(**_TOKENS).apply_transform(msgs)
    users = [m for m in out if m.get('name') == 'User']
    assert len(users) == 1, [m.get('name') for m in out]
    assert users[0]['content'].startswith(
        'Perform this action -> Action #1:get_self_build_status\n\n' + _WORDS)
    # In its place: before everything autogen kept, and autogen's window is
    # otherwise unchanged.
    assert out[0] is users[0] or out[0]['name'] == 'User'
    assert out[1:] == ag.MessageTokenLimiter(**_TOKENS).apply_transform(msgs)


def test_the_restored_task_is_bounded_like_any_message():
    huge = 'Perform this action -> Action #1:x\n\n' + _WORDS + ' ' + 'step ' * 6000
    msgs = [{'role': 'user', 'name': 'User', 'content': huge}] + _live_seat_buffer()[1:]
    out = token_limiter(**_TOKENS).apply_transform(msgs)
    user = next(m for m in out if m.get('name') == 'User')
    from autogen.agentchat.contrib.capabilities import transforms_util
    assert (transforms_util.count_text_tokens(user['content'])
            <= AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE)
    assert _WORDS in user['content']


def test_the_history_limiter_keeps_a_task_that_is_not_first():
    """keep_first_message keeps message 0, which is not always the task (a
    seat's own earlier reply can open its buffer)."""
    task = {'role': 'user', 'name': 'User', 'content': _TASK}
    msgs = ([{'role': 'assistant', 'name': 'Assistant', 'content': 'earlier'}, task]
            + [{'role': 'user' if i % 2 else 'assistant',
                'name': 'StatusVerifier', 'content': 'v%d' % i}
               for i in range(AUTOGEN_HISTORY_LIMIT + 5)])
    out = history_limiter(max_messages=AUTOGEN_HISTORY_LIMIT,
                          keep_first_message=True).apply_transform(msgs)
    assert task in out
    assert out[0] is msgs[0] and out[1] is task
    assert out[-1] is msgs[-1]


def test_a_buffer_that_fits_is_returned_unchanged():
    msgs = _live_seat_buffer()[:3]
    assert token_limiter(**_TOKENS).apply_transform(msgs) == msgs


def test_words_over_the_per_message_cap_keep_the_steps_boundary():
    """Review of f97b6bed8: user words over the 1,000-token cap were cut
    head-first by the limiter, and the "follow these steps:" boundary went
    with them before the wire trim saw the turn.  The limiter keeps the
    marker and the words whole and cuts only the steps -- the rule the trim
    uses (core.llm_outbound_logger.must_keep_head)."""
    from core.constants import ACTION_STEPS_SEPARATOR
    words = ' '.join('word%d' % i for i in range(1500))
    head = 'Perform this action -> Action #1:summarize\n\n' + words
    turn = {'role': 'user', 'name': 'User',
            'content': head + ACTION_STEPS_SEPARATOR
            + "{'s': 'step'}, " * 400 + 'LAST'}
    out = token_limiter(**_TOKENS).apply_transform([turn])
    kept = out[0]['content']
    assert kept.startswith(head + ACTION_STEPS_SEPARATOR), kept[-200:]
    assert len(kept) < len(turn['content'])


def test_a_turn_without_the_separator_is_cut_as_autogen_cuts_it():
    long = 'plain ' * 3000
    msgs = [{'role': 'user', 'name': 'User', 'content': long}]
    assert (token_limiter(**_TOKENS).apply_transform(msgs)
            == ag.MessageTokenLimiter(**_TOKENS).apply_transform(msgs))


def test_only_a_user_turn_keeps_its_head_in_the_limiter():
    """Review of e1a1aa233 (r0003_lim.py): autogen's per-message hook has
    no role, so an assistant or tool message holding the separator kept
    its head whole too (~6,000 tokens against 2,500)."""
    from core.constants import ACTION_STEPS_SEPARATOR
    body = 'word ' * 3000 + ACTION_STEPS_SEPARATOR + 'step ' * 50
    msgs = [{'role': 'assistant', 'name': 'Assistant', 'content': body},
            {'role': 'user', 'name': 'StatusVerifier', 'content': 'ok'}]
    out = token_limiter(**_TOKENS).apply_transform(msgs)
    want = ag.MessageTokenLimiter(**_TOKENS).apply_transform(msgs)
    assert out == want
