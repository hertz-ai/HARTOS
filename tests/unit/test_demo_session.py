"""A person shows an agent how to do something: the demonstration recorder.

Owner 2026-11-11 (to claude-autostart-cd): "so you can start and complete it",
agreed on 11-10 as: a consented, time-boxed demo session; the same
computer-use ribbon with a recording symbol; an obvious stop; no key content
kept; the existing resource_governor hooks extended, no second listener; local
only.  The recording becomes WORDS the existing computer-use loop and CREATE's
gather step already consume.  There is no replay path and no stored geometry
(integrations/vlm/local_loop.py re-grounds every step from a fresh screenshot),
so a step keeps the window, the element and the action, and a position only as
a tiebreak that never reaches the words.

The OS-touching pieces (which window a point is in, which control has the
focus) are replaced here; the hook itself is tested in
test_demo_recording_hook.py, and the real pieces are proven live.

Run: python -m pytest tests/unit/test_demo_session.py -q
"""
import threading
import time
from types import SimpleNamespace

import pytest

from core import resource_governor as rg
from integrations.vlm import activity_stream, local_loop
from integrations.vlm import demo_session as ds

LEFT_DOWN, RIGHT_DOWN, WHEEL = 0x201, 0x204, 0x20A
USER, PROMPT = 'owner-1', '555'

NOTEPAD = {'hwnd': 5, 'title': 'Untitled - Notepad', 'process': 'notepad.exe'}
FILE_MENU = {'role': 'menu item', 'name': 'File'}
BANK = {'hwnd': 6, 'title': 'My Online Banking - Chrome', 'process': 'chrome.exe'}
VAULT = {'hwnd': 7, 'title': 'Vault', 'process': 'bitwarden.exe'}


class _MonitorStub:
    """What core.resource_governor's one monitor offers a sink."""

    def __init__(self, world):
        self.world, self.sink = world, None

    def snapshot(self, start=True):
        return (0, None) if self.world.monitor_available else None

    def attach_sink(self, sink):
        if self.sink is not None:
            return False
        self.world.log.append('attach')
        self.sink = sink
        return True

    def detach_sink(self, sink):
        if self.sink is sink:
            self.world.log.append('detach')
            self.sink = None


@pytest.fixture
def world(monkeypatch):
    w = SimpleNamespace(
        log=[], ribbon=[], ribbon_ok=True, monitor_available=True,
        point={'window': NOTEPAD, 'element': FILE_MENU},
        focus={'window': NOTEPAD, 'element': {'role': 'text field', 'name': 'Text Editor'}},
        protected=False, protected_asked=[])
    w.monitor = _MonitorStub(w)

    def ribbon(show, text=None):
        w.log.append('ribbon-on' if show else 'ribbon-off')
        w.ribbon.append((show, text))
        return w.ribbon_ok

    def protected(hwnd):
        w.protected_asked.append(hwnd)
        return w.protected

    monkeypatch.setattr(ds, '_on_windows', lambda: True)
    monkeypatch.setattr(rg, '_physical_input_monitor', w.monitor)
    monkeypatch.setattr(ds, 'show_ribbon', ribbon)
    monkeypatch.setattr(ds, 'RIBBON_KEEPALIVE_SECONDS', 0.03)
    monkeypatch.setattr(ds, '_describe_point', lambda x, y: w.point)
    monkeypatch.setattr(ds, '_describe_focus', lambda hwnd: w.focus)
    monkeypatch.setattr(ds, '_msaa_focus_is_protected', protected)
    monkeypatch.setattr(ds, '_finished', {}, raising=False)   # no result survives into the next test
    yield w
    session = ds.active_session()
    if session is not None:
        session.stop('test cleanup')


def _click(session, t=1.0, x=4711, y=8150, message=LEFT_DOWN):
    session.feed((t, 'mouse', message, (x, y, 0)))


def _key(session, token, t=1.0, focus=(9, False)):
    session.feed((t, 'keyboard', 0x100, (token, focus[0], focus[1])))


