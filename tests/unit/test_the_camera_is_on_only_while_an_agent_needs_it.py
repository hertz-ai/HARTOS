"""The desktop camera is on only while an agent needs it.

Owner 2026-11-21: "The agent shd be able to say whether it needs the camera
on or off and its a shared resource so shd leverage the caption stream, if
none of the agents background or foreground agent needs it then it can be
stopped and started by the agent which needs captions or vision or always on,
again that shd be decided either at agent creation time or at reuse time by
agents".  The camera LED stayed on on the desktop: VisionService told the page
to free its camera only while the person's phone streamed (HARTOS 107331ddd),
and its socket is open for the app's whole life.

What says an agent needs the camera is what HARTOS already keeps
(core.agent_tools.camera_wanted):
  - creation time: a visual scheduled task of the person
    (reuse_recipe.create_schedule's visual_task_<user>_<prompt>_<n> jobs) runs,
    or runs within VISUAL_TASK_LEAD_S (call_visual_task reads 5 minutes of
    captions).  A task that fires at least that often is "always on";
  - reuse time: a visual turn of the person runs (reuse_recipe
    ._visual_turn_running), which the camera tool also sets while it waits for
    a frame; a visual watcher of theirs has not expired.
VisionService decides at the session start, before the first frame, and on
frames (asking at most every CAMERA_WANTED_RECHECK_S); the page's
"video_paused" heartbeat gets "video_resume" once a need appears.  The camera
tool waits, bounded, for a frame newer than its question and never answers
from an empty or older window.  An expired visual watcher is unregistered and
speaks no more (it was never registered at all: the lookup was a TypeError).

REUSE and the entry module are stand-ins holding the records they hold; the
VisionService handler, FrameStore, camera_wanted, the camera tool's wait, the
visual-turn marks and their one writer (cut from hartos/reuse_recipe.py), the
watcher functions and the chat camera tool (cut from
hart_intelligence_entry.py) are real.  No camera is opened and no screen is
grabbed: frames are made here, and the screen grab is a stub.

Review of 1fef56008 (rejected), fixed forward and tested below: the chat
camera tool answered from the desktop screen; the camera tool wrote the
visual turn's own mark; watchers fired across people and on one word; a turn
the 2 s poll started kept the camera on.

    python -m pytest tests/unit/test_the_camera_is_on_only_while_an_agent_needs_it.py -q -p no:cacheprovider --noconftest
"""
import ast
import asyncio
import logging
import os
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402

USER = '10202'
OTHER = '20303'
AGENT = '1794540972213'
IST = timezone(timedelta(hours=5, minutes=30))


def _jpeg(value=128):
    ok, enc = cv2.imencode('.jpg', np.full((48, 64, 3), value, dtype=np.uint8))
    assert ok
    return enc.tobytes()


class _Job:
    def __init__(self, job_id, next_run_time):
        self.id = job_id
        self.next_run_time = next_run_time


class _Scheduler:
    """What reuse_recipe.scheduler answers: its jobs, and the executor's count
    of each job's running instances (APScheduler BaseExecutor._instances)."""

    def __init__(self):
        self.jobs = []
        self._executors = {'default': types.SimpleNamespace(_instances={})}

    def get_jobs(self):
        return list(self.jobs)

    def running(self, job_id):
        self._executors['default']._instances[job_id] = 1


def _cut(path, names, ns):
    """Run the module-level definitions called ``names`` from the source file
    ``path`` in ``ns``, as written (with their decorators); returns the names
    found."""
    src = open(os.path.join(ROOT, path), encoding='utf-8').read()
    found = set()
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            name = target.id if isinstance(target, ast.Name) else None
        else:
            continue
        if name not in names:
            continue
        code = ''.join(f'@{ast.get_source_segment(src, d)}\n'
                       for d in getattr(node, 'decorator_list', []))
        exec(code + ast.get_source_segment(src, node), ns)
        found.add(name)
    return found


#: The visual-turn code of hartos/reuse_recipe.py: the marks, their one
#: writer and read, the turn and the poll.
_REUSE_NAMES = {
    '_VISUAL_TURN_MAX_S', '_visual_turn_running', '_visual_turn_lock',
    '_visual_turn_is_running', '_CaptionReader', '_visual_mark_set',
    '_visual_mark_clear', 'camera_held_for', 'camera_call_waits',
    '_VISUAL_POLL_PAUSE_S', '_visual_poll_paused_until',
    'call_visual_task', 'visual_based_execution'}


@pytest.fixture
def reuse(monkeypatch):
    """hartos.reuse_recipe's visual-turn code as written, and a scheduler."""
    import contextlib
    import json
    import pytz
    rr = types.ModuleType('hartos.reuse_recipe')
    rr.__dict__.update(
        threading=threading, time=time, contextlib=contextlib, json=json,
        pytz=pytz, datetime=datetime, timedelta=timedelta,
        logger=logging.getLogger('reuse'))
    _cut('hartos/reuse_recipe.py', _REUSE_NAMES, rr.__dict__)
    rr.scheduler = _Scheduler()
    monkeypatch.setitem(sys.modules, 'hartos.reuse_recipe', rr)
    return rr


@pytest.fixture
def entry(monkeypatch):
    """hart_intelligence_entry's watchers."""
    hie = types.ModuleType('hart_intelligence_entry')
    hie._active_watchers = {}
    monkeypatch.setitem(sys.modules, 'hart_intelligence_entry', hie)
    return hie


@pytest.fixture
def wanted(reuse, entry):
    from core.agent_tools import camera_wanted
    return camera_wanted


def _in(seconds):
    return datetime.now(IST) + timedelta(seconds=seconds)  # tz-aware, not UTC


# ── What says an agent needs the camera ─────────────────────────────────

class TestCameraWanted:
    def test_no_agent_needs_it(self, wanted):
        assert wanted(USER) is False

    def test_a_visual_turn_of_the_person_needs_it(self, wanted, reuse):
        reuse._visual_turn_running[(USER, AGENT)] = time.monotonic()
        assert wanted(USER) is True
        assert wanted(OTHER) is False

    def test_a_visual_turn_mark_past_the_turn_budget_does_not(self, wanted, reuse):
        reuse._visual_turn_running[(USER, AGENT)] = time.monotonic() - 301
        assert wanted(USER) is False

    def test_a_visual_task_due_within_the_lead_needs_it(self, wanted, reuse):
        from core.agent_tools import VISUAL_TASK_LEAD_S
        reuse.scheduler.jobs.append(
            _Job(f'visual_task_{USER}_{AGENT}_0', _in(VISUAL_TASK_LEAD_S - 60)))
        assert wanted(USER) is True
        assert wanted(OTHER) is False

    def test_a_visual_task_further_off_does_not(self, wanted, reuse):
        from core.agent_tools import VISUAL_TASK_LEAD_S
        reuse.scheduler.jobs.append(
            _Job(f'visual_task_{USER}_{AGENT}_0', _in(VISUAL_TASK_LEAD_S + 60)))
        assert wanted(USER) is False

    def test_a_running_visual_task_needs_it_for_its_whole_run(self, wanted, reuse):
        job_id = f'visual_task_{USER}_{AGENT}_0'
        reuse.scheduler.jobs.append(_Job(job_id, _in(3600)))  # next fire an hour off
        reuse.scheduler.running(job_id)
        assert wanted(USER) is True

    def test_always_on_is_a_visual_task_no_further_apart_than_the_lead(self, wanted, reuse):
        from core.agent_tools import VISUAL_TASK_LEAD_S
        reuse.scheduler.jobs.append(
            _Job(f'visual_task_{USER}_{AGENT}_0', _in(VISUAL_TASK_LEAD_S - 1)))
        assert wanted(USER) is True

    def test_the_2s_visual_poll_every_session_gets_is_no_need(self, wanted, reuse):
        reuse.scheduler.jobs.append(_Job(f'visual_poll_{USER}_{AGENT}', _in(2)))
        assert wanted(USER) is False

    def test_another_persons_job_with_a_longer_id_is_not_this_persons(self, wanted, reuse):
        reuse.scheduler.jobs.append(_Job(f'visual_task_{USER}5_{AGENT}_0', _in(10)))
        assert wanted(USER) is False

    def test_a_live_visual_watcher_needs_it_until_it_expires(self, wanted, entry):
        entry._active_watchers[int(USER)] = [
            {'modality': 'visual', 'expires_at': time.time() + 60}]
        assert wanted(USER) is True
        entry._active_watchers[int(USER)][0]['expires_at'] = time.time() - 1
        assert wanted(USER) is False

    def test_an_audio_watcher_does_not(self, wanted, entry):
        entry._active_watchers[USER] = [{'modality': 'audio', 'expires_at': time.time() + 60}]
        assert wanted(USER) is False


