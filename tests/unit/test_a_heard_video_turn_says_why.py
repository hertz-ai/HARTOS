"""A video turn that is heard and not seen says why, once (review G6).

Live 2026-11-26 (request ids 1795704815, 1795705194 on build #5): two video turns were heard,
the face sidecar was READY, and not one line said which check sent them to the voice: every
early return of add_live_avatar / live_session was silent.  Each now logs one INFO line naming
the request and the value that failed; what each returns is unchanged.
"""
import ast
import inspect
import logging
import re
import threading
from collections import deque
from concurrent.futures import Future

import pytest

from integrations.service_tools import avatarforcing_tool as af
from integrations.service_tools import runtime_manager as rm

RID = 'r-g6'


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._body = code, body

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, caplog):
    monkeypatch.setattr(af, '_portraits', {})
    monkeypatch.setattr(af, '_setup', {'started_at': 0.0, 'failed': False, 'future': None})
    monkeypatch.setattr(af, '_owners', af.OrderedDict())
    monkeypatch.setattr(af, '_backoff', {'until': 0.0})
    import integrations.service_tools.model_lifecycle as ml
    monkeypatch.setattr(ml, 'get_model_lifecycle_manager',
                        lambda: type('M', (), {'notify_access': lambda self, t: None})())
    monkeypatch.setattr(af, '_late', deque(maxlen=64))
    caplog.set_level(logging.INFO, logger=af.logger.name)


@pytest.fixture
def bundle():
    return {'generated_audio_url': '/tts/audio/a.wav', 'request_id': RID, 'action': 'TTS',
            'video_link': {'aud_url': 'http://10.0.0.5:5000/tts/audio/a.wav', 'request_id': RID,
                           'action': 'TTS', 'is_cartoon': True, 'priority': 0}}


@pytest.fixture
def audio(tmp_path):
    a = tmp_path / 'a.wav'
    a.write_bytes(b'wav')
    return str(a)


@pytest.fixture
def node(monkeypatch, tmp_path):
    """A node where the row is on, avatar 7 has a portrait, the GPU has room, and the model is
    not running (each test changes the one thing it is about)."""
    face = tmp_path / 'face.png'
    face.write_bytes(b'png')
    monkeypatch.setattr(rm.runtime_tool_manager, 'tool_enabled', lambda t: True)
    monkeypatch.setattr(rm.runtime_tool_manager, 'get_tool_port', lambda t: None)
    monkeypatch.setattr('core.teacher_avatar.image_reference',
                        lambda a: str(face) if a == 7 else None)
    monkeypatch.setattr(rm.runtime_tool_manager.vram, 'detect_gpu', lambda: {'cuda_available': True})
    monkeypatch.setattr(rm.runtime_tool_manager.vram, 'suggest_offload_mode', lambda t, need=None: 'gpu')
    monkeypatch.setattr(rm.runtime_tool_manager.vram, 'get_free_vram', lambda: 2.0)
    # the advisor's reading was taken 5 s ago (vram_manager's memo stamp)
    monkeypatch.setattr(rm.runtime_tool_manager.vram, '_gpu_info_ts', af.time.monotonic() - 5)
    monkeypatch.setattr(rm.runtime_tool_manager.vram, '_gpu_info',
                        {'free_gb': 2.0, 'cuda_available': True})
    started = []

    def setup_async(tool):
        started.append(tool)
        f = Future()
        f.set_result({'port': 4321})
        return f
    monkeypatch.setattr(rm.runtime_tool_manager, 'setup_tool_async', setup_async)
    return started


def _why(caplog):
    """The reason lines logged for RID."""
    return [r.getMessage() for r in caplog.records
            if f'request {RID} heard, not seen' in r.getMessage()]


def _turn(bundle, audio, avatar=7, mode='video'):
    return af.add_live_avatar(bundle, audio_path=audio, avatar_id=avatar, media_mode=mode)


@pytest.mark.parametrize('mode', ['audio', None])
def test_a_turn_that_is_not_video_says_its_mode(bundle, audio, node, caplog, mode):
    assert _turn(bundle, audio, mode=mode) is False
    (line,) = _why(caplog)
    assert f'media_mode={mode!r}' in line


def test_a_video_turn_without_an_avatar_says_so(bundle, audio, node, caplog):
    assert _turn(bundle, audio, avatar=None) is False
    (line,) = _why(caplog)
    assert 'avatar=None' in line and "media_mode='video'" in line


def test_a_turn_in_the_back_off_says_how_long_is_left(bundle, audio, node, caplog, monkeypatch):
    monkeypatch.setitem(af._backoff, 'until', af.time.time() + 20)
    assert _turn(bundle, audio) is False
    (line,) = _why(caplog)
    assert 'back-off left' in line


