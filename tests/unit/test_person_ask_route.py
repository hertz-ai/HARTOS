"""The answer to an ask reaches its agent through POST /api/agent/approval.

Driven through the REAL hart_intelligence_entry app (imported once per module,
as tests/unit/test_approval_ap2_branch.py does).  The answerer is the verified
Bearer identity, never a body field.  On the code before person_ask the route
answered an ``ask_id`` body 400 'invalid decision' and nothing reached the
agent.  Design: docs/design/PERSON_ASK_LIQUID_UI_TOOL.md.

    python -m pytest tests/unit/test_person_ask_route.py -q
"""
import os

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest  # noqa: E402

from integrations.social.auth import generate_jwt  # noqa: E402
from integrations.social.models import (  # noqa: E402
    Base, Notification, User, db_session, get_engine)


@pytest.fixture(scope='module')
def app():
    import logging
    root = logging.getLogger()
    saved = (root.level, list(root.handlers), logging.getLogRecordFactory())
    os.environ.setdefault('HEVOLVE_START_BACKGROUND_SERVICES', '0')
    hie = pytest.importorskip('hart_intelligence_entry')
    hie.app.config['TESTING'] = True
    yield hie.app
    root.setLevel(saved[0])
    for h in list(root.handlers):
        if h not in saved[1]:
            root.removeHandler(h)
    logging.setLogRecordFactory(saved[2])


@pytest.fixture
def asked(app, monkeypatch):
    """One open ask to 10202, its legs recorded instead of sent."""
    Base.metadata.create_all(get_engine())
    import integrations.agent_engine.liquid_ui_service as lui
    import integrations.social.realtime as rt
    monkeypatch.setattr(rt, 'publish_event', lambda *a, **k: None)
    monkeypatch.setattr(lui, 'push_agent_ui', lambda *a, **k: True)
    for uid in ('10202', '777'):
        with db_session() as db:
            if not db.query(User).filter_by(id=uid).first():
                db.add(User(id=uid, username=f'p{uid}', display_name=f'P {uid}',
                            user_type='human', api_token=f'tok-{uid}'))
    from integrations.agent_engine import person_ask
    out = person_ask.ask('10202', '342', {
        'type': 'file_request', 'title': 'Send me the clips', 'purpose': 'reel_source',
        'accept': 'video', 'max_files': 5}, prompt_id='342')
    assert out['ok'], out
    return {'client': app.test_client(), 'ask_id': out['ask_id']}


def _post(ctx, body, user='10202'):
    headers = {}
    if user:
        headers['Authorization'] = 'Bearer ' + generate_jwt(user, f'p{user}')
    return ctx['client'].post('/api/agent/approval', json=body, headers=headers)


def _is_read(ask_id):
    with db_session(commit=False) as db:
        return bool(db.query(Notification).filter_by(id=ask_id).one().is_read)


def test_the_person_answers_an_ask_through_the_one_door(asked):
    from integrations.agent_engine import person_ask
    xid = '1f' * 32
    r = _post(asked, {'ask_id': asked['ask_id'], 'answer': {'xids': [xid]}})
    assert r.status_code == 200, r.get_json()
    assert r.get_json() == {'status': 'ok', 'ask_id': asked['ask_id']}
    assert person_ask.answer_of(asked['ask_id']) == {'xids': [xid]}
    assert _is_read(asked['ask_id']) is True


def test_a_body_user_id_never_answers_for_the_person(asked):
    from integrations.agent_engine import person_ask
    r = _post(asked, {'ask_id': asked['ask_id'], 'answer': {'xids': []},
                      'user_id': '10202'}, user='777')
    assert r.status_code == 403 and r.get_json()['reason'] == 'not_yours'
    r = _post(asked, {'ask_id': asked['ask_id'], 'answer': {'xids': []},
                      'user_id': '10202'}, user=None)
    assert r.status_code == 403
    assert person_ask.answer_of(asked['ask_id']) is None
    assert _is_read(asked['ask_id']) is False


def test_a_malformed_answer_is_refused(asked):
    r = _post(asked, {'ask_id': asked['ask_id'], 'answer': {'xids': ['not-an-xid']}})
    assert r.status_code == 400 and r.get_json()['reason'] == 'bad_answer'


def test_a_game_sound_answer_still_reaches_its_own_branch_as_the_verified_caller(
        asked, monkeypatch):
    """#240's branch keeps its shape and its identity rule through the
    extended door (hartos-f3's acceptance case): a body without ask_id goes
    on to answer_game_sound_card, with the Bearer's user as the answerer."""
    import core.agent_tools as at
    calls = []

    def fake_answer(agent_id, action, approved, reason, approver_id):
        calls.append((agent_id, action, approved, reason, approver_id))
        return {'status': 'ok', 'kept': approved}, 200

    monkeypatch.setattr(at, 'answer_game_sound_card', fake_answer)
    r = _post(asked, {'agent_id': '342', 'action': 'game_sound:memory_match:win',
                      'decision': 'approve', 'user_id': '777'})
    assert r.status_code == 200 and r.get_json() == {'status': 'ok', 'kept': True}
    assert calls == [('342', 'game_sound:memory_match:win', True, '', '10202')]


def test_an_approval_without_an_ask_id_keeps_its_old_answers(asked):
    """The branches below the ask's keep their shape: a body with no decision
    word is still refused as before."""
    r = _post(asked, {'agent_id': '342', 'action': 'something'})
    assert r.status_code == 400
    assert r.get_json()['reason'] == 'invalid decision'
