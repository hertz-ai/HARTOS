"""A computer-use run that did not finish tells the model what to do next.

Owner 2026-11-09: every refusal says what was refused and what to do.  Live
2026-11-21, agent 15004929489, the command tool answered

    Not able to perform this action now please try later
    Paused because you resumed using the computer after 2 step(s).
    What it did before stopping: ...

at 13:23:59, 13:28:14 and 13:34:22 (and "Tried 30 step(s) without reaching a
clear completion" at 13:22:02).  None of it was built by core.tool_refusal,
so each reply logged [TOOL-REFUSAL-NO-HINT]; it told the agent that IT had
used the computer; and after the pause the agent restarted the step from its
first sentence ("Open a default web browser ...") and opened rss.com again.

The reply must keep TOOL_FAILURE_RESULTS first (the fabrication gate reads
it), and add the core.tool_refusal sentence.  Both legs' REAL tools run;
only the owner-consent check and the VLM are replaced.

    python -m pytest tests/unit/test_a_paused_computer_use_reply_says_what_to_do.py -q -p no:cacheprovider
"""
import pytest

import tests.unit.test_a_list_shaped_recipe_step_reaches_the_loop as list_steps
import tests.unit.test_create_vlm_learning_files_its_action as create_twin
from tests.unit.test_a_list_shaped_recipe_step_reaches_the_loop import (  # noqa: F401
    reuse_sent)
from tests.unit.test_create_loop_end_to_end import create_env  # noqa: F401
from tests.unit.test_create_vlm_learning_files_its_action import (  # noqa: F401
    computer)

TOOL = 'execute_windows_or_android_command'
DONE_STEP = 'opened the news page in the browser'


def _incomplete(reason):
    return {'status': 'incomplete', 'exit_reason': reason,
            'execution_time_seconds': 20.7,
            'extracted_responses': [
                {'type': 'action', 'iteration': 1,
                 'content': {'action': 'shell', 'reasoning': DONE_STEP,
                             'result': 'Exit code: 0\n(no output)',
                             'ok': True}},
                {'type': 'action', 'iteration': 2,
                 'content': {'action': 'scroll_down',
                             'reasoning': 'look for the category list',
                             'result': 'Scrolled down 5 notches',
                             'ok': True}}]}


def _assert_hinted(reply):
    from core.constants import TOOL_FAILURE_RESULTS, tool_reply_failed
    from core.tool_logging import note_unhinted_refusal
    from core.tool_refusal import refusal_has_hint
    assert isinstance(reply, str), reply
    assert reply.startswith(TOOL_FAILURE_RESULTS[0]), (
        'the fabrication gate keys on TOOL_FAILURE_RESULTS: append, never '
        'substitute\n' + reply)
    assert tool_reply_failed(reply), reply
    assert refusal_has_hint(reply), (
        'the reply says nothing the model can act on:\n' + reply)
    assert note_unhinted_refusal(TOOL, reply) is False


@pytest.mark.parametrize('reason', ['user_active', 'max_iterations',
                                    'action_error', 'timeout', 'stopped'])
def test_reuse_says_what_to_do_for_every_unfinished_run(
        reuse_sent, monkeypatch, reason):
    run, sent = reuse_sent
    monkeypatch.setattr(list_steps, '_done', lambda: _incomplete(reason))
    reply = run('Check the weather on the desktop widget', action=3)
    assert sent, 'the VLM was never called'
    _assert_hinted(reply)


def test_a_paused_reply_speaks_of_the_owner_and_keeps_the_steps_done(
        reuse_sent, monkeypatch):
    run, _sent = reuse_sent
    monkeypatch.setattr(list_steps, '_done', lambda: _incomplete('user_active'))
    reply = run('Check the weather on the desktop widget', action=3)
    assert 'you resumed' not in reply, (
        'the reply tells the agent that it was the one using the computer')
    assert 'owner' in reply
    assert DONE_STEP in reply, 'the steps already done were dropped'
    assert 'do not start the step over' in reply
    # RETRY_LATER: nothing changes during this turn; the next dispatch resumes.
    assert 'do not call %s again now' % TOOL in reply


def test_create_says_what_to_do_for_a_paused_run(computer, monkeypatch):
    _env, run, sent = computer
    monkeypatch.setattr(create_twin, '_vlm_run',
                        lambda *c: _incomplete('user_active'))
    reply = run('Open Notepad', 'open notepad', action=2)
    assert sent, 'the VLM was never called'
    _assert_hinted(reply)
    assert 'you resumed' not in reply


def test_a_finished_run_carries_no_refusal_and_any_other_one_does():
    from core.tool_refusal import refusal_has_hint
    from integrations.vlm import response_view
    assert response_view.failure_refusal(
        {'status': 'success', 'exit_reason': 'done',
         'extracted_responses': []}) == ''
    for odd in (None, {}, {'status': 'incomplete',
                           'exit_reason': 'never-heard-of'}):
        assert refusal_has_hint(response_view.failure_refusal(odd)), odd