def _wait(predicate, seconds=2.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def session(world):
    return ds.begin(USER, PROMPT)


# ── Starting: every refusal says what is wrong and what to do ─────────────

def test_it_refuses_off_windows_and_says_so(world, monkeypatch):
    monkeypatch.setattr(ds, '_on_windows', lambda: False)
    with pytest.raises(ds.DemoRefused) as caught:
        ds.begin(USER, PROMPT)
    assert 'Windows' in caught.value.problem and caught.value.fix
    assert world.log == []


def test_it_refuses_when_the_input_monitor_is_unavailable(world):
    world.monitor_available = False
    with pytest.raises(ds.DemoRefused) as caught:
        ds.begin(USER, PROMPT)
    assert 'input' in caught.value.problem.lower() and caught.value.fix
    assert 'ribbon-on' not in world.log


def test_it_refuses_when_the_ribbon_cannot_show_and_says_why(world):
    world.ribbon_ok = False
    with pytest.raises(ds.DemoRefused) as caught:
        ds.begin(USER, PROMPT)
    assert ('the ribbon is not available in this build, so a recording cannot '
            'show you it is running') in caught.value.problem
    assert 'attach' not in world.log, 'nothing may be captured without the ribbon up'
    assert world.monitor.sink is None
    assert ds.active_session() is None


def test_the_ribbon_is_up_before_anything_is_captured(world, session):
    assert world.log.index('ribbon-on') < world.log.index('attach')
    shown = [text for show, text in world.ribbon if show]
    assert '●' in shown[0] and 'REC' in shown[0]


def test_only_one_recording_at_a_time(world, session):
    before = list(world.log)
    with pytest.raises(ds.DemoRefused) as caught:
        ds.begin('someone-else', '1')
    assert 'a demonstration is already being recorded' in caught.value.problem
    assert world.log == before, (
        'the second request must not touch the ribbon or the monitor at all')


def test_the_time_box_is_bounded(world):
    s = ds.begin(USER, PROMPT, max_seconds=10 ** 6)
    assert s.max_seconds == ds.HARD_MAX_SECONDS
    s.stop('x')
    s = ds.begin(USER, PROMPT, max_seconds=-5)
    assert s.max_seconds == ds.DEFAULT_MAX_SECONDS


# ── The ribbon's own Stop button ends it ──────────────────────────────────

def test_the_stop_registry_lists_the_demo_so_the_ribbon_stop_reaches_it(world, session):
    assert (USER, session.id) in local_loop.list_active_sessions()
    assert local_loop.request_stop(USER, session.id) is True
    assert _wait(lambda: session.ended_by is not None)
    assert session.ended_by == 'stop button'
    assert (USER, session.id) not in local_loop.list_active_sessions()


def test_a_demo_never_clears_a_live_loops_stop_flag(world):
    local_loop._register_session(USER, '777')
    try:
        local_loop.request_stop(USER, '777')
        assert local_loop.register_stop_session(USER, '777') is None
        assert local_loop._is_stop_requested(USER, '777') is True
    finally:
        local_loop._unregister_session(USER, '777')


# ── Steps ─────────────────────────────────────────────────────────────────

def test_a_click_is_a_step_naming_window_element_and_action(world, session):
    _click(session)
    (step,) = session.steps
    assert step['action'] == 'left_click'
    assert step['window'] == NOTEPAD['title'] and step['app'] == 'notepad.exe'
    assert step['element'] == FILE_MENU


def test_two_quick_clicks_in_one_place_are_a_double_click(world, session):
    _click(session, t=1.0, x=100, y=100)
    _click(session, t=1.3, x=102, y=101)
    assert [s['action'] for s in session.steps] == ['double_click']


def test_slow_or_distant_clicks_are_two_clicks(world, session):
    _click(session, t=1.0, x=100, y=100)
    _click(session, t=2.5, x=100, y=100)
    _click(session, t=2.6, x=400, y=400)
    assert [s['action'] for s in session.steps] == ['left_click'] * 3


def test_a_right_click_is_its_own_action(world, session):
    _click(session, message=RIGHT_DOWN)
    assert session.steps[0]['action'] == 'right_click'


def test_scrolling_the_same_way_merges_into_one_step(world, session):
    for t in (1.0, 1.2, 1.4):
        session.feed((t, 'mouse', WHEEL, (5, 5, -120)))
    session.feed((1.6, 'mouse', WHEEL, (5, 5, 120)))
    assert [(s['action'], s.get('times')) for s in session.steps] == [
        ('scroll_down', 3), ('scroll_up', 1)]


def test_every_step_uses_the_action_names_the_loop_already_has(world, session):
    _click(session, t=1.0)
    _click(session, t=5.0, message=RIGHT_DOWN)
    session.feed((6.0, 'mouse', WHEEL, (5, 5, -120)))
    _key(session, 'char', t=7.0)
    _key(session, 'key:enter', t=8.0)
    _key(session, 'hotkey:ctrl+c', t=9.0)
    actions = {s['action'] for s in session.steps if 'action' in s}
    assert actions and actions <= set(activity_stream._ACTION_LABELS)


# ── What was typed is never in a step ─────────────────────────────────────

def test_a_typed_run_is_a_count_not_a_text(world, session):
    for i in range(5):
        _key(session, 'char', t=1.0 + i * 0.1, focus=(9, False))
    _key(session, 'key:enter', t=3.0)
    typed = [s for s in session.steps if s['action'] == 'type']
    assert len(typed) == 1
    assert typed[0]['chars'] == 5 and typed[0]['secret'] is False
    assert 'text' not in typed[0] and 'value' not in typed[0]


def test_a_native_password_field_makes_the_run_a_secret(world, session):
    _key(session, 'char', focus=(9, True))
    _key(session, 'key:enter', t=2.0)
    assert session.steps[0]['secret'] is True
    assert world.protected_asked == [], 'a native answer needs no second opinion'


def test_a_native_text_field_is_not_a_secret_and_is_not_second_guessed(world, session):
    _key(session, 'char', focus=(9, False))
    _key(session, 'key:enter', t=2.0)
    assert session.steps[0]['secret'] is False
    assert world.protected_asked == []


def test_a_browser_field_is_asked_about_and_a_protected_one_is_a_secret(world, session):
    world.protected = True
    _key(session, 'char', focus=(9, None))
    _key(session, 'key:enter', t=2.0)
    assert session.steps[0]['secret'] is True and world.protected_asked == [9]


def test_a_control_that_cannot_be_identified_is_treated_as_a_secret(world, session):
    world.protected = None          # unknown
    _key(session, 'char', focus=(9, None))
    _key(session, 'key:enter', t=2.0)
    assert session.steps[0]['secret'] is True


def test_a_failing_check_is_treated_as_a_secret(world, session, monkeypatch):
    def boom(hwnd):
        raise OSError('accessibility not available')
    monkeypatch.setattr(ds, '_msaa_focus_is_protected', boom)
    _key(session, 'char', focus=(9, None))
    _key(session, 'key:enter', t=2.0)
    assert session.steps[0]['secret'] is True


def test_one_secret_keystroke_makes_the_whole_run_a_secret(world, session):
    _key(session, 'char', t=1.0, focus=(9, False))
    _key(session, 'char', t=1.1, focus=(9, True))
    _key(session, 'char', t=1.2, focus=(9, False))
    _key(session, 'key:enter', t=3.0)
    assert [s['secret'] for s in session.steps if s['action'] == 'type'] == [True]


def test_a_change_of_focus_ends_the_typed_run(world, session):
    _key(session, 'char', t=1.0, focus=(9, False))
    _key(session, 'char', t=1.1, focus=(10, False))
    _key(session, 'key:enter', t=3.0)
    assert [s['chars'] for s in session.steps if s['action'] == 'type'] == [1, 1]


def test_a_hotkey_and_a_named_key_end_the_typed_run_first(world, session):
    _key(session, 'char', t=1.0)
    _key(session, 'hotkey:ctrl+c', t=1.1)
    _key(session, 'key:tab', t=1.2)
    assert [(s['action'], s.get('value')) for s in session.steps] == [
        ('type', None), ('hotkey', 'ctrl+c'), ('key', 'tab')]


# ── Sensitive windows are never recorded ──────────────────────────────────

def test_a_click_in_a_banking_window_is_dropped_with_one_note(world, session):
    world.point = {'window': BANK, 'element': FILE_MENU}
    _click(session, t=1.0)
    _click(session, t=5.0)
    assert [s for s in session.steps if 'action' in s] == []
    notes = [s for s in session.steps if 'note' in s]
    assert len(notes) == 1
    assert BANK['title'] not in str(notes), 'the title of a sensitive window is not kept'


def test_typing_in_a_password_manager_is_dropped(world, session):
    world.focus = {'window': VAULT, 'element': None}
    _key(session, 'char', focus=(9, None))
    _key(session, 'key:enter', t=2.0)
    assert [s for s in session.steps if 'action' in s] == []
    assert world.protected_asked == [], 'no question is asked about a sensitive window'


def test_recording_resumes_after_the_sensitive_window(world, session):
    world.point = {'window': BANK, 'element': FILE_MENU}
    _click(session, t=1.0)
    world.point = {'window': NOTEPAD, 'element': FILE_MENU}
    _click(session, t=5.0)
    assert [s['action'] for s in session.steps if 'action' in s] == ['left_click']
    assert len([s for s in session.steps if 'note' in s]) == 1


def test_a_window_that_cannot_be_identified_is_not_recorded(world, session):
    world.point = None
    _click(session)
    assert [s for s in session.steps if 'action' in s] == []
    assert any('note' in s for s in session.steps)


# ── The words ─────────────────────────────────────────────────────────────

def test_the_transcript_is_numbered_words_without_positions(world, session):
    _click(session, t=1.0, x=4711, y=8150)
    for i in range(3):
        _key(session, 'char', t=2.0 + i * 0.1)
    _key(session, 'hotkey:ctrl+s', t=4.0)
    result = session.stop('person')
    text = result.transcript
    assert '1.' in text and '2.' in text and '3.' in text
    assert 'notepad.exe' in text and 'Untitled - Notepad' in text
    assert 'File' in text and 'menu item' in text
    assert 'Ctrl+S' in text
    assert 'value not recorded' in text
    assert '4711' not in text and '8150' not in text


def test_a_secret_is_described_as_a_secret_and_says_how_replay_gets_it(world, session):
    _key(session, 'char', focus=(9, True))
    _key(session, 'key:enter', t=2.0)
    text = session.stop('person').transcript
    assert 'secret' in text.lower() and 'vault' in text.lower()


def test_an_empty_demonstration_says_nothing_was_recorded(world, session):
    assert 'nothing' in session.stop('person').transcript.lower()


# ── Ending ────────────────────────────────────────────────────────────────

def test_stop_takes_the_ribbon_down_detaches_and_unregisters(world, session):
    result = session.stop('person')
    assert result.ended_by == 'person'
    assert 'detach' in world.log and 'ribbon-off' in world.log
    assert world.log.index('detach') < world.log.index('ribbon-off'), (
        'capture stops first; the ribbon only comes down once nothing is recorded')
    assert world.monitor.sink is None
    assert (USER, session.id) not in local_loop.list_active_sessions()
    assert ds.active_session() is None


def test_stop_is_idempotent_and_keeps_the_first_reason(world, session):
    first = session.stop('person')
    again = session.stop('time box')
    assert again.ended_by == first.ended_by == 'person'


def test_events_after_stop_are_ignored(world, session):
    session.stop('person')
    _click(session)
    assert session.steps == []


def test_the_time_box_ends_it_by_itself(world):
    s = ds.begin(USER, PROMPT, max_seconds=0.15)
    assert _wait(lambda: s.ended_by is not None)
    assert s.ended_by == 'time box'
    assert world.monitor.sink is None
    assert world.ribbon[-1][0] is False


def test_a_lost_ribbon_ends_it_because_it_can_no_longer_show_it_is_running(world):
    s = ds.begin(USER, PROMPT, max_seconds=30)
    world.ribbon_ok = False
    assert _wait(lambda: s.ended_by is not None)
    assert s.ended_by == 'ribbon lost'
    assert world.monitor.sink is None


def test_the_ribbon_is_kept_alive_while_it_runs(world, session):
    assert _wait(lambda: sum(1 for show, _ in world.ribbon if show) >= 3)


def test_events_through_the_real_sink_become_steps(world, session):
    world.monitor.sink((1.0, 'mouse', LEFT_DOWN, (1, 2, 0)))
    assert session.settle(2.0)
    assert [s['action'] for s in session.steps] == ['left_click']


def test_a_consumer_failure_does_not_stop_the_recording(world, session, monkeypatch):
    calls = {'n': 0}
    real = ds._describe_point

    def flaky(x, y):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('accessibility hiccup')
        return real(x, y)
    monkeypatch.setattr(ds, '_describe_point', flaky)
    _click(session, t=1.0)
    _click(session, t=5.0)
    assert [s['action'] for s in session.steps if 'action' in s] == ['left_click']


# ── F1: a focus move inside one window must not reuse the last answer ─────
# (review, claude-autostart-77): a browser's username and password fields are
# ONE top-level window and both read native=None, so a cache keyed by
# (hwnd, native) carried "not a secret" from the username into the password.

def test_tab_from_a_username_to_a_password_field_is_not_a_stale_answer(world, session):
    world.protected = False
    _key(session, 'char', t=1.0, focus=(9, None))          # the username
    _key(session, 'key:tab', t=1.1)
    world.protected = True                                  # now the password field
    _key(session, 'char', t=1.2, focus=(9, None))          # within 0.25 s of the first
    _key(session, 'key:enter', t=3.0)
    assert [s['secret'] for s in session.steps if s['action'] == 'type'] == [False, True]
    assert world.protected_asked == [9, 9]


def test_a_click_into_another_field_is_not_a_stale_answer_either(world, session):
    world.protected = False
    _key(session, 'char', t=1.0, focus=(9, None))
    _click(session, t=1.05)
    world.protected = True
    _key(session, 'char', t=1.1, focus=(9, None))
    _key(session, 'key:enter', t=3.0)
    assert [s['secret'] for s in session.steps if s['action'] == 'type'] == [False, True]


def test_typing_within_one_field_still_asks_accessibility_only_once(world, session):
    """The 49 ms call stays off the per-keystroke path."""
    for i in range(6):
        _key(session, 'char', t=1.0 + i * 0.03, focus=(9, None))
    _key(session, 'key:enter', t=3.0)
    assert world.protected_asked == [9]


# ── F2: Stop never waits on another process ───────────────────────────────
# (review): feed() held the session lock across COM calls into other
# processes, and stop() takes that lock, so a wedged application made the
# person's Stop wait on it.

@pytest.mark.parametrize('blocker', ['point', 'protected'])
def test_stop_does_not_wait_for_an_application_that_has_stopped_answering(
        world, session, monkeypatch, blocker):
    entered, release = threading.Event(), threading.Event()

    def wedged(*args):
        entered.set()
        release.wait(10)
        return world.point if blocker == 'point' else False
    if blocker == 'point':
        monkeypatch.setattr(ds, '_describe_point', wedged)
        event = (1.0, 'mouse', LEFT_DOWN, (1, 2, 0))
    else:
        monkeypatch.setattr(ds, '_msaa_focus_is_protected', wedged)
        event = (1.0, 'keyboard', 0x100, ('char', 9, None))
    worker = threading.Thread(target=session.feed, args=(event,))
    worker.start()
    try:
        assert entered.wait(2.0), 'the wedged call was never reached'
        started = time.monotonic()
        result = session.stop('person')
        assert time.monotonic() - started < 2.0, 'Stop waited on the wedged application'
        assert result.ended_by == 'person'
    finally:
        release.set()
        worker.join(5)
    assert [s for s in session.steps if 'action' in s] == [], (
        'an event that finished after Stop must not be recorded')


# ── F3: nothing may flood the model's context ─────────────────────────────

def test_the_recording_stops_taking_steps_at_the_cap_and_says_so(world, session):
    for i in range(ds.MAX_STEPS + 25):
        _click(session, t=1.0 + i * 2, x=10 + i * 50, y=10)
    actions = [s for s in session.steps if 'action' in s]
    assert len(actions) == ds.MAX_STEPS
    assert [s for s in session.steps if 'note' in s and 'limit' in s['note']]


def test_the_words_the_model_receives_say_the_later_steps_were_not_recorded(world, session):
    """Not the data structure: what the model reads.  "not shown" (the transcript
    cap) and "not recorded" (the recording cap) are different facts, and only the
    second loses what the person demonstrated (review, claude-autostart-77)."""
    for i in range(ds.MAX_STEPS + 25):
        _click(session, t=1.0 + i * 2, x=10 + i * 50, y=10)
    lines = session.stop('person').transcript.splitlines()
    assert f'...and {ds.MAX_STEPS - ds.MAX_TRANSCRIPT_STEPS} more steps, not shown' in lines
    assert any('not recorded' in l and str(ds.MAX_STEPS) in l for l in lines), lines[-3:]


def test_a_note_after_the_shown_steps_is_still_told_once(world, session):
    for i in range(ds.MAX_TRANSCRIPT_STEPS + 5):
        _click(session, t=1.0 + i * 2, x=10 + i * 50, y=10)
    world.point = {'window': BANK, 'element': FILE_MENU}
    _click(session, t=900.0)
    _click(session, t=901.0)
    lines = session.stop('person').transcript.splitlines()
    assert sum('sensitive window' in l for l in lines) == 1


def test_the_transcript_is_capped_and_says_how_many_were_left_out(world, session):
    total = ds.MAX_TRANSCRIPT_STEPS + 40
    for i in range(total):
        _click(session, t=1.0 + i * 2, x=10 + i * 50, y=10)
    lines = session.stop('person').transcript.splitlines()
    numbered = [l for l in lines if l[:1].isdigit()]
    assert len(numbered) == ds.MAX_TRANSCRIPT_STEPS
    assert lines[-1] == '...and 40 more steps, not shown'


def test_a_transcript_at_exactly_the_cap_has_no_more_line(world, session):
    for i in range(ds.MAX_TRANSCRIPT_STEPS):
        _click(session, t=1.0 + i * 2, x=10 + i * 50, y=10)
    assert 'not shown' not in session.stop('person').transcript


# ── Other gestures are not recorded ───────────────────────────────────────

@pytest.mark.parametrize('message', [0x207, 0x20B, 0x20E])
def test_a_gesture_with_no_action_name_is_not_a_step(world, session, message):
    session.feed((1.0, 'mouse', message, (5, 5, 0)))
    assert [s for s in session.steps if 'action' in s] == []


# ── The one process-name implementation ───────────────────────────────────

@pytest.mark.skipif(not ds._on_windows(), reason='Windows process names')
def test_the_process_name_comes_from_window_capture_not_a_second_copy():
    import os
    from integrations.remote_desktop import window_capture
    assert window_capture.process_name_for_pid(os.getpid()).lower().startswith('python')
    assert window_capture.process_name_for_pid(0) == ''
    assert ds._process_name(os.getpid()).lower().startswith('python')


def test_the_enumerator_method_calls_the_public_function(monkeypatch):
    from integrations.remote_desktop import window_capture
    monkeypatch.setattr(window_capture, 'process_name_for_pid', lambda pid: f'p{pid}.exe')
    assert window_capture.WindowEnumerator()._get_process_name_win32(7) == 'p7.exe'


def test_the_recorder_uses_the_public_function(monkeypatch):
    from integrations.remote_desktop import window_capture
    monkeypatch.setattr(window_capture, 'process_name_for_pid', lambda pid: f'q{pid}.exe')
    assert ds._process_name(9) == 'q9.exe'


# ── The element words ─────────────────────────────────────────────────────

@pytest.mark.parametrize('role,word', [(43, 'button'), (12, 'menu item'),
                                       (30, 'link'), (42, 'text field')])
def test_accessible_roles_become_words(role, word):
    assert ds._role_word(role) == word


def test_an_unknown_role_is_just_a_control():
    assert ds._role_word(9999) == 'control'


# ── The ribbon says whether it showed ─────────────────────────────────────

class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def _ribbon_with(monkeypatch, bundled=True, answer=None, raises=None):
    monkeypatch.setattr('core.config_cache.is_bundled', lambda: bundled)
    monkeypatch.setattr('core.config_cache._local_base', lambda: 'http://127.0.0.1:5000')

    def get(url, timeout=None, **kw):
        if raises:
            raise raises
        return answer
    monkeypatch.setattr('core.http_pool.pooled_get', get)


def test_the_ribbon_reports_true_only_when_nunba_says_it_is_showing(monkeypatch):
    _ribbon_with(monkeypatch, answer=_Resp(200, {'success': True, 'status': 'showing'}))
    assert activity_stream._ribbon(True, 'x') is True


@pytest.mark.parametrize('answer', [
    _Resp(200, {'success': False, 'error': 'Indicator module not available'}),
    _Resp(500, {'success': True}), _Resp(200, None), None])
def test_the_ribbon_reports_false_when_it_did_not_show(monkeypatch, answer):
    _ribbon_with(monkeypatch, answer=answer)
    assert activity_stream._ribbon(True, 'x') is False


def test_the_ribbon_reports_false_outside_nunba_and_on_an_error(monkeypatch):
    _ribbon_with(monkeypatch, bundled=False)
    assert activity_stream._ribbon(True, 'x') is False
    _ribbon_with(monkeypatch, raises=OSError('refused'))
    assert activity_stream._ribbon(True, 'x') is False


def test_show_ribbon_is_the_same_ribbon(monkeypatch):
    seen = []
    monkeypatch.setattr(activity_stream, '_ribbon',
                        lambda show, text=None: seen.append((show, text)) or True)
    assert activity_stream.show_ribbon(True, 'hello') is True
    assert seen == [(True, 'hello')]


# ── The tools an agent calls ──────────────────────────────────────────────

def _tools():
    from core.agent_tools import build_core_tool_closures
    ctx = {
        'user_id': USER, 'prompt_id': PROMPT, 'agent_data': {},
        'helper_fun': SimpleNamespace(), 'user_prompt': f'{USER}_{PROMPT}',
        'request_id_list': {}, 'recent_file_id': None, 'scheduler': None,
        'send_message_to_user1': lambda *a, **k: None,
        'retrieve_json': lambda s: s, 'strip_json_values': lambda d: d,
        'save_conversation_db': lambda *a, **k: None}
    return {name: fn for name, _d, fn in build_core_tool_closures(ctx)}


@pytest.fixture
def turn(monkeypatch):
    """The running turn's id and the desktop owner, as the tools read them."""
    import core.agent_tools as agent_tools
    state = SimpleNamespace(rid='1731330000000')
    monkeypatch.setattr(agent_tools, 'camera_turn_id', lambda: state.rid)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', USER)
    return state


def test_the_tools_exist_and_are_not_on_the_main_legs_always_on_menu():
    from core.agent_tool_menu import MAIN_LEG_CORE_TOOLS
    tools = _tools()
    assert {'start_demonstration', 'stop_demonstration'} <= set(tools)
    assert not {'start_demonstration', 'stop_demonstration'} & set(MAIN_LEG_CORE_TOOLS)


def test_a_person_who_owns_the_desktop_can_start_and_stop(world, turn):
    tools = _tools()
    started = tools['start_demonstration']('how I file my expenses')
    assert 'REC' in started or 'recording' in started.lower()
    assert 'captions' in started, 'the tool must not let the model promise captions it cannot give'
    assert ds.active_session() is not None
    _click(ds.active_session())
    done = tools['stop_demonstration']()
    assert 'Untitled - Notepad' in done and ds.active_session() is None


@pytest.mark.parametrize('rid', ['daemon_goal-1', 'daemon_a2a_task-9', ''])
def test_a_background_or_peer_turn_cannot_start_a_recording(world, turn, rid):
    turn.rid = rid
    reply = _tools()['start_demonstration']('anything')
    assert reply.startswith('Error: the start_demonstration call could not be done: ')
    assert ds.active_session() is None and world.log == []


def test_a_user_who_is_not_the_desktop_owner_cannot_record_the_owners_input(
        world, turn, monkeypatch):
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'someone-else')
    reply = _tools()['start_demonstration']('anything')
    assert reply.startswith('Error: the start_demonstration call could not be done: ')
    assert 'owner' in reply and ds.active_session() is None


