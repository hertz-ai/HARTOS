"""A computer-use step is judged by what it achieved, and a repeat that can
only do the same thing again does not run.

The owner's design (2026-11-21): "Act, get feedback on all modalities from
environment where action is performed and proceed based on expected outcome;
if outcome met move on towards the goal via next action", the screen read
through the existing VisionService.  The loop asked the model about the last
step only, showed a click's receipt as "the REAL result -- trust it over the
screenshot", carried no expected outcome and had no repeat guard.  Live
2026-11-21, agent 15004929489, nobody at the desk:

* 13:08:31-13:22:02, one run, 30 steps: a taskbar click, then
  `start edge https://news.ycombinator.com/` (timed out), fifteen times.
* 11:45:18: `start <cnn url>` four times, a new tab each time; at step 3 the
  model said "the correct CNN article page is loaded" and opened it again.
* 13:32:58: two launchers, then eleven identical waits on a blank page.

These replay those runs through the REAL run_local_agentic_loop and
execute_action and a REAL VisionService (its MiniCPM call replaced); only the
VLM, the screenshot, the input/foreground probes and the part that would
touch this machine are replaced.

    python -m pytest tests/unit/test_a_computer_use_step_is_judged_by_what_it_achieved.py -q -p no:cacheprovider
"""
import base64
import functools
import io
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

TIMEOUT = ('Shell_Command timed out after 30s. For long-running work use '
           'Execute_Coding_Task instead, which has a longer budget.')
CNN = 'https://www.cnn.com/2024/05/02/world/international/story1/index.html'


def _jpeg(shade, size=(128, 72)):
    """A real JPEG screen of one shade."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', size, (shade, shade, shade)).save(buf, format='JPEG',
                                                       quality=70)
    return base64.b64encode(buf.getvalue()).decode()


def _say(action, **kw):
    reply = {'Reasoning': kw.pop('why', 'next step'), 'Next Action': action,
             'Status': kw.pop('status', 'IN_PROGRESS')}
    reply.update(kw)
    return json.dumps(reply)


DONE = _say('None', status='DONE', why='finished')


class _Replay:
    """Stands in for Qwen3VLBackend: answers the recorded replies in order,
    then DONE; records every prompt."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def route_task(self, _instruction):
        return 'multi_step'

    def try_taskbar_pre_check(self, *_a, **_k):
        return None

    def detect_grounding_bias(self, *_a, **_k):
        return None

    def _call_api(self, messages):
        for part in (messages[0].get('content') or []):
            if isinstance(part, dict) and part.get('type') == 'text':
                self.prompts.append(part.get('text', ''))
        return self.replies.pop(0) if self.replies else DONE


class _FastClock:
    """local_loop's view of the time module with the settle sleep removed;
    every other name is the real module's, and no other module sees it."""

    def __init__(self, real):
        self._real = real

    def sleep(self, _seconds):
        return None

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def vision(monkeypatch):
    """The live VisionService as the loop reaches it (safe_hartos_attr), a
    real instance marked running, with its MiniCPM describe replaced."""
    from integrations.vision.vision_service import VisionService
    import core.safe_hartos_attr as sha
    svc = VisionService()
    svc._running = True
    svc._description_interval = 0.0
    made = []

    def _describe(user_id, frame_bytes, prompt=''):
        made.append(frame_bytes)
        return 'screen caption %d' % len(made)
    monkeypatch.setattr(svc, '_describe_frame', _describe)
    monkeypatch.setattr(svc, '_post_description_to_db', lambda *a, **k: None)
    monkeypatch.setattr(svc, '_record_to_world_model', lambda *a, **k: None)
    real = sha.safe_hartos_attr
    asked = []

    def _attr(name):
        asked.append(name)
        if name == 'get_vision_service':
            return lambda: svc
        if name == 'get_frame_store':
            return lambda: svc.store
        return real(name)
    monkeypatch.setattr(sha, 'safe_hartos_attr', _attr)
    svc.made = made
    svc.asked = asked
    return svc


@pytest.fixture
def drive(monkeypatch, computer_control_granted):
    """drive(replies, results, screens=None, foreground=None) runs the real
    loop; results maps an action to a result dict, a list of them, or a
    callable(action); screens(i) is the b64 of capture i (default: every
    capture different)."""
    lct = pytest.importorskip('integrations.vlm.local_computer_tool')
    from integrations.vlm import local_loop, qwen3vl_backend, safety
    import core.resource_governor as governor

    monkeypatch.setenv('HEVOLVE_VLM_UNIFIED', '1')
    monkeypatch.setenv('HEVOLVE_VLM_LOOP_SAFETY', '1')
    monkeypatch.setenv('HEVOLVE_VLM_LOOP_VERIFY', '0')
    monkeypatch.setattr(lct, '_emit_audit', lambda *a, **k: None)
    monkeypatch.setattr(lct, '_check_reasoning_mismatch', lambda action: None)
    monkeypatch.setattr(governor, 'get_physical_input_state',
                        lambda **_k: (1, 0.0))
    # The loop's 0.5 s settle between steps, and the guard's 5 actions/s
    # throttle that a loop without it would trip.
    monkeypatch.setattr(local_loop, 'time', _FastClock(local_loop.time))
    monkeypatch.setattr(safety.get_session_guard().config,
                        'max_actions_per_second', 1000.0)

    def run(replies, results, screens=None, foreground=None,
            instruction='open the news site and list the top articles',
            user_id='u-judged', prompt_id='p-judged', action_id=None):
        backend = _Replay(replies)
        ran = []
        shots = {'n': 0}

        def _shot(_tier):
            shots['n'] += 1
            return (screens(shots['n']) if screens
                    else _jpeg(shots['n'] * 23 % 250))

        def _execute(action):
            ran.append(dict(action))
            res = results.get(action.get('action'))
            if callable(res):
                return res(action)
            if isinstance(res, list):
                return dict(res.pop(0) if len(res) > 1 else res[0])
            return dict(res or {'output': 'ok'})

        monkeypatch.setattr(qwen3vl_backend, 'get_qwen3vl_backend',
                            lambda: backend, raising=False)
        monkeypatch.setattr(lct, 'take_screenshot', _shot, raising=False)
        monkeypatch.setattr(lct, '_execute_inprocess', _execute)
        monkeypatch.setattr(lct, 'foreground_window_handle',
                            foreground or (lambda: 42))
        safety.get_session_guard().reset()
        out = local_loop.run_local_agentic_loop(
            {'instruction_to_vlm_agent': instruction,
             'enhanced_instruction': instruction,
             'user_id': user_id, 'prompt_id': prompt_id,
             'action_id': action_id, 'max_ETA_in_seconds': 600},
            tier='inprocess')
        steps = [r['content'] for r in out['extracted_responses']
                 if r.get('type') == 'action']
        return out, steps, ran, backend

    return run


