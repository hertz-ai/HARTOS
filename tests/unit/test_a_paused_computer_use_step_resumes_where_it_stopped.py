"""A computer-use step paused for the owner continues from the screen as it
is, told what it had done and what the screen showed then and now.

Owner 2026-11-21: after a yield, when the owner is idle again, the step must
observe the current state and continue toward its outcome, not restart from
its first sub-action.  Live that day (agent 15004929489, action 2), every run
after a pause began from nothing: 13:27:57 `start https://rss.com` with rss.com
already on screen, and again at 13:33.  The paused run's ledger task
(USER_STOPPED, written by finish_run) carried no action id and nothing read
it back.

The REAL run_local_agentic_loop, activity_stream (a real SmartLedger) and a
REAL VisionService (MiniCPM replaced) run; only the VLM, the screenshot, the
input probes and the executor are replaced.

    python -m pytest tests/unit/test_a_paused_computer_use_step_resumes_where_it_stopped.py -q -p no:cacheprovider
"""
import json

import pytest

from tests.unit.test_a_computer_use_step_is_judged_by_what_it_achieved import (  # noqa: F401
    _jpeg, _say, drive, vision)

RESULTS = {'shell': {'output': 'Exit code: 0\n(no output)'},
           'left_click': {'output': 'Clicked at [600, 400]'}}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    from agent_ledger import SmartLedger
    from agent_ledger.backends import JSONBackend
    from integrations.vlm import activity_stream
    book = SmartLedger('p-news', 'guest_p_news',
                       backend=JSONBackend(storage_dir=str(tmp_path)))
    monkeypatch.setattr(activity_stream, '_ledger_for', lambda *_a: book)
    return book


@pytest.fixture
def owner_returns(monkeypatch):
    """pause_at(n): the owner starts using the computer before step n."""
    from integrations.vlm import local_loop
    state = {'at': None, 'n': 0}

    def _must_yield():
        state['n'] += 1
        return state['at'] is not None and state['n'] >= state['at']
    monkeypatch.setattr(local_loop, '_background_run_must_yield', _must_yield)

    def pause_at(n):
        state['at'], state['n'] = n, 0
    return pause_at


def _paused_run(drive, pause_at, user_id='u-news', action_id=2):
    """Two steps, then the owner comes back: the 13:23 shape."""
    pause_at(3)
    out, _steps, _ran, _b = drive(
        [_say('shell', command='start https://rss.com'),
         _say('left_click', coordinate=[267, 792],
              why='open the news section')],
        RESULTS, screens=lambda i: _jpeg(30) if i == 1 else _jpeg(120),
        user_id=user_id, prompt_id='p-news', action_id=action_id)
    assert out['exit_reason'] == 'user_active', out['exit_reason']
    pause_at(None)


def _first_prompt(drive, user_id='u-news', action_id=2):
    _out, _steps, _ran, backend = drive(
        [_say('left_click', coordinate=[300, 500])], RESULTS,
        screens=lambda i: _jpeg(200), user_id=user_id, prompt_id='p-news',
        action_id=action_id)
    return backend.prompts[0]


def test_the_resumed_run_is_told_where_the_step_stopped(
        drive, vision, ledger, owner_returns):
    _paused_run(drive, owner_returns)
    first = _first_prompt(drive)
    assert 'This step was paused for the owner at' in first, (
        'the resumed run starts from nothing:\n' + first[:1200])
    assert 'after 2 step(s)' in first
    assert 'open the news section' in first, 'its last step was not named'
    assert 'At the pause the screen showed: screen caption 1' in first
    assert 'Now it shows: screen caption 2' in first
    assert 'Continue from the screen as it is now.' in first


def test_a_paused_run_is_taken_once(drive, vision, ledger, owner_returns):
    _paused_run(drive, owner_returns)
    assert 'paused for the owner' in _first_prompt(drive)
    assert 'paused for the owner' not in _first_prompt(drive)


def test_it_is_keyed_by_person_prompt_and_action(
        drive, vision, ledger, owner_returns):
    _paused_run(drive, owner_returns)
    assert 'paused for the owner' not in _first_prompt(drive, action_id=3)
    assert 'paused for the owner' not in _first_prompt(drive, user_id='u-other')
    assert 'paused for the owner' in _first_prompt(drive)


