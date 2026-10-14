"""A Reel Studio agent's turns run on this node only.

Plan WS-8a step 3 (critique finding 26, decision U4).  Her clients' faces and
her captions must not reach another node's or the cloud's model.  So a turn
for an agent whose record carries 'reel_studio' -- or whose creation hint does,
before the record exists -- runs local-only, in memory only: nothing is
persisted (the phone sends no preference at all, and the node's own setting
stays as the owner left it).

Where an agent-bound turn can leave the node: the body's ``model_config`` (a
hive/cloud tier its autogen agents then run on), ``speculative`` (the
speculative dispatcher's background expert) and the revenue lane (the only
lane the configured expert is offered to).  /chat's model decision has ONE
owner, core.request_lane (amendment A9 e, ruled with the lanes lane): the
turn's local-only requirement is an INPUT to it -- the person's own
local_only toggle, or the goal tags of the turn's agent, including the agent
an agentic_execute plan routes the turn to -- and it answers with no tier, no
speculation and the user lane.  Any other turn keeps the body's tier (first,
with this node's backend behind it) and its lane.  On the CENTRAL node the
tag is inert (amendment A8 d): a reel record or hint pins nothing there.

Driven through the REAL /chat (app.test_client), the real register_chat_lane
and the real thread-local.  Each turn stops where its agent would start
working and records the model tier it would run on and its lane.
"""
import json
import os
import sys
import tempfile
import uuid

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

from types import ModuleType, SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

from tests.conftest import refuse_all_network  # noqa: E402

HIVE_TIER = [{'model': 'hive-expert-70b', 'api_key': 'k',
              'base_url': 'http://hive-peer.invalid/v1'}]
CLOUD_TIER = [{'model': 'claude-sonnet-4-5', 'api_key': 'k',
               'base_url': 'https://cloud-gateway.invalid/llm/v1'}]


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
    from core import llm_outbound_logger as lol
    from core import user_file_store as ufs
    from integrations.agent_engine import agent_daemon, dispatch, speculative_dispatcher
    from integrations.social import chat_messages, rate_limiter
    refuse_all_network(monkeypatch)
    # A genuine request id marks the person active for the daemon's yield
    # gate; leave that gate as found (test_flagship_agents' tutor defers
    # behind it otherwise).
    monkeypatch.setattr(dispatch, '_last_user_chat_at', dispatch._last_user_chat_at)
    # Nunba's preference writer (llama.llama_config.LlamaConfig, the one
    # writer of the person's saved toggle): a local-only turn must never
    # reach it.  HARTOS does not import Nunba; the stand-in records a call
    # if anything here ever did.
    _llama, _llama_config = ModuleType('llama'), ModuleType('llama.llama_config')
    writer = MagicMock(name='LlamaConfig.set_intelligence_preference')
    _llama_config.LlamaConfig = type('LlamaConfig', (), {
        'set_intelligence_preference': lambda self, pref: writer(pref)})
    _llama.llama_config = _llama_config
    monkeypatch.setitem(sys.modules, 'llama', _llama)
    monkeypatch.setitem(sys.modules, 'llama.llama_config', _llama_config)
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

    def post(create_agent=False, headers=None, **extra):
        body = {'prompt': "make today's reel", 'user_id': user, 'prompt_id': int(pid),
                'request_id': 'rid-%s' % uuid.uuid4().hex[:12], 'casual_conv': False,
                'probe': False, 'intermediate': False, 'create_agent': create_agent,
                'file_id': 0, 'preferred_lang': 'en', 'media_mode': 'text',
                'autonomous': False, 'agentic_execute': False}
        body.update(extra)
        seen.clear()
        try:
            r = client.post('/chat', json=body, headers=headers or {})
        except _Stop:
            return dict(seen, lane=lol.lane_for_request(body['request_id']))
        raise AssertionError('the turn never reached its agent: %s %s'
                             % (r.status_code, r.get_data(as_text=True)[:300]))

    def shopper_token():
        """A JWT carrying the commerce tenant claim: the revenue lane's proof."""
        from integrations.commerce.bindings import TENANT
        from integrations.social.auth import generate_jwt
        return {'Authorization': 'Bearer ' + generate_jwt(user, 'Owner', tenant_id=TENANT)}

    yield SimpleNamespace(post=post, record=record, prompts=prompts, pid=pid,
                          writer=writer, shopper_token=shopper_token)
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
    turn.writer.assert_not_called()