def _ran(ran, action, **match):
    return [a for a in ran if a.get('action') == action
            and all(a.get(k) == v for k, v in match.items())]


# ── the live runs ───────────────────────────────────────────────────────────

def _run_1308():
    """13:08:31-13:22:02 as logged: click, taskbar click, then the same
    `start edge` (timed out) and a click elsewhere on the taskbar, x14."""
    xs = [536, 538, 562, 616, 574, 583, 626, 558, 574, None, 574, 602, 633,
          602]
    replies = [_say('left_click', coordinate=[656, 975]),
               _say('left_click', coordinate=[317, 963])]
    for x in xs:
        replies.append(_say('shell',
                            command='start edge https://news.ycombinator.com/'))
        if x is not None:
            replies.append(_say('left_click', coordinate=[x, 975]))
    return replies


def test_the_1308_alternation_stops_as_no_progress(drive):
    """Per target: the same failure, re-run, three times.  The clicks land at
    a different x each time and the screen keeps changing, so neither a
    "since the last new target" count nor a screen check would stop it."""
    out, steps, ran, _b = drive(
        _run_1308(),
        {'shell': {'output': TIMEOUT, 'status': 'error'},
         'left_click': lambda a: {'output': 'Clicked at %s' % a['coordinate']}})
    assert out['exit_reason'] == 'no_progress', (
        'the run spent %d steps alternating the same failing launcher and a '
        'click: exit_reason=%r' % (len(steps), out['exit_reason']))
    assert len(steps) <= 10, len(steps)
    assert len(_ran(ran, 'shell')) <= 4


def test_the_1145_launcher_opens_the_page_once(drive):
    out, steps, ran, _b = drive(
        [_say('shell', command='start ' + CNN, Expected='the CNN article'),
         _say('shell', command='start ' + CNN),
         _say('shell', command='start ' + CNN, **{'Previous met': True},
              why='the correct CNN article page is loaded'),
         _say('shell', command='print'),
         _say('shell', command='start ' + CNN),
         _say('open_file_gui', path=CNN)],
        {'shell': lambda a: {'output': 'Exit code: 0\n(no output)'
                             if a['command'].startswith('start')
                             else 'Exit code: 0\nNo file to print'},
         'open_file_gui': {'output': 'Opened ' + CNN}})
    assert len(_ran(ran, 'shell', command='start ' + CNN)) == 1, (
        'a launcher that printed nothing ran again: each run is another tab')
    refused = [s for s in steps if s.get('status') == 'refused']
    assert refused and 'printed nothing' in refused[0]['result']
    assert out['exit_reason'] == 'no_progress'


def test_the_1332_waits_on_a_blank_page_stop(drive, vision):
    """Run-level: three waits in a row that changed nothing on screen.  No
    wait is refused."""
    out, steps, ran, _b = drive(
        [_say('shell', command='msedge "https://rss.com"'),
         _say('shell', command='edge https://rss.com'),
         _say('shell', command='start msedge https://rss.com'),
         _say('shell', command='start https://rss.com')]
        + [_say('wait', why='The page is still blank') for _ in range(11)],
        {'shell': lambda a: (
            {'output': "Exit code: 1\n[stderr]\n'%s' is not recognized"
                       % a['command'].split()[0], 'status': 'error'}
            if not a['command'].startswith('start')
            else {'output': 'Exit code: 0\n(no output)'}),
         'wait': {'output': 'Waited 2s'}},
        screens=lambda i: _jpeg(30) if i <= 3 else _jpeg(120) if i == 4
        else _jpeg(255))
    assert out['exit_reason'] == 'no_progress', out['exit_reason']
    assert len(_ran(ran, 'wait')) == 3, len(_ran(ran, 'wait'))
    assert not [s for s in steps if s.get('status') == 'refused']


