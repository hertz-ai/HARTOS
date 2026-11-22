"""A no to one agent leaves the camera and the screen on for the others.

Review of HARTOS 59ff12f67 (rv_59ff, must-fix).  That commit made a no for
one agent stand under "Allow ALL agents" by writing the agent's own declined
row.  The row was then read as the FEED's last answer:

    allow all for the camera, then Deny for agent A
        -> at the next start the camera gate is closed for every agent
           (vision_service.restore_feed_answers -> feed_said_no), while
           check_consent(B) is True: B passes consent and gets no frames.

And the no itself stopped the feed at once for everyone
(announce_revocation), which was already so before that commit.

The camera and the screen are shared: the feed runs while any agent that
needs it is allowed.  So a no naming one agent, under a standing allow-all,
ends it for that agent only (check_consent; the tools ask turned_off_for
before they hand a frame) and never sets the node-wide gate.

The feed switch here is the real one: consent_service
._embodied_feed_from_consent -> admin.api.apply_embodied_answer ->
core.ai_sensing, on a temp admin config; only the hardware start/stop is
stubbed.

    python -m pytest tests/unit/test_a_no_to_one_agent_leaves_the_feed_on.py -q
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest  # noqa: E402

from integrations.social import consent_service as cs  # noqa: E402
from integrations.social.consent_service import ConsentService  # noqa: E402
from integrations.social.models import Base, UserConsent, db_session, get_engine  # noqa: E402

OWNER, SOMEONE_ELSE = 'owner-feed', 'another-person'
A, B = '111', '222'
FEEDS = [('camera_capture', 'camera', 'enable_camera'),
         ('screen_capture', 'screen', 'enable_screen')]


@pytest.fixture(autouse=True)
def world(monkeypatch, tmp_path):
    """The real consent table, the real feed switch, no hardware, nothing
    sent anywhere."""
    import integrations.channels.admin.api as admin_api
    from core import ai_sensing
    Base.metadata.create_all(get_engine())
    with db_session(commit=True) as db:
        db.query(UserConsent).delete()
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    monkeypatch.setattr(admin_api.AdminAPI, '_config_path',
                        lambda self: str(tmp_path / 'admin_config.json'))
    api = admin_api.AdminAPI()
    monkeypatch.setattr(admin_api, 'get_api', lambda: api)
    monkeypatch.setattr(admin_api, '_propagate_embodied_config', lambda c: None)
    monkeypatch.setattr(admin_api, '_apply_embodied_toggle', lambda *a: None)
    sent = []
    monkeypatch.setattr(cs, '_emit', lambda topic, data, **k: sent.append((topic, dict(data))))
    monkeypatch.setattr(cs, '_audit', lambda *a, **k: sent.append(('audit', k.get('action'))))
    monkeypatch.setattr(cs, '_drop_ask', lambda user, ask_id: sent.append(('drop', ask_id)))
    _restart()
    yield sent
    _restart()
    with db_session(commit=True) as db:
        db.query(UserConsent).delete()


def _restart():
    """What a new process holds: no answer for either feed."""
    from core import ai_sensing
    with ai_sensing._lock:
        for sensor in ai_sensing._withheld:
            ai_sensing._withheld[sensor] = False
        ai_sensing._answered.clear()


def _boot():
    """A restart, then the gate restored from the consents on file."""
    from integrations.vision.vision_service import restore_feed_answers
    _restart()
    restore_feed_answers()


def _gate(sensor):
    from core import ai_sensing
    return ai_sensing.allowed(sensor)


def _allow_all(consent_type, user=OWNER, ago=60):
    """"Allow ALL agents", put ``ago`` seconds back so the order of two
    answers never rests on the clock's resolution."""
    with db_session(commit=True) as db:
        row = ConsentService.grant_consent(db, user, consent_type)
    with db_session(commit=True) as db:
        kept = db.query(UserConsent).filter_by(id=row.id).one()
        kept.granted_at = kept.granted_at - timedelta(seconds=ago)


def _allowed(consent_type, agent_id, user=OWNER):
    with db_session() as db:
        return ConsentService.check_consent(db, user, consent_type, agent_id=agent_id)


