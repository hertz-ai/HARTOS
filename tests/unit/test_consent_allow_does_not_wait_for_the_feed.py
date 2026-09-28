"""An Allow on a camera/screen consent card answers at once, even when the
feed it starts never returns.

79928c2fc moved the feed start after the grant's commit (models.after_commit),
so a hung start no longer holds SQLite's write lock.  But after_commit runs
inside db.commit(), and the request commits on its own thread
(auth.require_auth: ``result = f(); db.commit(); return result``), so the
HTTP response still waited for the feed start.  Measured at HEAD with this
file: POST /api/social/consent for screen_capture, with the feed start held
by an Event, had not answered after 5 s -- the card's spinner, which is what
the owner saw live 2026-09-25, "Something's off on our end" on every Allow.

The feed start now runs off the request thread, on the shared background
executor, and it still runs: the owner pressed Allow to see the screen.

Real Flask route, real require_auth (only the token lookup is replaced),
real file-backed SQLite, real ConsentService.  The only stand-in is the
hardware: admin _apply_embodied_toggle (VisionService start/stop) and the
admin config object, so no test touches the owner's real config file.

    python -m pytest tests/unit/test_consent_allow_does_not_wait_for_the_feed.py -q
"""
import os
import sys
import threading
import time

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

UID = 'owner-allow'


def _until(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


@pytest.fixture
def hardware(monkeypatch):
    """The feed's hardware end: records each start/stop, and a start can be
    held open to stand in for VisionService.start() that never returns."""
    import integrations.channels.admin.api as admin_api

    class _Cfg:
        enabled = False
        camera_enabled = False
        screen_capture_enabled = False

    class _Api:
        _global_config = type('G', (), {'embodied_ai': _Cfg()})()

        def _save_config(self):
            pass

    state = {'calls': [], 'release': threading.Event(), 'hang': True}

    def _toggle(feed, enabled, cfg):
        state['calls'].append((feed, enabled))
        if state['hang']:
            state['release'].wait(30)

    monkeypatch.setattr(admin_api, 'get_api', lambda: _Api())
    monkeypatch.setattr(admin_api, '_apply_embodied_toggle', _toggle)
    yield state
    state['release'].set()


@pytest.fixture
def client(tmp_path, monkeypatch):
    from flask import Flask
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from integrations.social import auth as auth_mod
    from integrations.social.consent_api import consent_bp
    from integrations.social.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'allow.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    class _Owner:
        id = UID
        is_banned = False

    monkeypatch.setattr(auth_mod, '_get_user_from_token',
                        lambda token: (_Owner(), factory()))
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(consent_bp)
    yield app.test_client(), factory
    engine.dispose()


def _allow(test_client, consent_type):
    return test_client.post('/api/social/consent',
                            json={'consent_type': consent_type},
                            headers={'Authorization': 'Bearer t'})


def _granted(factory, consent_type):
    from integrations.social.models import UserConsent
    s = factory()
    try:
        return s.query(UserConsent).filter_by(
            user_id=UID, consent_type=consent_type, granted=True).count()
    finally:
        s.close()


def test_allow_answers_while_the_feed_start_hangs(client, hardware):
    test_client, factory = client
    answered = {}

    def _press_allow():
        resp = _allow(test_client, 'screen_capture')
        answered['status'] = resp.status_code

    t = threading.Thread(target=_press_allow, daemon=True)
    t.start()
    t.join(5)
    try:
        assert not t.is_alive(), (
            'the Allow request is still waiting on the feed start after 5 s')
        assert answered['status'] == 201
        assert _granted(factory, 'screen_capture') == 1
        # The owner asked for the screen: the start still happens.
        assert _until(lambda: hardware['calls'] == [('screen', True)]), \
            hardware['calls']
    finally:
        hardware['release'].set()
        t.join(10)


def test_the_last_answer_wins_when_answers_queue_behind_a_hung_start(
        client, hardware):
    """Allow, then Allow again while the first start is still hung: the
    second answer waits for the hardware (one start at a time) and is
    applied once the first returns, not lost and not run twice at once."""
    test_client, factory = client
    assert _allow(test_client, 'screen_capture').status_code == 201
    assert _until(lambda: hardware['calls'] == [('screen', True)])

    from integrations.social.consent_service import ConsentService
    s = factory()
    try:
        ConsentService.revoke_consent(s, UID, 'screen_capture')
        s.commit()
    finally:
        s.close()
    time.sleep(0.2)
    assert hardware['calls'] == [('screen', True)], (
        'a second start/stop ran while the first was still in the hardware')

    hardware['hang'] = False
    hardware['release'].set()
    assert _until(lambda: hardware['calls'] == [('screen', True),
                                                ('screen', False)]), \
        hardware['calls']
