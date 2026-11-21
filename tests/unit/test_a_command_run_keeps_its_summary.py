"""A code block's run keeps the end of its output, where the summary is.

Owner, 2026-11-21: coding agents run commands "like Claude Code" -- in the
background, through the Executor seat (autogen's code execution:
create_recipe.instantiate_executor_agent, reuse_recipe's Executor, work_dir
the coding workspace).  autogen returns the whole output as one reply,
"exitcode: <n> (execution failed)\\nCode output: <logs>"
(ConversableAgent.generate_code_execution_reply), and every seat's token
limiter (hartos.helper.token_limiter) cut a message to its first
AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE tokens.  A test run's summary line
("3 failed, 120 passed") is its LAST line, so a long run reached the
Assistant and the StatusVerifier without it.

Pinned here, through the real limiter: such a reply keeps its head (the exit
code, the first lines) and its tail (the summary) within the cap, in both
modes of the limiter; every other message is cut exactly as autogen cuts it.
"""
import os
import sys

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if _HARTOS not in sys.path:
    sys.path.insert(0, _HARTOS)

import pytest  # noqa: E402

SUMMARY = '=========== 3 failed, 120 passed, 2 skipped in 41.20s ==========='


def _run_reply():
    lines = [f'tests/unit/test_mod_{i:04d}.py::test_case PASSED [{i % 100:3d}%]'
             for i in range(1200)]
    return ('exitcode: 1 (execution failed)\nCode output: \n'
            + '\n'.join(lines) + '\n' + SUMMARY)


def _limiter(turn_window=False):
    from core.constants import (AUTOGEN_MESSAGE_TOKEN_BUDGET,
                                AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE)
    from hartos.helper import token_limiter
    return token_limiter(max_tokens=AUTOGEN_MESSAGE_TOKEN_BUDGET,
                         max_tokens_per_message=AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE,
                         min_tokens=0, turn_window=turn_window)


def _tokens(text):
    from autogen.agentchat.contrib.capabilities import transforms_util
    return transforms_util.count_text_tokens(text)


@pytest.mark.parametrize('turn_window', [False, True], ids=['fixed', 'turn-window'])
def test_a_long_run_keeps_its_exit_code_and_its_summary(turn_window, monkeypatch):
    from core.constants import AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE
    import core.llm_outbound_logger as wire
    # The 4B's window: one message may keep 3,072 tokens (turn_message_room).
    monkeypatch.setattr(wire, 'turn_message_room', lambda config_list: 3072,
                        raising=False)
    reply = _run_reply()
    assert _tokens(reply) > 5 * AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE
    # As the Assistant reads it: the Executor's reply is a role 'user' turn
    # from another seat, after the Assistant's own code block.
    kept = _limiter(turn_window).apply_transform([
        {'role': 'user', 'name': 'ChatInstructor', 'content': 'Execute Action 4: run the tests'},
        {'role': 'assistant', 'name': 'Assistant', 'content': '```sh\npython -m pytest\n```'},
        {'role': 'user', 'name': 'Executor', 'content': reply}])
    seen = kept[-1]['content']
    assert seen.startswith('exitcode: 1 (execution failed)'), seen[:80]
    assert seen.rstrip().endswith(SUMMARY), seen[-200:]
    assert len(seen) < len(reply)


def test_the_cut_stays_within_the_per_message_cap():
    from core.constants import AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE
    kept = _limiter().apply_transform([{'role': 'user', 'name': 'Executor',
                                        'content': _run_reply()}])
    assert _tokens(kept[-1]['content']) <= AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE


def test_every_other_message_is_cut_as_autogen_cuts_it():
    """Only a code block's run keeps its tail: a tool result, an assistant
    turn or a person's long words are cut by autogen's own rule."""
    import autogen.agentchat.contrib.capabilities.transforms as ag
    from core.constants import (AUTOGEN_MESSAGE_TOKEN_BUDGET,
                                AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE)
    body = _run_reply().replace('exitcode: 1 (execution failed)', 'Here are the results')
    msgs = [{'role': 'user', 'name': 'User', 'content': body}]
    want = ag.MessageTokenLimiter(
        max_tokens=AUTOGEN_MESSAGE_TOKEN_BUDGET,
        max_tokens_per_message=AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE,
        min_tokens=0).apply_transform([dict(m) for m in msgs])
    assert _limiter().apply_transform(msgs) == want
    assert SUMMARY not in want[-1]['content']