def test_jittered_clicks_on_one_icon_stop(drive, vision):
    """Every click a new target (a different x), the screen never changes."""
    out, _steps, ran, _b = drive(
        [_say('left_click', coordinate=[x, 975])
         for x in (601, 607, 612, 598, 615, 603)],
        {'left_click': lambda a: {'output': 'Clicked at %s' % a['coordinate']}},
        screens=lambda i: _jpeg(90))
    assert out['exit_reason'] == 'no_progress', out['exit_reason']
    assert len(_ran(ran, 'left_click')) == 3


# ── what the vision service observation may and may not say ─────────────────

def test_a_frame_older_than_the_action_never_reads_as_no_change(drive, vision):
    """The service already holds a description from before the action, and
    describes nothing new: the step's own after-capture decides."""
    vision.store.put_screen_description('u-judged', 'the old desktop')
    vision._describe_frame = lambda *a, **k: None
    out, steps, _ran_, _b = drive(
        [_say('left_click', coordinate=[500, 500])],
        {'left_click': {'output': 'Clicked at [1280, 720]'}},
        screens=lambda i: _jpeg(30) if i == 1 else _jpeg(200))
    assert steps[0]['screen_changed'] is True
    assert steps[0]['screen_after'] != 'the old desktop'


@pytest.mark.parametrize('off', ['not_allowed', 'not_running'])
def test_with_vision_off_the_verdict_still_holds_and_nothing_is_described(
        drive, vision, monkeypatch, off):
    """The verdict is the executor's own (the step's captures); the vision
    service only adds a description, and is not even reached while screen
    sensing is not allowed."""
    if off == 'not_allowed':
        import core.ai_sensing as sensing
        monkeypatch.setattr(sensing, 'allowed', lambda sensor: False)
    else:
        vision._running = False
    out, steps, ran, _b = drive(
        [_say('left_click', coordinate=[x, 975]) for x in (601, 607, 612, 598)],
        {'left_click': {'output': 'Clicked at [1, 1]'}},
        screens=lambda i: _jpeg(90))
    assert out['exit_reason'] == 'no_progress', out['exit_reason']
    assert len(_ran(ran, 'left_click')) == 3
    assert all(s.get('screen_changed') is False for s in steps)
    assert not vision.made and all(not s.get('screen_after') for s in steps)
    if off == 'not_allowed':
        assert 'get_vision_service' not in vision.asked, (
            'the service was looked up although screen sensing is off')


def test_screens_that_cannot_be_compared_are_unknown(drive):
    out, steps, ran, _b = drive(
        [_say('left_click', coordinate=[x, 975]) for x in (601, 607, 612, 598)],
        {'left_click': {'output': 'Clicked at [1, 1]'}},
        screens=lambda i: 'bm90IGFuIGltYWdlICVk%d' % i)
    assert len(_ran(ran, 'left_click')) == 4
    assert out['exit_reason'] == 'done', out['exit_reason']
    assert all(s.get('screen_changed') is None for s in steps)


# ── small real changes: the reviewers' frames ───────────────────────────────

def _page(lines, offset=0, typed=None):
    """A 1280x720 page of 18 px text (a form, a news list) as the loop
    captures it: JPEG q70."""
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (1280, 720), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for k, text in enumerate(lines):
        y = 120 + k * 40 - offset
        if -40 < y < 720:
            draw.text((300, y), text, fill=(20, 20, 20))
    for k, text in enumerate(typed or []):
        draw.rectangle([300, 160 + k * 90, 980, 190 + k * 90],
                       outline=(150, 150, 150))
        draw.text((310, 168 + k * 90), text, fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=70)
    return base64.b64encode(buf.getvalue()).decode()


def test_filling_a_form_keeps_running(drive, vision):
    # Not an example.com address: the safety guard refuses a reserved
    # documentation domain as a made-up credential.
    fields = [[], [], ['Ada Lovelace'], ['Ada Lovelace'],
              ['Ada Lovelace', 'ada@hertzai.com'],
              ['Ada Lovelace', 'ada@hertzai.com'],
              ['Ada Lovelace', 'ada@hertzai.com', '********']]
    shots = [_page(['Sign up'], typed=f) for f in fields]
    out, _steps, ran, _b = drive(
        [_say('left_click', coordinate=[640, 168]),
         _say('type', value='Ada Lovelace'),
         _say('key', value='tab'),
         _say('type', value='ada@hertzai.com'),
         _say('key', value='tab'),
         _say('type', value='hunter22'),
         _say('left_click', coordinate=[480, 540])],
        {'left_click': lambda a: {'output': 'Clicked at %s' % a['coordinate']},
         'type': lambda a: {'output': 'Typed %d characters' % len(a['text'])},
         'key': {'output': 'Pressed tab'}},
        screens=lambda i: shots[min(i - 1, len(shots) - 1)])
    assert out['exit_reason'] == 'done', out['exit_reason']
    assert len(ran) == 7