def _no(how, consent_type, action, agent_id, user=OWNER):
    with db_session(commit=True) as db:
        if how == 'asked_then_declined':
            ConsentService.request_consent(db, user, consent_type, agent_id=agent_id)
    with db_session(commit=True) as db:
        if how == 'approval_deny':
            ConsentService.record_capability_decision(db, user, action, False,
                                                      agent_id=agent_id)
        else:
            ConsentService.decline(db, user, consent_type, agent_id=agent_id)


HOWS = ['decline_never_asked', 'asked_then_declined', 'approval_deny']


# -- the must-fix: the others keep the feed ----------------------------------

@pytest.mark.parametrize('consent_type, sensor, action', FEEDS)
@pytest.mark.parametrize('how', HOWS)
def test_a_no_to_one_agent_leaves_the_feed_on_now(consent_type, sensor, action, how):
    _allow_all(consent_type)
    assert _gate(sensor) is True
    _no(how, consent_type, action, A)
    assert _gate(sensor) is True, (
        f'a no to one agent closed the {sensor} for every agent')
    assert _allowed(consent_type, A) is False
    assert _allowed(consent_type, B) is True


@pytest.mark.parametrize('consent_type, sensor, action', FEEDS)
@pytest.mark.parametrize('how', HOWS)
def test_a_no_to_one_agent_leaves_the_feed_on_after_a_restart(
        consent_type, sensor, action, how):
    _allow_all(consent_type)
    _no(how, consent_type, action, A)
    _boot()
    assert _gate(sensor) is True, (
        f'after a restart the {sensor} is closed for every agent, while '
        f'check_consent(B) is {_allowed(consent_type, B)}')
    assert _allowed(consent_type, A) is False
    assert _allowed(consent_type, B) is True


def test_the_agent_turned_off_is_the_one_the_tools_refuse():
    _allow_all('camera_capture')
    _no('decline_never_asked', 'camera_capture', 'enable_camera', A)
    assert cs.turned_off_for('camera_capture', A) is True
    assert cs.turned_off_for('camera_capture', B) is False
    assert cs.turned_off_for('screen_capture', A) is False
    # Each agent is read by its own no.
    _allow_all('screen_capture')
    _no('decline_never_asked', 'screen_capture', 'enable_screen', B)
    assert cs.turned_off_for('screen_capture', B) is True
    assert cs.turned_off_for('screen_capture', A) is False


def test_an_agent_nobody_answered_for_is_not_refused_by_the_tools(monkeypatch):
    """Only an explicit no refuses: no row, an open ask, no agent, no owner
    all read as before the check existed."""
    assert cs.turned_off_for('camera_capture', A) is False
    with db_session(commit=True) as db:
        ConsentService.request_consent(db, OWNER, 'camera_capture', agent_id=A)
    assert cs.turned_off_for('camera_capture', A) is False
    assert cs.turned_off_for('camera_capture', None) is False
    assert cs.turned_off_for('camera_capture', '0') is False
    with db_session(commit=True) as db:
        ConsentService.decline(db, OWNER, 'camera_capture', agent_id=A)
    assert cs.turned_off_for('camera_capture', A) is True
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID')
    assert cs.turned_off_for('camera_capture', A) is False


def test_a_store_that_cannot_be_read_does_not_refuse_and_never_raises(monkeypatch):
    monkeypatch.setattr(ConsentService, 'check_consent',
                        staticmethod(lambda *a, **k: 1 / 0))
    assert cs.turned_off_for('camera_capture', A) is False


def test_the_camera_tool_refuses_a_turned_off_agent_before_reading_a_frame(monkeypatch):
    from flask import Flask
    from hartos import helper
    from hartos.threadlocal import thread_local_data
    _allow_all('camera_capture')
    _no('decline_never_asked', 'camera_capture', 'enable_camera', A)
    read = []
    monkeypatch.setattr(helper, '_camera_frame_after_now',
                        lambda user_id: read.append(user_id))
    monkeypatch.setattr(helper, '_no_camera_frame_reason', lambda *a: 'no frame')
    monkeypatch.setattr(thread_local_data, 'get_prompt_id', lambda: A)
    with Flask('camera-off-test').app_context():
        with pytest.raises(RuntimeError) as refused:
            helper.get_user_camera_inp('what do you see', OWNER, 'req-1')
        assert 'turned the camera off for this agent' in str(refused.value)
        assert 'answer without it' in str(refused.value)
        assert read == [], 'a frame was read for an agent the owner turned off'
        # Another agent still reaches the frame read.
        monkeypatch.setattr(thread_local_data, 'get_prompt_id', lambda: B)
        with pytest.raises(RuntimeError) as other:
            helper.get_user_camera_inp('what do you see', OWNER, 'req-2')
        assert str(other.value) == 'no frame'
        assert read == [OWNER]


