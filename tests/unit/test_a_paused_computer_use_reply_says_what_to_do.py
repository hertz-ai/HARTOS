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
    # The refusal points to the steps listed: CREATE lists them too.
    assert 'What it did before stopping' in reply and DONE_STEP in reply, reply


def _cut_off_by_the_owner():
    """A real run the input check stopped: its last step never ran and
    carries the executor's own pause text."""
    import pytest as _pt
    lct = _pt.importorskip('integrations.vlm.local_computer_tool')
    import core.resource_governor as governor
    real = governor.get_physical_input_state
    governor.get_physical_input_state = lambda **_k: (2, 0.0)
    try:
        block = lct._input_context_block(
            {'action': 'left_click', '_human_input_token': 1})
    finally:
        governor.get_physical_input_state = real
    response = _incomplete('user_active')
    response['extracted_responses'].append(
        {'type': 'action', 'iteration': 3,
         'content': {'action': 'left_click', 'reasoning': 'open the menu',
                     'result': 'FAILED: ' + block['error'], 'ok': False,
                     'status': block['status']}})
    return block, response


def test_the_input_check_pause_speaks_of_the_owner():
    block, _response = _cut_off_by_the_owner()
    assert block['status'] == 'user_active'
    assert 'you' not in block['error'].lower().split(), block['error']
    assert 'owner' in block['error']


def test_a_step_cut_off_by_the_owner_is_not_called_done(
        reuse_sent, monkeypatch):
    run, _sent = reuse_sent
    _block, response = _cut_off_by_the_owner()
    monkeypatch.setattr(list_steps, '_done', lambda: response)
    reply = run('Check the weather on the desktop widget', action=3)
    assert 'you resumed' not in reply
    assert 'those that did not fail are done' in reply, reply


def test_stopped_is_said_of_the_owner():
    from integrations.vlm import response_view
    line = response_view.outcome_summary(_incomplete('stopped'))
    assert "owner's request" in line and 'your request' not in line


def test_a_finished_run_carries_no_refusal_and_any_other_one_does():
    from core.tool_refusal import refusal_has_hint
    from integrations.vlm import response_view
    assert response_view.failure_refusal(
        {'status': 'success', 'exit_reason': 'done',
         'extracted_responses': []}) == ''
    for odd in (None, {}, {'status': 'incomplete',
                           'exit_reason': 'never-heard-of'}):
        assert refusal_has_hint(response_view.failure_refusal(odd)), odd


# --- review of cc3897fc8 (rv_cc38): typing cut off partway, the pause worded
# --- for the model and for the screen, one builder for the reply -----------

TYPED = 'zebra quartz'


@pytest.fixture
def desk(monkeypatch):
    """The real executor on a fake keyboard.  ``state[0]`` is the physical
    input count the input check reads; a test moves it to play the owner."""
    from unittest.mock import Mock
    lct = pytest.importorskip('integrations.vlm.local_computer_tool')
    import core.resource_governor as governor
    state = [3]
    monkeypatch.setattr(governor, 'get_physical_input_state',
                        lambda **_k: (state[0], None))
    monkeypatch.setattr(lct, 'foreground_window_handle', lambda: 100)
    keys = Mock()
    monkeypatch.setattr(lct, 'pyautogui', keys)
    return lct, keys, state


def _type(lct, **more):
    return lct._execute_inprocess(dict(
        {'action': 'type', 'text': TYPED, '_human_input_token': 3}, **more))


def test_typing_cut_off_partway_says_how_far_it_got(desk, monkeypatch):
    """No pyperclip (HARTOS requirements.txt does not name it): the text is
    typed one character at a time and the owner can cut it off in the middle.
    Measured: "hel" of "hello world" was typed and the result said "this
    action was not performed", so the model typed everything again."""
    from integrations.vlm.local_loop import STEP_HISTORY_RESULT_CHARS
    lct, keys, state = desk
    monkeypatch.setattr(lct, 'pyperclip', None)
    typed = []

    def _key(character, **_k):
        typed.append(character)
        if len(typed) == 3:
            state[0] = 4                    # the owner touches the keyboard
    keys.typewrite.side_effect = _key
    result = _type(lct)
    assert ''.join(typed) == 'zeb'
    assert result['status'] == 'user_active'
    said = result['error']
    assert 'not performed' not in said, said
    assert 'typing was cut off after 3 of 12 characters' in said, said
    assert '"zeb"' in said, said
    assert 'owner' in said and 'you' not in said.lower().split(), said
    # The loop's step history keeps this much of "FAILED: <error>".
    assert 'Type only the rest, not the whole text' in (
        'FAILED: ' + said)[:STEP_HISTORY_RESULT_CHARS], said
    # The screen's line: no typed text (it may be a credential), nobody named.
    shown = result['block_reason']
    assert 'zeb' not in shown and 'owner' not in shown, shown
    assert 'rest' not in shown and 'typed' in shown, shown


