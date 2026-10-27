"""A REUSE turn restores the agent's store from disk; it never starts it empty.

hartos/reuse_recipe.py asked `prompt_id not in agent_data.keys()`, and a
TTLCache's keys() lists only what is cached, so the first REUSE turn after a
restart set the agent's store to {} without reading its file.  The tools then
saw no template and no game sounds, and the next save wrote that empty store
over the creator's (review of fafa03c40, D0: run against the real
chat_agent, a visitor's fill of one game state wiped the creator's lesson
template, approved music and a rejection).

This drives the REAL reuse_recipe.chat_agent up to the line, with the step
after it (create_agents_for_role) stopped, over the real agent_data cache.
"""
import json
import os
import sys
import tempfile

os.environ['HEVOLVE_DB_PATH'] = ':memory:'
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

AGENT = 4242
STORE = {'lesson_template': {'title': "the creator's template"},
         'games': {'eng-01': {'sounds': {'bgm': {'url': 'https://node/u.mp3',
                                                 'approved_at': 1.0}}}}}


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry  # noqa: TID251 -- chat_agent runs inside its app
    yield hart_intelligence_entry


@pytest.fixture
def reuse(hie, tmp_path, monkeypatch):
    from core import cache_loaders
    from hartos import helper, reuse_recipe
    prompts, data = tmp_path / 'prompts', tmp_path / 'agent_data'
    prompts.mkdir()
    data.mkdir()
    (prompts / f'{AGENT}.json').write_text(
        json.dumps({'prompt_id': AGENT, 'creator_user_id': 'creator'}))
    monkeypatch.setattr(cache_loaders, 'PROMPTS_DIR', str(prompts))
    monkeypatch.setattr(cache_loaders, '_agent_config_cache', {})
    monkeypatch.setattr(cache_loaders, 'AGENT_DATA_DIR', str(data))
    monkeypatch.setattr(helper, 'AGENT_DATA_DIR', str(data))
    cache_loaders.forget_cached_store(AGENT)
    cache_loaders.save_agent_data(AGENT, STORE)
    yield reuse_recipe
    cache_loaders.forget_cached_store(AGENT)


def test_the_first_reuse_turn_after_a_restart_keeps_the_agents_store(hie, reuse):
    assert AGENT not in reuse.agent_data.keys(), 'the store must start uncached'
    with hie.app.app_context(), hie.app.test_request_context('/chat'), \
            patch.object(reuse, 'create_agents_for_role',
                         side_effect=RuntimeError('stop after the store line')):
        with pytest.raises(RuntimeError, match='stop after the store line'):
            reuse.chat_agent('visitor-1', 'hello', AGENT, None, 'rid-store-1')
    held = reuse.agent_data.get(AGENT)
    assert held == STORE, held
