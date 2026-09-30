"""The camera/screen gate after a restart, and the eye button's camera cut.

1. core.ai_sensing._stop_vision imported get_vision_service from
   integrations.vision.vision_service, which does not define it.  The import
   raised inside a bare `except: pass`, so the eye button's camera cut never
   stopped a VisionService, and status()'s camera_service_running proof was
   always False.  Now every running VisionService stops, whichever of its
   three owners made it (vision_service.running_vision_services).

2. The capture gate is process memory and started open on every boot, so
   after a restart camera frames reached the store until the owner answered
   again.  Now the first VisionService constructed restores the saved
   answers (vision_service.restore_feed_answers): the last camera/screen
   answer on file, whoever gave it (ConsentService.feed_said_no, the rule
   the running process applies), so a No given in admin settings under a
   signed-in user who is not HEVOLVE_OWNER_USER_ID survives too.  A read
   error closes both; an answer this process already holds is never
   overwritten by the (possibly uncommitted) row.

3. A consent No's hardware stop drove only the integrations.vision
   singleton; in bundled Nunba the running instance is Nunba's own.  Every
   cut now stops every running VisionService
   (vision_service.stop_running_vision_services), and a Yes starts none
   while one already runs.

Real VisionService, real FrameStore, real ConsentService on a file SQLite.

    python -m pytest tests/unit/test_feed_gate_restart_and_eye_button.py -q
"""
import contextlib
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

OWNER = 'owner-restart'

import integrations.social.consent_service as _cs_module  # noqa: E402
#: The real feed answer, before any fixture swaps it.
_ORIGINAL_FEED_ANSWER = _cs_module._embodied_feed_from_consent


@pytest.fixture
def gate():
    """The gate as a fresh process has it: no answers held."""
    from core import ai_sensing
    with ai_sensing._lock:
        for sensor in ai_sensing._withheld:
            ai_sensing._withheld[sensor] = False
        getattr(ai_sensing, '_answered', set()).clear()
    yield ai_sensing
    ai_sensing.set_sense('camera', False)
    ai_sensing.set_sense('screen', False)
    with ai_sensing._lock:
        for sensor in ai_sensing._withheld:
            ai_sensing._withheld[sensor] = False
        getattr(ai_sensing, '_answered', set()).clear()