def test_a_long_typed_part_is_quoted_by_its_end(desk, monkeypatch):
    lct, keys, state = desk
    monkeypatch.setattr(lct, 'pyperclip', None)
    text = ''.join(chr(97 + i % 26) for i in range(200))
    count = [0]

    def _key(_character, **_k):
        count[0] += 1
        if count[0] == 150:
            state[0] = 4
    keys.typewrite.side_effect = _key
    said = _type(lct, text=text)['error']
    assert 'cut off after 150 of 200 characters' in said
    assert '"...%s"' % text[90:150] in said, said
    assert text[:90] not in said


def test_typing_stopped_before_its_first_character_was_not_performed(
        desk, monkeypatch):
    lct, keys, state = desk
    monkeypatch.setattr(lct, 'pyperclip', None)
    import core.resource_governor as governor
    reads = [0]

    def _state(**_k):
        reads[0] += 1                       # the third read is the first
        return (4 if reads[0] >= 3 else 3, None)        # character's check
    monkeypatch.setattr(governor, 'get_physical_input_state', _state)
    result = _type(lct)
    assert reads[0] == 3
    assert not keys.typewrite.called
    assert result['status'] == 'user_active'
    assert 'this action was not performed' in result['error']
    assert 'cut off' not in result['error']
    assert result['block_reason'] == lct._PAUSE_SHOWN


def test_a_paste_stopped_before_it_was_sent_was_not_performed(
        desk, monkeypatch):
    """With pyperclip the text goes in as one paste: all of it or none."""
    from unittest.mock import Mock
    lct, keys, state = desk
    clipboard = ['what the owner had copied']
    clip = Mock()
    clip.paste.side_effect = lambda: clipboard[0]

    def _copy(value):
        clipboard[0] = value
        state[0] = 4                # the owner moves while it is copied
    clip.copy.side_effect = _copy
    monkeypatch.setattr(lct, 'pyperclip', clip)
    result = _type(lct)
    assert not keys.hotkey.called and not keys.typewrite.called
    assert result['status'] == 'user_active'
    assert 'this action was not performed' in result['error']
    assert 'cut off' not in result['error']
    assert clipboard[0] == 'what the owner had copied'


def test_a_paste_that_was_sent_is_typed_whole(desk, monkeypatch):
    from unittest.mock import Mock
    lct, keys, _state = desk
    clip = Mock()
    clip.paste.return_value = ''
    monkeypatch.setattr(lct, 'pyperclip', clip)
    monkeypatch.setattr(lct.time, 'sleep', lambda _s: None)
    result = _type(lct)
    keys.hotkey.assert_called_once_with('ctrl', 'v')
    assert result == {'output': 'Typed 12 characters'}


def test_typing_cut_off_by_a_window_change_says_how_far_it_got(
        desk, monkeypatch):
    lct, keys, _state = desk
    monkeypatch.setattr(lct, 'pyperclip', None)
    count = [0]

    def _key(_character, **_k):
        count[0] += 1
        if count[0] == 2:
            monkeypatch.setattr(lct, 'foreground_window_handle', lambda: 200)
    keys.typewrite.side_effect = _key
    result = _type(lct, _expected_foreground=100)
    assert result['status'] == 'context_changed'
    assert result['error'].startswith('Foreground changed after capture')
    assert 'Typing was cut off after 2 of 12 characters' in result['error']
    assert '"ze"' in result['error']
    assert 'ze"' not in result['block_reason']


def test_the_pause_is_worded_for_the_model_and_for_the_screen():
    """The same pause text was the step's result (the model reads it) and
    the owner's activity line: the owner read about "the owner", with an
    instruction meant for the agent."""
    block, _response = _cut_off_by_the_owner()
    said, shown = block['error'], block['block_reason']
    assert 'this action was not performed' in said and 'Inspect' in said
    assert shown.startswith('Paused'), shown
    for word in ('owner', 'Inspect', 'resuming', 'you'):
        assert word not in shown, shown
    assert len(shown) <= 160            # activity_stream's payload cap