def test_an_allow_all_given_after_the_agents_no_covers_it_for_the_tools(monkeypatch):
    """The no is still on file (declined), and the newer "Allow ALL agents"
    covers the agent again (check_consent): the tools must not refuse it.
    turned_off_for reads both; without the check_consent half this agent
    stayed refused for good (review of 0be16584a: no test held that half)."""
    from flask import Flask
    from hartos import helper
    from hartos.threadlocal import thread_local_data
    with db_session(commit=True) as db:
        ConsentService.request_consent(db, OWNER, 'camera_capture', agent_id=A)
    with db_session(commit=True) as db:
        ConsentService.decline(db, OWNER, 'camera_capture', agent_id=A)
        row = db.query(UserConsent).filter_by(agent_id=A).one()
        row.revoked_at = row.revoked_at - timedelta(seconds=120)
    assert cs.turned_off_for('camera_capture', A) is True
    _allow_all('camera_capture')             # a minute ago: after the no
    with db_session() as db:
        assert ConsentService.declined(db, OWNER, 'camera_capture', agent_id=A)
    assert _allowed('camera_capture', A) is True
    assert cs.turned_off_for('camera_capture', A) is False, (
        'an agent the newer allow-all covers is still read as turned off')
    read = []
    monkeypatch.setattr(helper, '_camera_frame_after_now',
                        lambda user_id: read.append(user_id))
    monkeypatch.setattr(helper, '_no_camera_frame_reason', lambda *a: 'no frame')
    monkeypatch.setattr(thread_local_data, 'get_prompt_id', lambda: A)
    with Flask('camera-covered-test').app_context():
        with pytest.raises(RuntimeError) as reached:
            helper.get_user_camera_inp('what do you see', OWNER, 'req-3')
    assert str(reached.value) == 'no frame'
    assert read == [OWNER], 'the camera tool refused an agent that is allowed'


def _screenshot_tool(monkeypatch, grabs):
    """The real _handle_screenshot_tool, lifted from its module by name
    (hart_intelligence_entry cannot be imported in a unit test), with the
    grab and the model call recorded instead of made."""
    import ast
    import logging
    import types
    from hartos.threadlocal import thread_local_data

    class _Image:
        def resize(self, *a):
            return self

        def save(self, buf, *a, **k):
            buf.write(b'x')

    class _Reply:
        status_code = 200

        def json(self):
            return {'choices': [{'message': {'content': 'a desktop'}}]}

    pil = types.ModuleType('PIL')
    grab = types.ModuleType('PIL.ImageGrab')
    grab.grab = lambda *a, **k: grabs.append(1) or _Image()
    pil.ImageGrab = grab
    monkeypatch.setitem(sys.modules, 'PIL', pil)
    monkeypatch.setitem(sys.modules, 'PIL.ImageGrab', grab)
    src = (Path(ROOT) / 'hart_intelligence_entry.py').read_text(encoding='utf-8')
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == '_handle_screenshot_tool')
    ns = {'logging': logging, 'thread_local_data': thread_local_data,
          'pooled_post': lambda *a, **k: _Reply(), 'GPT_API': 'http://stub.invalid',
          'LLM_MODEL_NAME': 'stub', 'LLM_AUTH_HEADERS': {}}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), 'hart_intelligence_entry.py',
                 'exec'), ns)
    return ns['_handle_screenshot_tool']