# ── VisionService tells the page ────────────────────────────────────────

class _Page:
    """What a page sends over the socket; what it is told back."""

    def __init__(self, messages, between=None):
        self._messages = list(messages)
        self._between = between or {}
        self.told = []
        self.remote_address = ('127.0.0.1', 50000)

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for i, m in enumerate(self._messages):
            if i in self._between:
                self._between[i]()
            yield m

    async def send(self, text):
        self.told.append(text)


def _told(page):
    return page.told


@pytest.fixture
def svc(monkeypatch, reuse, entry):
    import core.ai_sensing as sensing
    monkeypatch.setattr(sensing, 'allowed', lambda sensor: True)
    from integrations.vision.frame_store import FrameStore
    from integrations.vision.vision_service import VisionService
    s = VisionService.__new__(VisionService)
    s.store = FrameStore()
    s._trigger_manager = None
    return s


def _run(svc, page):
    asyncio.run(svc._ws_handler(page))


class TestThePageIsTold:
    def test_nobody_needs_it_the_page_frees_it_before_its_first_frame(self, svc):
        page = _Page([USER, 'video_start'])
        _run(svc, page)
        assert _told(page) == ['video_pause']

    def test_nobody_needs_it_its_frames_are_dropped(self, svc):
        page = _Page([USER, 'video_start', _jpeg(), _jpeg()])
        _run(svc, page)
        assert _told(page) == ['video_pause'], 'told once'
        assert svc.store._frames.get(USER) is None

    def test_while_nobody_needs_it_the_heartbeat_gets_no_resume(self, svc, monkeypatch):
        import integrations.vision.vision_service as vsmod
        monkeypatch.setattr(vsmod, 'CAMERA_WANTED_RECHECK_S', 0.0)
        page = _Page([USER, 'video_start', 'video_paused', 'video_paused'])
        _run(svc, page)
        assert _told(page) == ['video_pause']

    def test_an_agent_needs_it_the_page_streams(self, svc, reuse):
        reuse._visual_turn_running[(USER, AGENT)] = time.monotonic()
        page = _Page([USER, 'video_start', _jpeg(), _jpeg()])
        _run(svc, page)
        assert _told(page) == []
        assert len(svc.store._frames[USER]) == 2

    def test_a_need_that_appears_brings_the_camera_back_at_the_heartbeat(self, svc, reuse, monkeypatch):
        def an_agent_asks():
            reuse._visual_turn_running[(USER, AGENT)] = time.monotonic()

        import integrations.vision.vision_service as vsmod
        monkeypatch.setattr(vsmod, 'CAMERA_WANTED_RECHECK_S', 0.0)
        page = _Page([USER, 'video_start', 'video_paused', 'video_paused'],
                     between={3: an_agent_asks})
        _run(svc, page)
        assert _told(page) == ['video_pause', 'video_resume']

    def test_a_need_that_ends_frees_the_camera_on_a_later_frame(self, svc, reuse, monkeypatch):
        import integrations.vision.vision_service as vsmod
        monkeypatch.setattr(vsmod, 'CAMERA_WANTED_RECHECK_S', 0.0)
        reuse._visual_turn_running[(USER, AGENT)] = time.monotonic()

        def the_turn_ends():
            reuse._visual_turn_running.clear()

        page = _Page([USER, 'video_start', _jpeg(), _jpeg(), _jpeg()],
                     between={4: the_turn_ends})
        _run(svc, page)
        assert _told(page) == ['video_pause']
        assert len(svc.store._frames[USER]) == 2

    def test_on_every_frame_the_need_is_asked_at_most_once_per_recheck(self, svc, reuse, monkeypatch):
        import core.agent_tools as tools
        real = tools.camera_wanted
        calls = []
        monkeypatch.setattr(tools, 'camera_wanted', lambda u: calls.append(u) or real(u))
        reuse._visual_turn_running[(USER, AGENT)] = time.monotonic()
        page = _Page([USER, 'video_start'] + [_jpeg() for _ in range(20)])
        _run(svc, page)
        assert len(svc.store._frames[USER]) == min(20, svc.store._max_frames)
        assert len(calls) == 1, f'asked {len(calls)} times for 20 frames'

    def test_a_screen_stream_is_not_held_by_the_cameras_rule(self, svc):
        page = _Page([USER, 'screen_start', _jpeg()])
        _run(svc, page)
        assert _told(page) == ['video_pause', 'video_resume']
        assert svc.store._screen_frames.get(USER)

    def test_one_call_is_cheap(self, wanted, reuse, entry):
        """Measured: camera_wanted with 200 jobs, 50 marks and 50 watchers of
        other people.  It runs on the socket's event loop."""
        for i in range(200):
            reuse.scheduler.jobs.append(_Job(f'visual_task_{i}_{AGENT}_0', _in(3600)))
        for i in range(50):
            reuse._visual_turn_running[(str(i), AGENT)] = time.monotonic()
            entry._active_watchers[str(i)] = [{'modality': 'visual',
                                               'expires_at': time.time() + 60}]
        n = 200
        t0 = time.perf_counter()
        for _ in range(n):
            assert wanted(USER) is False
        per_call_ms = (time.perf_counter() - t0) * 1000 / n
        print(f'camera_wanted: {per_call_ms:.3f} ms per call')
        assert per_call_ms < 5


# ── The camera tool: its call is the need, and it reads only a new frame ─

class _Clock:
    """time.monotonic and time.sleep for the tool's wait and the store's frame
    age, one clock; ``on_sleep`` runs at each sleep (a frame arriving)."""

    def __init__(self, monkeypatch):
        self.now = 1000.0
        self.on_sleep = None
        self.sleeps = 0
        monkeypatch.setattr(time, 'monotonic', lambda: self.now)
        monkeypatch.setattr(time, 'sleep', self.sleep)

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(self)


@pytest.fixture
def tool(monkeypatch, reuse, entry, tmp_path):
    import core.ai_sensing as sensing
    monkeypatch.setattr(sensing, 'allowed', lambda sensor: True)
    import flask
    import hartos.helper as h
    from hartos.threadlocal import thread_local_data
    from integrations.vision.frame_store import FrameStore
    store = FrameStore()
    import core.safe_hartos_attr as sha
    monkeypatch.setattr(sha, 'safe_hartos_attr',
                        lambda name, default=None: (lambda: store) if name == 'get_frame_store' else default)
    monkeypatch.setattr(thread_local_data, 'get_prompt_id', lambda: AGENT)
    monkeypatch.chdir(tmp_path)
    app = flask.Flask('camera_tool_waits')
    with app.app_context():
        yield types.SimpleNamespace(h=h, store=store)