def test_clicking_into_fields_then_typing_keeps_running(drive, vision):
    """A click into a field changes nothing visible; the typing after it
    does, and that is progress."""
    def _screen(i):
        # Unchanged after each click, changed after each typing.
        return _jpeg(40 + 30 * ((i - 1) // 2))
    out, _steps, ran, _b = drive(
        [_say('left_click', coordinate=[640, 168 + 90 * k]) if j == 0
         else _say('type', value='field %d' % k)
         for k in range(3) for j in range(2)],
        {'left_click': lambda a: {'output': 'Clicked at %s' % a['coordinate']},
         'type': lambda a: {'output': 'Typed %d characters' % len(a['text'])}},
        screens=_screen)
    assert out['exit_reason'] == 'done', out['exit_reason']
    assert len(ran) == 6


def test_scrolling_a_white_page_a_notch_at_a_time_keeps_running(drive, vision):
    lines = ['Story %d: officials announce a new plan for the city budget' % k
             for k in range(60)]
    out, _steps, ran, _b = drive(
        [_say('scroll_down') for _ in range(5)],
        {'scroll_down': {'output': 'Scrolled down 1 notches'}},
        screens=lambda i: _page(lines, offset=12 * i))
    assert out['exit_reason'] == 'done', out['exit_reason']
    assert len(ran) == 5


def test_three_silent_commands_keep_running(drive, vision):
    out, _steps, ran, _b = drive(
        [_say('shell', command='mkdir out'),
         _say('shell', command='del old.tmp'),
         _say('shell', command='copy /y a.txt out\\a.txt >nul'),
         _say('shell', command='dir out')],
        {'shell': lambda a: {'output': 'Exit code: 0\n a.txt'
                             if a['command'].startswith('dir')
                             else 'Exit code: 0\n(no output)'}},
        screens=lambda i: _jpeg(80))
    assert out['exit_reason'] == 'done', out['exit_reason']
    assert len(ran) == 4


# ── what the model is shown ─────────────────────────────────────────────────

def test_the_model_sees_the_whole_run_and_a_receipt_is_not_a_result(drive):
    _out, _steps, _ran_, backend = drive(
        [_say('shell', command='Get-Content first.log'),
         _say('left_click', coordinate=[500, 500],
              Expected='the Settings window opens'),
         _say('scroll_down')],
        {'shell': {'output': 'Exit code: 0\nalpha'},
         'left_click': {'output': 'Clicked at [1280, 720]'},
         'scroll_down': {'output': 'Scrolled down 5 notches'}})
    after_click = backend.prompts[2]
    assert 'Get-Content first.log' in after_click, (
        'the third prompt no longer shows the first step:\n' + after_click[:1500])
    assert 'this is the REAL result' not in after_click, (
        "a click's receipt is still presented as its result")
    assert 'You expected: the Settings window opens' in after_click
    assert '"Previous met"' in after_click
    # A deterministic action's stdout is still its result (09-09 defect).
    assert 'this is the REAL result' in backend.prompts[1]


def test_the_vision_description_reaches_the_model(drive, vision):
    _out, _steps, _ran_, backend = drive(
        [_say('left_click', coordinate=[500, 500])],
        {'left_click': {'output': 'Clicked at [1280, 720]'}},
        screens=lambda i: _jpeg(30) if i == 1 else _jpeg(200))
    assert 'The vision service describes the screen now as: screen caption' \
        in backend.prompts[1]


def test_a_window_change_before_dispatch_is_not_a_failed_action(drive):
    calls = {'n': 0}

    def _fg():
        calls['n'] += 1
        return 43 if calls['n'] >= 2 else 42

    _out, steps, ran, backend = drive(
        [_say('type', value='https://www.bbc.com/news'),
         _say('type', value='https://www.bbc.com/news')],
        {'type': {'output': 'Typed 24 characters'}}, foreground=_fg)
    assert steps[0]['status'] == 'context_changed'
    assert len(_ran(ran, 'type')) == 1, (
        'the step that never ran was not allowed on the new screenshot')
    assert 'nothing was done' in backend.prompts[1]
    assert 'Do not repeat it unchanged' not in backend.prompts[1]


def test_the_prompt_stays_bounded_over_a_long_run(drive, vision):
    """The worst case: long outputs, long expectations, long descriptions."""
    vision._describe_frame = lambda *a, **k: 'a described screen ' * 20
    replies = [_say('shell', command='Get-Content build.log -Tail %d' % i,
                    Expected='the log shows the build finished ' * 10)
               for i in range(29)]
    _out, _steps, _ran_, backend = drive(
        replies, {'shell': lambda a: {'output': 'Exit code: 0\n' + 'x' * 900
                                      + a['command']}})
    longest = max(len(p) for p in backend.prompts)
    growth = longest - len(backend.prompts[0])
    print('longest prompt %d chars, first %d, growth %d'
          % (longest, len(backend.prompts[0]), growth))
    assert growth <= 6000, growth


# ── the model's judgement refuses nothing ───────────────────────────────────

def test_a_wizard_next_met_on_every_page_keeps_running(drive, vision):
    out, _steps, ran, _b = drive(
        [_say('left_click', coordinate=[480, 540], Expected='page 2'),
         _say('left_click', coordinate=[480, 540], Expected='page 3',
              **{'Previous met': True}),
         _say('left_click', coordinate=[480, 540], Expected='page 4',
              **{'Previous met': True}),
         _say('left_click', coordinate=[480, 540], Expected='finish',
              **{'Previous met': True})],
        {'left_click': lambda a: {'output': 'Clicked at %s' % a['coordinate']}})
    assert len(_ran(ran, 'left_click')) == 4
    assert out['exit_reason'] == 'done', out['exit_reason']


def test_the_same_text_into_a_second_field_runs(drive, vision):
    _out, steps, ran, _b = drive(
        [_say('left_click', coordinate=[640, 348]),
         _say('type', value='hunter22', Expected='password field filled'),
         _say('left_click', coordinate=[640, 438], **{'Previous met': True}),
         _say('type', value='hunter22', Expected='confirm field filled')],
        {'left_click': lambda a: {'output': 'Clicked at %s' % a['coordinate']},
         'type': lambda a: {'output': 'Typed %d characters' % len(a['text'])}})
    assert len(_ran(ran, 'type')) == 2
    assert steps[1]['met'] is True


# ── launchers and silent scripts ────────────────────────────────────────────

def test_a_silent_script_runs_again_after_its_fix(drive, vision):
    out, _steps, ran, _b = drive(
        [_say('write_file', path='make_csv.py', value='write a'),
         _say('shell', command='python make_csv.py'),
         _say('read_file_and_understand', path='out.csv'),
         _say('write_file', path='make_csv.py', value='write a,b'),
         _say('shell', command='python make_csv.py'),
         _say('read_file_and_understand', path='out.csv')],
        {'write_file': {'output': 'Wrote make_csv.py'},
         'read_file_and_understand': [{'output': 'a'}, {'output': 'a,b'}],
         'shell': {'output': 'Exit code: 0\n(no output)'}},
        screens=lambda i: _jpeg(80))
    assert len(_ran(ran, 'shell')) == 2, 'the silent script was refused'
    assert out['exit_reason'] == 'done', out['exit_reason']


def test_a_silent_command_repeated_with_nothing_changed_stops(drive, vision):
    """It succeeded and printed nothing, and nothing it reads was written:
    it runs once, is refused with what to do instead, and the run stops
    after three refusals.  (It ran four times: `echo row >> list.txt`
    appended four rows; review of 5c29a2808.)"""
    out, steps, ran, _b = drive(
        [_say('shell', command='python make_csv.py') for _ in range(6)],
        {'shell': {'output': 'Exit code: 0\n(no output)'}},
        screens=lambda i: _jpeg(80))
    assert out['exit_reason'] == 'no_progress', out['exit_reason']
    assert len(_ran(ran, 'shell')) == 1
    refused = [s for s in steps if s.get('status') == 'refused']
    assert len(refused) == 3
    for hint in ('printed nothing', 'write_file', 'wait action'):
        assert hint in refused[0]['result'], refused[0]['result']


def test_a_launcher_reopens_after_its_page_is_rewritten(drive, vision):
    out, _steps, ran, _b = drive(
        [_say('write_file', path='page.html', value='<h1>a</h1>'),
         _say('shell', command='start page.html'),
         _say('write_file', path='page.html', value='<h1>b</h1>'),
         _say('shell', command='start page.html')],
        {'write_file': {'output': 'Wrote page.html'},
         'shell': {'output': 'Exit code: 0\n(no output)'}})
    assert len(_ran(ran, 'shell')) == 2
    assert out['exit_reason'] == 'done', out['exit_reason']


@pytest.mark.parametrize('judged', [False, 'maybe', None])
def test_a_step_not_judged_met_may_be_tried_again(drive, judged):
    second = {} if judged is None else {'Previous met': judged}
    _out, _steps, ran, _b = drive(
        [_say('left_click', coordinate=[400, 300], Expected='the menu opens'),
         _say('scroll_down', **second),
         _say('left_click', coordinate=[400, 300])],
        {'left_click': {'output': 'Clicked at [1024, 432]'},
         'scroll_down': {'output': 'Scrolled down 5 notches'}})
    assert len(_ran(ran, 'left_click')) == 2


# ── legitimate work that must keep running ──────────────────────────────────

def test_an_identical_poll_keeps_running_to_the_budget(drive, vision):
    """"still building" again and again is waiting, not a loop."""
    replies = []
    for _ in range(15):
        replies += [_say('shell', command='Get-Content build.log -Tail 20'),
                    _say('wait')]
    out, _steps, ran, _b = drive(
        replies,
        {'shell': {'output': 'Exit code: 0\nstill building'},
         'wait': {'output': 'Waited 2s'}},
        screens=lambda i: _jpeg(80))
    assert out['exit_reason'] == 'max_iterations', out['exit_reason']
    assert len(_ran(ran, 'shell')) == 15


def test_a_poll_that_brings_new_lines_keeps_running(drive, vision):
    lines = iter(['line1', 'line1\nline2', 'line1\nline2\nline3'])
    out, _steps, ran, _b = drive(
        [_say('shell', command='Get-Content build.log -Tail 20'),
         _say('wait'),
         _say('shell', command='Get-Content build.log -Tail 20'),
         _say('wait'),
         _say('shell', command='Get-Content build.log -Tail 20')],
        {'shell': lambda a: {'output': 'Exit code: 0\n' + next(lines)},
         'wait': {'output': 'Waited 2s'}},
        screens=lambda i: _jpeg(80))
    assert len(_ran(ran, 'shell')) == 3
    assert out['exit_reason'] == 'done', out['exit_reason']


def test_fixing_a_script_keeps_running(drive, vision):
    errors = iter(['Exit code: 1\nNameError: x', 'Exit code: 1\nTypeError: y',
                   'Exit code: 0\n42'])
    out, _steps, ran, _b = drive(
        [_say('write_file', path='a.py', value='print(x)'),
         _say('shell', command='python a.py'),
         _say('write_file', path='a.py', value='print(1 + "y")'),
         _say('shell', command='python a.py'),
         _say('write_file', path='a.py', value='print(42)'),
         _say('shell', command='python a.py')],
        {'write_file': {'output': 'Wrote a.py'},
         'shell': lambda a: {'output': next(errors)}},
        screens=lambda i: _jpeg(80))
    assert len(_ran(ran, 'write_file')) == 3 and len(_ran(ran, 'shell')) == 3
    assert out['exit_reason'] == 'done', out['exit_reason']


def test_scrolling_a_changing_page_keeps_running(drive, vision):
    out, _steps, ran, _b = drive(
        [_say('scroll_down') for _ in range(5)],
        {'scroll_down': {'output': 'Scrolled down 5 notches'}})
    assert len(_ran(ran, 'scroll_down')) == 5
    assert out['exit_reason'] == 'done', out['exit_reason']


# ── review of 5c29a2808: a desktop as it is captured ────────────────────────
# With vision ON the parent stopped a stuck run at 3 steps and 5c29a2808 ran
# it to 30: its verdict read 0.0 as "unknown", and after the comparison's
# 64x64 shrink and noise floor a caret blink, a clock tick and a tab spinner
# measure exactly 0.0, so only byte-identical captures ever read "unchanged".

@functools.lru_cache(maxsize=None)
def _desktop(caret=False, clock='13:08', spinner=None, typed='', field2='',
             checkbox=False, url='', dropdown=False, bottom=False):
    """The reviewer's 1920x1080 desktop (a browser window with a form, a
    taskbar with a clock), captured as take_screenshot does: LANCZOS to a
    1280 long edge, JPEG q70."""
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (1920, 1080), (243, 243, 243))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 1920, 80], fill=(222, 225, 230))
    d.rounded_rectangle([200, 44, 1500, 72], 12, fill=(255, 255, 255))
    d.text((220, 48), url or 'about:blank', fill=(30, 30, 30))
    d.rectangle([12, 8, 260, 36], fill=(255, 255, 255))
    d.text((40, 12), 'Sign up - News', fill=(30, 30, 30))
    if spinner is not None:
        d.arc([16, 14, 32, 30], spinner, spinner + 270, fill=(26, 115, 232),
              width=2)
    d.rectangle([0, 80, 1920, 1032], fill=(255, 255, 255))
    for k in range(40):
        y = 120 + k * 28 - (600 if bottom else 0)
        if 80 < y < 560:
            d.text((300, y), 'Story %d: officials announce a new plan for the '
                   'city budget and transport' % k, fill=(20, 20, 20))
    d.text((300, 600), 'Name', fill=(60, 60, 60))
    d.rectangle([300, 620, 900, 652], outline=(150, 150, 150), width=1)
    d.text((310, 627), typed, fill=(0, 0, 0))
    if caret:
        w = d.textlength(typed) if typed else 0
        d.line([311 + w, 626, 311 + w, 646], fill=(0, 0, 0), width=1)
    d.text((300, 670), 'Email', fill=(60, 60, 60))
    d.rectangle([300, 690, 900, 722], outline=(150, 150, 150), width=1)
    d.text((310, 697), field2, fill=(0, 0, 0))
    d.rectangle([300, 750, 313, 763], outline=(90, 90, 90), width=1,
                fill=(26, 115, 232) if checkbox else (255, 255, 255))
    if checkbox:
        d.line([302, 757, 305, 761, 311, 752], fill=(255, 255, 255), width=2)
    d.text((322, 748), 'I agree to the terms', fill=(30, 30, 30))
    if dropdown:
        d.rectangle([300, 652, 900, 812], fill=(255, 255, 255),
                    outline=(120, 120, 120))
        for k, t in enumerate(['India', 'Japan', 'Kenya', 'Mexico', 'Norway']):
            d.text((310, 660 + k * 30), t, fill=(0, 0, 0))
    d.rectangle([0, 1032, 1920, 1080], fill=(32, 32, 32))
    d.text((1830, 1038), clock, fill=(255, 255, 255))
    d.text((1822, 1056), '22-11-2026', fill=(255, 255, 255))
    img = img.resize((1280, 720), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=70)
    return base64.b64encode(buf.getvalue()).decode('ascii')