def test_a_reel_agent_turn_is_never_on_the_revenue_lane(turn):
    """Ruling (6): a local-only agent with a cloud tier, speculation and a
    revenue claim on a tenant JWT ends with no tier, no speculation and the
    user lane -- the configured expert is offered to revenue only."""
    turn.record(goal_tags=['reel_studio'])
    seen = turn.post(model_config=CLOUD_TIER, speculative=True, lane='revenue',
                     headers=turn.shopper_token())
    assert seen['where'] == 'chat_agent'
    assert seen['tier'] is None
    assert seen['lane'] == 'user', 'a reel agent turn was registered %r' % seen['lane']
    turn.writer.assert_not_called()


def test_any_other_agent_keeps_its_tier_first_and_its_lane(turn):
    from core.autogen_config import get_autogen_config_list
    turn.record()
    seen = turn.post(model_config=CLOUD_TIER, headers=turn.shopper_token())
    assert seen['where'] == 'chat_agent'
    assert seen['tier'][0]['model'] == 'claude-sonnet-4-5'
    own = get_autogen_config_list()[0]
    assert len(seen['tier']) == 2
    assert (seen['tier'][1].get('base_url'), seen['tier'][1].get('model')) == (
        own.get('base_url'), own.get('model')), 'the node\'s own backend is not behind the tier'
    assert seen['lane'] == 'revenue'


def test_the_persons_own_local_only_toggle_is_the_same_rule(turn):
    """Ruling (4): one rule for every source.  A Demopage local_only body for
    an agent with no reel tag gets what a reel agent gets."""
    turn.record(goal_tags=['news'])
    seen = turn.post(model_config=CLOUD_TIER, speculative=True, lane='revenue',
                     intelligence_preference='local_only', headers=turn.shopper_token())
    assert seen['where'] == 'chat_agent'
    assert seen['tier'] is None
    assert seen['lane'] == 'user'
    turn.writer.assert_not_called()


def test_an_agentic_plan_routed_to_a_reel_agent_keeps_to_this_node(turn):
    """The plan's matched agent replaces prompt_id after the model decision;
    the decision therefore reads that agent's tags too."""
    turn.record(goal_tags=['reel_studio'])
    other = str(int(turn.pid) + 1)
    seen = turn.post(prompt_id=int(other), agentic_execute=True,
                     agentic_plan={'matched_agent_id': turn.pid},
                     model_config=CLOUD_TIER, speculative=True)
    assert seen['where'] == 'chat_agent'
    assert seen['tier'] is None


def test_a_local_only_turn_on_a_node_whose_llm_is_an_api_says_so_once(turn, hie, monkeypatch,
                                                                     caplog):
    """Ruling (5), the known limit: a local-only turn loses the body's tier,
    speculation and the revenue lane, and runs on this node's OWN backend.
    When that backend is a public API (llm_stays_on_premises() False) the
    turn's text still leaves the machine.  /chat says so in one warning per
    process and serves the turn; a turn that is not local-only says nothing."""
    import logging
    from core import autogen_config
    monkeypatch.setattr(autogen_config, 'llm_stays_on_premises', lambda: False)
    monkeypatch.setattr(hie.chat, '_local_only_offsite_warned', False, raising=False)
    turn.record()
    hie.app.logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING):
            assert turn.post()['where'] == 'chat_agent'
            assert not [r for r in caplog.records if 'llm_stays_on_premises' in r.getMessage()]
            for _ in range(2):
                seen = turn.post(intelligence_preference='local_only', model_config=HIVE_TIER)
                assert seen['where'] == 'chat_agent' and seen['tier'] is None
    finally:
        hie.app.logger.removeHandler(caplog.handler)
    said = [r for r in caplog.records if 'llm_stays_on_premises' in r.getMessage()]
    assert len(said) == 1 and said[0].levelno == logging.WARNING


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


@pytest.fixture
def on_central(monkeypatch):
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'central')


def test_on_central_a_reel_record_pins_nothing(turn, on_central):
    turn.record(goal_tags=['reel_studio'])
    seen = turn.post(model_config=HIVE_TIER)
    assert seen['where'] == 'chat_agent'
    assert seen['tier'] and seen['tier'][0]['model'] == 'hive-expert-70b', (
        'central refused the body tier for a reel_studio record: %r' % (seen['tier'],))
    assert turn.post(speculative=True)['where'] == 'speculative_dispatcher'


def test_on_central_a_creation_hint_pins_nothing(turn, on_central):
    seen = turn.post(create_agent=True, goal_tags=['reel_studio'],
                     model_config=HIVE_TIER)
    assert seen['where'] == 'gather_info'
    assert seen['tier'] and seen['tier'][0]['model'] == 'hive-expert-70b'