class TestTheCameraTool:
    def test_a_paused_camera_is_asked_for_and_the_new_frame_is_read(self, tool, reuse, wanted, monkeypatch):
        clock = _Clock(monkeypatch)
        seen = []

        def frame_arrives(c):
            seen.append(wanted(USER))
            if c.sleeps == 6:
                tool.store.put_frame(USER, _jpeg(200), source='ws:127.0.0.1:50000')

        clock.on_sleep = frame_arrives
        frame = tool.h._camera_frame_after_now(USER)
        assert frame is not None and int(frame[5, 5, 0]) > 150
        assert seen and all(seen), 'the camera is needed while the tool waits'
        assert wanted(USER) is False, 'and no longer once it has its frame'
        assert reuse._visual_turn_running == {}

    def test_the_frame_from_before_the_question_is_never_read(self, tool, monkeypatch):
        clock = _Clock(monkeypatch)
        tool.store.put_frame(USER, _jpeg(10), source='ws:127.0.0.1:50000')
        clock.now += 1          # still fresh by the store's age limit
        assert tool.store.get_frame(USER) is not None
        assert tool.h._camera_frame_after_now(USER) is None
        assert clock.now - 1001 >= tool.h.CAMERA_FRAME_WAIT_S - 0.5, 'it waited, bounded'

    def test_an_empty_window_answers_nothing(self, tool, monkeypatch):
        _Clock(monkeypatch)
        assert tool.h._camera_frame_after_now(USER) is None

    def test_with_no_new_frame_the_tool_refuses_and_asks_no_vision_model(self, tool, monkeypatch):
        _Clock(monkeypatch)
        import requests
        monkeypatch.setattr(requests, 'post', lambda *a, **k: (_ for _ in ()).throw(
            AssertionError('a vision model was asked with no new frame')))
        monkeypatch.setattr(tool.h, '_no_camera_frame_reason',
                            lambda inp, user_id: 'no camera frame yet')
        with pytest.raises(RuntimeError, match='no camera frame yet'):
            tool.h.get_user_camera_inp('what am I holding?', USER, 'r-1')

    def test_a_visual_turns_mark_is_left_to_the_turn(self, tool, reuse, monkeypatch):
        clock = _Clock(monkeypatch)
        reuse._visual_turn_running[(USER, AGENT)] = mark = time.monotonic()
        clock.on_sleep = lambda c: tool.store.put_frame(USER, _jpeg(), source='ws:x')
        assert tool.h._camera_frame_after_now(USER) is not None
        assert reuse._visual_turn_running[(USER, AGENT)] is mark


# ── Visual watchers: registered, and gone when they expire ──────────────

def _entry_functions(monkeypatch):
    """_drop_expired_watchers and _handle_visual_watcher_tool as written in
    hart_intelligence_entry.py, over a watcher table of their own."""
    wanted_names = {'_drop_expired_watchers', '_handle_visual_watcher_tool'}
    ns = {'logging': logging, 'thread_local_data': types.SimpleNamespace(
        user=USER, get_user_id=lambda: ns['thread_local_data'].user)}
    found = _cut('hart_intelligence_entry.py',
                 wanted_names | {'WATCHER_TTL_MAX_MINUTES', '_watcher_caption_pattern',
                                 '_WATCHER_COOLDOWN_S', '_evaluate_audio_watchers'}, ns)
    assert wanted_names <= found
    ns['_active_watchers'] = {}
    # The model the one matcher by meaning asks (_evaluate_audio_watchers):
    # ``answer`` is what it replies to a prompt, ``asked`` the prompts.
    import json
    model = types.SimpleNamespace(asked=[], answer=lambda prompt: '[]')

    def _invoke(prompt):
        model.asked.append(prompt)
        return types.SimpleNamespace(content=model.answer(prompt))

    ns.update(json=json, model=model,
              app=types.SimpleNamespace(logger=logging.getLogger('entry')),
              get_llm=lambda **kw: types.SimpleNamespace(invoke=_invoke))
    return ns


@pytest.fixture
def watchers(monkeypatch):
    import core.ai_sensing as sensing
    monkeypatch.setattr(sensing, 'allowed', lambda sensor: True)
    from integrations.vision.frame_store import FrameStore
    import integrations.vision.vision_service as vsmod
    vs = vsmod.VisionService.__new__(vsmod.VisionService)
    vs.store = FrameStore()
    vs._trigger_manager = None
    monkeypatch.setattr(vsmod, 'running_vision_services', lambda: [vs])
    spoken = []
    import core.platform.events as events
    monkeypatch.setattr(events, 'emit_event',
                        lambda topic, data=None, **k: spoken.append((topic, data)))
    clock = types.SimpleNamespace(now=time.time())
    monkeypatch.setattr(time, 'time', lambda: clock.now)
    ns = _entry_functions(monkeypatch)
    return types.SimpleNamespace(ns=ns, vs=vs, spoken=spoken, clock=clock)


def _visual_triggers(vs):
    from integrations.channels.automation.triggers import TriggerType
    return vs._trigger_manager.list_triggers(TriggerType.VISUAL_MATCH) if vs._trigger_manager else []


class TestVisualWatchers:
    def test_a_visual_watcher_is_registered_with_the_running_vision_service(self, watchers):
        watchers.ns['_handle_visual_watcher_tool'](
            'CONDITION: the child holds the ball | ACTION: say well done | TTL: 1')
        assert len(_visual_triggers(watchers.vs)) == 1

    def test_before_it_expires_a_match_speaks_its_action(self, watchers):
        watchers.ns['_handle_visual_watcher_tool'](
            'CONDITION: the child holds the ball | ACTION: say well done | TTL: 1')
        _visual_triggers(watchers.vs)[0].callback(
            {'user_id': USER, 'description': 'the child holds the ball'})
        assert ('tts.speak', {'user_id': USER, 'text': 'say well done'}) in watchers.spoken

    def test_after_it_expires_a_match_speaks_nothing_and_unregisters_it(self, watchers):
        watchers.ns['_handle_visual_watcher_tool'](
            'CONDITION: the child holds the ball | ACTION: say well done | TTL: 1')
        trigger = _visual_triggers(watchers.vs)[0]
        watchers.clock.now += 61
        trigger.callback({'user_id': USER, 'description': 'ball'})
        assert watchers.spoken == []
        assert _visual_triggers(watchers.vs) == []
        assert watchers.ns['_active_watchers'] == {}

    def test_a_new_watcher_drops_the_expired_ones(self, watchers):
        watchers.ns['_handle_visual_watcher_tool'](
            'CONDITION: the child holds the ball | ACTION: say well done | TTL: 1')
        watchers.clock.now += 61
        watchers.ns['_handle_visual_watcher_tool'](
            'CONDITION: the child waves | ACTION: wave back | TTL: 5')
        assert len(_visual_triggers(watchers.vs)) == 1
        assert [w['condition'] for w in watchers.ns['_active_watchers'][USER]] == [
            'the child waves']


# ══ Review of 1fef56008, fixed forward ══════════════════════════════════

def _owner_answers(monkeypatch, outcome_name):
    """The owner's camera answer on file, as consent_service.ask_owner gives it."""
    from integrations.social import consent_service as cs
    asked = []
    monkeypatch.setattr(
        cs, 'ask_owner',
        lambda *a, **k: asked.append((a, k)) or getattr(cs, outcome_name))
    return asked


# ── The chat camera tool never answers from the desktop screen ──────────

