"""The phone's chat paths run on central HARTOS, as the phone's login.

Android posts every custom-agent turn to chat/custom_gpt and every Teach
Yourself turn to chat/teachme2 (CustomChatBotAPI, TeachYourselfActivity),
with the token its Hevolve login returned.  Measured 2026-11-19 on central:
Kong sends both paths to chatbot_pipeline, which has answered nothing since
2026-07-20, and central HARTOS had no such route; the adapter that serves
them lived only in Nunba (routes/chatbot_routes.py).  It now lives once in
HARTOS (core.phone_chat).  Central serves it through its own /chat, as the
account the login proves, never the body's user_id; Nunba imports the same
module for the desktop.

These drive the REAL routes behind the REAL gate, central tier.
"""
import os
import sys
import tempfile

os.environ['HEVOLVE_DB_PATH'] = ':memory:'
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from flask import g, jsonify, request  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from integrations.social import kong_identity  # noqa: E402
from integrations.social.models import Base, User, db_session, get_engine  # noqa: E402

PHONE = {'REMOTE_ADDR': '172.21.0.2'}   # Kong's address on central
OWNER = 'owner@example.com'
TOKEN = 'kong-opaque-token'

#: What Android's CustomBotsActivity posts for a custom agent's turn.
ANDROID_TURN = {
    'text': ['hello coach'], 'raw_text': 'hola coach', 'user_id': '10202',
    'teacher_avatar_id': 2933, 'conversation_id': 'c-1', 'prompt_id': 344,
    'request_id': 'phone-r1-abc', 'create_agent': False,
}


class _Stop(Exception):
    """Raised once /chat has resolved its user: the gate is behind us."""


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry  # noqa: TID251 -- the routes under test are on its app
    yield hart_intelligence_entry


@pytest.fixture(autouse=True)
def _central_env(monkeypatch):
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    monkeypatch.setenv('NUNBA_TEACH_AGENT_ID', '')
    for name in ('NUNBA_BUNDLED', 'HEVOLVE_API_KEY', 'HEVOLVE_REQUIRE_AUTH',
                 'TRUSTED_PROXY', 'NUNBA_CI', 'HEVOLVE_TRUST_KONG',
                 'HEVOLVE_CLOUD_MODE'):
        monkeypatch.delenv(name, raising=False)
    engine = get_engine()
    Base.metadata.create_all(engine)
    with db_session() as db:
        db.add(User(username='owner', display_name='Owner', email=OWNER,
                    user_type='human', api_token='t-owner'))
    from core import phone_chat
    phone_chat.TEACH_TOPICS.clear()
    yield
    Base.metadata.drop_all(engine)


def _owner_id():
    with db_session(commit=False) as db:
        return str(db.query(User).filter(User.email == OWNER).first().id)


def _post(hie, monkeypatch, path, body, token=TOKEN, proves=OWNER, chat=None):
    """POST ``path`` through the real gate, Kong answering ``proves`` for the
    token; ``chat`` stands in for /chat's view when given."""
    if chat is not None:
        monkeypatch.setitem(hie.app.view_functions, 'chat', chat)
    monkeypatch.setitem(hie.app.config, 'PROPAGATE_EXCEPTIONS', True)
    secrets = {'security.secrets_manager': MagicMock(
        get_secret=lambda name: os.environ.get(name, ''))}
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    with patch.dict(sys.modules, secrets), \
            patch.object(kong_identity, 'email_for_token', return_value=proves), \
            patch.object(hie, '_cleanup_stale_agents', lambda: None):
        return hie.app.test_client().post(path, json=body, environ_base=PHONE,
                                          headers=headers)


def _recording_chat(reply=('agent says hi', 200)):
    """A /chat view that records the body and the gate's verdict it saw."""
    seen = []

    def chat():
        seen.append({'body': request.get_json(),
                     'auth_source': getattr(g, 'auth_source', None),
                     'user_id': (getattr(g, 'jwt_payload', None) or {}).get('user_id')})
        text, status = reply
        body = text if isinstance(text, dict) else {'response': text}
        return jsonify(body), status
    return chat, seen


# ── chat/custom_gpt ──────────────────────────────────────────────────

