"""Recipe CAPABILITY MESH: the PROACTIVE advert layer over the reactive
per-goal peer recipe pull (integrations/google_a2a/peer_reuse).

Behavioural, two-node in-process: node A BANKS an exportable recipe and
gossip-announces a 'recipe_available' advert; node B ingests that exact
advert through the REAL discovery.py '/api/social/peers/broadcast' branch
and caches it; the daemon consult then pulls from the advertised peer
WITHOUT the O(peers) discovery sweep. The only mock is the network/gossip
boundary (gossip.broadcast, pooled pull, admitted_peers). Every asserted
symbol (announce_recipe_available, on_recipe_available_advert,
consume_advert, export_allowed, _advert_cache, the discovery blueprint,
create_recipe._announce_flow_recipe) is the real deployed code.

Covers the directive's five legs:
  (a) announce fires a gossip broadcast whose payload names the right
      slug/semantic_class; node B's discovery receiver caches {peer,slug}
  (b) advert hit -> consume pulls the advertised peer, sweep NOT called
  (c) export_allowed False -> the bank gate advertises NOTHING (private
      recipe never leaves the node); exportable counterpart DOES advertise
  (d) advert from a non-admitted peer -> rejected, cache untouched
  (e) empty/stale cache -> consume falls through to the reactive floor
"""
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.google_a2a import peer_reuse  # noqa: E402
from integrations.social import peer_discovery  # noqa: E402
from integrations.social.discovery import discovery_bp  # noqa: E402

PEER_PID = '77777777777'
PEER_URL = 'http://node-a:6777'
LOCAL_PID = '88888888888'
GOAL_SLUG = 'bootstrap_growth_analytics'
AGENT_ID = f'{PEER_PID}_0'
# Producer + consumer derive this SAME key from the goal identity.
SEMANTIC_KEY = 'analytics/bootstrap-growth-analytics'

A_GOAL_MAP = {PEER_PID: {
    'goal_id': 'a-goal-uuid',
    'goal_slug': GOAL_SLUG,
    'goal_type': 'analytics',
    'goal_title': 'Growth Analytics',
}}


def _identity(**over):
    base = {
        'goal_id': 'b-goal-uuid',
        'goal_slug': GOAL_SLUG,
        'goal_type': 'analytics',
        'goal_title': 'Growth Analytics',
        'goal_description': 'Track growth metrics for the platform.',
        'owner_id': 'user-b',
    }
    base.update(over)
    return base


def _write_bundle(prompts_dir, pid):
    """A minimal REAL banked recipe bundle so build_envelope produces a
    genuine checksum for the advert."""
    import json
    pdef = {'status': 'completed', 'name': 'Growth Metrics Analyst',
            'prompt_id': pid, 'broadcast_agent': False}
    recipe = {'status': 'completed', 'actions': [
        {'status': 'done', 'action': 'Collect metrics', 'action_id': 1,
         'recipe': [{'steps': 'Query metrics service'}]}]}
    for fname, obj in ((f'{pid}.json', pdef),
                       (f'{pid}_0_recipe.json', recipe)):
        with open(os.path.join(prompts_dir, fname), 'w',
                  encoding='utf-8', newline='') as f:
            f.write(json.dumps(obj))


def _make_advert(source_node='node-A', source_api_url=PEER_URL,
                 semantic_class='analytics', slug='bootstrap-growth-analytics'):
    return {
        'type': 'recipe_available',
        'capability': {
            'semantic_class': semantic_class, 'slug': slug,
            'goal_type': 'analytics', 'title': 'Growth Analytics',
            'agent_id': AGENT_ID, 'prompt_id': PEER_PID, 'flow_id': 0,
        },
        'source_node': source_node,
        'source_api_url': source_api_url,
        'checksum': 'deadbeef',
        'timestamp': time.time(),
    }


@pytest.fixture()
def clean_state():
    """Isolate the module-level advert cache + cooldown between tests."""
    with patch.object(peer_reuse, '_advert_cache', {}), \
            patch.object(peer_reuse, '_attempt_cooldown', {}), \
            patch.object(peer_reuse, '_goal_map_cache', (0.0, {})):
        yield