@pytest.fixture
def chat_tool(tool, svc, monkeypatch):
    """Visual_Context_Camera: parse_visual_context and get_frame as written in
    hart_intelligence_entry.py, over the camera tool's FrameStore.  The screen
    grab is a stub (a red image): no screen is captured."""
    import PIL.ImageGrab
    from PIL import Image
    svc.store = tool.store
    grabbed = []
    monkeypatch.setattr(
        PIL.ImageGrab, 'grab',
        lambda *a, **k: grabbed.append(1) or Image.new('RGB', (64, 48), (255, 0, 0)))
    import integrations.vision as vision
    monkeypatch.setattr(vision, 'get_vision_service',
                        lambda: types.SimpleNamespace(is_running=lambda: True))
    asked_models = []

    def _post(url, **kwargs):
        asked_models.append(url)
        return types.SimpleNamespace(
            status_code=200,
            json=lambda: {'choices': [{'message': {'content': 'what the model saw'}}]})

    ns = {'np': np, 'os': os, 'Image': Image, 'logging': logging,
          'requests': types.SimpleNamespace(post=_post),
          'app': types.SimpleNamespace(logger=logging.getLogger('entry')),
          'get_frame_store': lambda: tool.store, 'redis_client': None,
          'thread_local_data': types.SimpleNamespace(
              get_user_id=lambda: USER, get_request_id=lambda: 'r-1'),
          'GPT_API': 'http://llm.invalid/v1/chat/completions',
          'LLM_MODEL_NAME': 'm', 'LLM_AUTH_HEADERS': {}}
    assert _cut('hart_intelligence_entry.py',
                {'get_frame', 'parse_visual_context'}, ns) == {
                    'get_frame', 'parse_visual_context'}
    return types.SimpleNamespace(
        ask=ns['parse_visual_context'], store=tool.store, svc=svc,
        grabbed=grabbed, asked_models=asked_models, h=tool.h)


def _image_the_model_was_shown():
    from PIL import Image
    r, g, b = Image.open(os.path.join('output_images', f'{USER}_r-1_call.jpg')
                         ).convert('RGB').getpixel((5, 5))
    return 'the desktop screen' if r > 200 and g < 60 else 'the camera'


class TestTheChatCameraTool:
    def test_a_page_paused_before_its_first_frame_is_never_answered_from_the_screen(
            self, chat_tool, monkeypatch):
        """The measured defect: the page is told to free its camera before its
        first frame, so the store holds nothing and the camera never
        "stopped"; get_frame then handed the camera question the desktop."""
        page = _Page([USER, 'video_start', _jpeg(), _jpeg()])
        _run(chat_tool.svc, page)
        assert _told(page) == ['video_pause'] and chat_tool.store.get_frame(USER) is None
        _owner_answers(monkeypatch, 'OWNER_ALLOWED')
        _Clock(monkeypatch)
        reply = chat_tool.ask('what am I holding?')
        assert chat_tool.asked_models == [], (
            f'a vision model was shown {_image_the_model_was_shown()}')
        assert chat_tool.grabbed == [], 'the screen was grabbed for a camera question'
        from core.tool_refusal import refusal_has_hint
        assert refusal_has_hint(reply), reply
        assert 'Visual_Context_Camera' in reply

    def test_its_question_wakes_the_paused_camera_and_reads_the_new_frame(
            self, chat_tool, wanted, monkeypatch):
        clock = _Clock(monkeypatch)
        needed = []

        def the_page_takes_its_camera_back(c):
            needed.append(wanted(USER))
            if c.sleeps == 6:
                chat_tool.store.put_frame(USER, _jpeg(200), source='ws:127.0.0.1:50000')

        clock.on_sleep = the_page_takes_its_camera_back
        assert chat_tool.ask('what am I holding?') == 'what the model saw'
        assert needed and all(needed), 'the question is a need of the camera while it waits'
        assert _image_the_model_was_shown() == 'the camera'
        assert chat_tool.grabbed == []
        assert wanted(USER) is False, 'the need ends with the call'

    def test_with_no_frame_it_asks_the_owner_through_the_one_ask(self, chat_tool, monkeypatch):
        asked = _owner_answers(monkeypatch, 'OWNER_ASKED')
        _Clock(monkeypatch)
        reply = chat_tool.ask('what am I holding?')
        assert len(asked) == 1 and asked[0][0][0] == 'camera_capture'
        assert 'has been asked to allow the camera' in reply


class TestTheRefusalNamesAPausedCamera:
    def test_an_allowed_camera_with_no_frame_is_not_told_to_open_nunba(self, tool, monkeypatch):
        """Nunba may be open with its camera paused, or held by another app."""
        _owner_answers(monkeypatch, 'OWNER_ALLOWED')
        reason = tool.h._no_camera_frame_reason('what am I holding?', USER)
        assert 'paused' in reason and 'asking again' in reason
        assert 'open Nunba on this computer with a camera connected' not in reason
        assert 'get_user_camera_inp' in reason


# ── The camera tool's mark is its own ───────────────────────────────────

class TestACameraCallBesideAVisualTurn:
    def test_a_waiting_camera_call_does_not_stop_a_visual_turn(self, tool, reuse, monkeypatch):
        clock = _Clock(monkeypatch)
        ran = []
        reuse._run_visual_turn = lambda task, user, agent: ran.append((task, user, agent)) or 'turn ran'
        replies = []

        def a_visual_turn_arrives(c):
            if c.sleeps == 1:
                replies.append(reuse.visual_based_execution('look at the board', USER, AGENT))

        clock.on_sleep = a_visual_turn_arrives
        tool.h._camera_frame_after_now(USER)
        assert replies == ['turn ran'], 'the visual turn was refused while a camera call waited'
        assert ran == [('look at the board', USER, AGENT)]

    def test_a_waiting_camera_call_does_not_lose_a_scheduled_visual_run(self, tool, reuse, monkeypatch):
        clock = _Clock(monkeypatch)
        polled = []

        def _poll(method, url, **k):
            polled.append(url)
            return types.SimpleNamespace(status_code=200, json=lambda: [])

        reuse.ACTION_API = 'http://127.0.0.1:1/create_action'
        reuse.get_local_backend_url = lambda: 'http://127.0.0.1:1'
        reuse.pooled_request = _poll

        def the_task_fires(c):
            if c.sleeps == 1:
                reuse.call_visual_task('watch the child', USER, AGENT)

        clock.on_sleep = the_task_fires
        tool.h._camera_frame_after_now(USER)
        assert polled, 'the scheduled visual run ended at "a turn is running" and was lost'

    def test_a_visual_turn_and_a_camera_call_each_clear_only_their_own_mark(
            self, tool, reuse, wanted, monkeypatch):
        clock = _Clock(monkeypatch)
        during = []

        def the_turn(task, user, agent):
            clock.on_sleep = None
            tool.h._camera_frame_after_now(USER)      # a call inside the turn, no frame
            during.append((reuse._visual_turn_is_running(USER, AGENT), wanted(USER)))
            return 'turn ran'

        reuse._run_visual_turn = the_turn
        assert reuse.visual_based_execution('look', USER, AGENT) == 'turn ran'
        assert during == [(True, True)], "the call's end cleared the turn's mark"
        assert reuse._visual_turn_running == {}


class TestOverlappingCameraCalls:
    """Two calls of one person and agent overlap (real time, two threads):
    while either still waits the camera is still needed."""

    def _overlap(self, tool, monkeypatch, first_wait, second_wait):
        import flask
        app = flask.Flask('overlap')
        monkeypatch.setattr(tool.h, '_CAMERA_FRAME_POLL_S', 0.05)

        def call():
            with app.app_context():
                tool.h._camera_frame_after_now(USER)

        monkeypatch.setattr(tool.h, 'CAMERA_FRAME_WAIT_S', first_wait)
        first = threading.Thread(target=call, daemon=True)
        first.start()
        time.sleep(0.15)
        monkeypatch.setattr(tool.h, 'CAMERA_FRAME_WAIT_S', second_wait)
        second = threading.Thread(target=call, daemon=True)
        second.start()
        return first, second

    def test_the_first_call_to_end_leaves_the_second_ones_need(self, tool, reuse, wanted, monkeypatch):
        first, second = self._overlap(tool, monkeypatch, 0.4, 3.0)
        first.join(5)
        time.sleep(0.2)
        try:
            assert second.is_alive() and not first.is_alive()
            assert wanted(USER) is True, 'the first call to end cleared the only mark'
        finally:
            second.join(10)
        assert wanted(USER) is False
        assert reuse._visual_turn_running == {}

    def test_the_second_call_ending_first_leaves_the_first_ones_need(self, tool, reuse, wanted, monkeypatch):
        first, second = self._overlap(tool, monkeypatch, 3.0, 0.4)
        second.join(5)
        time.sleep(0.2)
        try:
            assert first.is_alive() and not second.is_alive()
            assert wanted(USER) is True, "the second call's end cleared the first one's mark"
        finally:
            first.join(10)
        assert wanted(USER) is False
        assert reuse._visual_turn_running == {}