def test_the_screenshot_tool_grabs_nothing_for_a_turned_off_agent(monkeypatch):
    from hartos.threadlocal import thread_local_data
    grabs = []
    tool = _screenshot_tool(monkeypatch, grabs)
    _allow_all('screen_capture')
    _no('decline_never_asked', 'screen_capture', 'enable_screen', A)
    monkeypatch.setattr(thread_local_data, 'get_prompt_id', lambda: A)
    reply = tool('what is on screen')
    assert grabs == [], f'the screen was grabbed for an agent the owner turned off: {reply}'
    assert 'turned screen access off for this agent' in reply
    assert 'answer without it' in reply
    # Another agent still gets its grab, and so does the camera's no.
    monkeypatch.setattr(thread_local_data, 'get_prompt_id', lambda: B)
    assert tool('what is on screen') == 'a desktop'
    assert grabs == [1]


def test_the_screenshot_tool_reads_the_screens_no_not_the_cameras(monkeypatch):
    from hartos.threadlocal import thread_local_data
    grabs = []
    tool = _screenshot_tool(monkeypatch, grabs)
    _allow_all('screen_capture')
    _allow_all('camera_capture')
    _no('decline_never_asked', 'camera_capture', 'enable_camera', A)
    monkeypatch.setattr(thread_local_data, 'get_prompt_id', lambda: A)
    assert tool('what is on screen') == 'a desktop'
    assert grabs == [1], 'a no for the camera closed the screenshot tool'


def test_the_screenshot_tool_asks_for_its_own_agent_before_the_grab():
    src = (Path(ROOT) / 'hart_intelligence_entry.py').read_text(encoding='utf-8')
    body = src[src.index('def _handle_screenshot_tool('):]
    body = body[:body.index('\ndef ', 1)]
    check = "turned_off_for('screen_capture', thread_local_data.get_prompt_id())"
    assert check in body
    assert body.index(check) < body.index('ImageGrab.grab()')


# -- what must not change ----------------------------------------------------

@pytest.mark.parametrize('consent_type, sensor, action', FEEDS)
def test_a_no_for_every_agent_still_closes_the_feed_now_and_after_a_restart(
        consent_type, sensor, action):
    _allow_all(consent_type)
    with db_session(commit=True) as db:
        ConsentService.revoke_consent(db, OWNER, consent_type)
    assert _gate(sensor) is False
    _boot()
    assert _gate(sensor) is False


@pytest.mark.parametrize('consent_type, sensor, action', FEEDS)
def test_with_no_allow_all_a_no_to_one_agent_is_the_feeds_last_answer_as_before(
        consent_type, sensor, action):
    """Nothing is granted, so no other agent holds the feed."""
    _no('asked_then_declined', consent_type, action, A)
    assert _gate(sensor) is False
    with db_session() as db:
        assert ConsentService.feed_said_no(db, consent_type) is True
        assert ConsentService.feed_said_no(db, consent_type, user_id=OWNER) is True
    _boot()
    assert _gate(sensor) is False


def test_ending_allow_all_after_the_agents_no_closes_the_feed():
    _allow_all('camera_capture')
    _no('decline_never_asked', 'camera_capture', 'enable_camera', A)
    with db_session(commit=True) as db:
        ConsentService.revoke_consent(db, OWNER, 'camera_capture')
    assert _gate('camera') is False
    _boot()
    assert _gate('camera') is False


def test_an_allow_all_that_was_ended_no_longer_sets_an_agents_no_aside():
    """Allow all, then off for everyone, then one agent is granted on its
    own, then another agent is refused on its card: nothing allows every
    agent any more, so that last no is the feed's last answer, as before."""
    now = datetime.utcnow()
    with db_session(commit=True) as db:
        ConsentService.grant_consent(db, OWNER, 'camera_capture')
        ConsentService.revoke_consent(db, OWNER, 'camera_capture')
        ConsentService.grant_consent(db, OWNER, 'camera_capture', agent_id=B)
        ConsentService.request_consent(db, OWNER, 'camera_capture', agent_id=A)
        ConsentService.revoke_consent(db, OWNER, 'camera_capture', agent_id=A)
    with db_session(commit=True) as db:
        ended = db.query(UserConsent).filter_by(agent_id=None).one()
        ended.granted_at = now - timedelta(seconds=400)
        ended.revoked_at = now - timedelta(seconds=300)
        db.query(UserConsent).filter_by(agent_id=B).one().granted_at = (
            now - timedelta(seconds=200))
        db.query(UserConsent).filter_by(agent_id=A).one().revoked_at = (
            now - timedelta(seconds=100))
    with db_session() as db:
        assert ConsentService.feed_said_no(db, 'camera_capture') is True


