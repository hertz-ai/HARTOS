"""
Unified Agent Goal Engine - Federated Learning Delta Aggregation

Periodic aggregation of learning metrics across HART nodes via gossip.
Complementary to HiveMind's inference-time tensor fusion — this handles
training-time metric synchronization.

Lifecycle (driven by AgentDaemon._tick every 2nd tick):
  1. extract_local_delta()  — pull metrics from WorldModelBridge
  2. broadcast_delta()      — sign + POST to peers
  3. receive_peer_delta()   — called by Flask endpoint
  4. aggregate()            — weighted FedAvg on metrics
  5. apply_aggregated()     — store for dashboard + benchmark consumption
  6. track_convergence()    — variance-based convergence score
"""
import logging
import math
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve_social')

DELTA_VERSION = 1
DELTA_MAX_AGE_SECONDS = 3600  # 1 hour freshness window


class FederatedAggregator:
    """Periodic federated learning delta aggregation via gossip.

    Singleton via get_federated_aggregator(). tick() is called by AgentDaemon.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._peer_deltas: Dict[str, dict] = {}  # node_id → latest delta
        self._local_delta: Optional[dict] = None
        self._epoch = 0
        self._convergence_history: List[float] = []
        self._last_aggregated: Optional[dict] = None
        # Embedding delta channel (Phase 1 gradient sync)
        self._embedding_lock = threading.Lock()
        self._embedding_deltas: Dict[str, dict] = {}  # node_id → compressed delta
        self._embedding_epoch = 0
        self._last_embedding_aggregated: Optional[dict] = None

    def tick(self) -> dict:
        """Full cycle: extract → broadcast → aggregate → apply → track."""
        result = {'epoch': self._epoch, 'aggregated': False}
        try:
            self._local_delta = self.extract_local_delta()
            if self._local_delta:
                self.broadcast_delta(self._local_delta)

            aggregated = self.aggregate()
            if aggregated:
                self.apply_aggregated(aggregated)
                convergence = self.track_convergence()
                self._epoch += 1
                result.update({
                    'aggregated': True,
                    'epoch': self._epoch,
                    'convergence': convergence,
                    'peer_count': len(self._peer_deltas),
                })

            # Embedding channel tick (Phase 1 gradient sync)
            embedding_result = self.embedding_tick()
            if embedding_result.get('aggregated'):
                result['embedding'] = embedding_result
        except Exception as e:
            logger.debug(f"Federation tick error: {e}")
            result['error'] = str(e)
        return result

    def extract_local_delta(self) -> Optional[dict]:
        """Pull learning metrics from WorldModelBridge + HiveMind."""
        try:
            from .world_model_bridge import get_world_model_bridge
            bridge = get_world_model_bridge()
            stats = bridge.get_stats()
            learning_stats = bridge.get_learning_stats()

            # Get node identity for signing
            node_id = ''
            public_key = ''
            try:
                from security.node_integrity import get_node_identity
                identity = get_node_identity()
                node_id = identity.get('node_id', '')
                public_key = identity.get('public_key', '')
            except Exception:
                pass

            # Get guardrail hash
            guardrail_hash = ''
            try:
                from security.hive_guardrails import compute_guardrail_hash
                guardrail_hash = compute_guardrail_hash()
            except Exception:
                pass

            # Get capability tier
            capability_tier = 'standard'
            try:
                from security.system_requirements import get_tier_name
                capability_tier = get_tier_name()
            except Exception:
                pass

            # Get contribution score
            contribution_score = 0.0
            try:
                from integrations.social.models import get_db, PeerNode
                db = get_db()
                try:
                    node = db.query(PeerNode).filter_by(
                        node_id=node_id).first()
                    if node:
                        contribution_score = getattr(
                            node, 'contribution_score', 0.0) or 0.0
                finally:
                    db.close()
            except Exception:
                pass

            # Build delta
            hivemind_stats = learning_stats.get('hivemind', {})
            bridge_stats = learning_stats.get('bridge', {})

            delta = {
                'version': DELTA_VERSION,
                'node_id': node_id,
                'public_key': public_key,
                'guardrail_hash': guardrail_hash,
                'timestamp': time.time(),
                'experience_stats': {
                    'total_recorded': bridge_stats.get('total_recorded', 0),
                    'total_flushed': bridge_stats.get('total_flushed', 0),
                    'flush_rate': (
                        bridge_stats.get('total_flushed', 0) /
                        max(1, bridge_stats.get('total_recorded', 1))
                    ),
                },
                'ralt_stats': {
                    'skills_distributed': bridge_stats.get(
                        'total_skills_distributed', 0),
                    'skills_blocked': bridge_stats.get(
                        'total_skills_blocked', 0),
                    'acceptance_rate': (
                        bridge_stats.get('total_skills_distributed', 0) /
                        max(1, bridge_stats.get('total_skills_distributed', 0) +
                            bridge_stats.get('total_skills_blocked', 0))
                    ),
                },
                'hivemind_state': {
                    'agent_count': hivemind_stats.get('agent_count', 0),
                    'total_queries': bridge_stats.get(
                        'total_hivemind_queries', 0),
                    'avg_fusion_latency_ms': hivemind_stats.get(
                        'avg_fusion_latency_ms', 0),
                },
                'quality_metrics': {
                    'correction_density': bridge_stats.get(
                        'total_corrections', 0),
                    'success_rate': 0.0,
                    'goal_throughput': 0,
                },
                'benchmark_results': self._get_benchmark_results(),
                'capability_tier': capability_tier,
                'contribution_score': contribution_score,
            }

            # Sign the delta
            try:
                from security.node_integrity import sign_payload
                delta['signature'] = sign_payload(delta)
            except Exception:
                delta['signature'] = ''

            return delta
        except Exception as e:
            logger.debug(f"Federation extract error: {e}")
            return None

    def _get_benchmark_results(self) -> dict:
        """Pull latest benchmark results if BenchmarkRegistry exists."""
        results = {}
        try:
            from .benchmark_registry import get_benchmark_registry
            registry = get_benchmark_registry()
            results = registry.get_latest_results()
        except Exception:
            pass

        # Include coding agent benchmarks for hive tool routing intelligence
        try:
            from integrations.coding_agent.benchmark_tracker import get_benchmark_tracker
            coding_delta = get_benchmark_tracker().export_learning_delta()
            if coding_delta:
                results['coding_benchmarks'] = coding_delta.get('coding_benchmarks', {})
        except Exception:
            pass

        return results

    def broadcast_delta(self, delta: dict):
        """POST delta to all known active peers."""
        try:
            from integrations.social.models import get_db, PeerNode
            import requests

            db = get_db()
            try:
                peers = db.query(PeerNode).filter_by(status='active').all()
                for peer in peers:
                    if not peer.url or peer.node_id == delta.get('node_id'):
                        continue
                    try:
                        url = f"{peer.url.rstrip('/')}/api/social/peers/federation-delta"
                        requests.post(url, json=delta, timeout=5)
                    except Exception:
                        pass
            finally:
                db.close()
        except Exception as e:
            logger.debug(f"Federation broadcast error: {e}")

    def receive_peer_delta(self, delta: dict) -> Tuple[bool, str]:
        """Validate and store incoming peer delta.

        Validates: schema version, freshness, Ed25519 signature, guardrail hash.
        """
        if not isinstance(delta, dict):
            return False, 'invalid payload'

        if delta.get('version') != DELTA_VERSION:
            return False, f'version mismatch (expected {DELTA_VERSION})'

        # Freshness check
        ts = delta.get('timestamp', 0)
        if abs(time.time() - ts) > DELTA_MAX_AGE_SECONDS:
            return False, 'delta too old or from the future'

        # Guardrail hash verification
        try:
            from security.hive_guardrails import compute_guardrail_hash
            local_hash = compute_guardrail_hash()
            if delta.get('guardrail_hash') and delta['guardrail_hash'] != local_hash:
                return False, 'guardrail hash mismatch'
        except ImportError:
            pass

        # Ed25519 signature verification
        sig = delta.get('signature', '')
        if sig:
            try:
                from security.node_integrity import verify_signed_payload
                if not verify_signed_payload(delta, delta.get('public_key', '')):
                    return False, 'invalid signature'
            except Exception:
                pass  # Verification module unavailable — accept

        node_id = delta.get('node_id', '')
        if not node_id:
            return False, 'missing node_id'

        with self._lock:
            self._peer_deltas[node_id] = delta

        return True, 'accepted'

    def aggregate(self) -> Optional[dict]:
        """Weighted FedAvg across all peer deltas + local delta."""
        with self._lock:
            all_deltas = list(self._peer_deltas.values())
        if self._local_delta:
            all_deltas.append(self._local_delta)

        if len(all_deltas) < 1:
            return None

        # Compute weights: log(1 + contribution_score) * tier_multiplier
        tier_multipliers = {
            'lite': 0.5, 'standard': 1.0, 'compute': 1.5, 'gpu': 2.0,
        }
        weights = []
        for d in all_deltas:
            cs = d.get('contribution_score', 0)
            tier = d.get('capability_tier', 'standard')
            w = math.log1p(max(0, cs)) * tier_multipliers.get(tier, 1.0)
            weights.append(max(0.1, w))  # Floor at 0.1

        total_weight = sum(weights)

        # Weighted average of numeric metrics
        aggregated = {
            'epoch': self._epoch + 1,
            'peer_count': len(all_deltas),
            'timestamp': time.time(),
            'experience_stats': self._weighted_avg_dict(
                [d.get('experience_stats', {}) for d in all_deltas], weights, total_weight),
            'ralt_stats': self._weighted_avg_dict(
                [d.get('ralt_stats', {}) for d in all_deltas], weights, total_weight),
            'hivemind_state': self._weighted_avg_dict(
                [d.get('hivemind_state', {}) for d in all_deltas], weights, total_weight),
            'quality_metrics': self._weighted_avg_dict(
                [d.get('quality_metrics', {}) for d in all_deltas], weights, total_weight),
        }
        return aggregated

    def _weighted_avg_dict(self, dicts: list, weights: list,
                           total_weight: float) -> dict:
        """Compute weighted average of numeric values in list of dicts."""
        result = {}
        if not dicts:
            return result
        keys = set()
        for d in dicts:
            keys.update(d.keys())
        for key in keys:
            vals = []
            ws = []
            for d, w in zip(dicts, weights):
                v = d.get(key)
                if isinstance(v, (int, float)):
                    vals.append(v)
                    ws.append(w)
            if vals:
                result[key] = sum(v * w for v, w in zip(vals, ws)) / max(1e-10, sum(ws))
        return result

    def apply_aggregated(self, aggregated: dict):
        """Store aggregated metrics locally for dashboard + benchmark consumption."""
        self._last_aggregated = aggregated
        try:
            from .world_model_bridge import get_world_model_bridge
            bridge = get_world_model_bridge()
            bridge._federation_aggregated = aggregated
        except Exception:
            pass

        # Feed hive-aggregated coding benchmarks back to local tool router
        coding_data = aggregated.get('benchmark_results', {}).get('coding_benchmarks')
        if coding_data:
            try:
                from integrations.coding_agent.benchmark_tracker import get_benchmark_tracker
                get_benchmark_tracker().import_hive_delta({'coding_benchmarks': coding_data})
            except Exception:
                pass

    def track_convergence(self) -> float:
        """Variance-based convergence score across peer deltas.

        Lower variance = higher convergence. Returns 0.0-1.0.
        """
        with self._lock:
            deltas = list(self._peer_deltas.values())

        if len(deltas) < 2:
            score = 1.0
        else:
            # Use flush_rate variance as proxy
            rates = [
                d.get('experience_stats', {}).get('flush_rate', 0)
                for d in deltas
            ]
            mean_rate = sum(rates) / len(rates)
            variance = sum((r - mean_rate) ** 2 for r in rates) / len(rates)
            score = 1.0 / (1.0 + variance * 100)

        self._convergence_history.append(score)
        if len(self._convergence_history) > 100:
            self._convergence_history = self._convergence_history[-100:]
        return score

    # ─── Embedding Delta Channel (Phase 1 Gradient Sync) ───

    def receive_embedding_delta(self, node_id: str, delta: dict):
        """Store a compressed embedding delta from a peer node."""
        if not node_id or not isinstance(delta, dict):
            return
        with self._embedding_lock:
            self._embedding_deltas[node_id] = delta

    def aggregate_embeddings(self) -> Optional[dict]:
        """Aggregate all embedding deltas using trimmed mean."""
        with self._embedding_lock:
            deltas = list(self._embedding_deltas.values())
        if not deltas:
            return None

        try:
            from .embedding_delta import trimmed_mean_aggregate
            weights = []
            for d in deltas:
                cs = d.get('contribution_score', 1.0)
                weights.append(max(0.01, cs if isinstance(cs, (int, float)) else 1.0))

            aggregated = trimmed_mean_aggregate(deltas, weights=weights)
            self._last_embedding_aggregated = aggregated
            self._embedding_epoch += 1
            return aggregated
        except Exception as e:
            logger.debug(f"Embedding aggregation error: {e}")
            return None

    def embedding_tick(self) -> dict:
        """Embedding channel tick: aggregate + clear stale deltas."""
        result = {'embedding_epoch': self._embedding_epoch, 'aggregated': False}
        try:
            aggregated = self.aggregate_embeddings()
            if aggregated:
                result.update({
                    'aggregated': True,
                    'embedding_epoch': self._embedding_epoch,
                    'peer_count': aggregated.get('peer_count', 0),
                    'outliers_removed': aggregated.get('outliers_removed', 0),
                })
                # Clear processed deltas
                with self._embedding_lock:
                    self._embedding_deltas.clear()
        except Exception as e:
            result['error'] = str(e)
        return result

    def get_embedding_stats(self) -> dict:
        """Return embedding sync stats for dashboard."""
        with self._embedding_lock:
            pending = len(self._embedding_deltas)
        return {
            'embedding_epoch': self._embedding_epoch,
            'pending_deltas': pending,
            'last_aggregated': self._last_embedding_aggregated,
        }

    def get_stats(self) -> dict:
        """Return federation stats for dashboard."""
        with self._lock:
            peer_count = len(self._peer_deltas)
        stats = {
            'epoch': self._epoch,
            'peer_count': peer_count,
            'convergence': self._convergence_history[-1] if self._convergence_history else 0.0,
            'convergence_history': self._convergence_history[-10:],
            'last_aggregated': self._last_aggregated,
        }
        # Include embedding stats
        try:
            stats['embedding'] = self.get_embedding_stats()
        except Exception:
            pass
        return stats


# ─── Singleton ───
_aggregator = None
_aggregator_lock = threading.Lock()


def get_federated_aggregator() -> FederatedAggregator:
    global _aggregator
    if _aggregator is None:
        with _aggregator_lock:
            if _aggregator is None:
                _aggregator = FederatedAggregator()
    return _aggregator