class _TypingBackend:
    def __init__(self):
        self.calls = 0

    def route_task(self, instruction):
        return 'single_shot'

    def try_taskbar_pre_check(self, *a, **kw):
        return None

    def detect_grounding_bias(self, *a, **kw):
        return None

    def _call_api(self, messages):
        self.calls += 1
        return ('{"Reasoning":"type","Next Action":"type","value":"hello",'
                '"Status":"IN_PROGRESS"}')


def _run_the_loop(monkeypatch, result=None):
    """The real loop on a fake screen and model.  Returns (run result, what
    the activity line was given): [(phase or 'close', error text)]."""
    from integrations.vlm import (
        activity_stream, local_computer_tool as lct, local_loop,
        qwen3vl_backend)
    monkeypatch.setattr(qwen3vl_backend, 'get_qwen3vl_backend',
                        lambda b=_TypingBackend(): b)
    monkeypatch.setattr(lct, 'take_screenshot', lambda tier: 'ZmFrZQ==')
    monkeypatch.setattr(lct, 'execute_action',
                        lambda *a, **kw: dict(result or {'output': 'ok'}))
    monkeypatch.setenv('HEVOLVE_VLM_UNIFIED', '1')
    shown = []
    real_step = activity_stream.record_activity
    real_close = activity_stream.finish_run

    def _step(**kw):
        shown.append((kw['phase'], kw.get('error', '')))
        return real_step(**kw)

    def _close(**kw):
        shown.append(('close', kw.get('error', '')))
        return real_close(**kw)
    monkeypatch.setattr(activity_stream, 'record_activity', _step)
    monkeypatch.setattr(activity_stream, 'finish_run', _close)
    out = local_loop.run_local_agentic_loop({
        'instruction_to_vlm_agent': 'type hello',
        'enhanced_instruction': 'type hello', 'user_id': 'pause-text-test',
        'prompt_id': 'pause-text-test', 'max_ETA_in_seconds': 30},
        tier='inprocess')
    return out, shown


def test_the_activity_line_shows_the_screen_text_and_the_model_gets_its_own(
        monkeypatch, computer_control_granted):
    block, _response = _cut_off_by_the_owner()
    out, shown = _run_the_loop(monkeypatch, block)
    assert out['exit_reason'] == 'user_active'
    step = out['extracted_responses'][-1]['content']
    assert step['result'] == 'FAILED: ' + block['error']
    assert step['ok'] is False and step['status'] == 'user_active'
    lines = [text for phase, text in shown if phase != 'executing']
    assert lines == [block['block_reason']] * 2, shown   # the step, the close
    for _phase, text in shown:
        assert 'owner' not in text and 'Inspect' not in text, shown


def test_a_refusal_worded_once_still_reaches_the_activity_line(
        monkeypatch, computer_control_granted):
    """A result with no block_reason of its own (an executor error) shows its
    error, as before."""
    out, shown = _run_the_loop(
        monkeypatch, {'output': '', 'error': 'pyautogui not installed'})
    assert out['extracted_responses'][0]['content']['result'] == \
        'FAILED: pyautogui not installed'
    failed = [text for phase, text in shown if phase == 'failed']
    assert failed and set(failed) == {'pyautogui not installed'}, shown


def test_a_run_that_yields_between_steps_names_nobody_on_the_screen(
        monkeypatch, computer_control_granted):
    from integrations.vlm import local_loop
    monkeypatch.setattr(local_loop, '_background_run_must_yield', lambda: True)
    out, shown = _run_the_loop(monkeypatch)
    assert out['exit_reason'] == 'user_active'
    assert not out['extracted_responses']
    assert [text for phase, text in shown if phase != 'executing'] == [
        'Paused: this computer is in use'], shown


try:
    import hart_intelligence_entry as hie  # noqa: TID251 -- the direct-chat tool lives here
except Exception:
    hie = None


def _direct_chat(monkeypatch, response):
    import integrations.vlm.vlm_adapter as adapter
    monkeypatch.setattr(adapter, 'execute_vlm_instruction',
                        lambda message: response)
    monkeypatch.setattr(adapter, 'resolve_task_workspace', lambda **_k: '')
    return hie._handle_computer_action_tool('open the menu')