def test_a_no_that_names_no_agent_stops_the_feed_even_for_one_scope():
    """Only a no that names an agent is that agent's alone."""
    _allow_all('camera_capture')
    with db_session(commit=True) as db:
        ConsentService.announce_revocation(OWNER, 'camera_capture',
                                           scope='vision', db=db)
    assert _gate('camera') is False


def test_one_persons_allow_all_does_not_hide_another_persons_no():
    """The gate is node-wide and reads every person's answers: a no for one
    agent is set aside only by the allow-all of the person who gave it."""
    _allow_all('camera_capture', user=SOMEONE_ELSE, ago=120)
    _no('asked_then_declined', 'camera_capture', 'enable_camera', A, user=OWNER)
    with db_session() as db:
        assert ConsentService.feed_said_no(db, 'camera_capture') is True
        assert ConsentService.feed_said_no(db, 'camera_capture', user_id=SOMEONE_ELSE) is False


def test_an_agents_no_without_the_session_stops_the_feed_as_before():
    """A caller that hands no session cannot show allow-all stands: the safe
    side."""
    _allow_all('camera_capture')
    ConsentService.announce_revocation(OWNER, 'camera_capture', agent_id=A)
    assert _gate('camera') is False


# -- the tie rule, one for both ----------------------------------------------

def test_on_a_tie_the_no_wins_for_the_agent_and_for_the_feed():
    at = datetime.utcnow().replace(microsecond=0)
    with db_session(commit=True) as db:
        ConsentService.grant_consent(db, OWNER, 'camera_capture')
        ConsentService.revoke_consent(db, OWNER, 'camera_capture', agent_id=A)
    with db_session(commit=True) as db:
        db.query(UserConsent).filter_by(agent_id=None).one().granted_at = at
        db.query(UserConsent).filter_by(agent_id=A).one().revoked_at = at
    assert _allowed('camera_capture', A) is False
    with db_session(commit=True) as db:
        ConsentService.grant_consent(db, SOMEONE_ELSE, 'screen_capture')
        ConsentService.revoke_consent(db, SOMEONE_ELSE, 'screen_capture')
    with db_session(commit=True) as db:
        row = db.query(UserConsent).filter_by(user_id=SOMEONE_ELSE).one()
        row.granted_at = row.revoked_at = at
    with db_session() as db:
        assert ConsentService.feed_said_no(db, 'screen_capture') is True


# -- medium and minor --------------------------------------------------------

def test_the_newest_of_stacked_grants_is_the_active_one():
    _allow_all('camera_capture', ago=300)
    _allow_all('camera_capture', ago=10)
    _allow_all('camera_capture', ago=100)
    with db_session() as db:
        newest = ConsentService.active_grant(db, OWNER, 'camera_capture')
        times = sorted(r.granted_at for r in db.query(UserConsent).all())
        assert newest.granted_at == times[-1]
    assert not hasattr(ConsentService, '_blanket_grant')


_TOLD = {'user_id': OWNER, 'consent_type': 'computer_control', 'scope': '*',
         'agent_id': A}


def test_a_no_for_a_never_asked_agent_is_heard_by_pages_and_drops_no_card(world):
    """Review of 0be16584a: the first no for such an agent was silent, so a
    page showing the agent's permissions missed a turn-off made in chat; the
    second no was broadcast and sent a phone drop for a card never shown.
    Now every no is broadcast and neither drops a card."""
    _allow_all('computer_control')
    for nth in ('first', 'second'):
        del world[:]
        with db_session(commit=True) as db:
            ConsentService.decline(db, OWNER, 'computer_control', agent_id=A)
        assert [d for t, d in world if t == 'consent.revoked'] == [_TOLD], (
            f'the {nth} no was not published once for pages: {world}')
        assert [e for e in world if e[0] == 'drop'] == [], (
            f'the {nth} no sent a drop for a card never shown: {world}')
        assert ('audit', 'consent.revoked:computer_control') in world, (
            'the no must still be in the audit trail')