def test_a_run_that_yields_before_its_first_step_leaves_it(
        drive, vision, ledger, owner_returns):
    _paused_run(drive, owner_returns)
    owner_returns(1)                 # the owner is still there: no step runs
    out, steps, _ran, _b = drive([_say('left_click', coordinate=[1, 1])],
                                 RESULTS, user_id='u-news', prompt_id='p-news',
                                 action_id=2)
    assert out['exit_reason'] == 'user_active' and not steps
    owner_returns(None)
    first = _first_prompt(drive)
    assert 'paused for the owner' in first
    assert 'after 2 step(s)' in first, (
        'the run that never took a step was taken instead of the paused one')


def test_with_the_vision_service_off_it_still_says_where_it_stopped(
        drive, vision, ledger, owner_returns, monkeypatch):
    _paused_run(drive, owner_returns)
    import core.ai_sensing as sensing
    monkeypatch.setattr(sensing, 'allowed', lambda sensor: False)
    first = _first_prompt(drive)
    assert 'This step was paused for the owner at' in first
    assert 'screen showed' not in first and 'Now it shows' not in first


def test_a_long_pause_shows_the_screen_at_the_pause_not_the_current_one(
        drive, vision, ledger, owner_returns):
    """Ten minutes paused: the screen at the pause is the description filed
    by then, not the one filed a minute ago."""
    import collections
    import time
    from datetime import datetime, timedelta
    from agent_ledger import TaskStatus
    _paused_run(drive, owner_returns)
    task = [t for t in ledger.get_tasks_by_status(TaskStatus.USER_STOPPED)
            if t.task_id.startswith('computer_use_')][0]
    task.updated_at = (datetime.now() - timedelta(seconds=600)).isoformat()
    now = time.time()
    vision.store._screen_desc_history['u-news'] = collections.deque(
        [(now - 605, 'the rss.com front page'), (now - 30, 'a mail client')],
        maxlen=20)
    first = _first_prompt(drive)
    assert 'At the pause the screen showed: the rss.com front page' in first, (
        first[:1200])


def test_the_note_is_in_the_first_prompt_only(
        drive, vision, ledger, owner_returns):
    _paused_run(drive, owner_returns)
    _out, _steps, _ran, backend = drive(
        [_say('left_click', coordinate=[300, 500]),
         _say('left_click', coordinate=[320, 520])], RESULTS,
        user_id='u-news', prompt_id='p-news', action_id=2)
    assert 'paused for the owner' in backend.prompts[0]
    assert not any('paused for the owner' in p for p in backend.prompts[1:])


def _resumed_by(ledger):
    from agent_ledger import TaskStatus
    return [(t.context or {}).get('resumed_by')
            for t in ledger.get_tasks_by_status(TaskStatus.USER_STOPPED)
            if t.task_id.startswith('computer_use_')
            and int((t.context or {}).get('sequence') or 0) > 0]


def test_a_run_that_ends_done_without_an_action_closes_the_paused_run(
        drive, vision, ledger, owner_returns):
    """The model looks at the screen and says the step is finished: the
    paused run is over.  It was left for the next run, so every later run of
    the action was told it had been paused (review of 53b5cfd74)."""
    _paused_run(drive, owner_returns)
    out, steps, _ran, backend = drive(
        [], RESULTS, user_id='u-news', prompt_id='p-news', action_id=2)
    assert out['exit_reason'] == 'done' and not steps
    assert 'paused for the owner' in backend.prompts[0]
    assert all(_resumed_by(ledger)), _resumed_by(ledger)
    assert 'paused for the owner' not in _first_prompt(drive), (
        'a step that finished is still told it was paused')


PROSE = 'I can see the rss.com page; I will now click the news section'


@pytest.mark.parametrize('reply', ['', None, '   \n', PROSE,
                                   '{"Next Action": "None", "Status": "DO'])