# ── A turn the 2 s poll started keeps no camera on ──────────────────────

def _poll_job_kwargs():
    """The kwargs reuse_recipe.create_schedule gives the visual_poll job, read
    from its add_job call."""
    src = open(os.path.join(ROOT, 'hartos', 'reuse_recipe.py'), encoding='utf-8').read()
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call) and getattr(node.func, 'attr', '') == 'add_job'):
            continue
        kw = {k.arg: k.value for k in node.keywords}
        args = kw.get('args')
        if (isinstance(args, ast.List) and args.elts
                and isinstance(args.elts[0], ast.Constant)
                and args.elts[0].value == 'get past 1 mins visual information'):
            return ast.literal_eval(kw['kwargs']) if 'kwargs' in kw else {}
    raise AssertionError('no visual_poll add_job found')


def _the_routes_call():
    """The visual_based_execution(...) call of hart_intelligence_entry's
    /visual_agent route, as an expression to evaluate."""
    src = open(os.path.join(ROOT, 'hart_intelligence_entry.py'), encoding='utf-8').read()
    route = next(n for n in ast.parse(src).body
                 if isinstance(n, ast.FunctionDef) and n.name == 'visual_agent')
    call = next(n for n in ast.walk(route) if isinstance(n, ast.Call)
                and getattr(n.func, 'id', '') == 'visual_based_execution')
    return compile(ast.Expression(call), '<the /visual_agent route>', 'eval')


@pytest.fixture
def poll(reuse, wanted):
    """One tick of a scheduled visual job, through to the turn: the action
    rows hold one camera caption 90 s old, and the POST to /visual_agent runs
    the route's own visual_based_execution call."""
    import json
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    rows = [{'action_id': 1, 'zeroshot_label': 'Video Reasoning',
             'action': 'a person at a desk', 'created_date': stamp}]
    reuse.ACTION_API = 'http://127.0.0.1:1/create_action'
    reuse.get_local_backend_url = lambda: 'http://127.0.0.1:1'
    reuse.pooled_request = lambda *a, **k: types.SimpleNamespace(
        status_code=200, json=lambda: rows)
    during = []
    reuse._run_visual_turn = lambda task, user, agent: during.append(wanted(user)) or 'ok'
    route_call = _the_routes_call()

    def post(url, data=None, headers=None, **k):
        body = json.loads(data)
        eval(route_call, {'visual_based_execution': reuse.visual_based_execution,
                          'task_description': body['task_description'],
                          '_uid': body['user_id'], '_pid': body['prompt_id'],
                          'data': body, 'str': str})
        return types.SimpleNamespace(status_code=200)

    reuse.pooled_post = post
    return types.SimpleNamespace(rr=reuse, during=during)


class TestAPollStartedTurn:
    def test_the_poll_on_an_old_caption_starts_a_turn_that_keeps_no_camera_on(self, poll, wanted):
        """Counted as a need, the turn turned the camera on, the camera wrote
        new captions, and the poll started the next turn on them, for good."""
        assert wanted(USER) is False
        assert poll.rr.call_visual_task(
            'get past 1 mins visual information', USER, AGENT, **_poll_job_kwargs()) == 'done'
        assert poll.during == [False], 'the camera was wanted inside the turn the poll started'

    def test_a_visual_tasks_turn_still_holds_the_camera(self, poll):
        assert poll.rr.call_visual_task('watch the child', USER, AGENT) == 'done'
        assert poll.during == [True]

    def test_a_poll_started_turn_still_stops_a_second_turn(self, poll):
        seen = []
        poll.rr._run_visual_turn = lambda task, user, agent: seen.append(
            poll.rr.visual_based_execution('again', user, agent)) or 'ok'
        assert poll.rr.call_visual_task(
            'get past 1 mins visual information', USER, AGENT, **_poll_job_kwargs()) == 'done'
        assert seen == ['A visual turn for this agent is already running.']
        assert poll.rr._visual_turn_running == {}


# ── camera_wanted never ends the page's socket ──────────────────────────

class TestTheSocketOutlivesAFailedAsk:
    def test_a_failing_ask_keeps_the_camera_as_before_and_the_socket_open(self, svc, monkeypatch):
        import core.agent_tools as tools

        def broken(user_id):
            raise TypeError("can't subtract offset-naive and offset-aware datetimes")

        monkeypatch.setattr(tools, 'camera_wanted', broken)
        page = _Page([USER, 'video_start', _jpeg(), _jpeg()])
        _run(svc, page)
        assert _told(page) == []
        assert len(svc.store._frames.get(USER) or []) == 2, 'the socket ended on the failed ask'

    def test_a_job_time_with_no_zone_is_read_as_local_time(self, wanted, reuse):
        reuse.scheduler.jobs.append(
            _Job(f'visual_task_{USER}_{AGENT}_0', datetime.now() + timedelta(seconds=10)))
        try:
            got = wanted(USER)
        except TypeError as e:
            got = e
        assert got is True


# ── Watchers: one person's, the whole condition, their own name, bounded ─

def _caption(watchers, user, text):
    watchers.vs._evaluate_visual_triggers(user, text, 'camera')


class TestWatchersFireOnlyForTheirPersonAndCondition:
    WATCH = 'CONDITION: the child holds the ball | ACTION: say well done | TTL: 1'

    def test_another_persons_caption_never_speaks(self, watchers):
        watchers.ns['_handle_visual_watcher_tool'](self.WATCH)
        _caption(watchers, OTHER, 'the child holds the ball')
        assert watchers.spoken == []

    def test_its_own_persons_matching_caption_speaks(self, watchers):
        watchers.ns['_handle_visual_watcher_tool'](self.WATCH)
        _caption(watchers, USER, 'A child holds a red ball in the garden')
        assert ('tts.speak', {'user_id': USER, 'text': 'say well done'}) in watchers.spoken

    def test_one_word_of_the_condition_is_not_a_match(self, watchers):
        watchers.ns['_handle_visual_watcher_tool'](self.WATCH)
        _caption(watchers, USER, 'a man holds a coffee mug')
        assert watchers.spoken == []

    def test_a_word_inside_another_word_is_not_a_match(self, watchers):
        watchers.ns['_handle_visual_watcher_tool'](self.WATCH)
        _caption(watchers, USER, 'a grandchild upholds the football rules')
        assert watchers.spoken == []

    def test_a_condition_of_short_words_needs_the_whole_condition(self, watchers):
        watchers.ns['_handle_visual_watcher_tool']('CONDITION: a cat is in | ACTION: say cat | TTL: 1')
        _caption(watchers, USER, 'a man holds a coffee mug')
        assert watchers.spoken == []
        _caption(watchers, USER, 'A cat is in the box')
        assert ('tts.speak', {'user_id': USER, 'text': 'say cat'}) in watchers.spoken

    def test_a_person_with_an_int_id_is_matched_to_their_captions(self, watchers):
        watchers.ns['thread_local_data'].user = int(USER)
        watchers.ns['_handle_visual_watcher_tool'](self.WATCH)
        _caption(watchers, USER, 'the child holds the ball')
        assert [topic for topic, _ in watchers.spoken].count('tts.speak') == 1