@pytest.mark.skipif(hie is None, reason='hart_intelligence_entry import failed')
def test_direct_chat_says_plainly_that_it_paused_for_the_owner(monkeypatch):
    """It said "Loop exited with reason 'user_active' after 3 actions" and
    then offered a different approach."""
    _block, response = _cut_off_by_the_owner()
    out = _direct_chat(monkeypatch, response)
    assert 'Loop exited' not in out and 'user_active' not in out, out
    from integrations.vlm.response_view import outcome_summary
    # The command tools' own sentence for a pause, from their one table.
    assert out.startswith(outcome_summary(response) + ' Last attempt'), out
    assert out.startswith('Paused because the owner started using this '
                          'computer after 3 step(s).'), out
    assert 'Last attempt: left_click (open the menu).' in out, out
    assert 'paused, not failed' in out and 'do not start over' in out, out
    assert 'different approach' not in out, out


@pytest.mark.skipif(hie is None, reason='hart_intelligence_entry import failed')
def test_direct_chat_keeps_its_other_endings(monkeypatch):
    _block, response = _cut_off_by_the_owner()
    response['exit_reason'] = 'timeout'
    assert _direct_chat(monkeypatch, response) == (
        'I tried for 21s (3 actions) but ran out of time before finishing. '
        'Last attempt: left_click (open the menu). Want me to try a '
        'different approach, or should I describe the steps so you can do '
        'it manually?')
    response['exit_reason'] = 'no_progress'
    assert _direct_chat(monkeypatch, response) == (
        "Loop exited with reason 'no_progress' after 3 actions. "
        'Last attempt: left_click (open the menu). Want me to try a '
        'different approach, or should I describe the steps so you can do '
        'it manually?')


# The layout CREATE and REUSE each built by hand before failure_reply, kept
# here word for word: the one builder must return the same bytes.

def _by_hand_create(failure, why, hint, observed, vlm_context):
    return (f"{failure}\n"
            f"{why or vlm_context or 'Computer action refused'}"
            + (f"\n{hint}" if hint else '')
            + (f"\nWhat it did before stopping:\n{observed}"
               if observed else ''))


def _by_hand_reuse(failure, why, hint, observed):
    if why or observed or hint:
        return '%s\n%s%s%s' % (
            failure,
            why,
            ('\n' + hint) if hint else '',
            ('\nWhat it did before stopping:\n' + observed)
            if observed else '')
    return failure


def _parts(response):
    from integrations.vlm import response_view as rv
    return (rv.outcome_summary(response), rv.failure_refusal(response),
            rv.observation_text(response))


def _responses():
    out = [_incomplete(reason) for reason in (
        'user_active', 'max_iterations', 'action_error', 'timeout',
        'stopped', 'grounding_failed', 'no_progress', 'never-heard-of')]
    out.append(_cut_off_by_the_owner()[1])
    out += [
        {'status': 'blocked', 'exit_reason': 'consent_required',
         'extracted_responses': [{'type': 'error', 'content': 'Not run'}]},
        {'status': 'incomplete', 'exit_reason': 'destructive_operation',
         'extracted_responses': []},
        {'status': 'incomplete'}, {}, None, 'not a dict',
        {'status': 'success', 'exit_reason': 'done',
         'extracted_responses': []},
        {'message': 'Failed to capture screenshot', 'status': 'error'}]
    return out


def test_the_one_reply_builder_returns_what_both_legs_built_by_hand():
    from core.constants import TOOL_FAILURE_RESULTS
    from integrations.vlm import response_view as rv
    for response in _responses():
        why, hint, observed = _parts(response)
        for failure in TOOL_FAILURE_RESULTS[:2]:
            assert rv.failure_reply(failure, response) == _by_hand_reuse(
                failure, why, hint, observed), response
            for context in ('', 'windows Agent did:\n- a step'):
                assert rv.failure_reply(
                    failure, response,
                    fallback=context or 'Computer action refused',
                ) == _by_hand_create(failure, why, hint, observed,
                                     context), response


