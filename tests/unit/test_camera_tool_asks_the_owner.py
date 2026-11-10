"""An agent that needs the camera asks the owner once, through ConsentService.

Owner, 2026-11-09: "Camera access is gated- consent ask shd be the only gate
and when consented it shd work without any additional asks for vision
captioning etc i do not see that happen".

MEASURED on the MSI desktop, 2026-11-09:

  * agent_system.log*: the owner's own turns (session 10202_1794031134533,
    the speech-therapy agent) called get_user_camera_inp 28 times on
    2026-11-07.  Every call raised "No camera frame is shared ...", and the
    agent told the owner "the camera is not currently active".
  * ~/Documents/Nunba/data/hevolve_database.db: user_consents holds 0
    camera_capture rows, ever.  No ask was filed, so no card was shown, and
    nothing could turn the camera on.

With no frame the tool now goes through the one ask,
consent_service.ask_owner -- the ask Request_Camera_Access makes too
(hart_intelligence_entry._request_consent) -- and says what happened.  Only on
the owner's own turn: frames are keyed by the user whose Nunba window streams
them, so a turn of the daemon's account or of another user can never be
answered by a frame, and asking the owner for it is an ask they do not need
(the same logs: 530 of 732 calls on 2026-09-22..29 were the daemon's account).

    python -m pytest tests/unit/test_camera_tool_asks_the_owner.py -q
"""
import os

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402

flask = pytest.importorskip('flask')

from integrations.social.models import Base, UserConsent, db_session, get_engine  # noqa: E402

OWNER = '10202'
DAEMON = 'c23d388c-07a0-4a79-816d-5b95642683c0'
QUESTION = 'Look at the child and tell me if they are smiling'


@pytest.fixture(autouse=True)
def _fresh_db():
    engine = get_engine()
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def emitted(monkeypatch):
    """Every consent event, as (topic, data); the delivery legs (event bus,
    notification, phone push) are not this test's subject."""
    from integrations.social import consent_service
    sent = []
    monkeypatch.setattr(consent_service, '_emit',
                        lambda topic, data, msg_id=None: sent.append((topic, dict(data))))
    monkeypatch.setattr(consent_service, '_embodied_feed_from_consent',
                        lambda *a, **k: None)
    return sent


@pytest.fixture
def helper(tmp_path, monkeypatch):
    """hartos.helper in a Flask app context (it logs through current_app),
    with no frame for anyone, run from a scratch cwd."""
    import hartos.helper as h
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    app = flask.Flask('camera_tool_asks_the_owner')
    with app.app_context():
        with patch.object(h, 'get_frame', return_value=None):
            yield h


def _camera_rows(user_id=OWNER):
    with db_session() as db:
        return [(r.granted, r.revoked_at is not None, r.agent_id)
                for r in db.query(UserConsent).filter(
                    UserConsent.user_id == user_id,
                    UserConsent.consent_type == 'camera_capture').all()]


def _asks(sent):
    return [d for t, d in sent
            if t == 'consent.request' and d.get('consent_type') == 'camera_capture']


def _call(helper, user_id):
    with pytest.raises(RuntimeError) as err:
        helper.get_user_camera_inp(QUESTION, user_id, 'r-1')
    return str(err.value)


# ---------------------------------------------------------------------------
# THE DEFECT: the owner's turn had no frame and nobody was asked
# ---------------------------------------------------------------------------

def test_the_owners_turn_with_no_frame_asks_the_owner(helper, emitted):
    """Pre-fix: 0 rows, 0 asks -- the 11-07 state on the desktop."""
    text = _call(helper, OWNER)
    assert _camera_rows() == [(False, False, None)], _camera_rows()
    asks = _asks(emitted)
    assert len(asks) == 1 and asks[0]['user_id'] == OWNER, emitted
    low = text.lower()
    assert 'camera' in low and 'asked' in low, text


def test_asking_again_shows_one_card(helper, emitted):
    """A second call before the answer re-sends the same ask (one row, one
    stable card id), it does not file a second one."""
    _call(helper, OWNER)
    _call(helper, OWNER)
    assert len(_camera_rows()) == 1, _camera_rows()


# ---------------------------------------------------------------------------
# After the answer: no further asks
# ---------------------------------------------------------------------------

