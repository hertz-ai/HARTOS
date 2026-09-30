"""The owner's No to the camera or the screen takes effect at once, even when
every shared background worker is busy.

8d8ac0a11 moved the feed answer (the admin config save and the VisionService
start/stop) off the request thread onto the SHARED parallel_dispatch pool,
the same 8 workers the agent daemon fills with /chat jobs
(agent_daemon -> dispatch_parallel_tasks -> get_executor).  Measured by the
review: with the 8 workers held, a revoke did nothing for 3 s.  Two
consequences, both pinned here:

  * capture went on after the owner said No: nothing on the capture path
    (FrameStore, the screen-capture loop) read any in-memory answer, so it
    stopped only when VisionService.stop() finally ran;
  * a No queued behind a hung start was never saved, so the config still
    said "on" after a restart.  Measured at HEAD separately: _save_config
    never wrote embodied_ai at all, so no No ever survived a restart.

Now the No closes core.ai_sensing's gate on the caller's thread, the flag is
saved there too, and only the slow hardware start/stop runs off-thread, on a
worker of its own (admin.api._FEED_WORKER), never on the shared pool.

Real ConsentService, real file-backed SQLite, real AdminAPI writing a real
file under tmp_path, real FrameStore, real shared pool held busy.  Stand-ins:
the hardware (admin _apply_embodied_toggle) and the consent broadcast.

    python -m pytest tests/unit/test_feed_no_takes_effect_while_the_pool_is_busy.py -q
"""
import ast
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

UID = '10'


def _until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


@pytest.fixture
def busy_pool():
    """Every worker of the shared parallel_dispatch pool held, the way a
    batch of /chat jobs holds it."""
    from integrations.agent_engine.parallel_dispatch import get_executor
    pool = get_executor()
    release = threading.Event()
    started = []
    lock = threading.Lock()

    def _chat_job():
        with lock:
            started.append(1)
        release.wait(60)

    for _ in range(pool._max_workers):
        pool.submit(_chat_job)
    assert _until(lambda: len(started) == pool._max_workers), (
        'could not hold every shared worker')
    yield pool
    release.set()


@pytest.fixture
def hardware(monkeypatch):
    """VisionService's start/stop: records each call and the thread it ran
    on, and a start can be held open like the one that never returned."""
    import integrations.channels.admin.api as admin_api

    state = {'calls': [], 'threads': [], 'release': threading.Event(),
             'hang': False, 'running': 0, 'max_running': 0}
    lock = threading.Lock()

    def _toggle(feed, enabled, cfg):
        with lock:
            state['running'] += 1
            state['max_running'] = max(state['max_running'], state['running'])
        state['calls'].append((feed, enabled))
        state['threads'].append(threading.current_thread().name)
        try:
            if state['hang'] and enabled:
                state['release'].wait(30)
        finally:
            with lock:
                state['running'] -= 1

    monkeypatch.setattr(admin_api, '_apply_embodied_toggle', _toggle)
    yield state
    state['hang'] = False
    state['release'].set()
    worker = getattr(admin_api, '_FEED_WORKER', None)
    if worker is not None:
        _until(lambda: not worker._draining, 10)


@pytest.fixture
def admin(tmp_path, monkeypatch):
    """A real AdminAPI whose config file lives under tmp_path."""
    import integrations.channels.admin.api as admin_api
    cfg_file = tmp_path / 'admin_config.json'
    monkeypatch.setattr(admin_api.AdminAPI, '_config_path',
                        lambda self: str(cfg_file))
    api = admin_api.AdminAPI()
    monkeypatch.setattr(admin_api, 'get_api', lambda: api)
    monkeypatch.setattr(admin_api, '_propagate_embodied_config', lambda c: None)
    return api, cfg_file


@pytest.fixture
def consent_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import integrations.social.consent_service as cs
    from integrations.social.models import UserConsent

    engine = create_engine(f"sqlite:///{tmp_path / 'consent.db'}")
    UserConsent.__table__.create(engine)
    monkeypatch.setattr(cs, '_emit', lambda *a, **k: None)
    monkeypatch.setattr(cs, '_copilot_switch_from_consent', lambda *a, **k: None)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture(autouse=True)
def _reopen_gate():
    yield
    from core import ai_sensing
    withhold = getattr(ai_sensing, 'withhold', None)
    if withhold is not None:
        for sensor in ('camera', 'screen'):
            withhold(sensor, False)