@pytest.mark.parametrize('why', ['', 'Paused after 2 step(s).'])
@pytest.mark.parametrize('hint', ['', 'Not run: do this instead.'])
@pytest.mark.parametrize('observed', ['', '- a\n- b'])
def test_the_builder_keeps_every_part_combination_of_both_legs(
        monkeypatch, why, hint, observed):
    """outcome_summary is '' only for a response it cannot read, so the
    parts are set here to reach each branch the two copies had."""
    from integrations.vlm import response_view as rv
    monkeypatch.setattr(rv, 'outcome_summary', lambda _r: why)
    monkeypatch.setattr(rv, 'failure_refusal', lambda _r: hint)
    monkeypatch.setattr(rv, 'observation_text', lambda _r: observed)
    assert rv.failure_reply('FAILED', {}) == _by_hand_reuse(
        'FAILED', why, hint, observed)
    for context in ('', 'context'):
        assert rv.failure_reply(
            'FAILED', {}, fallback=context or 'Computer action refused',
        ) == _by_hand_create('FAILED', why, hint, observed, context)


def test_the_builder_never_raises_and_keeps_the_failure_line(monkeypatch):
    from integrations.vlm import response_view as rv

    def _boom(_response):
        raise RuntimeError('reader broke')
    monkeypatch.setattr(rv, 'observation_text', _boom)
    assert rv.failure_reply('FAILED', _incomplete('timeout')) == 'FAILED'
    assert rv.failure_reply('FAILED', _incomplete('timeout'),
                            fallback='context') == 'FAILED'


@pytest.mark.parametrize('reason', ['user_active', 'timeout', 'stopped'])
def test_reuse_returns_the_builders_reply_byte_for_byte(
        reuse_sent, monkeypatch, reason):
    from core.constants import TOOL_FAILURE_RESULTS
    run, _sent = reuse_sent
    response = _incomplete(reason)
    monkeypatch.setattr(list_steps, '_done', lambda: response)
    reply = run('Check the weather on the desktop widget', action=3)
    assert reply == _by_hand_reuse(TOOL_FAILURE_RESULTS[0], *_parts(response))
    assert '\nWhat it did before stopping:\n- shell - ' in reply


def test_reuse_keeps_the_screenshot_failure_line(reuse_sent, monkeypatch):
    from core.constants import TOOL_FAILURE_RESULTS
    run, _sent = reuse_sent
    response = dict(_incomplete('action_error'),
                    message='Failed to capture screenshot')
    monkeypatch.setattr(list_steps, '_done', lambda: response)
    reply = run('Check the weather on the desktop widget', action=3)
    assert reply == _by_hand_reuse(TOOL_FAILURE_RESULTS[1], *_parts(response))
    assert reply.startswith(TOOL_FAILURE_RESULTS[1])


@pytest.mark.parametrize('reason', ['user_active', 'timeout', 'stopped'])
def test_create_returns_the_builders_reply_byte_for_byte(
        computer, monkeypatch, reason):
    from core.constants import TOOL_FAILURE_RESULTS
    _env, run, _sent = computer
    response = _incomplete(reason)
    monkeypatch.setattr(create_twin, '_vlm_run', lambda *c: response)
    reply = run('Open Notepad', 'open notepad', action=2)
    assert reply == _by_hand_create(TOOL_FAILURE_RESULTS[0], *_parts(response),
                                    vlm_context='')
    assert '\nWhat it did before stopping:\n- shell - ' in reply


def test_create_keeps_its_own_text_when_the_run_has_no_summary(
        computer, monkeypatch):
    """CREATE's second line falls back to what its agent did, then to
    'Computer action refused'; REUSE's has no fallback."""
    from core.constants import TOOL_FAILURE_RESULTS
    from integrations.vlm import response_view as rv
    _env, run, _sent = computer
    response = _incomplete('timeout')
    monkeypatch.setattr(create_twin, '_vlm_run', lambda *c: response)
    monkeypatch.setattr(rv, 'outcome_summary', lambda _r: '')
    lines = run('Open Notepad', 'open notepad', action=2).split('\n')
    assert lines[0] == TOOL_FAILURE_RESULTS[0]
    assert lines[1].endswith(' Agent did:') and DONE_STEP in lines[2], lines
    response = dict(_incomplete('timeout'), extracted_responses=[])
    monkeypatch.setattr(rv, 'failure_refusal', lambda _r: '')
    assert run('Open Notepad', 'open notepad', action=2) == (
        TOOL_FAILURE_RESULTS[0] + '\nComputer action refused')


