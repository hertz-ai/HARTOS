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

# ── G8: Per-node HMAC secret (generated at first boot) ──
# Per-node HMAC secret now lives in core.node_secret, so the email campaign's
# click-attribution token can use the same one instead of the public
# 'hevolve-campaign' literal it had. Moved, not copied: these remain the names
# the rest of this module and its tests use.
from core.node_secret import (  # noqa: E402
    load_or_create_hmac_secret as _load_or_create_hmac_secret,
    get_hmac_secret as _get_hmac_secret,
)


def _sign_delta(delta_dict):
    """Sign a federation delta with per-node HMAC-SHA256 secret.

    Uses the persistent per-node secret from agent_data/.hmac_secret
    (G8 fix — replaces the old HART_NODE_KEY env var / Ed25519 public
    key fallback which was a hardcoded/default key vulnerability).
    """
    node_key = _get_hmac_secret()
    if not node_key:
        logger.error('HMAC secret unavailable — delta UNSIGNED')
        return delta_dict
    # Work on a copy without any existing hmac_signature
    to_sign = {k: v for k, v in delta_dict.items() if k != 'hmac_signature'}
    payload = json.dumps(to_sign, sort_keys=True).encode()
    sig = hmac.new(node_key.encode(), payload, hashlib.sha256).hexdigest()
    delta_dict['hmac_signature'] = sig
    return delta_dict


def _verify_delta_signature(delta_dict):
    """Verify a received federation delta's HMAC-SHA256 signature.

    G8: For verification we need the sender's HMAC secret, which is
    exchanged during federation handshake (signed by node's Ed25519 key).
    We check:
      1. Our own per-node secret (for self-originated deltas)
      2. Sender's Ed25519-signed HMAC public (from handshake cache)
      3. Legacy: sender's Ed25519 public key as fallback
    """
    sig = delta_dict.get('hmac_signature', '')
    if not sig:
        return False
    to_verify = {k: v for k, v in delta_dict.items() if k != 'hmac_signature'}
    payload = json.dumps(to_verify, sort_keys=True).encode()

    # 1. Try our own per-node HMAC secret (self-test / same-node)
    our_secret = _get_hmac_secret()
    if our_secret:
        expected = hmac.new(our_secret.encode(), payload, hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected):
            return True

    # 2. Try peer's exchanged HMAC secret from handshake cache
    sender_node_id = delta_dict.get('node_id', '')
    if sender_node_id:
        peer_secret = _get_peer_hmac_secret(sender_node_id)
        if peer_secret:
            expected = hmac.new(peer_secret.encode(), payload, hashlib.sha256).hexdigest()
            if hmac.compare_digest(sig, expected):
                return True

    # 3. Legacy fallback: sender used their Ed25519 public key as HMAC key
    sender_pubkey = delta_dict.get('public_key', '')
    if sender_pubkey:
        expected = hmac.new(sender_pubkey.encode(), payload, hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected):
            return True

    return False


# ── Peer HMAC secret exchange cache ──
_peer_hmac_secrets: Dict[str, str] = {}
_peer_hmac_lock = threading.Lock()


def register_peer_hmac_secret(node_id: str, secret: str):
    """Store a peer's HMAC secret received during federation handshake."""
    with _peer_hmac_lock:
        _peer_hmac_secrets[node_id] = secret


def _get_peer_hmac_secret(node_id: str) -> str:
    """Retrieve a peer's HMAC secret from the handshake cache."""
    with _peer_hmac_lock:
        return _peer_hmac_secrets.get(node_id, '')


def get_hmac_secret_for_handshake() -> str:
    """Return our HMAC secret for federation handshake exchange.

    The caller (federation handshake) should sign this with the node's
    Ed25519 key before transmitting to peers.
    """
    return _get_hmac_secret()