# ─── (a) announce broadcasts + receiver caches ───────────────────────

def test_announce_broadcasts_advert_and_receiver_caches(tmp_path, clean_state):
    a_dir = str(tmp_path / 'node_a_prompts')
    os.makedirs(a_dir)
    _write_bundle(a_dir, PEER_PID)

    captured = {}

    def spy_broadcast(message, targets=None):
        captured['msg'] = message
        return 1

    # Node A: bank an exportable recipe, then announce.
    with patch.object(peer_reuse, 'local_goal_identity_by_prompt_id',
                      lambda: dict(A_GOAL_MAP)), \
            patch('core.platform_paths.get_recipe_prompts_dir',
                  lambda: a_dir), \
            patch.object(peer_discovery.gossip, 'broadcast', spy_broadcast), \
            patch.object(peer_discovery.gossip, 'node_id', 'node-A'), \
            patch.object(peer_discovery.gossip, 'base_url', PEER_URL):
        assert peer_reuse.export_allowed(PEER_PID) is True
        assert peer_reuse.announce_recipe_available(PEER_PID, 0) is True

    advert = captured['msg']
    assert advert['type'] == 'recipe_available'
    cap = advert['capability']
    assert cap['slug'] == 'bootstrap-growth-analytics'
    assert cap['semantic_class'] == 'analytics'
    assert cap['agent_id'] == AGENT_ID
    assert cap['prompt_id'] == PEER_PID
    assert cap['flow_id'] == 0
    assert advert['source_node'] == 'node-A'
    assert advert['source_api_url'] == PEER_URL
    assert advert['checksum'], 'build_envelope should yield a real checksum'

    # Node B: feed the EXACT broadcast into the real discovery receiver.
    app = Flask('node_b')
    app.register_blueprint(discovery_bp)
    client = app.test_client()
    with patch.object(peer_reuse, 'admitted_peers',
                      lambda *a, **k: [{'node_id': 'node-A',
                                        'url': PEER_URL}]), \
            patch.object(peer_discovery.gossip, 'node_id', 'node-B'):
        resp = client.post('/api/social/peers/broadcast', json=advert)

    assert resp.status_code == 200
    body = resp.get_json()
    assert body['success'] is True
    assert body['cached'] == SEMANTIC_KEY

    entry = peer_reuse._advert_cache.get(SEMANTIC_KEY)
    assert entry is not None
    assert entry['peer_url'] == PEER_URL
    assert entry['agent_id'] == AGENT_ID
    assert entry['prompt_id'] == PEER_PID


# ─── (b) advert hit -> pull without discovery sweep ──────────────────

def test_consume_pulls_from_advert_without_discovery_sweep(clean_state):
    peer_reuse._advert_cache[SEMANTIC_KEY] = {
        'peer_url': PEER_URL, 'agent_id': AGENT_ID, 'prompt_id': PEER_PID,
        'flow_id': 0, 'checksum': 'x', 'ts': time.time(),
    }
    sweep_calls = []
    pull_calls = []

    def spy_sweep(*a, **k):
        sweep_calls.append((a, k))
        return None

    def spy_pull(peer_url, agent_id, local_prompt_id=None, timeout=None):
        pull_calls.append((peer_url, agent_id, local_prompt_id, timeout))
        return True

    with patch.object(peer_reuse, 'discover_peer_agent', spy_sweep), \
            patch.object(peer_reuse, 'pull_recipe', spy_pull):
        verdict = peer_reuse.consume_advert(_identity(), LOCAL_PID)

    assert verdict == 'pulled'
    assert sweep_calls == [], 'discovery sweep must be skipped on advert hit'
    assert len(pull_calls) == 1
    peer_url, agent_id, local_pid, timeout = pull_calls[0]
    assert peer_url == PEER_URL
    assert agent_id == AGENT_ID
    assert local_pid == str(LOCAL_PID)
    assert timeout is not None, 'pull must carry an explicit timeout'


# ─── (c) private recipe never advertised on bank ─────────────────────