def test_source_guard_the_heading_is_written_once():
    import os
    from integrations.vlm import response_view as rv
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(rv.__file__))))
    count = 0
    for rel in ('integrations/vlm/response_view.py', 'hartos/create_recipe.py',
                'hartos/reuse_recipe.py'):
        with open(os.path.join(root, rel), encoding='utf-8') as source:
            count += source.read().count('What it did before stopping')
    assert count == 1, count
    for reason in ('user_active', 'timeout'):
        assert '"%s"' % rv.STEPS_HEADING in rv.failure_refusal(
            _incomplete(reason))


# --- review of 5c93a9cfc: a filled-in credential is never quoted or counted,
# --- and the cut-off note reaches the model that types next ----------------

REAL = 'Hunter22Secret!'


@pytest.fixture
def vault(monkeypatch):
    """The real vault class holding one owner credential, BANK_PW."""
    import types
    import core.tool_logging as tool_logging
    from hartos.ai_key_vault import AIKeyVault
    held = AIKeyVault()
    held.owner_credential_names = lambda strict=False: {'BANK_PW'}
    held.owner_credential = (
        lambda name, names=None: REAL if name == 'BANK_PW' else '')
    held._granted_names = lambda: set()
    held._secrets_manager = lambda: types.SimpleNamespace(_cache={})
    held.get_tool_key = lambda name: None
    monkeypatch.setattr(tool_logging, 'credential_vault', lambda: held)
    return held


def _typed_until(lct, keys, state, monkeypatch, stop_after, window=False):
    typed = []

    def _key(character, **_k):
        typed.append(character)
        if len(typed) == stop_after:
            if window:
                monkeypatch.setattr(lct, 'foreground_window_handle',
                                    lambda: 200)
            else:
                state[0] = 4
    keys.typewrite.side_effect = _key
    monkeypatch.setattr(lct, 'pyperclip', None)
    return typed


def _leaked(said):
    """The longest piece of the credential, four characters or more, that
    ``said`` holds."""
    pieces = [REAL[i:j] for i in range(len(REAL))
              for j in range(i + 4, len(REAL) + 1) if REAL[i:j] in said]
    return max(pieces, key=len) if pieces else ''


@pytest.mark.parametrize('text', ['{{secret:BANK_PW}}',
                                  'login {{secret:BANK_PW}} then enter'])
@pytest.mark.parametrize('stop_after', [6, 9, 14])
@pytest.mark.parametrize('window', [False, True])
def test_a_credential_cut_off_partway_is_never_quoted_or_counted(
        desk, vault, monkeypatch, text, stop_after, window):
    """The real execute_action fills the alias, the keyboard gets the value
    and the owner cuts the typing off.  The result quoted the typed part
    after the fill and masking replaces whole values only: the model read
    "Hunter22S", 9 of a 15-character password, and the count gave away its
    length."""
    import json
    lct, keys, state = desk
    typed = _typed_until(lct, keys, state, monkeypatch, stop_after, window)
    result = lct.execute_action(
        {'action': 'type', 'text': text, '_human_input_token': 3,
         '_expected_foreground': 100}, 'inprocess', safety=False)
    # The keyboard got the real text, as far as it went.
    assert ''.join(typed) == text.replace('{{secret:BANK_PW}}', REAL)[:stop_after]
    assert result['status'] == ('context_changed' if window else 'user_active')
    said = json.dumps(result)
    assert not _leaked(said), 'a piece of the credential reached the result: %r' % _leaked(said)
    assert not any(character.isdigit() for character in said), (
        'a count gives the length away: ' + said)
    assert 'not performed' not in said, said
    for part in (result['error'], result['typing_cut_off']):
        assert 'cut off partway through text that holds a credential' in part
        assert 'Clear the field, then type the whole text again.' in part
    assert 'credential' not in result['block_reason'], result['block_reason']


def test_text_with_no_credential_keeps_its_count_beside_a_vault(
        desk, vault, monkeypatch):
    lct, keys, state = desk
    _typed_until(lct, keys, state, monkeypatch, 3)
    result = lct.execute_action(
        {'action': 'type', 'text': TYPED, '_human_input_token': 3},
        'inprocess', safety=False)
    assert ('typing was cut off after 3 of 12 characters. Type only the '
            'rest, not the whole text. Typed so far: "zeb"') in result['error']
    assert result['typing_cut_off'] == (
        'Typing was cut off after 3 of 12 characters. Type only the rest, '
        'not the whole text. Typed so far: "zeb"')