def test_a_switched_off_row_says_so(bundle, audio, node, caplog, monkeypatch):
    monkeypatch.setattr(rm.runtime_tool_manager, 'tool_enabled', lambda t: False)
    assert _turn(bundle, audio) is False
    (line,) = _why(caplog)
    assert 'switched off' in line


def test_no_room_names_the_answer_and_both_free_vram_figures(bundle, audio, node, caplog,
                                                             monkeypatch):
    """No mode a person can watch live here: the advisor, asked need='live', answers 'cpu_only'
    (offload patch 03/04: an offload mode is answered for 'live' only once measured at real time)."""
    asked = []
    monkeypatch.setattr(rm.runtime_tool_manager.vram, 'suggest_offload_mode',
                        lambda t, need=None: asked.append(need) or 'cpu_only')
    monkeypatch.setattr(rm.runtime_tool_manager.vram, 'get_free_vram', lambda: 0.6)
    assert _turn(bundle, audio) is False
    (line,) = _why(caplog)
    assert "live mode answer 'cpu_only'" in line
    assert re.search(r"free 0\.6 GB \(read \d+ s ago\)", line), line
    assert node == []  # nothing set up, as before
    assert set(asked) == {'live'}  # the face only ever asks for a mode a person can watch


def test_an_offload_mode_measured_live_here_sets_the_face_up(bundle, audio, node, caplog,
                                                             monkeypatch):
    """The owner's 11-26 rule, offload or GPU by the resources available: an offload mode the
    advisor answers for 'live' was measured at real time on this machine, so the face is set up."""
    monkeypatch.setattr(rm.runtime_tool_manager.vram, 'suggest_offload_mode',
                        lambda t, need=None: 'cpu_audio' if need == 'live' else 'cpu_only')
    assert _turn(bundle, audio) is False  # this turn is heard while it sets up
    (line,) = _why(caplog)
    assert 'setting it up' in line and node == ['avatarforcing']


def test_a_reading_never_timed_says_its_age_is_unknown(bundle, audio, node, caplog, monkeypatch):
    monkeypatch.setattr(rm.runtime_tool_manager.vram, 'suggest_offload_mode', lambda t, need=None: 'cpu_only')
    monkeypatch.setattr(rm.runtime_tool_manager.vram, '_gpu_info_ts', 0.0)
    assert _turn(bundle, audio) is False
    (line,) = _why(caplog)
    assert 'free 2.0 GB (age unknown)' in line


def test_no_reading_yet_is_never_a_stale_zero(bundle, audio, node, caplog, monkeypatch):
    monkeypatch.setattr(rm.runtime_tool_manager.vram, 'suggest_offload_mode', lambda t, need=None: 'cpu_only')
    monkeypatch.setattr(rm.runtime_tool_manager.vram, 'get_free_vram', lambda: 0.0)
    monkeypatch.setattr(rm.runtime_tool_manager.vram, '_gpu_info', None)
    assert _turn(bundle, audio) is False
    (line,) = _why(caplog)
    assert 'no reading yet' in line and '0.0 GB' not in line


def test_a_log_line_never_reads_the_card(monkeypatch):
    """The report prints only what the advisor already read (rv_g6): the face tool starts no
    process of its own (no second nvidia-smi path, no console window), never refreshes the
    GPU reading (a refresh could hold the face worker up to the probe's 15 s, races the
    memo, and changes what other decisions read), and repeated reports cost no new read."""
    tree = ast.parse(inspect.getsource(af))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert 'subprocess' not in imported
    import importlib
    import core.subprocess_safe as ss
    # the module, not the package's `vram_manager` instance of the same name
    vm = importlib.import_module('integrations.service_tools.vram_manager')
    calls = []

    def bounded(cmd, timeout=5.0, **kw):
        calls.append(list(cmd))
        return type('R', (), {'returncode': 0, 'stdout': 'RTX 3070, 8192, 1600, 560.94\n',
                              'stderr': ''})()
    monkeypatch.setattr(ss, 'run_bounded', bounded)
    monkeypatch.setattr(vm, '_resolve_nvidia_smi', lambda: 'resolved-nvidia-smi')
    manager = vm.VRAMManager()
    manager._vendor_tools_absent = False

    def no_refresh(*a, **k):
        raise AssertionError('a log line refreshed the GPU reading')
    monkeypatch.setattr(manager, 'refresh_gpu_info', no_refresh)
    rtm = type('RTM', (), {'vram': manager})()
    reports = [af._room_report(rtm) for _ in range(5)]
    assert len(calls) == 1  # the advisor's own first read (detect_gpu's memo), nothing more
    assert all('free 1.56 GB' in r for r in reports)


def test_no_portrait_says_which_avatar(bundle, audio, node, caplog):
    assert _turn(bundle, audio, avatar=8) is False
    (line,) = _why(caplog)
    assert 'avatar 8 has no portrait' in line