def test_with_no_desktop_owner_it_refuses(world, turn, monkeypatch):
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)
    assert _tools()['start_demonstration']('x').startswith('Error: ')
    assert ds.active_session() is None


def test_a_refusal_from_the_recorder_reaches_the_model_with_its_fix(world, turn):
    world.ribbon_ok = False
    reply = _tools()['start_demonstration']('x')
    assert 'the ribbon is not available in this build' in reply
    assert 'Error: the start_demonstration call could not be done: ' in reply


@pytest.mark.parametrize('rid', ['daemon_goal-1', 'daemon_a2a_task-9', ''])
def test_a_background_turn_cannot_read_or_end_a_recording(world, turn, rid):
    started = ds.begin(USER, PROMPT)
    _click(started)
    turn.rid = rid
    reply = _tools()['stop_demonstration']()
    assert reply.startswith('Error: the stop_demonstration call could not be done: ')
    assert 'Notepad' not in reply, 'the owner\'s steps must not reach a background turn'
    assert ds.active_session() is started and started.ended_by is None


def test_stopping_when_nothing_is_recording_says_what_to_do(world, turn):
    reply = _tools()['stop_demonstration']()
    assert reply.startswith('Error: the stop_demonstration call could not be done: ')
    assert 'start_demonstration' in reply