def test_an_alias_the_vault_does_not_hold_is_typed_and_counted_as_written(
        desk, vault, monkeypatch):
    """Nothing was filled in, so nothing secret was typed."""
    lct, keys, state = desk
    _typed_until(lct, keys, state, monkeypatch, 4)
    result = lct.execute_action(
        {'action': 'type', 'text': '{{secret:NOT_HELD}}',
         '_human_input_token': 3}, 'inprocess', safety=False)
    assert 'cut off after 4 of 19 characters' in result['error']
    assert '"{{se"' in result['error']


def test_the_executor_alone_takes_the_fill_mark_from_execute_action(desk, monkeypatch):
    """_execute_inprocess with the mark set says nothing of the text; with
    no mark it says how far, as before."""
    lct, keys, state = desk
    _typed_until(lct, keys, state, monkeypatch, 3)
    marked = _type(lct, _secret_filled=True)
    assert 'zeb' not in str(marked) and ' of ' not in marked['error']
    assert 'holds a credential' in marked['error']
    state[0] = 3
    _typed_until(lct, keys, state, monkeypatch, 3)
    assert '"zeb"' in _type(lct)['error']


def _cut(lct, status, secret=False):
    """The executor's own result for TYPED cut off after 3 characters."""
    if status == 'user_active':
        block = {'output': '', 'status': 'user_active',
                 'error': lct._PAUSED_FOR_OWNER + '; this action was not '
                 'performed.', 'block_reason': lct._PAUSE_SHOWN}
    else:
        why = ('Foreground changed after capture; take a new screenshot '
               'before acting.')
        block = {'output': '', 'status': 'context_changed', 'error': why,
                 'block_reason': why}
    return lct._typing_cut_off(block, TYPED, 3, secret=secret)


from tests.unit.test_a_computer_use_step_is_judged_by_what_it_achieved import (  # noqa: E402,F401
    _say, drive, vision)
from tests.unit.test_a_paused_computer_use_step_resumes_where_it_stopped import (  # noqa: E402,F401
    ledger, owner_returns)


def test_after_a_window_change_the_next_prompt_says_how_far_the_typing_got(
        drive, vision):
    """The same run's next prompt said "That action was not performed ...
    so nothing was done" with three characters in the field."""
    from integrations.vlm import local_computer_tool as lct
    out, steps, _ran, backend = drive(
        [_say('type', value=TYPED)],
        {'type': lambda _action: _cut(lct, 'context_changed')})
    assert out['exit_reason'] == 'done' and len(steps) == 1
    assert steps[0]['typing_cut_off'].startswith('Typing was cut off after 3')
    second = backend.prompts[1]
    assert 'nothing was done' not in second, second[-900:]
    assert 'That action was cut off partway: the window in front changed ' \
        'while it was typing.' in second
    assert ('Typing was cut off after 3 of 12 characters. Type only the '
            'rest, not the whole text. Typed so far: "zeb"') in second
    assert 'This screenshot is new; decide again from it.' in second


def test_a_window_change_before_the_first_character_still_says_nothing_was_done(
        drive, vision):
    why = 'Foreground changed after capture; take a new screenshot before acting.'
    out, steps, _ran, backend = drive(
        [_say('type', value=TYPED)],
        {'type': {'output': '', 'status': 'context_changed', 'error': why,
                  'block_reason': why}})
    assert 'typing_cut_off' not in steps[0]
    assert ('That action was not performed: the window in front changed '
            'after its screenshot was taken, so nothing was done.'
            ) in backend.prompts[1]