def test_a_turn_off_in_chat_is_heard_by_pages_and_drops_no_card(world):
    _allow_all('computer_control')
    del world[:]
    assert cs.turn_off_for_agent('computer_control', A,
                                 turn_user=OWNER) == cs.TURNED_OFF
    assert [d for t, d in world if t == 'consent.revoked'] == [_TOLD], world
    assert [e for e in world if e[0] == 'drop'] == []


def test_a_second_no_on_an_ask_already_declined_drops_no_second_card(world):
    with db_session(commit=True) as db:
        ask_id = ConsentService.request_consent(
            db, OWNER, 'computer_control', agent_id=A).id
    with db_session(commit=True) as db:
        ConsentService.decline(db, OWNER, 'computer_control', agent_id=A)
    assert ('drop', ask_id) in world, 'the open ask kept its card'
    del world[:]
    with db_session(commit=True) as db:
        ConsentService.decline(db, OWNER, 'computer_control', agent_id=A)
    assert [e for e in world if e[0] == 'drop'] == [], world
    assert [d for t, d in world if t == 'consent.revoked'] == [_TOLD]


def test_a_no_on_a_filed_ask_still_drops_its_card_everywhere(world):
    with db_session(commit=True) as db:
        ask = ConsentService.request_consent(db, OWNER, 'computer_control', agent_id=A)
        ask_id = ask.id
    _allow_all('computer_control')
    del world[:]
    with db_session(commit=True) as db:
        ConsentService.decline(db, OWNER, 'computer_control', agent_id=A)
    assert ('drop', ask_id) in world
    assert [d for t, d in world if t == 'consent.revoked'] == [{
        'user_id': OWNER, 'consent_type': 'computer_control', 'scope': '*',
        'agent_id': A}]


@pytest.mark.parametrize('body, said', [
    ({'consent_type': 'computer_control', 'agent_id': 'a' * 65}, 'agent_id exceeds 64'),
    ({'consent_type': 'computer_control', 'agent_id': A, 'scope': 's' * 101},
     'scope exceeds 100'),
    ({'consent_type': 'c' * 31, 'agent_id': A}, 'consent_type exceeds 30'),
])
def test_the_decline_route_caps_what_it_can_write(monkeypatch, body, said):
    from types import SimpleNamespace
    from flask import Flask
    from integrations.social import auth as auth_mod
    from integrations.social import consent_api
    from sqlalchemy.orm import sessionmaker
    _allow_all('computer_control')
    monkeypatch.setattr(auth_mod, '_get_user_from_token', lambda token: (
        SimpleNamespace(id=OWNER, is_admin=False, is_moderator=False),
        sessionmaker(bind=get_engine(), expire_on_commit=False)()))
    app = Flask(__name__)
    app.register_blueprint(consent_api.consent_bp)
    resp = app.test_client().post('/api/social/consent/decline', json=body,
                                  headers={'Authorization': 'Bearer t'})
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert said in resp.get_json()['error']
    with db_session() as db:
        assert db.query(UserConsent).filter(UserConsent.agent_id != None).count() == 0  # noqa: E711


def test_the_decline_route_takes_the_longest_values_the_columns_hold(monkeypatch):
    from types import SimpleNamespace
    from flask import Flask
    from integrations.social import auth as auth_mod
    from integrations.social import consent_api
    from sqlalchemy.orm import sessionmaker
    _allow_all('computer_control')
    monkeypatch.setattr(auth_mod, '_get_user_from_token', lambda token: (
        SimpleNamespace(id=OWNER, is_admin=False, is_moderator=False),
        sessionmaker(bind=get_engine(), expire_on_commit=False)()))
    app = Flask(__name__)
    app.register_blueprint(consent_api.consent_bp)
    resp = app.test_client().post(
        '/api/social/consent/decline', headers={'Authorization': 'Bearer t'},
        json={'consent_type': 'computer_control', 'agent_id': 'a' * 64, 'scope': 's' * 100})
    assert resp.status_code == 200, resp.get_data(as_text=True)