def _seeing(on, monkeypatch):
    import core.ai_sensing as sensing
    monkeypatch.setattr(sensing, 'allowed', lambda sensor: on)


BOTH = pytest.mark.parametrize('seeing', [True, False])
CLICKED = {'left_click': lambda a: {'output': 'Clicked at %s' % a['coordinate']}}

#: reply, result, screens of a run that changes nothing it can be seen by.
STUCK = {
    'clicks_caret_blink': (
        lambda k: _say('left_click', coordinate=[600 + k % 7, 975]), CLICKED,
        lambda i: _desktop(caret=bool(i % 2))),
    'waits_tab_spinner': (
        lambda k: _say('wait', why='page still blank'),
        {'wait': {'output': 'Waited 2s'}},
        lambda i: _desktop(spinner=(i * 90) % 360)),
    'clicks_clock_tick': (
        lambda k: _say('left_click', coordinate=[600 + k % 7, 975]), CLICKED,
        lambda i: _desktop(clock='13:%02d' % (8 + i))),
    'scroll_at_page_end': (
        lambda k: _say('scroll_down'),
        {'scroll_down': {'output': 'Scrolled down 5 notches'}},
        lambda i: _desktop(bottom=True)),
    'typing_unfocused': (
        lambda k: _say('type', value='Ada Lovelace'),
        {'type': lambda a: {'output': 'Typed %d characters' % len(a['text'])}},
        lambda i: _desktop()),
    'enter_key': (
        lambda k: _say('key', value='enter'),
        {'key': {'output': 'Pressed enter'}},
        lambda i: _desktop(caret=bool(i % 2))),
}