# ── The person's own Stop (or the time box) must not lose what they showed ──
# The reviewer of 63abdc857 measured it: the ribbon text and the start tool both
# tell the person to press Stop AI control, which ends the session; the agent's
# next stop_demonstration then found no active session, refused, and the steps
# never reached the agent.

@pytest.mark.parametrize('why', ['stop button', 'time box', 'ribbon lost'])
def test_what_the_person_showed_reaches_the_agent_after_the_recording_ended_itself(
        world, turn, why):
    tools = _tools()
    tools['start_demonstration']('how I file my expenses')
    _click(ds.active_session())
    ds.active_session().stop(why)                       # the ribbon, the clock, or a lost ribbon
    assert ds.active_session() is None
    reply = tools['stop_demonstration']()
    assert not reply.startswith('Error'), reply
    assert 'Untitled - Notepad' in reply, 'the steps the person showed must come back'
    assert 'Nothing was recorded' not in reply


def test_the_reply_says_how_the_recording_ended(world, turn):
    tools = _tools()
    tools['start_demonstration']('x')
    _click(ds.active_session())
    ds.active_session().stop('stop button')
    assert 'Stop AI control' in tools['stop_demonstration']()
    tools['start_demonstration']('x')
    _click(ds.active_session())
    ds.active_session().stop('time box')
    assert 'time' in tools['stop_demonstration']().lower()


