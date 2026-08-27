"""Proof: a node in the hive hands a newcomer more than a solo node can — the
composed network > a single isolated intelligence.

Not about raw FLOPs; about the thing the agentic scaffolding actually
accumulates and shares: benchmarks (which tools work for which tasks), a recipe
index (trained task patterns for one-shot REUSE), and community-validated
quality heuristics. A node that has federated with peers can bootstrap a joiner
with all of it; a solo node has nothing to give because it never saw anyone
else's learning.

Mechanism only (no LLM): receive_peer_delta on the hive node, then compare what
bootstrap_new_node() yields from a hive node vs a solo node. The security gate
that makes those deltas trustworthy is proven separately (test_ws12,
two_node_collaboration); here we isolate the LEARNING-TRANSFER value under soft
enforcement so the point is the knowledge, not the crypto.

    python tests/standalone/network_beats_solo_proof.py

Exit 0 iff the hive node bootstraps a newcomer with heuristics/benchmarks the
solo node cannot.
"""
import os
import sys
import tempfile
import time

_d = tempfile.mkdtemp(prefix='net_vs_solo_')
os.environ.update(
    NUNBA_DATA_DIR=_d, HEVOLVE_KEY_DIR=os.path.join(_d, 'keys'),
    HEVOLVE_AGENT_DATA=os.path.join(_d, 'agent_data'),
    HEVOLVE_ENFORCEMENT_MODE='soft', HEVOLVE_BASE_URL='http://127.0.0.1:7896')
sys.path.insert(0, r'C:\Users\sathi\PycharmProjects\HARTOS')

from unittest.mock import patch
from integrations.agent_engine.federated_aggregator import FederatedAggregator, DELTA_VERSION


def peer_contribution(node_id, success_rate, latency_ms, tool):
    """A learning delta a real peer would federate: aggregate heuristics +
    a benchmark result, never raw user data."""
    return {
        'version': DELTA_VERSION, 'node_id': node_id, 'timestamp': time.time(),
        'experience_stats': {'total_recorded': 500, 'total_flushed': 470,
                             'flush_rate': 0.94},
        'quality_metrics': {'success_rate': success_rate,
                            'avg_latency_ms': latency_ms},
        'benchmark_results': {tool: {'score': success_rate, 'n': 500}},
    }


def main():
    hive = FederatedAggregator()   # a node that participates in the network
    solo = FederatedAggregator()   # an isolated single node

    # Three peers federate their learning into the hive node. This is the
    # network doing what a single model cannot: pooling many nodes' experience.
    with patch('security.master_key.get_enforcement_mode', return_value='soft'):
        for nid, sr, lat, tool in (
            ('peer-alpha', 0.91, 120, 'web_search'),
            ('peer-bravo', 0.88, 90, 'code_exec'),
            ('peer-charlie', 0.95, 140, 'vision_ground'),
        ):
            ok, msg = hive.receive_peer_delta(peer_contribution(nid, sr, lat, tool))
            print(f'  hive absorbs {nid}: {ok} ({msg})')

    print()
    newcomer_from_hive = hive.bootstrap_new_node('newcomer-1')
    newcomer_from_solo = solo.bootstrap_new_node('newcomer-2')

    h_q = newcomer_from_hive.get('quality_baselines') or {}
    h_b = newcomer_from_hive.get('benchmarks') or {}
    s_q = newcomer_from_solo.get('quality_baselines') or {}
    s_b = newcomer_from_solo.get('benchmarks') or {}

    print('NEWCOMER joining via the HIVE node inherits:')
    print('  quality_baselines:', h_q)
    print('  benchmarks tools :', list(h_b.keys()) if isinstance(h_b, dict) else h_b)
    print('NEWCOMER joining via a SOLO node inherits:')
    print('  quality_baselines:', s_q)
    print('  benchmarks tools :', list(s_b.keys()) if isinstance(s_b, dict) else s_b)
    print()

    hive_gives_more = bool(h_q) and not s_q
    if hive_gives_more:
        print('RESULT: PROVEN — the hive node bootstraps a newcomer with '
              f'{len(h_q)} community-validated heuristic(s) pooled from 3 peers '
              '(success_rate, latency) that the solo node cannot provide. The '
              'composed network hands a joiner accumulated intelligence a single '
              'isolated node does not have.')
        return 0
    print('RESULT: NOT proven —', {'hive_quality': h_q, 'solo_quality': s_q})
    return 1


if __name__ == '__main__':
    sys.exit(main())