def test_a_setup_it_starts_says_so_and_one_already_running_says_so(bundle, audio, node, caplog,
                                                                   monkeypatch):
    assert _turn(bundle, audio) is False
    (line,) = _why(caplog)
    assert 'setting it up' in line and node == ['avatarforcing']
    caplog.clear()
    pending = Future()  # a setup still running
    monkeypatch.setitem(af._setup, 'future', pending)
    assert _turn(bundle, audio) is False
    (line,) = _why(caplog)
    assert 'its setup is still running' in line and node == ['avatarforcing']
    caplog.clear()
    pending.set_result({'port': 4321})  # ended a moment ago: the retry window
    assert _turn(bundle, audio) is False
    (line,) = _why(caplog)
    assert 'the last setup began' in line and node == ['avatarforcing']


def test_a_model_still_loading_says_503(bundle, audio, node, caplog, monkeypatch):
    monkeypatch.setattr(rm.runtime_tool_manager, 'get_tool_port', lambda t: 4321)
    monkeypatch.setattr('core.http_pool.pooled_post_once',
                        lambda *a, **k: _Resp(503, {'error': 'loading'}))
    assert _turn(bundle, audio) is False
    (line,) = _why(caplog)
    assert 'still loading (503' in line
    assert 'generated_video_url' not in bundle


def test_a_slow_face_says_the_budget_ran_out_once(bundle, audio, node, caplog, monkeypatch):
    """Past the budget the turn says so; the worker's own reason, when it comes later, is
    DEBUG -- one INFO line per heard turn (review rv_g6 nit)."""
    caplog.set_level(logging.DEBUG, logger=af.logger.name)
    release = threading.Event()
    done = threading.Event()
    monkeypatch.setattr(af, 'SESSION_BUDGET_S', 0.2)

    def slow_portrait(a):
        release.wait(5)
        done.set()
        return None  # then live_session says "no portrait"
    monkeypatch.setattr('core.teacher_avatar.image_reference', slow_portrait)
    try:
        assert _turn(bundle, audio) is False
    finally:
        release.set()  # the shared worker is free for the next test
    assert done.wait(5)
    af._worker.submit(lambda: None).result(timeout=5)  # the worker has logged by now
    info = [r.getMessage() for r in caplog.records
            if r.levelno == logging.INFO and f'request {RID} heard, not seen' in r.getMessage()]
    assert len(info) == 1 and 'no face within' in info[0]
    assert any('has no portrait' in r.getMessage() and r.levelno == logging.DEBUG
               for r in caplog.records)  # the worker's reason, kept, at DEBUG


def test_a_refused_session_says_its_status(bundle, audio, node, caplog, monkeypatch):
    monkeypatch.setattr(rm.runtime_tool_manager, 'get_tool_port', lambda t: 4321)
    monkeypatch.setattr('core.http_pool.pooled_post_once',
                        lambda *a, **k: _Resp(409, {'error': 'busy'}))
    assert _turn(bundle, audio) is False
    (line,) = _why(caplog)
    assert 'session refused (409): busy' in line


def test_no_answer_says_so_and_the_next_turn_says_back_off(bundle, audio, node, caplog,
                                                           monkeypatch):
    monkeypatch.setattr(rm.runtime_tool_manager, 'get_tool_port', lambda t: 4321)

    def boom(*a, **k):
        raise ConnectionError('refused')
    monkeypatch.setattr('core.http_pool.pooled_post_once', boom)
    assert _turn(bundle, audio) is False
    (line,) = _why(caplog)
    assert 'no session for this reply (ConnectionError: refused)' in line
    caplog.clear()
    assert _turn(bundle, audio) is False
    (line,) = _why(caplog)
    assert 'back-off left' in line


def test_a_face_lookup_that_raises_says_so(bundle, audio, node, caplog, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError('worker broke')
    monkeypatch.setattr(af, 'live_session', broken)
    assert _turn(bundle, audio) is False
    (line,) = _why(caplog)
    assert 'live avatar skipped: RuntimeError: worker broke' in line


def test_a_setup_the_gpu_lost_room_for_says_so(bundle, audio, node, caplog, monkeypatch):
    """Room at the first look (live_session), none by the setup's own look (_start_setup)."""
    answers = iter([True, False])
    monkeypatch.setattr(af, '_can_run_here', lambda rtm: next(answers))
    assert _turn(bundle, audio) is False
    (line,) = _why(caplog)
    assert 'not set up: no CUDA GPU with room' in line and node == []


def test_a_seen_turn_logs_no_reason(bundle, audio, node, caplog, monkeypatch):
    monkeypatch.setattr(rm.runtime_tool_manager, 'get_tool_port', lambda t: 4321)
    monkeypatch.setattr('core.http_pool.pooled_post_once',
                        lambda *a, **k: _Resp(200, {'sid': 'abcdef012345', 'frames': 50, 'fps': 25}))
    assert _turn(bundle, audio) is True
    assert _why(caplog) == []