def test_a_finished_recording_is_handed_over_once(world, turn):
    tools = _tools()
    tools['start_demonstration']('x')
    _click(ds.active_session())
    ds.active_session().stop('stop button')
    assert 'Untitled - Notepad' in tools['stop_demonstration']()
    again = tools['stop_demonstration']()
    assert again.startswith('Error: the stop_demonstration call could not be done: ')
    assert 'Notepad' not in again, 'a second read must not hand the same steps out again'


def test_the_agents_own_stop_is_not_kept_for_a_second_read(world, turn):
    tools = _tools()
    tools['start_demonstration']('x')
    _click(ds.active_session())
    assert 'Untitled - Notepad' in tools['stop_demonstration']()
    assert tools['stop_demonstration']().startswith('Error: ')


def test_a_finished_recording_is_not_offered_after_its_time_is_up(world, turn, monkeypatch):
    tools = _tools()
    tools['start_demonstration']('x')
    _click(ds.active_session())
    ds.active_session().stop('stop button')
    monkeypatch.setattr(ds, '_FINISHED_SECONDS', 0.0)
    time.sleep(0.02)
    reply = tools['stop_demonstration']()
    assert reply.startswith('Error: ') and 'Notepad' not in reply


def test_a_new_recording_drops_a_stale_finished_one(world, turn):
    tools = _tools()
    tools['start_demonstration']('first')
    _click(ds.active_session())
    ds.active_session().stop('stop button')
    tools['start_demonstration']('second')
    ds.active_session().stop('person')
    assert ds.take_finished(USER) is None, 'the first recording must not be offered after a second began'