def _answer(factory, consent_type, granted):
    from integrations.social.consent_service import ConsentService
    db = factory()
    try:
        if granted:
            ConsentService.grant_consent(db, UID, consent_type)
        else:
            ConsentService.revoke_consent(db, UID, consent_type)
        db.commit()
    finally:
        db.close()


def _saved_embodied(cfg_file):
    if not cfg_file.exists():
        return {}
    return json.loads(cfg_file.read_text(encoding='utf-8')).get('embodied_ai') or {}


def test_a_camera_no_stops_capture_at_once_while_the_pool_is_busy(
        consent_db, admin, hardware, busy_pool):
    from integrations.vision.frame_store import FrameStore

    store = FrameStore()
    _answer(consent_db, 'camera_capture', True)
    assert _until(lambda: hardware['calls'] == [('camera', True)]), \
        hardware['calls']
    store.put_frame(UID, b'before-the-no')
    assert store.get_frame(UID) == b'before-the-no'

    t0 = time.monotonic()
    _answer(consent_db, 'camera_capture', False)
    answered_in = time.monotonic() - t0

    # Capture stops now, not when the hardware stop gets a worker.
    store.put_frame(UID, b'after-the-no')
    assert store.get_frame(UID) is None, (
        'a camera frame was still served after the owner said No')
    assert store.get_frame_count(UID) == 0, (
        'frames taken before the No are still held for describing')
    # The No is on disk now, and a restart reads it back.
    assert _saved_embodied(admin[1]).get('camera_enabled') is False, (
        f'the saved config does not say off: {_saved_embodied(admin[1])}')
    from integrations.channels.admin.api import AdminAPI
    assert AdminAPI()._global_config.embodied_ai.camera_enabled is False
    assert answered_in < 2.0, f'the revoke took {answered_in:.2f}s'
    # The hardware stop still happens, with every shared worker still held.
    assert _until(lambda: hardware['calls'] == [('camera', True),
                                                ('camera', False)], 3.0), (
        f'the stop waited on the shared pool: {hardware["calls"]}')


def test_a_no_behind_a_hung_start_is_gated_and_saved_at_once(
        consent_db, admin, hardware, busy_pool):
    from integrations.vision.frame_store import FrameStore

    hardware['hang'] = True
    _answer(consent_db, 'screen_capture', True)
    assert _until(lambda: hardware['calls'] == [('screen', True)]), \
        hardware['calls']

    _answer(consent_db, 'screen_capture', False)
    store = FrameStore()
    store.put_screen_frame(UID, b'after-the-no')
    assert store.get_screen_frame(UID) is None
    assert _saved_embodied(admin[1]).get('screen_capture_enabled') is False
    # Never alongside: the stop waits for the hung start, then runs once.
    time.sleep(0.2)
    assert hardware['calls'] == [('screen', True)]
    hardware['release'].set()
    assert _until(lambda: hardware['calls'] == [('screen', True),
                                                ('screen', False)])
    assert hardware['max_running'] == 1


def test_a_yes_after_a_no_lets_frames_in_again(consent_db, admin, hardware):
    from integrations.vision.frame_store import FrameStore
    store = FrameStore()
    _answer(consent_db, 'camera_capture', True)
    store.put_frame(UID, b'taken-before-the-no')
    _answer(consent_db, 'camera_capture', False)
    assert store.get_frame(UID) is None
    store.put_frame(UID, b'refused')
    _answer(consent_db, 'camera_capture', True)
    assert store.get_frame(UID) is None, (
        'a frame from before the No, or refused under it, came back')
    store.put_frame(UID, b'allowed')
    assert store.get_frame(UID) == b'allowed'
    assert _saved_embodied(admin[1]).get('camera_enabled') is True


def test_the_eye_button_cannot_lift_the_owners_no(consent_db, admin, hardware):
    """Two different answers: the eye button's wake must not undo a consent
    No, and a consent Yes must not undo the eye button's cut."""
    from core import ai_sensing
    _answer(consent_db, 'screen_capture', True)
    _answer(consent_db, 'screen_capture', False)
    ai_sensing.enable_all()
    assert ai_sensing.allowed('screen') is False
    _answer(consent_db, 'screen_capture', True)
    assert ai_sensing.allowed('screen') is True
    ai_sensing.set_sense('screen', True)
    try:
        _answer(consent_db, 'screen_capture', True)
        assert ai_sensing.allowed('screen') is False
    finally:
        ai_sensing.set_sense('screen', False)