def test_the_comparison_tells_unreadable_from_the_same(vision):
    from integrations.vlm import local_loop
    from integrations.vlm.local_computer_tool import (
        _image_change, _quick_image_diff)
    plain, caret, ticked = _desktop(), _desktop(caret=True), _desktop(checkbox=True)
    assert caret != plain
    assert _image_change(plain, caret) == 0.0
    assert _image_change(plain, 'bm90IGFuIGltYWdl') is None
    assert 0.0 < _image_change(plain, ticked) < 0.005
    # _post_click_verify's reader keeps its answer for an unreadable capture.
    assert _quick_image_diff('not-base64!!!', 'also-bad') == 0.0
    assert _quick_image_diff(plain, ticked) == _image_change(plain, ticked)
    verdict = lambda a, b: local_loop._screen_observation(   # noqa: E731
        'u-judged', a, b, time.time())[0]
    assert verdict(plain, caret) is False, 'a caret blink read as a change'
    assert verdict(_desktop(spinner=0), _desktop(spinner=90)) is False
    assert verdict(_desktop(clock='13:08'), _desktop(clock='13:09')) is False
    assert verdict(plain, ticked) is True, 'a ticked checkbox read as nothing'
    assert verdict(plain, _desktop(dropdown=True)) is True
    assert verdict(plain, _desktop(typed='Ada Lovelace')) is True
    assert verdict(plain, 'bm90IGFuIGltYWdl') is None