class TestWatchersHaveTheirOwnNameAndABoundedLife:
    def test_two_watchers_made_in_the_same_second_have_their_own_names(self, watchers):
        watchers.ns['_handle_visual_watcher_tool']('CONDITION: child waves hand | ACTION: wave | TTL: 1')
        watchers.ns['_handle_visual_watcher_tool']('CONDITION: child sits down | ACTION: clap | TTL: 30')
        names = [t.name for t in _visual_triggers(watchers.vs)]
        assert len(set(names)) == 2, names
        watchers.clock.now += 61
        watchers.ns['_drop_expired_watchers']()
        assert [w['condition'] for w in watchers.ns['_active_watchers'][USER]] == ['child sits down']
        assert len(_visual_triggers(watchers.vs)) == 1, (
            'the first to expire removed the trigger of the one still watching')
        _caption(watchers, USER, 'the child sits down on the mat')
        assert ('tts.speak', {'user_id': USER, 'text': 'clap'}) in watchers.spoken

    def test_a_ttl_is_capped(self, watchers):
        reply = watchers.ns['_handle_visual_watcher_tool'](
            'CONDITION: child waves hand | ACTION: wave | TTL: 100000')
        entry = watchers.ns['_active_watchers'][USER][0]
        cap_s = watchers.ns.get('WATCHER_TTL_MAX_MINUTES', 0) * 60
        assert cap_s == 120 * 60
        assert entry['expires_at'] - watchers.clock.now <= cap_s, (
            f"a watcher holds the camera for {(entry['expires_at'] - watchers.clock.now) / 3600:.0f} h")
        assert 'TTL: 120 minutes' in reply, 'the reply states the TTL it was given'


class TestAnOverdueTurnsEnd:
    def test_it_leaves_the_mark_of_the_turn_that_started_since(self, reuse, wanted, monkeypatch):
        """A turn past its budget is ignored, a newer one starts, and the old
        one's end clears only its own mark (two threads, one clock)."""
        clock = _Clock(monkeypatch)
        started = {'old': threading.Event(), 'new': threading.Event()}
        release = {'old': threading.Event(), 'new': threading.Event()}

        def turn(task, user, agent):
            started[task].set()
            release[task].wait(10)
            return 'turn ran'

        reuse._run_visual_turn = turn
        threads = {name: threading.Thread(
            target=reuse.visual_based_execution, args=(name, USER, AGENT), daemon=True)
            for name in ('old', 'new')}
        threads['old'].start()
        assert started['old'].wait(5)
        clock.now += reuse._VISUAL_TURN_MAX_S + 1
        threads['new'].start()
        assert started['new'].wait(5), 'the newer turn did not start past the old mark'
        release['old'].set()
        threads['old'].join(5)
        try:
            assert reuse._visual_turn_is_running(USER, AGENT), (
                "the overdue turn's end cleared the newer turn's mark")
            assert wanted(USER) is True
        finally:
            release['new'].set()
            threads['new'].join(5)
        assert reuse._visual_turn_running == {}


# ══ Review of 47f2252e5 (approved with nits), fixed forward ═════════════

PERSON_TURN = '9f2c1e5a-3b7d-4c2a-9e11-2f6b8d0a4c77'
GOAL_TURN = 'daemon_goal_74416785'
PEER_TURN = 'daemon_a2a_task_9'


def _chat_turn(monkeypatch, request_id, bound=''):
    """A turn as the LangChain chat path leaves it: no bound turn id, and the
    request id /chat set on its thread.  ``bound``: the id REUSE binds for a
    chat_agent turn (core.agent_tools.camera_turn_id)."""
    import core.agent_tools as tools
    from hartos.threadlocal import thread_local_data
    monkeypatch.setattr(thread_local_data, 'get_request_id', lambda: request_id)
    monkeypatch.setattr(tools, 'camera_turn_id', lambda: bound)


# ── A goal or peer turn gets no camera through the chat tools ───────────

class TestABackgroundTurnRegistersNoWatcher:
    WATCH = 'CONDITION: the child holds the ball | ACTION: say well done | TTL: 120'

    @pytest.mark.parametrize('turn, what', [
        (GOAL_TURN, 'works on a goal in the background'),
        (PEER_TURN, "another agent's request")])
    def test_a_goal_or_peer_turn_is_refused_and_nothing_is_registered(
            self, watchers, entry, monkeypatch, turn, what):
        _chat_turn(monkeypatch, turn)
        entry._active_watchers = watchers.ns['_active_watchers']
        reply = watchers.ns['_handle_visual_watcher_tool'](self.WATCH)
        assert watchers.ns['_active_watchers'] == {}, 'a background turn registered a watcher'
        assert _visual_triggers(watchers.vs) == []
        from core.agent_tools import camera_wanted
        assert camera_wanted(USER) is False, "it keeps the person's camera on"
        from core.tool_refusal import refusal_has_hint
        assert refusal_has_hint(reply), reply
        assert 'Visual_Context_Watcher' in reply and what in reply, reply

    def test_an_audio_watcher_of_a_background_turn_is_refused_too(self, watchers, monkeypatch):
        _chat_turn(monkeypatch, GOAL_TURN)
        watchers.ns['_handle_visual_watcher_tool'](
            'CONDITION: user mentions the dog | ACTION: say vet | TTL: 60 | MODALITY: audio')
        assert watchers.ns['_active_watchers'] == {}

    def test_reuses_tool_is_refused_under_its_own_name(self, watchers, monkeypatch):
        """REUSE's register_visual_watcher calls the same function on a
        chat_agent turn, whose id is bound."""
        _chat_turn(monkeypatch, None, bound=GOAL_TURN)
        watchers.ns['_handle_visual_watcher_tool'](self.WATCH)
        assert watchers.ns['_active_watchers'] == {}
        reply = watchers.ns['_handle_visual_watcher_tool'](
            self.WATCH, tool='register_visual_watcher')
        assert watchers.ns['_active_watchers'] == {}
        assert 'register_visual_watcher' in reply and 'Visual_Context_Watcher' not in reply
        src = open(os.path.join(ROOT, 'hartos', 'reuse_recipe.py'), encoding='utf-8').read()
        assert "_handle(input_text, tool='register_visual_watcher')" in src

    def test_the_bound_turn_id_is_read_before_the_threads(self, watchers, monkeypatch):
        """A goal turn that runs on a thread still holding a person's /chat id."""
        _chat_turn(monkeypatch, PERSON_TURN, bound=GOAL_TURN)
        reply = watchers.ns['_handle_visual_watcher_tool'](self.WATCH)
        assert watchers.ns['_active_watchers'] == {}
        assert 'works on a goal in the background' in reply

    def test_a_persons_chat_turn_registers_it(self, watchers, entry, monkeypatch):
        """The LangChain path binds no turn id: a person is told apart there
        by the request id on the thread, and is not refused."""
        _chat_turn(monkeypatch, PERSON_TURN)
        entry._active_watchers = watchers.ns['_active_watchers']
        reply = watchers.ns['_handle_visual_watcher_tool'](self.WATCH)
        assert reply.startswith('Watcher registered'), reply
        assert len(_visual_triggers(watchers.vs)) == 1
        from core.agent_tools import camera_wanted
        assert camera_wanted(USER) is True

    def test_a_persons_reuse_turn_registers_it(self, watchers, monkeypatch):
        _chat_turn(monkeypatch, None, bound=PERSON_TURN)
        reply = watchers.ns['_handle_visual_watcher_tool'](self.WATCH)
        assert reply.startswith('Watcher registered'), reply