def test_a_first_reply_with_nothing_to_read_leaves_the_paused_run(
        drive, vision, ledger, owner_returns, reply):
    """An empty reply (every token spent thinking), prose, or JSON cut off
    mid-word: the parser's fallback ends the run as done, but the model said
    nothing about the step.  It closed the paused run, and the verifier's
    retry began again with no note, the rss.com failure 53b5cfd74 fixed
    (review of 25e615941)."""
    _paused_run(drive, owner_returns)
    out, steps, _ran, backend = drive(
        [reply], RESULTS, user_id='u-news', prompt_id='p-news', action_id=2)
    assert out['exit_reason'] == 'done' and not steps      # ends as it did
    assert 'paused for the owner' in backend.prompts[0]
    assert not any(_resumed_by(ledger)), (
        'a reply with nothing to read closed the paused run: %r' % (reply,))
    first = _first_prompt(drive)
    assert 'paused for the owner' in first and 'after 2 step(s)' in first


@pytest.mark.parametrize('reply', [
    json.dumps({'Reasoning': 'I will now click the news section'}),
    _say('None', why='still looking'),          # Status IN_PROGRESS
    '{}'])
def test_a_reply_naming_no_action_and_no_done_leaves_the_paused_run(
        drive, vision, ledger, owner_returns, reply):
    """Readable JSON that names no next action and does not say DONE ends
    the run too; it does not say the step is finished either."""
    _paused_run(drive, owner_returns)
    out, steps, _ran, _b = drive(
        [reply], RESULTS, user_id='u-news', prompt_id='p-news', action_id=2)
    assert out['exit_reason'] == 'done' and not steps
    assert not any(_resumed_by(ledger)), _resumed_by(ledger)
    assert 'paused for the owner' in _first_prompt(drive)


@pytest.mark.parametrize('status', ['DONE', 'done', ' Done '])
def test_the_models_own_done_closes_the_paused_run_however_it_is_cased(
        drive, vision, ledger, owner_returns, status):
    _paused_run(drive, owner_returns)
    out, steps, _ran, _b = drive(
        [_say('None', status=status, why='the articles are listed')], RESULTS,
        user_id='u-news', prompt_id='p-news', action_id=2)
    assert out['exit_reason'] == 'done' and not steps
    assert all(_resumed_by(ledger)), _resumed_by(ledger)
    assert 'paused for the owner' not in _first_prompt(drive)


def test_the_paused_run_is_marked_once_in_a_run(
        drive, vision, ledger, owner_returns, monkeypatch):
    """Two actions, then DONE: the first action makes the mark and nothing
    after it does."""
    from integrations.vlm import activity_stream
    _paused_run(drive, owner_returns)
    real, marks = activity_stream.mark_paused_run_resumed, []

    def _spy(**kw):
        marks.append(kw)
        return real(**kw)
    monkeypatch.setattr(activity_stream, 'mark_paused_run_resumed', _spy)
    out, steps, _ran, _b = drive(
        [_say('left_click', coordinate=[300, 500]),
         _say('left_click', coordinate=[320, 520])], RESULTS,
        user_id='u-news', prompt_id='p-news', action_id=2)
    assert out['exit_reason'] == 'done' and len(steps) == 2
    assert len(marks) == 1, marks
    assert marks[0]['run_id'] in _resumed_by(ledger)


def test_the_first_action_closes_the_paused_run_in_a_run_that_does_not_finish(
        drive, vision, ledger, owner_returns):
    """One action, then the owner is back: no DONE ever comes, and the
    first action alone has closed the older paused run.  The next run is
    told of this pause, not of that one."""
    _paused_run(drive, owner_returns)                 # paused after 2 steps
    owner_returns(2)
    out, steps, _ran, _b = drive(
        [_say('left_click', coordinate=[300, 500]),
         _say('left_click', coordinate=[320, 520])], RESULTS,
        user_id='u-news', prompt_id='p-news', action_id=2)
    assert out['exit_reason'] == 'user_active' and len(steps) == 1
    owner_returns(None)
    assert sorted(bool(mark) for mark in _resumed_by(ledger)) == [False, True]
    first = _first_prompt(drive)
    assert 'paused for the owner' in first and 'after 1 step(s)' in first