@BOTH
@pytest.mark.parametrize('how', sorted(STUCK))
def test_a_run_that_changes_nothing_stops_at_three_steps(
        drive, vision, monkeypatch, seeing, how):
    _seeing(seeing, monkeypatch)
    reply, results, screens = STUCK[how]
    out, steps, ran, backend = drive(
        [reply(k) for k in range(30)], results, screens=screens)
    assert out['exit_reason'] == 'no_progress', (
        '%s: %d steps, exit %r' % (how, len(steps), out['exit_reason']))
    assert len(ran) == 3 and len(steps) == 3
    # The third step's judgement is asked for; nothing runs after it.
    assert len(backend.prompts) == 4
    assert all(s.get('screen_changed') is False for s in steps[:2])


@BOTH
def test_three_real_checkbox_clicks_are_progress(
        drive, vision, monkeypatch, seeing):
    _seeing(seeing, monkeypatch)
    shots = [_desktop(), _desktop(checkbox=True), _desktop(checkbox=False),
             _desktop(checkbox=True), _desktop(checkbox=True)]
    out, steps, ran, _b = drive(
        [_say('left_click', coordinate=[306, 756]) for _ in range(3)], CLICKED,
        screens=lambda i: shots[min(i - 1, len(shots) - 1)])
    assert out['exit_reason'] == 'done', out['exit_reason']
    assert [s.get('screen_changed') for s in steps] == [True, True, True]
    assert len(ran) == 3


@BOTH
def test_a_form_fill_on_a_real_desktop_completes(
        drive, vision, monkeypatch, seeing):
    _seeing(seeing, monkeypatch)
    name, mail = 'Ada Lovelace', 'ada@hertzai.com'
    shots = [_desktop(), _desktop(caret=True), _desktop(typed=name),
             _desktop(typed=name), _desktop(typed=name, field2=mail),
             _desktop(typed=name, field2=mail, checkbox=True),
             _desktop(typed=name, field2=mail, checkbox=True, url='x')]
    out, _steps, ran, _b = drive(
        [_say('left_click', coordinate=[600, 636]),
         _say('type', value=name),
         _say('key', value='tab'),
         _say('type', value=mail),
         _say('left_click', coordinate=[306, 756]),
         _say('left_click', coordinate=[480, 900])],
        dict(CLICKED, type=lambda a: {'output': 'Typed %d characters'
                                      % len(a['text'])},
             key={'output': 'Pressed tab'}),
        screens=lambda i: shots[min(i - 1, len(shots) - 1)])
    assert out['exit_reason'] == 'done', out['exit_reason']
    assert len(ran) == 6


@pytest.mark.parametrize('judged', [True, None])
def test_a_change_too_small_to_measure_counts_as_the_model_judged_it(
        drive, vision, judged):
    """One character typed measures 0.0, as a caret blink does.  The model
    sees the full capture: a step it judged met is progress, and its
    judgement stops nothing.  With no judgement the steps count."""
    from integrations.vlm.local_computer_tool import _image_change
    shots = [_desktop(typed='A' * k) for k in range(8)]
    assert _image_change(shots[0], shots[1]) == 0.0
    met = {} if judged is None else {'Previous met': judged}
    out, _steps, ran, _b = drive(
        [_say('type', value='A', Expected='one more letter', **met)
         for _ in range(5)] + [_say('None', status='DONE', **met)],
        {'type': {'output': 'Typed 1 characters'}},
        screens=lambda i: shots[min(i - 1, len(shots) - 1)])
    if judged:
        assert out['exit_reason'] == 'done', out['exit_reason']
        assert len(ran) == 5
    else:
        assert out['exit_reason'] == 'no_progress', out['exit_reason']
        assert len(ran) == 3


def test_three_steps_that_changed_nothing_are_not_a_finished_task(
        drive, vision):
    """The model answering DONE after three steps with nothing to show is
    no_progress, as it was while the count was made before its reply."""
    out, _steps, ran, _b = drive(
        [_say('key', value='enter') for _ in range(3)],
        {'key': {'output': 'Pressed enter'}}, screens=lambda i: _desktop())
    assert len(ran) == 3
    assert out['exit_reason'] == 'no_progress', out['exit_reason']