def test_the_turn_runs_in_central_chat_as_the_login(hie, monkeypatch):
    """The REAL /chat receives HARTOS's own body shape and resolves the user
    the login proves, not the body's legacy id."""
    seen = {}

    def stop_at_user(user_id=None, **_k):
        seen['user_id'] = user_id
        seen['auth_source'] = getattr(g, 'auth_source', None)
        seen['body'] = request.get_json()
        raise _Stop()

    with patch.object(hie.thread_local_data, 'set_user_id', stop_at_user):
        _post(hie, monkeypatch, '/chat/custom_gpt', ANDROID_TURN)
    assert seen, '/chat never ran the turn'
    assert seen['user_id'] == _owner_id(), "the turn ran as the body's user_id"
    assert seen['auth_source'] == 'hevolve'
    body = seen['body']
    assert body['prompt'] == 'hello coach'
    assert body['prompt_id'] == 344
    assert body['request_id'] == 'phone-r1-abc'
    assert body['draft_first'] is False
    assert body['casual_conv'] is False, 'an agent-bound turn is not casual'
    assert body['teacher_avatar_id'] == 2933


def test_the_reply_is_the_phones_shape(hie, monkeypatch):
    chat, seen = _recording_chat(('Lets practise ordering coffee.', 200))
    resp = _post(hie, monkeypatch, '/chat/custom_gpt', ANDROID_TURN, chat=chat)
    assert resp.status_code == 200
    body = resp.get_json()
    # Android draws the bubble from answerList and drops a priority-less one.
    assert body['answerList'] == ['Lets practise ordering coffee.']
    assert body['priority'] == 99
    assert body['text'] == 'Lets practise ordering coffee.'
    assert body['request_id'] == 'phone-r1-abc'
    assert body['dynamic_data'] == {} and body['optionLists'] == []
    assert seen[0]['auth_source'] == 'hevolve' and seen[0]['user_id'] == _owner_id()


def test_without_a_login_nothing_runs(hie, monkeypatch):
    chat, seen = _recording_chat()
    resp = _post(hie, monkeypatch, '/chat/custom_gpt', ANDROID_TURN,
                 token=None, chat=chat)
    assert resp.status_code == 401
    assert seen == []


def test_a_login_kong_does_not_vouch_for_runs_nothing(hie, monkeypatch):
    chat, seen = _recording_chat()
    resp = _post(hie, monkeypatch, '/chat/custom_gpt', ANDROID_TURN,
                 proves=None, chat=chat)
    assert resp.status_code == 401
    assert seen == []


def test_a_failed_turn_is_non_2xx_and_never_leaks(hie, monkeypatch):
    from core.phone_chat import TURN_FAILED_TEXT
    chat, _ = _recording_chat(({'error': 'Traceback at C:\\secret\\path'}, 500))
    resp = _post(hie, monkeypatch, '/chat/custom_gpt', ANDROID_TURN, chat=chat)
    assert resp.status_code >= 500
    assert 'secret' not in resp.get_data(as_text=True)
    assert resp.get_json()['answerList'] == [TURN_FAILED_TEXT]


def test_a_blank_turn_is_refused_without_running(hie, monkeypatch):
    chat, seen = _recording_chat()
    resp = _post(hie, monkeypatch, '/chat/custom_gpt',
                 {**ANDROID_TURN, 'text': ['  '], 'raw_text': ''}, chat=chat)
    assert resp.status_code == 400
    assert seen == []


# ── chat/teachme2 ────────────────────────────────────────────────────

def test_a_topic_is_taught_and_continued_for_the_login(hie, monkeypatch):
    """The topic is kept for the account the login proves: a later turn from
    the same login continues it whatever user_id its body names."""
    chat, seen = _recording_chat()
    body = _post(hie, monkeypatch, '/chat/teachme2', {
        'text': ['teach me'], 'user_id': '10202',
        'goals': {'name': 'Photosynthesis', 'scope': 'topic'}},
        chat=chat).get_json()
    assert seen[0]['body']['prompt'].startswith(
        'teach me\nYou are my patient tutor. Teach Photosynthesis')
    assert body['dynamic_data'] == {'topic': 'Photosynthesis',
                                    'options': ['continue'], 'teachme': True}
    assert body['teachme'] is True
    _post(hie, monkeypatch, '/chat/teachme2',
          {'text': ['continue'], 'user_id': 'someone-else'}, chat=chat)
    assert seen[1]['body']['prompt'] == (
        'Continue teaching Photosynthesis: the next step, '
        'then one quick check question.')


@pytest.mark.parametrize('scope', ['kids_game', 'progress_report', 'batch_sync'])
def test_kids_transport_calls_get_a_non_2xx_and_no_turn(hie, monkeypatch, scope):
    chat, seen = _recording_chat()
    resp = _post(hie, monkeypatch, '/chat/teachme2', {
        'text': ['Game completed: {}'], 'user_id': '10202',
        'goals': {'name': 'kids_learning_progress', 'scope': scope}}, chat=chat)
    # 501 exactly: the refusal the client retries on, not a missing route.
    assert resp.status_code == 501
    assert seen == []