class TestABackgroundTurnGetsNoCameraFrame:
    @pytest.mark.parametrize('turn, what', [
        (GOAL_TURN, 'works on a goal in the background'),
        (PEER_TURN, "another agent's request")])
    def test_a_goal_or_peer_turn_is_refused_before_the_camera_is_woken(
            self, chat_tool, wanted, monkeypatch, turn, what):
        _chat_turn(monkeypatch, turn)
        asked = _owner_answers(monkeypatch, 'OWNER_ASKED')
        clock = _Clock(monkeypatch)
        needed = []
        clock.on_sleep = lambda c: (
            needed.append(wanted(USER)),
            chat_tool.store.put_frame(USER, _jpeg(200), source='ws:127.0.0.1:50000'))
        reply = chat_tool.ask('what is on the desk?')
        assert clock.sleeps == 0 and needed == [], (
            "a background turn held the person's camera while it waited")
        assert chat_tool.asked_models == [], 'the frame was sent to the model'
        assert asked == [], "the owner's camera ask was filed for a background turn"
        assert chat_tool.grabbed == []
        from core.tool_refusal import refusal_has_hint
        assert refusal_has_hint(reply), reply
        assert 'Visual_Context_Camera' in reply and what in reply, reply

    def test_a_persons_chat_turn_still_sees_through_the_camera(
            self, chat_tool, wanted, monkeypatch):
        _chat_turn(monkeypatch, PERSON_TURN)
        clock = _Clock(monkeypatch)
        clock.on_sleep = lambda c: c.sleeps == 6 and chat_tool.store.put_frame(
            USER, _jpeg(200), source='ws:127.0.0.1:50000')
        assert chat_tool.ask('what am I holding?') == 'what the model saw'
        assert _image_the_model_was_shown() == 'the camera'
        assert chat_tool.grabbed == []


class TestTheOneRefusalOfAChatCameraTool:
    @staticmethod
    def _the_refusal():
        import core.agent_tools as tools
        refusal = getattr(tools, 'camera_refusal_on_a_background_turn', None)
        assert refusal is not None, 'the chat camera tools have no refusal for a background turn'
        return refusal

    def test_it_is_none_for_a_person_and_for_a_run_with_no_id(self, monkeypatch):
        camera_refusal_on_a_background_turn = self._the_refusal()
        for request_id, bound in ((PERSON_TURN, ''), (None, ''), ('', PERSON_TURN),
                                  (GOAL_TURN, PERSON_TURN)):
            _chat_turn(monkeypatch, request_id, bound)
            assert camera_refusal_on_a_background_turn('Visual_Context_Camera') is None, (
                request_id, bound)

    def test_it_names_the_turn_by_the_id_the_check_read(self, monkeypatch):
        from core.agent_tools import camera_refusal
        camera_refusal_on_a_background_turn = self._the_refusal()
        _chat_turn(monkeypatch, PEER_TURN)
        reply = camera_refusal_on_a_background_turn('Visual_Context_Camera')
        assert "another agent's request" in reply
        assert "answers no person's message" not in reply, (
            'the turn was named from the bound id, which the chat path leaves empty')
        # Left out, the bound id is named, as the other camera tools use it.
        assert "answers no person's message" in camera_refusal('get_user_camera_inp')


# ── A visual watcher is matched by meaning, by the one matcher ──────────

def _prompt_conditions(prompt):
    return [line for line in prompt.splitlines() if line.startswith('- Watcher ')]


class TestAVisualWatcherIsMatchedByMeaning:
    WATCH = 'CONDITION: the child holds the ball | ACTION: say well done | TTL: 1'
    SPOKE = ('tts.speak', {'user_id': USER, 'text': 'say well done'})

    @pytest.mark.parametrize('caption', [
        'A child is holding a red ball', 'The kid holds a ball'])
    def test_a_caption_that_means_the_condition_speaks(self, watchers, caption):
        watchers.ns['model'].answer = lambda prompt: 'Triggered: [1]'
        watchers.ns['_handle_visual_watcher_tool'](self.WATCH)
        _caption(watchers, USER, caption)
        assert self.SPOKE in watchers.spoken
        (prompt,) = watchers.ns['model'].asked
        assert f'The user\'s camera now shows: "{caption}"' in prompt
        assert _prompt_conditions(prompt) == ['- Watcher 1: the child holds the ball']
        assert 'triggered by what the camera shows?' in prompt

    def test_a_caption_the_matcher_does_not_pass_speaks_nothing(self, watchers):
        watchers.ns['_handle_visual_watcher_tool'](self.WATCH)
        _caption(watchers, USER, 'a man holds a coffee mug')
        assert watchers.spoken == []
        assert len(watchers.ns['model'].asked) == 1

    def test_a_caption_with_every_key_word_speaks_without_asking(self, watchers):
        watchers.ns['get_llm'] = lambda **kw: pytest.fail('the matcher was asked')
        watchers.ns['_handle_visual_watcher_tool'](self.WATCH)
        _caption(watchers, USER, 'A child holds a red ball in the garden')
        assert self.SPOKE in watchers.spoken

    def test_a_matcher_that_cannot_be_asked_speaks_nothing_and_raises_nothing(self, watchers):
        def down(**kw):
            raise ConnectionError('no model')

        watchers.ns['get_llm'] = down
        watchers.ns['_handle_visual_watcher_tool'](self.WATCH)
        _visual_triggers(watchers.vs)[0].callback(
            {'user_id': USER, 'description': 'The kid holds a ball'})
        assert watchers.spoken == []

    def test_a_caption_is_put_to_its_own_watcher_alone(self, watchers):
        """Each watcher has its own trigger: asked about every watcher of the
        person, two watchers would each speak both actions."""
        watchers.ns['model'].answer = lambda prompt: (
            '[1]' if 'Watcher 1: the child holds the ball' in prompt else '[]')
        watchers.ns['_handle_visual_watcher_tool'](self.WATCH)
        watchers.ns['_handle_visual_watcher_tool'](
            'CONDITION: someone waves | ACTION: wave back | TTL: 1')
        watchers.ns['_handle_visual_watcher_tool'](
            'CONDITION: user mentions the dog | ACTION: say vet | TTL: 1 | MODALITY: audio')
        _caption(watchers, USER, 'The kid holds a ball')
        asked = [_prompt_conditions(p) for p in watchers.ns['model'].asked]
        assert sorted(asked) == [['- Watcher 1: someone waves'],
                                 ['- Watcher 1: the child holds the ball']], asked
        assert [d['text'] for t, d in watchers.spoken if t == 'tts.speak'] == ['say well done']

    def test_another_persons_caption_asks_nothing(self, watchers):
        watchers.ns['_handle_visual_watcher_tool'](self.WATCH)
        _caption(watchers, OTHER, 'The kid holds a ball')
        assert watchers.ns['model'].asked == [] and watchers.spoken == []

    def test_an_empty_condition_watches_for_nothing(self, watchers):
        watchers.ns['model'].answer = lambda prompt: '[1]'
        watchers.ns['_handle_visual_watcher_tool']('CONDITION:  | ACTION: say hello | TTL: 1')
        _caption(watchers, USER, 'The kid holds a ball')
        assert watchers.ns['model'].asked == [] and watchers.spoken == []

    def test_an_expired_watcher_asks_nothing(self, watchers):
        watchers.ns['_handle_visual_watcher_tool'](self.WATCH)
        trigger = _visual_triggers(watchers.vs)[0]
        watchers.clock.now += 61
        trigger.callback({'user_id': USER, 'description': 'The kid holds a ball'})
        assert watchers.ns['model'].asked == [] and watchers.spoken == []

    def test_the_caption_after_one_that_did_not_match_is_still_read(self, watchers):
        """Captions come seconds apart; a cooldown on the trigger would start
        at the caption that did not match and skip the next one."""
        watchers.ns['_handle_visual_watcher_tool'](self.WATCH)
        _caption(watchers, USER, 'a man holds a coffee mug')
        _caption(watchers, USER, 'the child holds the ball')
        assert self.SPOKE in watchers.spoken

    def test_between_two_fires_the_cooldown_is_kept(self, watchers):
        watchers.ns['model'].answer = lambda prompt: '[1]'
        watchers.ns['_handle_visual_watcher_tool'](self.WATCH)
        speaks = lambda: [t for t, _ in watchers.spoken].count('tts.speak')
        _caption(watchers, USER, 'the child holds the ball')
        watchers.clock.now += watchers.ns.get('_WATCHER_COOLDOWN_S', 5) - 1
        _caption(watchers, USER, 'the child holds the ball')
        _caption(watchers, USER, 'The kid holds a ball')
        assert speaks() == 1 and watchers.ns['model'].asked == [], (
            'a caption inside the cooldown spoke, or asked the matcher')
        watchers.clock.now += 1
        _caption(watchers, USER, 'The kid holds a ball')
        assert speaks() == 2

    def test_the_audio_evaluation_asks_as_it_did(self, watchers):
        fired = []
        watchers.ns['_active_watchers'][USER] = [
            {'trigger_id': 'a', 'modality': 'audio', 'expires_at': watchers.clock.now + 60,
             'condition': 'user mentions the dog', 'callback': fired.append},
            {'trigger_id': 'v', 'modality': 'visual', 'expires_at': watchers.clock.now + 60,
             'condition': 'the child holds the ball', 'callback': fired.append}]
        watchers.ns['model'].answer = lambda prompt: '[1]'
        watchers.ns['_evaluate_audio_watchers'](USER, 'my puppy walked in')
        (prompt,) = watchers.ns['model'].asked
        assert prompt == (
            'The user just said: "my puppy walked in"\n\n'
            'Active watchers:\n- Watcher 1: user mentions the dog\n\n'
            'Which watcher conditions (if any) are semantically triggered by what the user said? '
            'Return ONLY a JSON array of triggered watcher numbers, e.g. [1, 3]. '
            'Return [] if none match.')
        assert fired == ['my puppy walked in']