def test_an_allowed_camera_is_not_asked_for_again(helper, emitted):
    """The yes is the only gate.  The card's Allow writes a blanket grant
    (consentApi.grant with no agent); after it the tool asks nothing more."""
    from integrations.social.consent_service import ConsentService
    with db_session(commit=True) as db:
        ConsentService.grant_consent(db, OWNER, 'camera_capture')
    emitted.clear()
    text = _call(helper, OWNER)
    assert _asks(emitted) == [], emitted
    assert _camera_rows() == [(True, False, None)], _camera_rows()
    assert 'allowed' in text.lower(), text


def test_a_no_is_not_asked_for_again(helper, emitted):
    from integrations.social.consent_service import ConsentService
    _call(helper, OWNER)
    with db_session(commit=True) as db:
        ConsentService.decline(db, OWNER, 'camera_capture')
    emitted.clear()
    text = _call(helper, OWNER)
    assert _asks(emitted) == [], emitted
    assert 'said no' in text.lower(), text


# ---------------------------------------------------------------------------
# Turns no camera on this computer can answer: nobody is asked
# ---------------------------------------------------------------------------

def test_the_daemon_accounts_turn_asks_nobody(helper, emitted):
    text = _call(helper, DAEMON)
    assert _camera_rows(OWNER) == [] and _camera_rows(DAEMON) == []
    assert _asks(emitted) == [], emitted
    assert 'camera' in text.lower(), text


def test_with_nobody_signed_in_nobody_is_asked(helper, emitted, monkeypatch):
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)
    _call(helper, OWNER)
    assert _camera_rows() == [] and _asks(emitted) == []


def test_the_message_never_names_redis(helper, emitted):
    """Kept from test_get_frame_redis_down: naming the Redis leg sent agents
    off to start Redis."""
    assert 'redis' not in _call(helper, OWNER).lower()


# ---------------------------------------------------------------------------
# Review 2026-11-09 (hartos-16) of 7f281204b
# ---------------------------------------------------------------------------

def _as_agent(prompt_id):
    from hartos.threadlocal import thread_local_data
    return thread_local_data.turn_of(prompt_id)


def test_a_no_to_one_agent_stops_the_next_agents_automatic_ask(helper, emitted):
    """F2.  The camera is one device and the owner's last answer about it
    stands (consent_service.feed_said_no).  Pre-fix: a "Don't allow" on
    agent 54's card, and agent 55's next no-frame call filed a new card."""
    from integrations.social.consent_service import ConsentService
    with _as_agent('54'):
        _call(helper, OWNER)
    with db_session(commit=True) as db:
        ConsentService.decline(db, OWNER, 'camera_capture', agent_id='54')
    emitted.clear()
    with _as_agent('55'):
        text = _call(helper, OWNER)
    assert _asks(emitted) == [], emitted
    assert _camera_rows() == [(False, True, '54')], _camera_rows()
    assert 'said no' in text.lower(), text


def test_a_revoked_camera_is_not_asked_for_again(helper, emitted):
    """F2.  Allowed for all agents, then revoked on the privacy page: the
    next agent's no-frame call files nothing.  Pre-fix it filed a card."""
    from integrations.social.consent_service import ConsentService
    with db_session(commit=True) as db:
        ConsentService.grant_consent(db, OWNER, 'camera_capture')
    with db_session(commit=True) as db:
        ConsentService.revoke_consent(db, OWNER, 'camera_capture')
    emitted.clear()
    with _as_agent('54'):
        text = _call(helper, OWNER)
    assert _asks(emitted) == [], emitted
    assert _camera_rows() == [(False, True, None)], _camera_rows()
    assert 'said no' in text.lower(), text


def test_an_agents_ask_answered_for_all_agents_is_not_asked_again(helper, emitted):
    """F3.  The ask carries the agent (prompt id); the card's Allow writes a
    blanket grant, which check_consent's blanket step matches."""
    from integrations.social.consent_service import ConsentService
    with _as_agent('54'):
        _call(helper, OWNER)
    assert _camera_rows() == [(False, False, '54')], _camera_rows()
    assert _asks(emitted)[0]['agent_id'] == '54', emitted
    with db_session(commit=True) as db:
        ConsentService.grant_consent(db, OWNER, 'camera_capture')
    emitted.clear()
    with _as_agent('54'):
        text = _call(helper, OWNER)
    assert _asks(emitted) == [], emitted
    assert 'allowed' in text.lower(), text