def test_the_parser_says_when_it_read_nothing():
    """The fallback's DONE is told apart from the model's by one private
    key on the loop's own parse; a reply that was read never has it."""
    from integrations.vlm.local_loop import _parse_vlm_response
    for raw in ('', None, PROSE):
        assert _parse_vlm_response(raw).get('_unparsed') is True, raw
    for raw in (_say('None', status='DONE'), '{}',
                _say('left_click', coordinate=[1, 2]),
                '```json\n' + _say('None', status='DONE') + '\n```'):
        assert '_unparsed' not in _parse_vlm_response(raw), raw
    from integrations.vlm.parser import parse_vlm_action
    assert parse_vlm_action('', expected_shape='action_json').unparsed is True
    assert parse_vlm_action(PROSE, expected_shape='som_bbox').unparsed is True
    assert parse_vlm_action('{}', expected_shape='action_json').unparsed is False
    assert parse_vlm_action('DONE', expected_shape='point_only').unparsed is False
    assert parse_vlm_action('nothing to act on',
                            expected_shape='point_only').unparsed is False


def test_the_fallback_reads_as_it_always_did():
    """What every other caller of the parser gets for an empty or prose
    reply is unchanged, key for key."""
    from integrations.vlm.local_loop import _parse_vlm_response
    from integrations.vlm.parser import parse_vlm_action
    from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
    empty = {'Next Action': 'None', 'Status': 'DONE',
             'Reasoning': 'Empty VLM response'}
    prose = dict(empty, Reasoning=PROSE)
    for shape in ('action_json', 'som_bbox'):
        assert parse_vlm_action(
            '', expected_shape=shape).to_action_json_dict() == empty
        assert parse_vlm_action(
            PROSE, expected_shape=shape).to_action_json_dict() == prose
    assert Qwen3VLBackend._parse_unified_response(None, '') == dict(
        empty, UI_Elements=[])
    assert Qwen3VLBackend._parse_unified_response(None, PROSE) == dict(
        prose, UI_Elements=[])
    for raw, want in (('', empty), (None, empty), (PROSE, prose)):
        got = _parse_vlm_response(raw)
        got.pop('_unparsed', None)
        assert got == want


def test_a_run_stopped_before_its_first_action_leaves_it(
        drive, vision, ledger, owner_returns, monkeypatch):
    """Not finished: Stop pressed while the first reply was awaited."""
    from integrations.vlm import local_loop
    _paused_run(drive, owner_returns)
    asked = {'n': 0}

    def _stop(_user, _prompt):
        asked['n'] += 1
        return asked['n'] >= 2       # not at the top of the step: before it acts
    monkeypatch.setattr(local_loop, '_is_stop_requested', _stop)
    out, steps, _ran, _b = drive([_say('left_click', coordinate=[1, 1])],
                                 RESULTS, user_id='u-news', prompt_id='p-news',
                                 action_id=2)
    assert out['exit_reason'] == 'stopped' and not steps
    monkeypatch.setattr(local_loop, '_is_stop_requested', lambda *_a: False)
    assert not any(_resumed_by(ledger)), _resumed_by(ledger)
    assert 'paused for the owner' in _first_prompt(drive)


def test_a_pause_on_an_earlier_day_is_said_with_its_date(
        drive, vision, ledger, owner_returns):
    """The note said "paused for the owner at 13:23:59" for a pause three
    days old.  The paused run's own stamp dates it."""
    from datetime import datetime, timedelta
    from agent_ledger import TaskStatus
    _paused_run(drive, owner_returns)
    task = [t for t in ledger.get_tasks_by_status(TaskStatus.USER_STOPPED)
            if t.task_id.startswith('computer_use_')][0]
    then = datetime.now() - timedelta(days=3)
    task.updated_at = then.isoformat()
    first = _first_prompt(drive)
    assert ('This step was paused for the owner on %s at %s after 2 step(s).'
            % (then.strftime('%Y-%m-%d'), then.strftime('%H:%M:%S'))) in first, (
        first[:600])


def test_a_pause_today_is_said_with_its_time(
        drive, vision, ledger, owner_returns):
    import re
    _paused_run(drive, owner_returns)
    first = _first_prompt(drive)
    assert re.search(r'This step was paused for the owner at \d\d:\d\d:\d\d '
                     r'after 2 step\(s\)\.', first), first[:600]


def test_the_paused_run_names_its_action_in_the_ledger(
        drive, vision, ledger, owner_returns):
    from agent_ledger import TaskStatus
    _paused_run(drive, owner_returns)
    paused = [t for t in ledger.get_tasks_by_status(TaskStatus.USER_STOPPED)
              if t.task_id.startswith('computer_use_')]
    assert [str(t.recipe_action_id) for t in paused] == ['2']