@pytest.mark.parametrize('secret', [False, True])
def test_the_run_that_resumes_cut_off_typing_is_told_how_far_it_got(
        drive, vision, ledger, owner_returns, secret):
    """The resumed run's prompt said only "paused after 1 step(s). Its last
    step: type": the count reached the outer agent's tool reply and never
    the model that types."""
    from agent_ledger import TaskStatus
    from integrations.vlm import local_computer_tool as lct
    cut = _cut(lct, 'user_active', secret=secret)
    out, steps, _ran, _b = drive(
        [_say('type', value=TYPED, why='fill in the name')],
        {'type': lambda _action: dict(cut)},
        user_id='u-news', prompt_id='p-news', action_id=2)
    assert out['exit_reason'] == 'user_active' and len(steps) == 1
    # The owner's surfaces carry the screen's wording and no typed text.
    paused = [t for t in ledger.get_tasks_by_status(TaskStatus.USER_STOPPED)
              if t.task_id.startswith('computer_use_')][0]
    assert paused.context['error'] == lct._PAUSE_SHOWN_TYPING
    assert 'zeb' not in paused.context['caption']
    assert paused.context['cut_off'] == cut['typing_cut_off']
    _out, _steps, _ran, backend = drive(
        [], {}, user_id='u-news', prompt_id='p-news', action_id=2)
    first = backend.prompts[0]
    assert 'This step was paused for the owner' in first
    assert 'Its last step: fill in the name (type)' in first
    if secret:
        assert 'Typing was cut off partway through text that holds a ' \
            'credential' in first, first[:900]
        assert 'zeb' not in first and ' of 12' not in first
    else:
        assert ('Typing was cut off after 3 of 12 characters. Type only the '
                'rest, not the whole text. Typed so far: "zeb"') in first, (
            first[:900])


def test_a_pause_that_cut_no_typing_adds_no_line(
        drive, vision, ledger, owner_returns):
    from agent_ledger import TaskStatus
    from integrations.vlm import activity_stream
    owner_returns(2)
    drive([_say('left_click', coordinate=[5, 5]),
           _say('left_click', coordinate=[9, 9])], {},
          user_id='u-news', prompt_id='p-news', action_id=2)
    owner_returns(None)
    paused = [t for t in ledger.get_tasks_by_status(TaskStatus.USER_STOPPED)
              if t.task_id.startswith('computer_use_')][0]
    assert 'cut_off' not in paused.context
    found = activity_stream.paused_run(
        user_id='u-news', prompt_id='p-news', action_id=2)
    assert sorted(found) == ['caption', 'paused_at', 'steps', 'task_id']
    _out, _steps, _ran, backend = drive(
        [], {}, user_id='u-news', prompt_id='p-news', action_id=2)
    assert 'cut off' not in backend.prompts[0]


def test_the_cut_off_note_goes_to_no_surface(ledger, monkeypatch):
    """The ribbon line and the step's client payload do not carry it."""
    from integrations.vlm import activity_stream
    shown = []
    monkeypatch.setattr(activity_stream, '_ribbon',
                        lambda *a, **k: shown.append((a, k)))
    monkeypatch.setattr(activity_stream, '_fan_out',
                        lambda user_id, payload: shown.append(payload))
    note = 'Typing was cut off after 3 of 12 characters.'
    payload = activity_stream.record_activity(
        user_id='u-news', prompt_id='p-news', run_id='surface1',
        iteration=1, action='type', phase='blocked', caption='fill in',
        error='Paused', action_id=2, cut_off=note)
    assert payload and len(shown) >= 2, shown
    assert 'cut off' not in str(shown) and '3 of 12' not in str(shown), shown
    task = ledger.get_task(activity_stream.run_task_id('surface1'))
    assert task.context['cut_off'] == note


@pytest.mark.skipif(hie is None, reason='hart_intelligence_entry import failed')
@pytest.mark.parametrize('reason', ['user_active', 'timeout'])
def test_direct_chat_says_how_far_cut_off_typing_got(monkeypatch, reason):
    _block, response = _cut_off_by_the_owner()
    response['exit_reason'] = reason
    note = ('Typing was cut off after 3 of 12 characters. Type only the '
            'rest, not the whole text. Typed so far: "zeb"')
    response['extracted_responses'][-1]['content'].update(
        action='type', reasoning='fill in the name', typing_cut_off=note)
    out = _direct_chat(monkeypatch, response)
    assert 'Last attempt: type (fill in the name). ' + note in out, out
    del response['extracted_responses'][-1]['content']['typing_cut_off']
    assert 'cut off' not in _direct_chat(monkeypatch, response)


def test_source_guard_direct_chat_has_no_pause_row_of_its_own():
    """The pause is said by response_view.outcome_summary's table; direct
    chat's own table keeps its four first-person endings."""
    import inspect
    if hie is None:
        pytest.skip('hart_intelligence_entry import failed')
    source = inspect.getsource(hie._handle_computer_action_tool)
    assert "'user_active': f" not in source
    assert 'outcome_summary(result)' in source
    for row in ("'timeout':", "'max_iterations':", "'action_error':",
                "'grounding_failed':"):
        assert source.count(row) == 1, row