def test_a_numeric_user_id_is_the_owners_turn(helper, emitted):
    """F3.  The tool's signature says ``user_id: int``; the owner id is a
    string in the environment."""
    _call(helper, int(OWNER))
    assert _camera_rows() == [(False, False, None)], _camera_rows()


def _broken_store(*a, **k):
    raise RuntimeError('database is locked')


def test_a_consent_store_failure_still_says_no_frame(helper, emitted, monkeypatch):
    """F3.  The store cannot be opened: the call still fails as a camera
    refusal, and nothing is filed.  (_camera_rows reads through this module's
    own db_session, which the patch does not touch.)"""
    from integrations.social import models
    monkeypatch.setattr(models, 'db_session', _broken_store)
    text = _call(helper, OWNER)
    assert 'camera' in text.lower() and 'locked' not in text, text
    assert _camera_rows() == [] and _asks(emitted) == []


@pytest.mark.parametrize('answer', [None, 'allowed', 'said no', 'daemon', 'failed'])
def test_every_no_frame_reply_says_what_to_do(helper, emitted, monkeypatch, answer):
    """F4.  Owner 2026-11-09: every tool refusal says what was wrong and what
    to do (core.tool_refusal).  The 28 calls of 11-07 retried a reply that
    said neither."""
    from core.tool_refusal import refusal_has_hint
    from integrations.social import models
    from integrations.social.consent_service import ConsentService
    user = OWNER
    if answer == 'allowed':
        with db_session(commit=True) as db:
            ConsentService.grant_consent(db, OWNER, 'camera_capture')
    elif answer == 'said no':
        with db_session(commit=True) as db:
            ConsentService.request_consent(db, OWNER, 'camera_capture')
            ConsentService.decline(db, OWNER, 'camera_capture')
    elif answer == 'daemon':
        user = DAEMON
    elif answer == 'failed':
        monkeypatch.setattr(models, 'db_session', _broken_store)
    text = _call(helper, user)
    assert refusal_has_hint(text), text
    assert text.startswith('Error: the get_user_camera_inp call'), text
    assert 'camera' in text.lower() and 'redis' not in text.lower(), text


def test_the_deliberate_ask_keeps_a_no_to_one_agent_that_agents():
    """F2's other half.  Request_Camera_Access is the agent asking in so many
    words, and the card's "Don't allow this agent" is per agent: a No to
    agent 54 leaves agent 55's own ask open."""
    from integrations.social import consent_service as cs
    from integrations.social.consent_service import ConsentService
    with patch.dict(os.environ, {'HEVOLVE_OWNER_USER_ID': OWNER}), \
            patch.object(cs, '_emit'), \
            patch.object(cs, '_embodied_feed_from_consent'):
        assert cs.ask_owner('camera_capture', 'look', '54') == cs.OWNER_ASKED
        with db_session(commit=True) as db:
            ConsentService.decline(db, OWNER, 'camera_capture', agent_id='54')
        assert cs.ask_owner('camera_capture', 'look', '54') == cs.OWNER_SAID_NO
        assert cs.ask_owner('camera_capture', 'look', '55') == cs.OWNER_ASKED
        assert cs.ask_owner('camera_capture', 'look', '56',
                            stop_at_last_no=True) == cs.OWNER_SAID_NO
    assert sorted(a or '' for _, _, a in _camera_rows()) == ['54', '55']


def test_the_two_camera_asks_are_one_ask():
    """F1 (Gate 4).  The camera tool's ask was a copy of Request_Camera_Access's
    (hart_intelligence_entry._request_consent), without its central check.
    Both now call consent_service.ask_owner and neither asks by itself."""
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    asking = {'check_or_request', 'request_consent', 'check_consent', 'declined'}
    for path, name in ((root / 'hartos' / 'helper.py', '_no_camera_frame_reason'),
                       (root / 'hart_intelligence_entry.py', '_request_consent')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        node = next((n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == name), None)
        assert node is not None, f'{name} not found in {path.name}'
        called = {c.func.attr if isinstance(c.func, ast.Attribute)
                  else getattr(c.func, 'id', None)
                  for c in ast.walk(node) if isinstance(c, ast.Call)}
        assert 'ask_owner' in called, (path.name, name, sorted(filter(None, called)))
        assert not (asking & called), (path.name, name, sorted(asking & called))