# ── With the vision service off the tool waits once ─────────────────────

@pytest.fixture
def idle_service(chat_tool, monkeypatch):
    """The chat camera tool with the VisionService stopped: what it did, in
    order ('start', 'wait')."""
    did = []
    service = types.SimpleNamespace(running=False)
    service.is_running = lambda: service.running

    def start(mode=None):
        did.append('start')
        service.running = True

    service.start = start
    import integrations.vision as vision
    monkeypatch.setattr(vision, 'get_vision_service', lambda: service)
    real_wait = chat_tool.h._camera_frame_after_now

    def wait(user_id):
        did.append('wait')
        return real_wait(user_id)

    monkeypatch.setattr(chat_tool.h, '_camera_frame_after_now', wait)
    _owner_answers(monkeypatch, 'OWNER_ALLOWED')
    return types.SimpleNamespace(did=did, service=service, tool=chat_tool,
                                 clock=_Clock(monkeypatch))


class TestTheChatCameraToolWaitsOnce:
    def test_a_stopped_service_is_started_before_the_one_wait(self, idle_service):
        t0 = idle_service.clock.now
        reply = idle_service.tool.ask('what am I holding?')
        assert idle_service.did == ['start', 'wait'], idle_service.did
        waited = idle_service.clock.now - t0
        assert waited <= idle_service.tool.h.CAMERA_FRAME_WAIT_S + 0.5, (
            f'the person waited {waited:.0f} s for the refusal')
        assert 'Visual_Context_Camera' in reply

    def test_the_frame_a_started_service_brings_is_read(self, idle_service):
        tool = idle_service.tool
        idle_service.clock.on_sleep = lambda c: (
            idle_service.service.running and c.sleeps == 6
            and tool.store.put_frame(USER, _jpeg(200), source='ws:127.0.0.1:50000'))
        assert tool.ask('what am I holding?') == 'what the model saw'
        assert idle_service.did == ['start', 'wait']

    def test_a_service_still_coming_up_is_not_started_or_waited_for_twice(self, idle_service):
        idle_service.service.start = lambda mode=None: idle_service.did.append('start')
        idle_service.tool.ask('what am I holding?')
        assert idle_service.did == ['start', 'wait'], idle_service.did

    def test_a_running_service_is_not_started_and_is_waited_for_once(self, idle_service):
        idle_service.service.running = True
        idle_service.tool.ask('what am I holding?')
        assert idle_service.did == ['wait']

    def test_a_phone_streaming_is_read_without_starting_the_service(self, idle_service):
        tool = idle_service.tool
        tool.store.put_frame(USER, _jpeg(), source='device:phone-1')
        idle_service.clock.on_sleep = lambda c: c.sleeps == 2 and tool.store.put_frame(
            USER, _jpeg(200), source='device:phone-1')
        assert tool.ask('what am I holding?') == 'what the model saw'
        assert idle_service.did == ['wait'], idle_service.did

    def test_without_an_in_process_store_it_reads_first_as_before(self, idle_service, monkeypatch):
        """A cloud camera pipeline: the frame is read through get_frame, and
        the service is started only when that read had nothing."""
        import core.safe_hartos_attr as sha
        monkeypatch.setattr(sha, 'safe_hartos_attr', lambda name, default=None: default)
        monkeypatch.setattr(idle_service.tool.h, 'get_frame', lambda user_id: None)
        idle_service.tool.ask('what am I holding?')
        assert idle_service.did == ['wait', 'start', 'wait'], idle_service.did

    def test_a_service_that_cannot_start_ends_in_the_refusal(self, idle_service):
        def broken(mode=None):
            idle_service.did.append('start')
            raise OSError('port in use')

        idle_service.service.start = broken
        reply = idle_service.tool.ask('what am I holding?')
        assert idle_service.did == ['start', 'wait', 'start'], idle_service.did
        from core.tool_refusal import refusal_has_hint
        assert refusal_has_hint(reply), reply


# ── A failing ask is logged in full once per socket ─────────────────────

class TestAFailingAskIsLoggedOnce:
    def _logged(self, caplog):
        return [r for r in caplog.records if 'camera_wanted' in r.getMessage()]

    def test_the_traceback_once_then_one_line(self, svc, monkeypatch, caplog):
        import core.agent_tools as tools
        import integrations.vision.vision_service as vsmod

        def broken(user_id):
            raise TypeError("can't subtract offset-naive and offset-aware datetimes")

        monkeypatch.setattr(tools, 'camera_wanted', broken)
        monkeypatch.setattr(vsmod, 'CAMERA_WANTED_RECHECK_S', 0.0)
        caplog.set_level(logging.WARNING, logger=vsmod.logger.name)
        page = _Page([USER, 'video_start', _jpeg(), _jpeg(), _jpeg()])
        _run(svc, page)
        assert _told(page) == [] and len(svc.store._frames[USER]) == 3
        logged = self._logged(caplog)
        assert len(logged) == 4, 'every failed ask is on the log'
        assert [bool(r.exc_info) for r in logged] == [True, False, False, False], (
            'the traceback was logged again')
        assert all('offset-naive' in r.getMessage() and USER in r.getMessage()
                   for r in logged[1:]), 'the short line says who and what failed'

    def test_a_failure_after_an_ask_that_worked_is_logged_in_full_again(
            self, svc, monkeypatch, caplog):
        import core.agent_tools as tools
        import integrations.vision.vision_service as vsmod
        answers = iter([TypeError('first'), TypeError('first'), True, TypeError('second')])

        def flaky(user_id):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(tools, 'camera_wanted', flaky)
        monkeypatch.setattr(vsmod, 'CAMERA_WANTED_RECHECK_S', 0.0)
        caplog.set_level(logging.WARNING, logger=vsmod.logger.name)
        _run(svc, _Page([USER, 'video_start', _jpeg(), _jpeg(), _jpeg()]))
        assert [bool(r.exc_info) for r in self._logged(caplog)] == [True, False, True]
