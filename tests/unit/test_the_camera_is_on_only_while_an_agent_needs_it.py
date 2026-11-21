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
VisionService handler, FrameStore, camera_wanted, the camera tool's wait and
the watcher functions (cut from hart_intelligence_entry.py) are real.

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


@pytest.fixture
def reuse(monkeypatch):
    """hartos.reuse_recipe's records: the scheduler, the visual-turn marks."""
    rr = types.ModuleType('hartos.reuse_recipe')
    rr.scheduler = _Scheduler()
    rr._VISUAL_TURN_MAX_S = 300
    rr._visual_turn_running = {}
    rr._visual_turn_lock = threading.Lock()

    def _visual_turn_is_running(user_id, prompt_id):
        began = rr._visual_turn_running.get((str(user_id), str(prompt_id)))
        return began is not None and time.monotonic() - began < rr._VISUAL_TURN_MAX_S

    rr._visual_turn_is_running = _visual_turn_is_running
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
    src = open(os.path.join(ROOT, 'hart_intelligence_entry.py'), encoding='utf-8').read()
    wanted_names = {'_drop_expired_watchers', '_handle_visual_watcher_tool'}
    nodes = [n for n in ast.parse(src).body
             if isinstance(n, ast.FunctionDef) and n.name in wanted_names]
    assert {n.name for n in nodes} == wanted_names
    ns = {'_active_watchers': {}, 'logging': logging,
          'thread_local_data': types.SimpleNamespace(get_user_id=lambda: USER)}
    for n in nodes:
        exec(ast.get_source_segment(src, n), ns)
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
        _visual_triggers(watchers.vs)[0].callback({'user_id': USER, 'description': 'ball'})
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