@pytest.mark.parametrize('rid', ['daemon_goal-1', 'daemon_a2a_task-9', ''])
def test_a_background_turn_cannot_read_a_finished_recording_and_does_not_consume_it(
        world, turn, rid):
    tools = _tools()
    tools['start_demonstration']('x')
    _click(ds.active_session())
    ds.active_session().stop('stop button')
    turn.rid = rid
    reply = tools['stop_demonstration']()
    assert reply.startswith('Error: the stop_demonstration call could not be done: ')
    assert 'Notepad' not in reply
    turn.rid = '1731330000000'                                    # the person's own turn again
    assert 'Untitled - Notepad' in tools['stop_demonstration'](), \
        'a refused background read must leave the steps for the person\'s turn'


def test_the_stored_copy_is_discarded_when_the_agents_stop_loses_a_race(world, turn, monkeypatch):
    """The ribbon's Stop can land between the tool finding the session and
    stopping it: stop('person') then returns the result the ribbon's stop made,
    and the copy kept for a later read must not hand the same steps out again."""
    tools = _tools()
    tools['start_demonstration']('x')
    racing = ds.active_session()
    _click(racing)
    racing.stop('stop button')                           # the ribbon won
    monkeypatch.setattr(ds, 'active_session', lambda: racing)   # the tool had already found it
    assert 'Untitled - Notepad' in tools['stop_demonstration']()
    assert ds.take_finished(USER) is None, 'the same steps must not be offered a second time'


