"""A Reel Studio agent's turns run on this node only.

Plan WS-8a step 3 (critique finding 26, decision U4).  Her clients' faces and
her captions must not reach another node's or the cloud's model.  So a turn
for an agent whose record carries 'reel_studio' -- or whose creation hint does,
before the record exists -- runs with intelligence_preference 'local_only',
in memory only: nothing is persisted (the phone sends no preference at all,
and the node's own setting stays as the owner left it).

Where an agent-bound turn can leave the node, read in hart_intelligence_entry:
its only intelligence_preference reader, dispatch_draft_first, is skipped for
agent-bound turns, so the ways out are the body's ``model_config`` (a
hive/cloud tier the turn's autogen agents then run on) and ``speculative``
(the speculative dispatcher's background expert).  Both are refused for a
reel agent; any other agent keeps them.

Driven through the REAL /chat (app.test_client).  Each turn stops where its
agent would start working and records the model tier it would run on.
"""
import json
import os
import sys
import tempfile
import uuid

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

from tests.conftest import refuse_all_network  # noqa: E402

HIVE_TIER = [{'model': 'hive-expert-70b', 'api_key': 'k',
              'base_url': 'http://hive-peer.invalid/v1'}]


class _Stop(Exception):
    pass


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


@pytest.fixture
def turn(hie, tmp_path, monkeypatch):
    prompts = tmp_path / 'prompts'
    prompts.mkdir()
    import core.cache_loaders as cache_loaders
    import core.recipe_sync as recipe_sync
    from core import user_file_store as ufs
    from integrations.agent_engine import agent_daemon, speculative_dispatcher
    from integrations.social import chat_messages, rate_limiter
    refuse_all_network(monkeypatch)
    monkeypatch.delenv('HEVOLVE_API_KEY', raising=False)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    monkeypatch.setitem(hie.app.config, 'PROPAGATE_EXCEPTIONS', True)
    monkeypatch.setattr(hie, 'PROMPTS_DIR', str(prompts))
    monkeypatch.setattr(cache_loaders, 'PROMPTS_DIR', str(prompts))
    monkeypatch.setattr(rate_limiter._limiter, 'check', lambda *a, **k: True)
    monkeypatch.setattr(recipe_sync, 'pull_recipe', lambda *a, **k: False)
    monkeypatch.setattr(chat_messages, 'persist_and_publish_async', MagicMock())
    monkeypatch.setattr(ufs, '_STORE', ufs.UserFileStore(
        str(tmp_path / 'user_files'), disk_free=lambda _p: 1 << 40))
    for name in ('_persist_language', '_cleanup_stale_agents', '_record_lifecycle',
                 '_tts_synthesize_and_publish', 'publish_async'):
        monkeypatch.setattr(hie, name, lambda *a, **k: None)
    monkeypatch.setattr(hie, 'get_memory', lambda **k: None)
    monkeypatch.setattr(hie, '_get_or_create_graph', lambda *a, **k: None)
    monkeypatch.setattr(hie, 'pooled_get', MagicMock(side_effect=ConnectionError('no cloud')))
    monkeypatch.setattr(hie, 'pooled_post', MagicMock())
    monkeypatch.setattr(agent_daemon, '_flow_recipe_exists', lambda _pid: True)

    seen = {}

    def working(where):
        def _stand_in(*_a, **_k):
            seen['where'] = where
            seen['tier'] = hie.thread_local_data.get_model_config_override()
            raise _Stop()
        return _stand_in

    def speculative_asked():
        seen['where'] = 'speculative_dispatcher'
        raise _Stop()

    monkeypatch.setattr(hie, 'chat_agent', working('chat_agent'))
    monkeypatch.setitem(sys.modules, 'hartos.gather_agentdetails',
                        SimpleNamespace(gather_info=working('gather_info')))
    monkeypatch.setattr(speculative_dispatcher, 'get_speculative_dispatcher',
                        speculative_asked)

    pid = str(uuid.uuid4().int % 10 ** 11)
    user = 'u-local-%s' % pid[-6:]
    client = hie.app.test_client()

    def record(goal_tags=None):
        rec = {'name': 'Studio', 'goal': 'Help my salon', 'prompt_id': pid,
               'creator_user_id': user,
               'flows': [{'flow_name': 'main', 'persona': 'Artist',
                          'actions': ['Make a reel'], 'sub_goal': 'reels'}]}
        if goal_tags is not None:
            rec['goal_tags'] = goal_tags
        path = prompts / f'{pid}.json'
        path.write_text(json.dumps(rec))
        (prompts / f'{pid}_0_recipe.json').write_text(
            json.dumps({'status': 'completed', 'actions': []}))
        return path

    def post(create_agent=False, **extra):
        body = {'prompt': "make today's reel", 'user_id': user, 'prompt_id': int(pid),
                'request_id': 'rid-%s' % uuid.uuid4().hex[:12], 'casual_conv': False,
                'probe': False, 'intermediate': False, 'create_agent': create_agent,
                'file_id': 0, 'preferred_lang': 'en', 'media_mode': 'text',
                'autonomous': False, 'agentic_execute': False}
        body.update(extra)
        seen.clear()
        try:
            r = client.post('/chat', json=body)
        except _Stop:
            return dict(seen)
        raise AssertionError('the turn never reached its agent: %s %s'
                             % (r.status_code, r.get_data(as_text=True)[:300]))

    yield SimpleNamespace(post=post, record=record, prompts=prompts)
    key = f'{user}_{pid}'
    for table in (hie.review_agents, hie.conversation_agent, hie._gather_turn_counts):
        table.pop(key, None)
    if pid in hie.first_promts:
        hie.first_promts.remove(pid)


def test_a_reel_agent_turn_ignores_a_hive_tier_and_speculation(turn):
    path = turn.record(goal_tags=['reel_studio'])
    before = path.read_bytes()
    seen = turn.post(model_config=HIVE_TIER, speculative=True,
                     intelligence_preference='hive_preferred')
    assert seen['where'] == 'chat_agent', (
        'a reel agent turn was handed to the speculative (hive/cloud) dispatcher')
    assert seen['tier'] is None, (
        'a reel agent turn ran on the tier the body named: %r' % (seen['tier'],))
    assert path.read_bytes() == before, 'the forced preference was written somewhere'
    assert sorted(p.name for p in turn.prompts.iterdir()) == sorted(
        [path.name, path.name.replace('.json', '_0_recipe.json')])


def test_a_creation_hint_makes_the_turn_local_before_the_record_exists(turn):
    seen = turn.post(create_agent=True, goal_tags=['reel_studio'],
                     model_config=HIVE_TIER, speculative=True)
    assert seen['where'] == 'gather_info'
    assert seen['tier'] is None


def test_any_other_agent_keeps_the_body_tier(turn):
    turn.record()
    seen = turn.post(model_config=HIVE_TIER)
    assert seen['where'] == 'chat_agent'
    assert seen['tier'] and seen['tier'][0]['model'] == 'hive-expert-70b'


def test_any_other_agent_keeps_speculation(turn):
    turn.record(goal_tags=['news'])
    assert turn.post(speculative=True)['where'] == 'speculative_dispatcher'