def test_private_recipe_is_not_advertised_on_bank(tmp_path, clean_state):
    """The REAL bank gate (create_recipe._announce_flow_recipe) checks
    export_allowed before announcing. A private recipe (not goal-linked,
    no broadcast_agent opt-in) -> export_allowed False -> NO broadcast."""
    import create_recipe

    broadcasts = []

    def spy_broadcast(message, targets=None):
        broadcasts.append(message)
        return 1

    # Private recipe: real export_allowed returns False.
    with patch.object(peer_reuse, 'local_goal_identity_by_prompt_id',
                      lambda: {}), \
            patch.object(peer_reuse, '_broadcast_opt_in',
                         lambda *a, **k: False), \
            patch.object(peer_discovery.gossip, 'broadcast', spy_broadcast):
        assert peer_reuse.export_allowed(PEER_PID) is False
        emitted = create_recipe._announce_flow_recipe(PEER_PID, 0)
    assert emitted is False
    assert broadcasts == [], 'a private recipe must never be advertised'

    # Exportable counterpart: the same gate lets it through and it DOES
    # advertise (proves the gate is bidirectional, not always-False).
    with patch.object(peer_reuse, 'local_goal_identity_by_prompt_id',
                      lambda: dict(A_GOAL_MAP)), \
            patch('core.platform_paths.get_recipe_prompts_dir',
                  lambda: str(tmp_path)), \
            patch.object(peer_discovery.gossip, 'broadcast', spy_broadcast), \
            patch.object(peer_discovery.gossip, 'node_id', 'node-A'), \
            patch.object(peer_discovery.gossip, 'base_url', PEER_URL):
        emitted2 = create_recipe._announce_flow_recipe(PEER_PID, 0)
    assert emitted2 is True
    assert len(broadcasts) == 1
    assert broadcasts[0]['capability']['slug'] == 'bootstrap-growth-analytics'


# ─── (d) advert from a non-admitted peer is rejected ─────────────────

def test_advert_from_non_admitted_peer_rejected(clean_state):
    advert = _make_advert(source_node='rogue-node',
                          source_api_url='http://rogue:6777')
    with patch.object(peer_reuse, 'admitted_peers',
                      lambda *a, **k: [{'node_id': 'node-A',
                                        'url': PEER_URL}]), \
            patch.object(peer_discovery.gossip, 'node_id', 'node-B'):
        result = peer_reuse.on_recipe_available_advert(advert)
    assert result['success'] is False
    assert result['reason'] == 'peer_not_admitted'
    assert peer_reuse._advert_cache == {}, 'rejected advert must not cache'


# ─── (e) empty/stale cache falls through to the reactive floor ───────

def test_empty_and_stale_cache_fall_through_to_reactive(clean_state):
    # Empty cache -> consume returns None so the daemon uses the reactive
    # try_peer_recipe_reuse floor UNCHANGED.
    assert peer_reuse.consume_advert(_identity(), LOCAL_PID) is None

    # Stale entry -> evicted and treated as a miss (no pull attempted).
    peer_reuse._advert_cache[SEMANTIC_KEY] = {
        'peer_url': PEER_URL, 'agent_id': AGENT_ID, 'prompt_id': PEER_PID,
        'flow_id': 0, 'checksum': 'x',
        'ts': time.time() - (peer_reuse._advert_ttl_s() + 60),
    }

    def boom_pull(*a, **k):
        raise AssertionError('stale advert must not trigger a pull')

    with patch.object(peer_reuse, 'pull_recipe', boom_pull):
        assert peer_reuse.consume_advert(_identity(), LOCAL_PID) is None
    assert SEMANTIC_KEY not in peer_reuse._advert_cache, 'stale not evicted'

    # The daemon consult helper mirrors this: no fresh advert -> None, so
    # the daemon proceeds to the reactive _try_peer_recipe_reuse floor.
    from integrations.agent_engine.agent_daemon import _consult_recipe_advert
    goal = SimpleNamespace(
        id='b-goal-uuid', goal_type='analytics', title='Growth Analytics',
        description='Track growth metrics.',
        config_json={'bootstrap_slug': GOAL_SLUG},
        owner_id=None, created_by=None)
    assert _consult_recipe_advert(goal, LOCAL_PID) is None