def test_the_reply_says_how_it_ended_when_the_ribbon_wins_the_race(world, turn, monkeypatch):
    """Review of 9c7fc3072, should-fix 3: when the ribbon's Stop lands between the
    tool finding the session and stopping it, the result is the ribbon's, and the
    reply must say the person pressed Stop like it does on the other path."""
    tools = _tools()
    tools['start_demonstration']('x')
    racing = ds.active_session()
    _click(racing)
    racing.stop('stop button')
    monkeypatch.setattr(ds, 'active_session', lambda: racing)
    assert 'Stop AI control' in tools['stop_demonstration']()


def test_the_agents_own_stop_does_not_say_the_person_or_the_clock_ended_it(world, turn):
    tools = _tools()
    tools['start_demonstration']('x')
    _click(ds.active_session())
    reply = tools['stop_demonstration']()
    assert 'Untitled - Notepad' in reply
    for words in ('Stop AI control', 'time limit', 'ribbon', 'on its own'):
        assert words not in reply, f'the agent ended it itself, so the reply must not say: {words}'


def test_how_a_recording_ended_is_told_from_one_table(world, turn, monkeypatch):
    """Review of 9c7fc3072, should-fix 4: the words for each ending live in one
    place next to where the endings are raised, not as a second copy in the tool."""
    assert {'stop button', 'time box', 'ribbon lost'} <= set(ds.ENDED_BY_TOLD)
    monkeypatch.setitem(ds.ENDED_BY_TOLD, 'time box', ', MARKER-FROM-THE-TABLE')
    tools = _tools()
    tools['start_demonstration']('x')
    _click(ds.active_session())
    ds.active_session().stop('time box')
    assert 'MARKER-FROM-THE-TABLE' in tools['stop_demonstration']()