def test_writing_the_same_file_again_does_not_hide_stuck_clicks(drive, vision):
    replies = []
    for k in range(6):
        replies += [_say('write_file', path='notes.txt', value='same'),
                    _say('left_click', coordinate=[600 + k, 975])]
    out, _steps, ran, _b = drive(
        replies, dict(CLICKED, write_file={'output': 'Wrote notes.txt'}),
        screens=lambda i: _desktop())
    assert out['exit_reason'] == 'no_progress', out['exit_reason']
    assert len(_ran(ran, 'left_click')) == 3


# ── a command that printed nothing runs once ────────────────────────────────

@BOTH
@pytest.mark.parametrize('screen', ['caret_blink', 'identical', 'changing'])
def test_a_launcher_runs_once_whatever_the_next_capture_shows(
        drive, vision, monkeypatch, seeing, screen):
    """`start <url>` whose tab opens late, behind, or on another monitor:
    one capture half a second later said nothing had opened, and it ran
    four times (four tabs)."""
    _seeing(seeing, monkeypatch)
    screens = {'caret_blink': lambda i: _desktop(caret=bool(i % 2)),
               'identical': lambda i: _desktop(), 'changing': None}[screen]
    out, steps, ran, _b = drive(
        [_say('shell', command='start https://news.ycombinator.com/')
         for _ in range(8)],
        {'shell': {'output': 'Exit code: 0\n(no output)'}}, screens=screens)
    assert len(_ran(ran, 'shell')) == 1, 'the launcher ran again: another tab'
    assert out['exit_reason'] == 'no_progress', out['exit_reason']
    assert [s.get('status') for s in steps] == ['', 'refused', 'refused',
                                                'refused']


def test_a_silent_append_runs_once(drive, vision):
    out, _steps, ran, _b = drive(
        [_say('shell', command='echo row >> list.txt') for _ in range(8)],
        {'shell': {'output': 'Exit code: 0\n(no output)'}},
        screens=lambda i: _jpeg(80))
    assert len(_ran(ran, 'shell')) == 1, 'the row was appended again'
    assert out['exit_reason'] == 'no_progress', out['exit_reason']


def test_a_command_that_failed_without_a_word_may_be_tried_again(drive, vision):
    """Exit code 1 and no output is a failure, not a launcher: it is tried
    again and counted (first run + 3 identical re-runs)."""
    out, steps, ran, _b = drive(
        [_say('shell', command='findstr done status.txt') for _ in range(8)],
        {'shell': {'output': 'Exit code: 1\n(no output)'}},
        screens=lambda i: _jpeg(80))
    assert len(_ran(ran, 'shell')) == 4
    assert not [s for s in steps if s.get('status') == 'refused']
    assert out['exit_reason'] == 'no_progress', out['exit_reason']


def test_the_same_page_written_again_is_not_reopened(drive, vision):
    """A stuck model rewriting the SAME page and reopening it: every write
    reset every mark, and 15 tabs opened."""
    replies = []
    for _ in range(15):
        replies += [_say('write_file', path='page.html', value='<h1>a</h1>'),
                    _say('shell', command='start page.html')]
    out, steps, ran, _b = drive(
        replies, {'write_file': {'output': 'Wrote page.html'},
                  'shell': {'output': 'Exit code: 0\n(no output)'}})
    assert len(_ran(ran, 'shell')) == 1, len(_ran(ran, 'shell'))
    assert out['exit_reason'] == 'no_progress', out['exit_reason']
    assert len(steps) == 8


def test_a_write_restarts_only_the_steps_that_read_that_file(drive, vision):
    out, steps, ran, _b = drive(
        [_say('shell', command='start page.html'),
         _say('shell', command='python make_csv.py'),
         _say('write_file', path='notes.txt', value='todo'),
         _say('shell', command='start page.html'),           # refused
         _say('write_file', path='out\\page.html', value='<h1>b</h1>'),
         _say('shell', command='start page.html'),           # runs
         _say('shell', command='python make_csv.py'),        # refused
         _say('write_file', path='make_csv.py', value='write a,b'),
         _say('shell', command='python make_csv.py')],       # runs
        {'write_file': {'output': 'Wrote it'},
         'shell': {'output': 'Exit code: 0\n(no output)'}})
    assert [s.get('status') for s in steps if s['action'] == 'shell'] == [
        '', '', 'refused', '', 'refused', '']
    assert len(_ran(ran, 'shell', command='start page.html')) == 2
    assert len(_ran(ran, 'shell', command='python make_csv.py')) == 2
    assert out['exit_reason'] == 'done', out['exit_reason']


def test_which_steps_read_a_file():
    from integrations.vlm.local_loop import _reads_file
    assert _reads_file('start page.html', 'page.html')
    assert _reads_file('start page.html', 'C:\\work\\out\\Page.html')
    assert _reads_file('python -m report --fast', 'tools/report.py')
    assert _reads_file('type C:\\work\\a.py', 'c:/work/a.py')
    assert _reads_file('python ab.py', 'out/ab.py'), 'by its file name alone'
    assert not _reads_file('start page.html', 'notes.txt')
    assert not _reads_file('start page.html', 'a.py'), 'a one-letter name'
    assert not _reads_file('start page.html', '')