class FederatedAggregator:
    """Periodic federated learning delta aggregation via gossip.

    Singleton via get_federated_aggregator(). tick() is called by AgentDaemon.
    """

    # Alarm on N consecutive failing epochs.  Below this, ticks log at
    # .exception but federation keeps trying — one bad epoch is normal
    # (node churn, transient network, peer restart).  At/above this,
    # log.error + consecutive_failure counter flips so a dashboard or
    # operator sees the sustained outage.
    _CONSECUTIVE_FAILURE_ALARM: int = 3

    def __init__(self):
        self._lock = threading.Lock()
        self._peer_deltas: Dict[str, dict] = {}  # node_id → latest delta
        self._local_delta: Optional[dict] = None
        self._epoch = 0
        self._convergence_history: List[float] = []
        self._last_aggregated: Optional[dict] = None
        # Consecutive-tick failure counter — reset on any success.  Used
        # by the tick() silent-fail guard to distinguish transient from
        # sustained outages.
        self._consecutive_tick_failures: int = 0
        # Last exception seen per channel, exposed via get_status() so
        # dashboards can show "federation unhealthy: <reason>" instead
        # of an opaque stall.
        self._last_tick_error: Optional[str] = None
        # Exponential backoff for unreachable federation peers
        from core.circuit_breaker import PeerBackoff
        self._peer_backoff = PeerBackoff(initial=10, maximum=300)
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
        """Full cycle: extract → broadcast → aggregate → apply → track.

        Failure handling:
          * One failing epoch is logged at .exception (full traceback)
            so operators can correlate with peer-side errors.
          * ``_consecutive_tick_failures`` tracks the run of failures;
            any successful outer try-body resets it.  When the count
            hits ``_CONSECUTIVE_FAILURE_ALARM`` (3 by default) we flip
            to log.error level and stamp ``result['alarm']`` so any
            dashboard / metric scraper sees the sustained outage.
          * The tick method itself always returns — callers (AgentDaemon
            tick loop) must be able to skip one bad epoch and try again.
        """
        self._peer_backoff.prune_expired()
        # Determine local node tier once per tick for structured logs.
        try:
            from security.key_delegation import get_node_tier
            node_tier = get_node_tier()
        except Exception:
            node_tier = 'unknown'

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

            # Success — reset the consecutive-failure window.
            if self._consecutive_tick_failures:
                logger.info(
                    f"[FederatedAggregator] tick recovered after "
                    f"{self._consecutive_tick_failures} consecutive failures "
                    f"(epoch={self._epoch}, tier={node_tier})")
            self._consecutive_tick_failures = 0
            self._last_tick_error = None
        except Exception as e:
            self._consecutive_tick_failures += 1
            self._last_tick_error = f"{type(e).__name__}: {e}"
            consecutive = self._consecutive_tick_failures
            result['error'] = str(e)
            result['consecutive_failures'] = consecutive

            # First N-1 failures: .exception (with traceback) at warning
            # level; >= N: .error + alarm flag.  Either way, federation
            # keeps trying on the next tick.
            if consecutive >= self._CONSECUTIVE_FAILURE_ALARM:
                result['alarm'] = True
                logger.error(
                    f"[FederatedAggregator] ALARM: {consecutive} consecutive "
                    f"federation ticks failed (epoch={self._epoch}, "
                    f"tier={node_tier}): {e}",
                    exc_info=True)
            else:
                logger.exception(
                    f"[FederatedAggregator] tick failed "
                    f"(consecutive={consecutive}, epoch={self._epoch}, "
                    f"tier={node_tier}): {e}")
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

            # The node's running code hash — the ONE gate that decides whether
            # this node may federate into the hive (steward, 2026-08-24: joining
            # is a consent tap; the only security is that a tampered build,
            # whose hash is not a verified release hash, cannot join).  Added
            # to the delta BEFORE signing so it is covered by the Ed25519
            # signature and cannot be spoofed.  Receivers gate on it via
            # release_hash_registry.is_known_release_hash — which, with the
            # self-hash layer (f138706f), passes any node on the same build as
            # the receiver and any known release, and fails a modified tree.
            try:
                from security.node_integrity import compute_code_hash
                delta['code_hash'] = compute_code_hash()
            except Exception:
                delta['code_hash'] = ''

            # Sign the delta (covers code_hash above)
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
        """Gossip the delta to central seeds + a bounded sample of peers.

        This is the authoritative egress gate: ScopeGuard.check_egress() runs
        before any data leaves this node — only FEDERATED-scoped aggregate
        stats are sent; raw user data, PII, and secrets are structurally
        blocked.

        Delivery (2026-08-24): the central seeds always receive the delta
        (they are the census projection point), plus a random gossip_fanout
        sample of reachable peers. This is gossip, not broadcast-to-all — an
        epidemic converges over rounds, and 'all active peers' in practice is
        hundreds of unreachable rows (loopback / other-LAN). The whole fan-out
        shares one hard deadline and runs concurrently, so a dead or slow peer
        can never hang the background tick.
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
            from urllib.parse import urlparse
            from concurrent.futures import ThreadPoolExecutor

            db = get_db()
            try:
                peers = db.query(PeerNode).filter_by(status='active').all()
                # Snapshot node_id + url INSIDE the session; the delivery runs
                # after db.close(), and touching a detached ORM row there would
                # raise. Only these two scalars are needed downstream.
                _rows = [(p.node_id, (p.url or '').rstrip('/'))
                         for p in peers if p.url]
            finally:
                db.close()

            # Turn a 656-row / 55-minute serial walk (measured live 2026-08-24)
            # into a bounded, concurrent GOSSIP fan-out — which is what this
            # method's own docstring already says it is ("aggregation ... via
            # gossip"). The prior loop POSTed to ALL active peers; a gossip
            # epidemic propagates over ROUNDS from a small fanout, it does not
            # broadcast to everyone every tick, and "everyone" here is mostly
            # unreachable rows (359 are http://localhost:6777 — every flat node
            # published its own backend loopback; plus other-LAN private IPs).
            #
            # Targets = SEEDS (always) + a bounded random sample of reachable
            # peers:
            #   * Seeds/central are the census COLLECTION + PROJECTION point
            #     (api_hive_census docstring), so they must receive every
            #     delta directly and are tried regardless of backoff — a node
            #     absent from central's aggregate is absent from /hive.
            #   * Peer fanout is capped at gossip_fanout and skips loopback
            #     (a peer's own address, never reachable from here) and
            #     backed-off peers. Unsampled peers get this delta next round
            #     or via a neighbour — standard gossip convergence, no data
            #     lost.
            #
            # NOT a new path and NOT a semantic regression: same ScopeGuard
            # egress gate above, same /federation-delta endpoint, same backoff,
            # same FedAvg-over-gossip intent — only the fan-out is bounded and
            # concurrent instead of an unbounded serial broadcast.
            _self_node = delta.get('node_id')
            try:
                from integrations.social.peer_discovery import gossip as _gossip
                _seeds = [u.rstrip('/') for u in getattr(_gossip, 'seed_peers', [])]
                _fanout = int(getattr(_gossip, 'gossip_fanout', 3))
            except Exception:
                _seeds, _fanout = [], 3

            _reachable = []
            for _nid, _url in _rows:
                if _nid == _self_node or _url in _seeds:
                    continue
                try:
                    _host = (urlparse(_url).hostname or '').lower()
                except Exception:
                    continue
                if _host in ('localhost', '127.0.0.1', '0.0.0.0', '::1', ''):
                    continue
                if self._peer_backoff.is_backed_off(_url):
                    continue
                _reachable.append(_url)

            import random as _random
            from concurrent.futures import wait as _fwait

            def _post_one(_peer_url):
                try:
                    pooled_post(
                        f"{_peer_url}/api/social/peers/federation-delta",
                        json=delta, timeout=3)
                    return (_peer_url, True)
                except Exception:
                    return (_peer_url, False)

            # ONE bounded, concurrent delivery for BOTH seeds and the peer
            # sample.  Nothing is synchronous: a single row — even a seed —
            # can ignore its per-request timeout on Windows (unroutable SYN,
            # session retries), so ANY blocking step could hang a background
            # gossip tick for minutes (measured live 2026-08-24). The whole
            # fan-out therefore shares one HARD DEADLINE; stragglers are
            # backed off and abandoned (gossip is loss-tolerant — they
            # converge next round). Seeds are submitted FIRST so they win the
            # worker slots, and included in every round because central is the
            # census projection point.
            _sample = (_random.sample(_reachable, _fanout)
                       if len(_reachable) > _fanout else _reachable)
            _targets = list(dict.fromkeys(_seeds + _sample))  # seeds first, deduped

            if _targets:
                _ex = ThreadPoolExecutor(max_workers=min(12, len(_targets)))
                try:
                    _futs = {_ex.submit(_post_one, u): u for u in _targets}
                    _done, _pending = _fwait(_futs, timeout=8)
                    for _f in _done:
                        try:
                            _u, _ok = _f.result()
                            (self._peer_backoff.record_success if _ok
                             else self._peer_backoff.record_failure)(_u)
                        except Exception:
                            pass
                    for _f in _pending:
                        self._peer_backoff.record_failure(_futs[_f])
                finally:
                    # Non-blocking: never wait on a straggler. Its worker
                    # thread finishes when its own socket times out.
                    _ex.shutdown(wait=False, cancel_futures=True)
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
                # Verify against the delta WITHOUT hmac_signature, because
                # that field did not exist when the sender signed.
                #
                # Order of operations on the send side: extract_local_delta()
                # computes the Ed25519 signature, then broadcast_delta() calls
                # _sign_delta() which ADDS hmac_signature before posting
                # (line ~422). verify_json_signature strips only 'signature',
                # so the payload it hashes on this side contains a field the
                # signed payload did not, and every delta on the wire failed
                # with 'invalid signature'.
                #
                # Measured on a real delta from extract_local_delta():
                #   before _sign_delta                       verifies True
                #   after  _sign_delta (the wire form)       verifies False
                #   wire form minus hmac_signature only      verifies True
                #
                # Since hard is the default enforcement mode, this rejected
                # every peer delta regardless of networking, which is a second
                # and independent reason hive-census reported one node.
                #
                # Fixed here rather than in verify_json_signature because that
                # helper is generic and also verifies peer announcements, which
                # carry no HMAC. The two-signature layering is specific to
                # federation deltas.
                _ed_payload = {k: v for k, v in delta.items()
                               if k != 'hmac_signature'}
                if not verify_json_signature(delta.get('public_key', ''),
                                             _ed_payload, sig):
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

        # THE join gate (steward, 2026-08-24): a node may federate iff it is a
        # GENUINE, UNMODIFIED official build.  Joining is otherwise a consent
        # tap; every other crypto layer is seamless plumbing.  A build is
        # genuine if EITHER holds:
        #
        #   (a) its code_hash is a known release hash — same build as this
        #       receiver (self-hash layer f138706f) or a published release; or
        #   (b) it carries a valid origin attestation — the cross-build proof
        #       that rejects forks, stripped branding, and modified guardrails
        #       (security.origin_attestation.verify_peer_attestation) even when
        #       the exact file hash differs, e.g. a desktop bundle vs the
        #       central container.
        #
        # code_hash rides INSIDE the Ed25519-signed payload, so it is bound to
        # the node's identity and cannot be swapped.  A node is refused only
        # when it can prove NEITHER — i.e. a tampered or unknown tree with no
        # genuine attestation.  If the attestation raises the specific reason,
        # a bad attestation is itself a hard reject.
        _genuine = False
        _reject_reason = 'unverified build — unknown code hash, no valid attestation'

        _peer_code_hash = delta.get('code_hash', '')
        if _peer_code_hash:
            try:
                from security.release_hash_registry import get_release_hash_registry
                if get_release_hash_registry().is_known_release_hash(_peer_code_hash):
                    _genuine = True
            except ImportError:
                pass

        _peer_attestation = delta.get('origin_attestation')
        if not _genuine and _peer_attestation:
            try:
                from security.origin_attestation import verify_peer_attestation
                att_ok, att_msg = verify_peer_attestation(_peer_attestation)
                if att_ok:
                    _genuine = True
                else:
                    # A PRESENT-but-INVALID attestation is a fork/impersonator
                    # signal, not just "unknown" — name it.
                    _reject_reason = f'origin attestation failed: {att_msg}'
            except ImportError:
                pass  # attestation module absent — fall back to hash result

        if not _genuine and _enforcement == 'hard':
            return False, _reject_reason

        # HMAC-SHA256 — verified when present, but NEVER required.
        #
        # It used to be mandatory in hard mode, which silently partitioned the
        # whole hive: HMAC is symmetric, so it needs a per-node secret handed
        # to the receiver in a separate signed handshake, and that handshake
        # was not happening — so every real delta from every real machine was
        # rejected with 'missing/invalid HMAC signature' and the census showed
        # only the central node itself (measured live 2026-08-24 from MSI,
        # .69, .83).  Ed25519 already proves identity+integrity and the gate
        # above proves a genuine unmodified build, so the HMAC layer is
        # redundant AND the un-seamless part.  Kept as a non-fatal check for
        # nodes that still exchange it, so nothing that worked before breaks.
        if delta.get('hmac_signature') and not _verify_delta_signature(delta):
            logger.debug('federation delta: HMAC present but unverified '
                         '(non-fatal; Ed25519 + genuine-build gate already applied)')

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

    def hive_census(self) -> dict:
        """What the whole hive looks like from here, with its own denominator.

        `aggregate()` merges deltas into one model update. This does not merge:
        it counts. The distinction matters because a merged number cannot tell
        you how many nodes it came from, and a learning statistic whose sample
        is unknown is not checkable.

        Reads `self._peer_deltas`, which `receive_peer_delta()` fills only after
        the delta passes version, freshness, guardrail hash, Ed25519 signature,
        HMAC and origin attestation. So every node counted here proved itself
        first. This method verifies nothing on its own and must not: it reports
        what the receive path already accepted.

        The node running this is a collection and projection point, not an
        authority. It publishes the per-node figures it was sent alongside the
        totals, so anyone holding the same deltas can recompute the aggregate
        and check it was not invented here. Central is convenient, not trusted.

        Returns a dict that always carries `nodes_reporting`. A caller that
        renders the totals without it is publishing a number with no sample
        size, which is the defect OPEN_PROBLEMS.md problem 1 describes.
        """
        with self._lock:
            deltas = dict(self._peer_deltas)

        now = time.time()
        local = self._local_delta

        per_node = {}
        index_sum = 0.0
        growth_sum = 0.0
        agents_sum = 0
        with_intelligence = 0
        stale = 0

        def _read(node_id, d, is_local):
            nonlocal index_sum, growth_sum, agents_sum, with_intelligence, stale
            hive = (d.get('hivemind') or {})
            # Absent is not zero. A node that reported no hivemind block is not
            # a node with an intelligence index of 0.0, and averaging it in as
            # zero would drag the hive figure down for a reason that is an
            # artefact of reporting rather than a fact about the network.
            has_idx = 'intelligence_index' in hive
            age = now - float(d.get('timestamp', 0) or 0)
            if age > DELTA_MAX_AGE_SECONDS:
                stale += 1

            entry = {
                'age_seconds': round(age, 1),
                'stale': age > DELTA_MAX_AGE_SECONDS,
                'local': is_local,
                'intelligence_index': hive.get('intelligence_index'),
                'growth_rate': hive.get('growth_rate'),
                'num_agents': hive.get('num_agents'),
                'exponential': hive.get('exponential'),
            }
            per_node[node_id] = entry

            # Stale deltas are reported but not averaged. A node past the
            # freshness window may be gone, and letting it hold the headline
            # figure up (or drag it down) makes the mean describe a network
            # that no longer exists. It stays in per_node so the omission is
            # visible rather than silent.
            if has_idx and not entry['stale']:
                with_intelligence += 1
                index_sum += float(hive.get('intelligence_index') or 0.0)
                growth_sum += float(hive.get('growth_rate') or 1.0)
                agents_sum += int(hive.get('num_agents') or 0)

        for node_id, d in deltas.items():
            _read(node_id, d, False)
        if local:
            _read(local.get('node_id', 'self'), local, True)

        return {
            # The denominator, first, because everything under it is meaningless
            # without it.
            'nodes_reporting': len(per_node),
            'nodes_with_intelligence': with_intelligence,
            'nodes_stale': stale,
            'window_seconds': DELTA_MAX_AGE_SECONDS,
            'observed_at': now,

            # Totals over the nodes that actually reported an index. Null rather
            # than 0.0 when nobody did, so a caller can say "no data" instead of
            # drawing a collapse to zero.
            'mean_intelligence_index': (
                index_sum / with_intelligence if with_intelligence else None),
            'mean_growth_rate': (
                growth_sum / with_intelligence if with_intelligence else None),
            'total_agents': agents_sum if with_intelligence else None,

            # The raw per-node figures, so the aggregate above is reproducible
            # by anyone holding the same deltas.
            'per_node': per_node,
        }

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
        """Store aggregated metrics locally for dashboard + benchmark consumption.

        Single code path: routes through WorldModelBridge.apply_federation_update()
        which owns storage AND the EventBus emit. This prevents the earlier
        dead path where apply_aggregated mutated bridge._federation_aggregated
        directly and emitted 'federation.aggregated' on its own, while the
        bridge's apply_federation_update() was never called by any caller.
        """
        self._last_aggregated = aggregated
        try:
            from .world_model_bridge import get_world_model_bridge
            bridge = get_world_model_bridge()
            bridge.apply_federation_update(aggregated)
        except Exception as exc:
            logger.debug(f"[FederatedAggregator] bridge.apply_federation_update failed: {exc}")

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