@pytest.fixture
def saved_consent(tmp_path, monkeypatch):
    """The owner's consent rows on a real file DB, as the restore reads them."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import integrations.social.consent_service as cs
    import integrations.social.models as models
    from integrations.social.models import UserConsent

    engine = create_engine(f"sqlite:///{tmp_path / 'saved.db'}")
    UserConsent.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(cs, '_emit', lambda *a, **k: None)
    monkeypatch.setattr(cs, '_copilot_switch_from_consent', lambda *a, **k: None)
    # Writing the rows must not drive the feed: this is the state on disk
    # from a previous run.
    monkeypatch.setattr(cs, '_embodied_feed_from_consent', lambda *a, **k: None)

    @contextlib.contextmanager
    def _session(commit=True):
        s = factory()
        try:
            yield s
            if commit:
                s.commit()
        finally:
            s.close()

    monkeypatch.setattr(models, 'db_session', _session)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)

    def _answer(consent_type, granted):
        from integrations.social.consent_service import ConsentService
        with _session() as s:
            if granted:
                ConsentService.grant_consent(s, OWNER, consent_type)
            else:
                ConsentService.revoke_consent(s, OWNER, consent_type)

    yield _answer
    engine.dispose()


def _new_service():
    from integrations.vision.frame_store import FrameStore
    from integrations.vision.vision_service import VisionService
    return VisionService(frame_store=FrameStore())


# ── 1. the eye button stops the camera ──────────────────────────────────────

def test_the_eye_buttons_camera_cut_stops_the_running_service(gate):
    vs = _new_service()
    vs._running = True          # stands in for start()'s hardware
    try:
        assert gate.status()['proof']['camera_service_running'] is True
        gate.set_sense('camera', True)
        assert vs.is_running() is False, 'the camera cut left VisionService running'
        assert gate.status()['proof']['camera_service_running'] is False
    finally:
        vs._running = False


def test_disable_all_stops_every_running_service(gate):
    """Nunba's boot instance and the integrations.vision singleton can both
    exist: the cut stops each one that runs."""
    a, b = _new_service(), _new_service()
    a._running = b._running = True
    try:
        gate.disable_all()
        assert (a.is_running(), b.is_running()) == (False, False)
    finally:
        gate.enable_all()
        a._running = b._running = False


# ── 2. a restart keeps the owner's No ───────────────────────────────────────

def test_a_saved_no_closes_the_feed_before_any_frame(gate, saved_consent):
    saved_consent('camera_capture', True)
    saved_consent('camera_capture', False)
    saved_consent('screen_capture', True)

    vs = _new_service()           # the boot: first VisionService
    vs.store.put_frame(OWNER, b'after-restart')
    assert vs.store.get_frame(OWNER) is None, (
        'a camera frame reached the store after a restart despite the saved No')
    assert gate.allowed('camera') is False
    assert gate.allowed('screen') is True
    vs.store.put_screen_frame(OWNER, b'screen')
    assert vs.store.get_screen_frame(OWNER) == b'screen'


def test_a_yes_given_after_a_no_is_what_the_restart_keeps(gate, saved_consent):
    """The revoked row stays on file as history; the later grant is the
    answer."""
    saved_consent('camera_capture', True)
    saved_consent('camera_capture', False)
    saved_consent('camera_capture', True)
    _new_service()
    assert gate.allowed('camera') is True


def test_no_answer_on_file_leaves_the_feeds_open(gate, saved_consent):
    vs = _new_service()
    vs.store.put_frame(OWNER, b'frame')
    assert vs.store.get_frame(OWNER) == b'frame'
    assert (gate.allowed('camera'), gate.allowed('screen')) == (True, True)


def test_a_consent_that_cannot_be_read_closes_both(gate, saved_consent,
                                                   monkeypatch):
    import integrations.social.models as models

    def _broken(commit=True):
        raise RuntimeError('database is locked')

    monkeypatch.setattr(models, 'db_session', _broken)
    _new_service()
    assert (gate.allowed('camera'), gate.allowed('screen')) == (False, False)


def test_a_database_with_no_consent_table_leaves_the_feeds_open(
        gate, tmp_path, monkeypatch):
    """No consent table means no answer was ever recorded in this database:
    a fresh install whose VisionService starts before init_db creates the
    schema, or a file whose migrations never ran.  That is "no answer on
    file" (open), not "cannot tell" (closed): closing here shut the camera
    and screen for the whole first boot, and test_vision_sidecar's frame
    tests failed on exactly this (main 53ddce893)."""
    import contextlib
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import integrations.social.models as models

    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    factory = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def _session(commit=True):
        s = factory()
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setattr(models, 'db_session', _session)
    try:
        vs = _new_service()
        assert (gate.allowed('camera'), gate.allowed('screen')) == (True, True)
        vs.store.put_frame(OWNER, b'first-boot')
        assert vs.store.get_frame(OWNER) == b'first-boot'
    finally:
        engine.dispose()


def test_an_answer_given_in_this_process_is_not_overwritten(gate, saved_consent):
    """The worker may construct the first VisionService while a No is still
    uncommitted, so the row reads Yes: the No must stand."""
    saved_consent('camera_capture', True)
    gate.withhold('camera', True)
    _new_service()
    assert gate.allowed('camera') is False


def test_a_later_service_does_not_read_again(gate, saved_consent, monkeypatch):
    """The restore happens once: a VisionService made later (the admin
    toggle's singleton, a crash-recovery one) must not re-read, or a DB
    error at that moment would close feeds the owner left open."""
    import integrations.social.models as models
    _new_service()
    assert (gate.allowed('camera'), gate.allowed('screen')) == (True, True)

    def _broken(commit=True):
        raise RuntimeError('database is locked')

    monkeypatch.setattr(models, 'db_session', _broken)
    _new_service()
    assert (gate.allowed('camera'), gate.allowed('screen')) == (True, True)


def test_the_restore_reads_once(gate, saved_consent):
    """A later VisionService does not re-read: a Yes given since the boot
    stands even though the row said No at boot."""
    saved_consent('camera_capture', True)
    saved_consent('camera_capture', False)
    _new_service()
    assert gate.allowed('camera') is False
    gate.withhold('camera', False)
    _new_service()
    assert gate.allowed('camera') is True


# ── source guard: one writer each ───────────────────────────────────────────

def test_source_guard_one_restore_writer():
    from tests.unit.test_feed_no_takes_effect_while_the_pool_is_busy import (
        _callers)
    found = _callers({'restore_withheld'})
    assert found and {(f, o) for f, o, _ in found} == {
        ('integrations/vision/vision_service.py', 'restore_feed_answers')}, found
    restore = _callers({'restore_feed_answers'})
    assert {(f, o) for f, o, _ in restore} == {
        ('integrations/vision/vision_service.py', 'VisionService')}, restore


# ── 3. one source of truth: the last answer on file, whoever filed it ───────

def _restart(gate):
    """What a new process sees: no answers held, the rows still on disk."""
    with gate._lock:
        for sensor in gate._withheld:
            gate._withheld[sensor] = False
        gate._answered.clear()


@pytest.fixture
def admin_toggle(tmp_path, monkeypatch, saved_consent):
    """POST /config/embodied/toggle for real, signed in as someone who is not
    HEVOLVE_OWNER_USER_ID, writing to the same file DB the restore reads."""
    from flask import Flask, g
    import integrations.channels.admin.api as admin_api
    import integrations.social.consent_service as cs
    import integrations.social.models as models

    monkeypatch.setattr(admin_api.AdminAPI, '_config_path',
                        lambda self: str(tmp_path / 'admin_config.json'))
    api = admin_api.AdminAPI()
    monkeypatch.setattr(admin_api, 'get_api', lambda: api)
    monkeypatch.setattr(admin_api, '_propagate_embodied_config', lambda c: None)
    monkeypatch.setattr(admin_api, '_apply_embodied_toggle', lambda *a: None)
    # saved_consent silenced the feed answer; here it runs for real, as in
    # production: the toggle's answer sets the gate.
    monkeypatch.setattr(cs, '_embodied_feed_from_consent', _ORIGINAL_FEED_ANSWER)
    app = Flask(__name__)

    def _toggle(feed, enabled, user='signed-in-not-owner'):
        with app.test_request_context(json={'feed': feed, 'enabled': enabled}):
            with models.db_session() as db:
                g.db = db
                g.user_id = user
                admin_api.toggle_embodied_feed()

    return _toggle


def test_a_no_given_in_admin_settings_survives_a_restart(gate, admin_toggle):
    """The toggle files its answer under the signed-in user, not
    HEVOLVE_OWNER_USER_ID; the restore must still see it."""
    admin_toggle('camera', True)
    admin_toggle('camera', False)
    assert gate.allowed('camera') is False
    _restart(gate)
    assert gate.allowed('camera') is True
    vs = _new_service()
    vs.store.put_frame(OWNER, b'after-restart')
    assert vs.store.get_frame(OWNER) is None, (
        'the No given in admin settings was lost on restart')


def test_the_latest_answer_stands_whoever_gave_it(gate, admin_toggle):
    admin_toggle('screen', True, user='a')
    admin_toggle('screen', False, user='a')
    admin_toggle('screen', True, user='b')
    _restart(gate)
    _new_service()
    assert gate.allowed('screen') is True
    admin_toggle('screen', False, user='a')
    _restart(gate)
    _new_service()
    assert gate.allowed('screen') is False


def test_no_owner_configured_still_honours_a_no_on_file(gate, saved_consent,
                                                        monkeypatch):
    saved_consent('camera_capture', True)
    saved_consent('camera_capture', False)
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID')
    _new_service()
    assert gate.allowed('camera') is False
    assert gate.allowed('screen') is True


# ── 4. a consent No stops every running VisionService ──────────────────────

@pytest.fixture
def two_services(gate, saved_consent, tmp_path, monkeypatch):
    """Nunba's boot instance and the integrations.vision singleton, both
    running; the consent path drives the REAL _apply_embodied_toggle."""
    import integrations.channels.admin.api as admin_api
    import integrations.social.consent_service as cs
    import integrations.vision as vision_pkg

    monkeypatch.setattr(cs, '_embodied_feed_from_consent', _ORIGINAL_FEED_ANSWER)
    monkeypatch.setattr(admin_api.AdminAPI, '_config_path',
                        lambda self: str(tmp_path / 'admin_config.json'))
    api = admin_api.AdminAPI()
    monkeypatch.setattr(admin_api, 'get_api', lambda: api)
    nunba, singleton = _new_service(), _new_service()
    monkeypatch.setattr(vision_pkg, '_vision_service_singleton', singleton)
    starts = []
    singleton.start = lambda mode='auto': starts.append(mode)
    yield nunba, singleton, starts
    nunba._running = singleton._running = False
    worker = admin_api._FEED_WORKER
    _until(lambda: not worker._draining, 10)


def _until(cond, timeout=5.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end and not cond():
        time.sleep(0.02)
    return cond()


def test_a_consent_no_stops_nunbas_instance_too(two_services, saved_consent):
    nunba, singleton, _ = two_services
    saved_consent('camera_capture', True)
    nunba._running = singleton._running = True
    saved_consent('camera_capture', False)
    assert _until(lambda: not nunba.is_running() and not singleton.is_running()), (
        f'still running after the No: nunba={nunba.is_running()} '
        f'singleton={singleton.is_running()}')


def test_a_yes_while_nunbas_instance_runs_starts_no_second_service(
        two_services, saved_consent):
    nunba, singleton, starts = two_services
    nunba._running = True
    saved_consent('camera_capture', True)
    import time
    time.sleep(0.3)
    assert starts == [], 'a second VisionService was started beside Nunba\'s'
    nunba._running = False
    saved_consent('camera_capture', False)
    saved_consent('camera_capture', True)
    assert _until(lambda: starts), 'with nothing running, a Yes starts the camera'


def test_source_guard_one_stop_for_every_cut():
    from tests.unit.test_feed_no_takes_effect_while_the_pool_is_busy import (
        _callers)
    found = {(f, o) for f, o, _ in _callers({'stop_running_vision_services'})}
    assert found == {('integrations/channels/admin/api.py',
                      '_apply_embodied_toggle')}, found
    # core.ai_sensing reaches it by name through _vision_call (core imports
    # no integrations module at load); that reach is pinned by
    # test_ai_sensing_never_swallows (the names exist; a missing one is an
    # ERROR and an 'unknown' proof) and by the eye-button tests above.
    import inspect
    from core import ai_sensing
    assert "_vision_call('stop_running_vision_services')" in         inspect.getsource(ai_sensing._stop_vision)


def test_feed_said_no_reads_the_last_answer_and_a_tie_is_a_no(tmp_path):
    """The rule itself, on rows as the writers leave them: a pending ask is
    no answer; the later of a grant and a No stands; the same instant is a
    No (fail closed); another feed's answers do not count."""
    import uuid
    from datetime import datetime, timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from integrations.social.consent_service import ConsentService
    from integrations.social.models import UserConsent

    engine = create_engine(f"sqlite:///{tmp_path / 'rule.db'}")
    UserConsent.__table__.create(engine)
    db = sessionmaker(bind=engine)()
    t0 = datetime(2026, 9, 28, 12, 0, 0)

    def row(user, ctype='camera_capture', granted_at=None, revoked_at=None):
        db.add(UserConsent(id=str(uuid.uuid4()), user_id=user, agent_id=None,
                           consent_type=ctype, scope='*',
                           granted=granted_at is not None and revoked_at is None,
                           granted_at=granted_at, revoked_at=revoked_at))
        db.flush()

    try:
        row('pending-ask')
        assert ConsentService.feed_said_no(db, 'camera_capture') is False
        row('a', granted_at=t0)
        assert ConsentService.feed_said_no(db, 'camera_capture') is False
        row('b', granted_at=t0 - timedelta(hours=1), revoked_at=t0)
        assert ConsentService.feed_said_no(db, 'camera_capture') is True, \
            'a No at the same instant as the last Yes must close'
        row('c', granted_at=t0 + timedelta(seconds=1))
        assert ConsentService.feed_said_no(db, 'camera_capture') is False
        row('d', ctype='screen_capture', revoked_at=t0 + timedelta(hours=1))
        assert ConsentService.feed_said_no(db, 'camera_capture') is False
        assert ConsentService.feed_said_no(db, 'screen_capture') is True
    finally:
        db.close()
        engine.dispose()