def test_the_screen_loop_takes_no_screenshot_after_the_no(monkeypatch):
    """The screen-capture loop reads the gate before it grabs, so a No stops
    the grab itself, not only what is kept of it."""
    from core import ai_sensing
    import integrations.remote_desktop.frame_capture as fc
    import integrations.agent_engine.dispatch as dispatch
    from integrations.social.consent_service import ConsentService
    from integrations.vision.frame_store import FrameStore
    from integrations.vision.vision_service import VisionService

    grabs = []

    class _Capture:
        def capture_frame(self):
            grabs.append(1)
            return b'jpeg'

    monkeypatch.setattr(fc, 'FrameCapture', _Capture)
    monkeypatch.setattr(dispatch, 'should_yield_to_user', lambda: False)
    # The standing consent row still reads Yes: only the gate says No.
    monkeypatch.setattr(ConsentService, 'check_or_request',
                        staticmethod(lambda *a, **k: True))
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', UID)

    ai_sensing.withhold('screen', True)
    vs = VisionService.__new__(VisionService)
    vs._running = True
    vs.store = FrameStore()
    vs._start_screen_capture(interval=0.02)
    try:
        time.sleep(0.3)
        assert grabs == [], f'{len(grabs)} screenshots taken after the No'
        ai_sensing.withhold('screen', False)
        assert _until(lambda: grabs, 3.0), 'a Yes did not resume capture'
    finally:
        vs._running = False
        vs._screen_capture_thread.join(3)


def test_the_admin_toggle_never_runs_the_hardware_on_its_request(
        admin, hardware, monkeypatch):
    """/config/embodied/toggle's own apply (a feed whose consent write
    failed, or 'audio') goes to the same one feed worker: it answers while a
    start hangs, and the hardware never runs two at once."""
    from flask import Flask, g
    import integrations.channels.admin.api as admin_api
    from integrations.social.consent_service import ConsentService

    monkeypatch.setattr(
        ConsentService, 'record_capability_decision',
        staticmethod(lambda *a, **k: (_ for _ in ()).throw(RuntimeError('db gone'))))
    hardware['hang'] = True
    app = Flask(__name__)
    done = {}

    def _press(feed, enabled):
        with app.test_request_context(json={'feed': feed, 'enabled': enabled}):
            g.db = None
            g.user_id = UID
            admin_api.toggle_embodied_feed()
        done.setdefault(feed, []).append(threading.current_thread().name)

    t = threading.Thread(target=_press, args=('screen', True), daemon=True)
    t.start()
    t.join(3)
    assert not t.is_alive(), 'the toggle request waited on the hardware'
    assert _until(lambda: hardware['calls'] == [('screen', True)])
    _press('screen', False)
    from core import ai_sensing
    assert ai_sensing.allowed('screen') is False, \
        'the toggle-off did not gate capture at once'
    assert _saved_embodied(admin[1]).get('screen_capture_enabled') is False
    time.sleep(0.2)
    assert hardware['calls'] == [('screen', True)], 'ran alongside the hung start'
    hardware['release'].set()
    assert _until(lambda: hardware['calls'] == [('screen', True),
                                                ('screen', False)])
    assert hardware['max_running'] == 1
    assert threading.current_thread().name not in hardware['threads']


# ── source guard: the ONE hardware path and the ONE gate writer ─────────────

def _python_sources():
    skip = {'tests', 'venv', 'venv311', 'build', 'node_modules', '.git',
            'python-embed', '__pycache__', '.claude'}
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in skip and not d.startswith('.venv')]
        for name in filenames:
            if name.endswith('.py'):
                yield Path(dirpath) / name


def _callers(names):
    """(file, enclosing top-level def, name) for every call to one of names."""
    found = []
    for path in _python_sources():
        try:
            src = path.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        if not any(n in src for n in names):
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for top in tree.body:
            owner = getattr(top, 'name', '<module>')
            for node in ast.walk(top):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                called = (fn.id if isinstance(fn, ast.Name)
                          else fn.attr if isinstance(fn, ast.Attribute) else None)
                if called in names:
                    found.append((path.relative_to(_ROOT).as_posix(),
                                  owner, called))
    return found


def test_source_guard_one_hardware_path_and_one_gate_writer():
    """A second caller of the synchronous start/stop runs the hardware off the
    feed worker (on a request, or alongside a hung start); a second writer of
    the owner's-answer gate is a second consent path.  Both stay at one."""
    rel = 'integrations/channels/admin/api.py'
    hw = _callers({'_apply_embodied_toggle'})
    assert hw and {(f, o) for f, o, _ in hw} == {(rel, 'apply_embodied_answer')}, hw
    gate = _callers({'withhold'})
    assert gate and {(f, o) for f, o, _ in gate} == {(rel, 'apply_embodied_answer')}, gate