def test_a_stop_in_progress_is_never_seen_as_no_recording_at_all(world, turn, monkeypatch):
    """Review of 9c7fc3072, should-fix 1: stop() set ended_by, then took the module
    lock to store the result.  A stop_demonstration landing in between found no
    active session and no stored result, was refused ('call start_demonstration
    first'), and the next start_demonstration dropped the steps.  Held here with a
    gate on the lock: the stopping thread waits at the module lock, a
    stop_demonstration arrives, and it must get the steps."""
    tools = _tools()
    tools['start_demonstration']('x')
    racing = ds.active_session()
    _click(racing)
    real = ds._active_lock
    at_gate, go, who = threading.Event(), threading.Event(), {}

    class GatedLock:
        def __enter__(self):
            if threading.current_thread() is who.get('stopper') and not at_gate.is_set():
                at_gate.set()
                assert go.wait(10), 'the test never released the gate'
            real.acquire()

        def __exit__(self, *exc):
            real.release()

    monkeypatch.setattr(ds, '_active_lock', GatedLock())
    stopper = threading.Thread(target=lambda: racing.stop('stop button'))
    who['stopper'] = stopper
    stopper.start()
    assert at_gate.wait(10), 'stop() never reached the module lock'
    out = {}
    caller = threading.Thread(target=lambda: out.update(reply=tools['stop_demonstration']()))
    caller.start()
    caller.join(0.5)               # a refusal comes straight back; a correct reply waits for the stop
    go.set()
    stopper.join(10)
    caller.join(10)
    assert 'reply' in out and not out['reply'].startswith('Error'), out
    assert 'Untitled - Notepad' in out['reply'], 'the steps the person showed were lost in the window'


def test_a_finished_recording_is_kept_for_its_own_user_only(world, turn):
    ds.begin(USER, PROMPT)
    _click(ds.active_session())
    ds.active_session().stop('stop button')
    assert ds.take_finished('someone-else') is None
    assert ds.take_finished(USER) is not None


def test_the_start_reply_says_where_the_words_go(world, turn):
    """'Local only' was overstated: the recorder keeps nothing and sends nothing
    itself, but the words it returns go to the model running the turn."""
    started = _tools()['start_demonstration']('x')
    assert 'model' in started.lower()
    assert 'local only' not in started.lower()


def test_a_session_survives_a_tool_call_on_another_thread(world, turn):
    """The tools run on autogen's worker threads; the recording must not be
    tied to the thread that started it."""
    _tools()['start_demonstration']('x')
    out = {}
    t = threading.Thread(target=lambda: out.update(s=ds.active_session()))
    t.start()
    t.join()
    assert out['s'] is not None


def test_the_sensitive_window_decision_is_made_once_per_event(world, session, monkeypatch):
    calls = []
    real = ds.DemoSession._blocked

    def counting(info):
        calls.append(1)
        return real(info)
    monkeypatch.setattr(ds.DemoSession, '_blocked', staticmethod(counting))
    _key(session, 'char', t=1.0, focus=(9, False))
    _click(session, t=2.0)
    assert len(calls) == 2, 'one decision per event, not one per use'
