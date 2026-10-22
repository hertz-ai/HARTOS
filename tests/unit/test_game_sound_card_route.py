"""POST /api/agent/approval for a game sound, through the REAL route.

Owner ruling 2026-10-17: only an agent's creator chooses its sounds for
everyone; anyone else's answer is their own.  The route names the answerer
from the caller's verified token and never from the body (review of
c48e90601, F3: a mutant that read the body's user_id passed every test,
because the card tests called the helper, not the route).
"""
import json
import os
import sys
import tempfile

os.environ['HEVOLVE_DB_PATH'] = ':memory:'
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

import pytest  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

CREATOR, VISITOR, AGENT = 'creator-1', 'visitor-1', 4242
TAKE = 'https://node/a.mp3'
CARD = {'agent_id': str(AGENT), 'action': 'game_sound:eng-01:bgm',
        'decision': 'deny', 'reason': 'too jangly'}


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry  # noqa: TID251 -- the route under test is on its app
    yield hart_intelligence_entry


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """Agent 4242, built by CREATOR, with one bgm take, in a temp store."""
    from core import cache_loaders
    prompts, data = tmp_path / 'prompts', tmp_path / 'agent_data'
    prompts.mkdir()
    data.mkdir()
    (prompts / f'{AGENT}.json').write_text(
        json.dumps({'prompt_id': AGENT, 'creator_user_id': CREATOR}))
    monkeypatch.setattr(cache_loaders, 'PROMPTS_DIR', str(prompts))
    monkeypatch.setattr(cache_loaders, '_agent_config_cache', {})
    monkeypatch.setattr(cache_loaders, 'AGENT_DATA_DIR', str(data))
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)
    cache_loaders.save_agent_data(
        AGENT, {'games': {'eng-01': {'sounds': {'bgm': {'url': TAKE}}}}})
    return cache_loaders


def _post(hie, body, user=None):
    from integrations.social.auth import generate_jwt
    headers = {'Authorization': f'Bearer {generate_jwt(user, user)}'} if user else {}
    response = hie.app.test_client().post('/api/agent/approval', json=body,
                                          headers=headers)
    return response.status_code, response.get_json()


def _bgm(cache_loaders, store_id):
    store = cache_loaders.load_agent_data(store_id) or {}
    return (((store.get('games') or {}).get('eng-01') or {})
            .get('sounds') or {}).get('bgm') or {}


def test_a_body_naming_the_creator_decides_nothing(hie, stores):
    code, body = _post(hie, {**CARD, 'user_id': CREATOR, 'userId': CREATOR})
    assert (code, body['applied']) == (401, False)
    assert _bgm(stores, AGENT)['url'] == TAKE


def test_a_bearer_that_names_nobody_is_refused_with_one_line(hie, stores):
    """#935: the 401 for a bearer that names no one is said once in the log
    (integrations.social.auth.log_refused_credential): the route and the
    reason, never the bearer itself."""
    import logging
    from unittest.mock import MagicMock, patch
    unknown = 'opaque-credential-no-account-has-it'
    lines = []

    class _Refusals(logging.Handler):
        def emit(self, record):
            if record.funcName == 'log_refused_credential':
                lines.append(record.getMessage())
    log, handler = logging.getLogger('hevolve_social'), _Refusals(logging.WARNING)
    level = log.level
    log.addHandler(handler)
    log.setLevel(logging.WARNING)
    db = MagicMock()        # no account holds the bearer
    db.query.return_value.filter.return_value.first.return_value = None
    try:
        with patch('integrations.social.models.get_db', return_value=db):
            response = hie.app.test_client().post(
                '/api/agent/approval', json=CARD,
                headers={'Authorization': f'Bearer {unknown}'})
    finally:
        log.removeHandler(handler)
        log.setLevel(level)
    assert (response.status_code, response.get_json()['applied']) == (401, False)
    (line,) = lines
    assert 'POST /api/agent/approval' in line and 'unknown_token' in line
    assert unknown not in line
    assert _bgm(stores, AGENT)['url'] == TAKE


def test_the_creators_signed_answer_is_the_agents(hie, stores):
    code, body = _post(hie, CARD, CREATOR)
    assert (code, body['scope'], body['applied']) == (200, 'agent', True)
    assert _bgm(stores, AGENT)['rejected_url'] == TAKE


def test_a_visitors_signed_answer_is_theirs_alone(hie, stores):
    from core.agent_tools import person_store_for
    code, body = _post(hie, {**CARD, 'user_id': CREATOR}, VISITOR)
    assert (code, body['scope'], body['applied']) == (200, 'mine', True)
    assert _bgm(stores, AGENT)['url'] == TAKE
    assert _bgm(stores, person_store_for(VISITOR, AGENT))['rejected_url'] == TAKE
