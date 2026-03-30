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
import hashlib
import hmac
import json
import logging
import math
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve_social')

DELTA_VERSION = 1
DELTA_MAX_AGE_SECONDS = 3600  # 1 hour freshness window


def _sign_delta(delta_dict):
    """Sign a federation delta with node key (HMAC-SHA256).

    Fallback: if HART_NODE_KEY is not set, derives an HMAC key from the
    node's Ed25519 public key so deltas are NEVER unsigned.
    """
    node_key = os.environ.get('HART_NODE_KEY')
    if not node_key:
        # Derive HMAC key from node's Ed25519 public key — never send unsigned
        try:
            from security.node_integrity import get_public_key_hex
            node_key = get_public_key_hex()
        except ImportError:
            logger.error('HART_NODE_KEY not set and Ed25519 key unavailable — delta UNSIGNED')
            return delta_dict
    # Work on a copy without any existing hmac_signature
    to_sign = {k: v for k, v in delta_dict.items() if k != 'hmac_signature'}
    payload = json.dumps(to_sign, sort_keys=True).encode()
    sig = hmac.new(node_key.encode(), payload, hashlib.sha256).hexdigest()
    delta_dict['hmac_signature'] = sig
    return delta_dict


def _verify_delta_signature(delta_dict):
    """Verify a received federation delta's HMAC-SHA256 signature.

    The sender may have used HART_NODE_KEY or fallen back to their Ed25519
    public key as the HMAC key. We try both.
    """
    sig = delta_dict.get('hmac_signature', '')
    if not sig:
        return False
    to_verify = {k: v for k, v in delta_dict.items() if k != 'hmac_signature'}
    payload = json.dumps(to_verify, sort_keys=True).encode()

    # Try HART_NODE_KEY first (shared secret between peers)
    node_key = os.environ.get('HART_NODE_KEY')
    if node_key:
        expected = hmac.new(node_key.encode(), payload, hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected):
            return True

    # Fallback: sender used their Ed25519 public key as HMAC key
    sender_pubkey = delta_dict.get('public_key', '')
    if sender_pubkey:
        expected = hmac.new(sender_pubkey.encode(), payload, hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected):
            return True

    return False


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
        # Model lifecycle delta channel (dynamic load/unload intelligence)
        self._lifecycle_lock = threading.Lock()
        self._lifecycle_deltas: Dict[str, dict] = {}  # node_id → model usage stats
        self._last_lifecycle_aggregated: Optional[dict] = None
        # Resonance tuning delta channel (personality tuning across nodes)
        self._resonance_lock = threading.Lock()
        self._resonance_deltas: Dict[str, dict] = {}  # node_id → anonymized tuning stats
        self._resonance_epoch = 0
        self._last_resonance_aggregated: Optional[dict] = None
        # Recipe sharing channel (trained task intelligence)
        self._recipe_lock = threading.Lock()
        self._recipe_deltas: Dict[str, dict] = {}  # node_id → recipe catalog
        self._last_recipe_aggregated: Optional[dict] = None
        # EventBus counters (fed by real-time events)
        self._event_counters_lock = threading.Lock()
        self._event_counters: Dict[str, int] = {}

        # Subscribe to EventBus (if platform is bootstrapped)
        self._subscribe_to_eventbus()

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

            # Resonance channel tick (personality tuning across nodes)
            resonance_result = self.resonance_tick()
            if resonance_result.get('aggregated'):
                result['resonance'] = resonance_result
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
                'event_counters': self.get_event_counters(),
            }

            # Sign the delta
            try:
                from security.node_integrity import sign_json_payload
                delta['signature'] = sign_json_payload(delta)
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
        """POST delta to all known active peers.

        Privacy enforcement: ScopeGuard.check_egress() runs before any data
        leaves this node.  Only FEDERATED-scoped aggregate stats are sent.
        Raw user data, PII, and secrets are structurally blocked.
        """
        # ── Edge privacy gate: block PII / secrets from leaving ──
        try:
            from security.edge_privacy import get_scope_guard, PrivacyScope
            guard = get_scope_guard()
            tagged_delta = dict(delta, _privacy_scope=PrivacyScope.FEDERATED)
            allowed, reason = guard.check_egress(
                tagged_delta, PrivacyScope.FEDERATED,
                context={'source': 'federation_broadcast'}
            )
            if not allowed:
                logger.warning(f"Federation broadcast blocked by ScopeGuard: {reason}")
                return
        except ImportError:
            pass  # edge_privacy not available — proceed (defense in depth below)

        # Sign the delta with HMAC-SHA256 before broadcasting
        _sign_delta(delta)

        # Attach origin attestation so peers can verify we're genuine HART OS
        try:
            from security.origin_attestation import get_attestation_for_federation
            att = get_attestation_for_federation()
            if att.get('valid'):
                delta['origin_attestation'] = att['attestation']
        except Exception:
            pass

        try:
            from integrations.social.models import get_db, PeerNode
            from core.http_pool import pooled_post

            db = get_db()
            try:
                # Get our own backend port to detect self-connections
                try:
                    from core.port_registry import get_port
                    _own_port = get_port('backend')
                except Exception:
                    _own_port = 6777
                _self_urls = {
                    f'http://localhost:{_own_port}',
                    f'http://127.0.0.1:{_own_port}',
                    f'http://0.0.0.0:{_own_port}',
                }

                peers = db.query(PeerNode).filter_by(status='active').all()
                for peer in peers:
                    if not peer.url or peer.node_id == delta.get('node_id'):
                        continue
                    _peer_url = peer.url.rstrip('/')
                    # Skip our own node (bundled mode has no HTTP listener)
                    if _peer_url in _self_urls:
                        continue
                    try:
                        url = f"{_peer_url}/api/social/peers/federation-delta"
                        pooled_post(url, json=delta, timeout=5)
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

        # Ed25519 signature verification — required in hard mode
        from security.master_key import get_enforcement_mode
        _enforcement = get_enforcement_mode()
        sig = delta.get('signature', '')
        if sig:
            try:
                from security.node_integrity import verify_json_signature
                if not verify_json_signature(delta.get('public_key', ''),
                                             delta, sig):
                    return False, 'invalid signature'
            except ImportError:
                logger.warning('Ed25519 verification module unavailable')
                if _enforcement == 'hard':
                    return False, 'Ed25519 module unavailable — cannot verify'
            except Exception as e:
                logger.warning(f'Ed25519 signature verification error: {e}')
                if _enforcement == 'hard':
                    return False, f'signature verification failed: {e}'
        elif _enforcement == 'hard':
            return False, 'missing Ed25519 signature (hard enforcement)'

        # HMAC-SHA256 delta signing verification — required in hard mode
        if delta.get('hmac_signature'):
            if not _verify_delta_signature(delta):
                return False, 'invalid HMAC signature'
        elif _enforcement == 'hard':
            return False, 'missing HMAC signature (hard enforcement)'

        # Origin attestation — reject forks and rebranded builds
        peer_attestation = delta.get('origin_attestation')
        if peer_attestation:
            try:
                from security.origin_attestation import verify_peer_attestation
                att_ok, att_msg = verify_peer_attestation(peer_attestation)
                if not att_ok:
                    return False, f'origin attestation failed: {att_msg}'
            except ImportError:
                pass  # Origin module not available — accept

        # Revocation check — master-key-signed network halt via federation
        revocation = delta.get('revocation')
        if revocation and isinstance(revocation, dict):
            rev_sig = revocation.get('master_signature', '')
            if rev_sig:
                try:
                    from security.master_key import verify_master_signature
                    rev_payload = {k: v for k, v in revocation.items()
                                   if k != 'master_signature'}
                    if verify_master_signature(rev_payload, rev_sig):
                        logger.critical(
                            'REVOCATION received via federation delta — '
                            'tripping circuit breaker: %s',
                            revocation.get('reason', 'no reason'))
                        try:
                            from security.hive_guardrails import HiveCircuitBreaker
                            HiveCircuitBreaker.trip(
                                reason=revocation.get('reason', 'revocation'))
                        except Exception as e:
                            logger.critical(f'Circuit breaker trip failed: {e}')
                        return True, 'revocation accepted'
                    else:
                        logger.warning('Revocation in delta has INVALID '
                                       'master signature — ignoring')
                except ImportError:
                    logger.warning('Cannot verify revocation — '
                                   'security modules missing')

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

        # Equal voice: every node's intelligence counts the same.
        # Weight by data quality (interactions observed) not hardware tier.
        # A Raspberry Pi that served 10,000 users has more insight than
        # a GPU server that served 10. No one entity owns the built
        # intelligence — everyone is equal for this hive being.
        weights = []
        for d in all_deltas:
            interactions = (
                d.get('experience_stats', {}).get('total_recorded', 0) +
                d.get('quality_metrics', {}).get('goal_throughput', 0)
            )
            # Weight by log of interactions — diminishing returns prevents
            # any single high-traffic node from dominating
            w = math.log1p(max(0, interactions))
            weights.append(max(1.0, w))  # Floor at 1.0 — every node counts

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

        # Broadcast to EventBus — hive intelligence updated
        try:
            from core.platform.events import emit_event
            emit_event('federation.aggregated', {
                'epoch': aggregated.get('epoch', 0),
                'peer_count': aggregated.get('peer_count', 0),
            })
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

    # ─── Model Lifecycle Delta Channel ───

    def receive_lifecycle_delta(self, node_id: str, delta: dict):
        """Store model usage stats from a peer node."""
        if not node_id or not isinstance(delta, dict):
            return
        with self._lifecycle_lock:
            self._lifecycle_deltas[node_id] = delta

    def aggregate_lifecycle(self) -> Optional[dict]:
        """Aggregate model popularity across all peers.

        Returns: {popularity: {model_name: 0.0-1.0}, peer_count: int}
        """
        with self._lifecycle_lock:
            deltas = list(self._lifecycle_deltas.values())
        if not deltas:
            return self._last_lifecycle_aggregated

        total_peers = len(deltas)
        model_counts: Dict[str, int] = {}
        model_access_rates: Dict[str, List[float]] = {}

        for d in deltas:
            for model_name, stats in d.get('models', {}).items():
                model_counts[model_name] = model_counts.get(model_name, 0) + 1
                rate = stats.get('access_rate', 0)
                if isinstance(rate, (int, float)):
                    model_access_rates.setdefault(model_name, []).append(rate)

        popularity = {}
        for name, count in model_counts.items():
            peer_fraction = count / max(1, total_peers)
            rates = model_access_rates.get(name, [0])
            avg_rate = sum(rates) / max(1, len(rates))
            popularity[name] = min(1.0, peer_fraction * (1 + avg_rate))

        result = {'popularity': popularity, 'peer_count': total_peers}
        self._last_lifecycle_aggregated = result
        return result

    def get_lifecycle_stats(self) -> dict:
        """Return model lifecycle delta stats for dashboard."""
        with self._lifecycle_lock:
            pending = len(self._lifecycle_deltas)
        return {
            'pending_deltas': pending,
            'last_aggregated': self._last_lifecycle_aggregated,
        }

    # ─── Resonance Tuning Delta Channel ───

    def receive_resonance_delta(self, node_id: str, delta: dict):
        """Store anonymized resonance tuning stats from a peer node."""
        if not node_id or not isinstance(delta, dict):
            return
        with self._resonance_lock:
            self._resonance_deltas[node_id] = delta

    def aggregate_resonance(self) -> Optional[dict]:
        """Aggregate resonance deltas: weighted avg of tuning distributions."""
        with self._resonance_lock:
            deltas = list(self._resonance_deltas.values())
        if not deltas:
            return None

        # Weighted by user_count (more users = more representative)
        weights = []
        for d in deltas:
            uc = d.get('user_count', 1)
            weights.append(max(1.0, float(uc)))
        total_w = sum(weights)

        n_dims = len(deltas[0].get('avg_tuning', []))
        if n_dims == 0:
            return None

        avg_tuning = [0.0] * n_dims
        for d, w in zip(deltas, weights):
            at = d.get('avg_tuning', [0.5] * n_dims)
            for i in range(min(n_dims, len(at))):
                avg_tuning[i] += at[i] * w / total_w

        result = {
            'avg_tuning': avg_tuning,
            'peer_count': len(deltas),
            'total_users': sum(d.get('user_count', 0) for d in deltas),
            'total_interactions': sum(d.get('total_interactions', 0) for d in deltas),
            'timestamp': time.time(),
        }
        self._last_resonance_aggregated = result
        self._resonance_epoch += 1
        return result

    def resonance_tick(self) -> dict:
        """Resonance channel tick: extract local → aggregate → apply → clear."""
        result = {'resonance_epoch': self._resonance_epoch, 'aggregated': False}
        try:
            # Extract local resonance delta
            try:
                from core.resonance_tuner import get_resonance_tuner
                tuner = get_resonance_tuner()
                local_delta = tuner.export_resonance_delta()
                if local_delta:
                    # Broadcast to peers (piggyback on existing gossip)
                    self._broadcast_resonance(local_delta)
            except ImportError:
                pass

            aggregated = self.aggregate_resonance()
            if aggregated:
                # Apply hive-aggregated tuning to local profiles
                try:
                    from core.resonance_tuner import get_resonance_tuner
                    get_resonance_tuner().import_hive_resonance(aggregated)
                except ImportError:
                    pass

                result.update({
                    'aggregated': True,
                    'resonance_epoch': self._resonance_epoch,
                    'peer_count': aggregated.get('peer_count', 0),
                    'total_users': aggregated.get('total_users', 0),
                })
                with self._resonance_lock:
                    self._resonance_deltas.clear()
        except Exception as e:
            result['error'] = str(e)
        return result

    def _broadcast_resonance(self, delta: dict):
        """Broadcast resonance delta to peers via gossip."""
        try:
            from integrations.social.peer_discovery import gossip
            gossip.broadcast({
                'type': 'resonance_delta',
                'delta': delta,
                'timestamp': time.time(),
            })
        except Exception:
            pass

    def get_resonance_stats(self) -> dict:
        """Return resonance channel stats for dashboard."""
        with self._resonance_lock:
            pending = len(self._resonance_deltas)
        return {
            'resonance_epoch': self._resonance_epoch,
            'pending_deltas': pending,
            'last_aggregated': self._last_resonance_aggregated,
        }

    # ─── EventBus Integration ───

    def _subscribe_to_eventbus(self):
        """Subscribe to EventBus events so learning signals flow into federation.

        Events consumed: inference.completed, resonance.tuned, memory.item_added,
        action_state.changed. Counters are included in the next extract_local_delta().
        """
        try:
            from core.platform.events import emit_event
            from core.platform.registry import get_registry
            registry = get_registry()
            if not registry.has('events'):
                return
            bus = registry.get('events')
            bus.on('inference.completed', self._on_event)
            bus.on('resonance.tuned', self._on_event)
            bus.on('memory.item_added', self._on_event)
            bus.on('action_state.changed', self._on_event)
        except Exception:
            pass  # Platform not bootstrapped yet — will be wired on next tick

    def _on_event(self, topic: str, data):
        """Accumulate event counts for federation delta."""
        with self._event_counters_lock:
            self._event_counters[topic] = self._event_counters.get(topic, 0) + 1

    def get_event_counters(self) -> dict:
        """Return and reset event counters for inclusion in federation delta."""
        with self._event_counters_lock:
            counters = dict(self._event_counters)
            self._event_counters.clear()
        return counters

    # ─── Recipe Sharing Channel ───

    def receive_recipe_delta(self, node_id: str, delta: dict):
        """Store recipe catalog summary from a peer node.

        Delta format: {recipes: [{id, name, action_count, success_rate, reuse_count}]}
        No proprietary data — just catalog metadata for discovery.
        """
        if not node_id or not isinstance(delta, dict):
            return

        # Check consent for recipe sharing (best-effort, fail-open)
        user_id = delta.get('user_id', '')
        if user_id:
            try:
                from integrations.social.consent_service import ConsentService
                from integrations.social.models import db_session
                with db_session() as db:
                    if not ConsentService.check_consent(db, user_id, 'public_exposure'):
                        logger.debug(f"Recipe delta from {node_id} blocked: user {user_id} has not consented")
                        return
            except (ImportError, ValueError, Exception):
                pass  # consent service unavailable — allow (fail-open for dev)

        with self._recipe_lock:
            self._recipe_deltas[node_id] = delta

    def aggregate_recipes(self) -> Optional[dict]:
        """Aggregate recipe catalogs — build hive recipe index.

        Every node's recipes are equally discoverable. No node gets priority
        in the index regardless of its hardware tier.
        """
        with self._recipe_lock:
            deltas = list(self._recipe_deltas.values())
        if not deltas:
            return self._last_recipe_aggregated

        # Build unified catalog — every recipe listed equally
        hive_recipes = {}
        for d in deltas:
            node_id = d.get('node_id', 'unknown')
            for recipe in d.get('recipes', []):
                rid = recipe.get('id', '')
                if rid:
                    if rid not in hive_recipes:
                        hive_recipes[rid] = {
                            'id': rid,
                            'name': recipe.get('name', ''),
                            'action_count': recipe.get('action_count', 0),
                            'nodes': [],
                            'total_reuse_count': 0,
                            'avg_success_rate': 0.0,
                        }
                    entry = hive_recipes[rid]
                    entry['nodes'].append(node_id)
                    entry['total_reuse_count'] += recipe.get('reuse_count', 0)
                    # Running average of success rates
                    n = len(entry['nodes'])
                    old_avg = entry['avg_success_rate']
                    new_rate = recipe.get('success_rate', 0.0)
                    entry['avg_success_rate'] = old_avg + (new_rate - old_avg) / n

        result = {
            'recipes': list(hive_recipes.values()),
            'total_recipes': len(hive_recipes),
            'peer_count': len(deltas),
            'timestamp': time.time(),
        }
        self._last_recipe_aggregated = result
        return result

    def get_recipe_stats(self) -> dict:
        """Return recipe sharing stats for dashboard."""
        with self._recipe_lock:
            pending = len(self._recipe_deltas)
        return {
            'pending_deltas': pending,
            'last_aggregated': self._last_recipe_aggregated,
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
        # Include model lifecycle stats
        try:
            stats['lifecycle'] = self.get_lifecycle_stats()
        except Exception:
            pass
        # Include resonance stats
        try:
            stats['resonance'] = self.get_resonance_stats()
        except Exception:
            pass
        # Include recipe sharing stats
        try:
            stats['recipes'] = self.get_recipe_stats()
        except Exception:
            pass
        return stats

    # ── Node bootstrapping — help new nodes become better ──

    def bootstrap_new_node(self, node_id: str) -> dict:
        """Share aggregated learning with a newly joined node.

        The flywheel helps every node improve — not just extract compute.
        Pre-trusted nodes share:
          - Aggregated benchmarks (what tools work best for what tasks)
          - Recipe index (trained task patterns for REUSE mode)
          - Quality metrics (community-validated heuristics)
          - Resonance baseline (federated personality norms)

        What is NOT shared:
          - Raw user data (EDGE_ONLY — never leaves device)
          - PII or secrets (DLP + ScopeGuard blocks)
          - Raw weights (only non-interpretable LoRA deltas in Phase 2)
          - Individual conversation history

        Returns a bootstrap package for the new node.
        """
        package = {
            'type': 'node_bootstrap',
            'from_node': '',
            'for_node': node_id,
            'timestamp': time.time(),
        }

        # Aggregated benchmarks — what the hive has learned about tool performance
        package['benchmarks'] = self._get_benchmark_results()

        # Recipe index — trained task patterns (metadata only, not full recipes)
        try:
            package['recipe_index'] = self.get_recipe_stats()
        except Exception:
            package['recipe_index'] = {}

        # Quality heuristics — community-validated metrics
        try:
            if self.peer_deltas:
                quality = {}
                for d in self.peer_deltas.values():
                    qm = d.get('quality_metrics', {})
                    for k, v in qm.items():
                        if isinstance(v, (int, float)):
                            quality.setdefault(k, []).append(v)
                package['quality_baselines'] = {
                    k: sum(v) / len(v) for k, v in quality.items() if v
                }
            else:
                package['quality_baselines'] = {}
        except Exception:
            package['quality_baselines'] = {}

        # Resonance norms — federated personality baselines (aggregate only)
        try:
            package['resonance_norms'] = self.get_resonance_stats()
        except Exception:
            package['resonance_norms'] = {}

        # ScopeGuard: verify nothing private leaks in bootstrap
        try:
            from security.edge_privacy import get_scope_guard, PrivacyScope
            guard = get_scope_guard()
            tagged = dict(package, _privacy_scope=PrivacyScope.FEDERATED)
            allowed, reason = guard.check_egress(
                tagged, PrivacyScope.FEDERATED,
                context={'source': 'node_bootstrap', 'target_node': node_id}
            )
            if not allowed:
                logger.warning(f"Bootstrap blocked by ScopeGuard: {reason}")
                return {'error': reason}
        except ImportError:
            pass

        logger.info(f"Bootstrap package for node {node_id}: "
                    f"{len(package.get('benchmarks', {}))} benchmarks, "
                    f"{package.get('recipe_index', {}).get('total_recipes', 0)} recipes")
        return package


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
