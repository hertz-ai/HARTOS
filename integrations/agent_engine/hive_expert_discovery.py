"""Auto-register hive-served expert-tier models into the canonical
``ModelRegistry`` by subscribing to ``peer.capability.*`` gossip on
the platform EventBus.

Design intent
=============

The speculative dispatcher's design has a "hive expert" tier: when a
local draft escalates, the bigger / fine-tuned model that actually
takes the turn should run on a peer compute node (regional or central
deployment), not the same local 4B that served the draft.  Without
that, ``_pick_expert_for_delegate('hive', ...)`` falls back to local
fast and the speculative refine path is just the local 4B refining
its own kind of output — pure waste.

This module closes that gap on the **consumer** side.  When a peer
emits a ``peer.capability.announce`` event (producer side is
``hive_capability_advertiser``, attached at boot), this subscriber:

  1. Validates the peer's trust signature against the master-key
     delegation chain (``security.key_delegation.verify_peer_attestation``).
     Until that API ships, an env-var allowlist
     (``HEVOLVE_HIVE_TRUSTED_PEERS``) is the safe fallback so an
     operator can manually whitelist peers they trust.
  2. Pings the advertised endpoint for reachability and latency
     (re-uses the same HTTP probe pattern ``LlamaConfig`` uses for
     llama-server health — see commit 3f9be3be for the 503-aware
     liveness check).
  3. Registers each ``tier=expert`` model the peer advertises as a
     ``ModelBackend(is_local=False, tier=EXPERT, ...)`` in the
     canonical ``model_registry``.  The dispatcher's existing
     ``get_expert_model()`` then returns the hive-served entry
     automatically — no dispatcher-side change required.
  4. Health-checks each registered peer every ``_HEALTH_CHECK_INTERVAL_S``.
     After ``_HEALTH_CHECK_FAIL_BUDGET`` consecutive failures the peer's
     models get ``ModelRegistry.unregister``'d so the dispatcher never
     dispatches to a dead endpoint.
  5. ``peer.capability.revoke`` events drop the peer's models
     immediately (clean shutdown path).

Producer-side gap
=================

The producer shipped: ``hive_capability_advertiser`` emits the gossip
and ``core/platform/bootstrap.py`` attaches both sides at boot.  It is
opt-in per node though, gated on ``HEVOLVE_HIVE_ADVERTISE=1`` plus
``HEVOLVE_HIVE_PUBLIC_ENDPOINT``, so on a network where nobody has
opted in this subscriber still sits idle: ``on_peer_announce`` is never
called, the registry has no hive entries, and the dispatcher falls
through to local langchain.  Telemetry's ``served_by`` reads
``local_langchain``
100% of the time until peers start announcing, giving you a clean
metric for hive-tier uptake afterwards.

Reuse
=====

Nothing in this file duplicates existing primitives:

  - ``ModelRegistry.register`` / ``unregister`` — canonical registration
  - ``ModelBackend`` — same shape every other backend uses
  - ``ModelTier.EXPERT`` — single source of truth for tier identification
  - ``core.platform.registry.get_registry`` + ``.get('events')`` — same
    EventBus access pattern as the rest of the platform (see
    ``core.platform.events.emit_event``)
  - ``security.key_delegation.verify_peer_attestation`` (when shipped) —
    same trust chain the rest of the security layer uses
  - ``requests`` — already a project dependency; no new transport
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Set

import requests

from integrations.agent_engine.model_registry import (
    ModelBackend,
    ModelRegistry,
    ModelTier,
    model_registry as _default_registry,
)

logger = logging.getLogger('hevolve_social')

# Health-check cadence and fail budget.  Tuned so a transient network
# blip (one missed ping) doesn't drop a peer, but a genuinely dead peer
# is dropped within ~3 minutes (3 misses × 60s).
_HEALTH_CHECK_INTERVAL_S = 60
_HEALTH_CHECK_FAIL_BUDGET = 3
# How long shutdown() waits for the health thread to notice `_stop`. Short on
# purpose: the loop wakes immediately on the event, so this only ever absorbs a
# tick that is mid-ping, and the thread is a daemon so overrunning it is safe.
_SHUTDOWN_JOIN_TIMEOUT_S = 5
_PING_TIMEOUT_S = 2.0

# Minimum verified_baseline a peer's advertised model needs in order to
# be registered.  Below this we treat the advertisement as untrusted
# noise — the peer is free to advertise but the dispatcher must never
# route real turns to a model whose own self-reported baseline is
# below random-chance reasoning quality.
_MIN_VERIFIED_BASELINE = 0.5

# Gossip topics.  Producer side (peer's capability advertiser daemon)
# ships separately; this module just listens.
_TOPIC_ANNOUNCE = 'peer.capability.announce'
_TOPIC_REVOKE = 'peer.capability.revoke'


def _backend_id_for(peer_id: str, model_id: str) -> str:
    """Canonical ID format for a hive-served backend entry.

    Centralised so the announce / revoke / health-check paths all
    speak the same prefix convention.
    """
    return f'hive-{peer_id}-{model_id}'


class HiveExpertDiscovery:
    """Subscribes to ``peer.capability.*`` gossip and keeps the canonical
    ``ModelRegistry`` in sync with reachable hive expert peers.

    Thread-safety
    -------------
    Internal state (``_peer_models``, ``_fail_count``) is guarded by
    ``self._lock``.  ``ModelRegistry.register`` / ``unregister`` carry
    their own lock so cross-thread registration is safe.  The health
    check loop runs on a dedicated DAEMON thread; it holds ``self._lock``
    only while reading the peer list, never while pinging.
    """

    def __init__(self, registry: Optional[ModelRegistry] = None):
        self._registry = registry or _default_registry
        self._lock = threading.Lock()
        self._peer_models: Dict[str, Set[str]] = {}
        self._fail_count: Dict[str, int] = {}
        # The health check is an INFINITE loop, so it runs on a DAEMON THREAD
        # and never on a ThreadPoolExecutor worker.
        #
        # It used to be `pool.submit(self._health_check_loop)` on a 2-worker
        # pool, guarded by `atexit.register(pool.shutdown(wait=False))` — the
        # same guard agent_baseline_service / world_model_bridge /
        # speculative_dispatcher use. That guard CANNOT work here, and the
        # difference is what the loop does, not how the pool is closed:
        #
        #   * shutdown(wait=False) only stops the pool ACCEPTING work. It
        #     cannot interrupt a worker that is already inside a task, and this
        #     task never returns.
        #   * concurrent.futures joins its non-daemon workers from
        #     threading._register_atexit, which runs during threading._shutdown
        #     — BEFORE any atexit callback. Measured on 3.12.10: the atexit
        #     hook never ran at all while the worker ticked on forever.
        #
        # For the other three pools the guard is fine: their tasks are short
        # fire-and-forget, so the worker is back to IDLE in _worker and the
        # joiner's sentinel wakes it. That is exactly the split the CI shard
        # reported — spec_expert_0 and wm_flush_0 idle, 'hive_expert_discovery_0'
        # BLOCKED — before force-exiting the interpreter (task #30).
        #
        # A daemon thread is not joined at interpreter exit, so the process can
        # always leave; `self._stop` still gives shutdown() a clean cooperative
        # stop. tests/unit/test_hive_expert_discovery.py already drove the loop
        # on `threading.Thread(..., daemon=True)` — the test had the right shape
        # and production did not.
        self._health_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._subscribed = False

    # ── Public API ──────────────────────────────────────────────────

    def attach_to_event_bus(self) -> bool:
        """Wire up gossip subscriptions on the platform EventBus.

        Idempotent: returns True on first successful attach, False on
        subsequent calls (already subscribed) or if the bus isn't
        bootstrapped.

        Safe to call from anywhere — degrades to no-op when the
        platform layer hasn't initialised yet.  Bootstrap should call
        this after ``platform.init`` succeeds.
        """
        with self._lock:
            if self._subscribed:
                return False
        try:
            from core.platform.registry import get_registry
            registry = get_registry()
            if not registry.has('events'):
                logger.debug(
                    "HiveExpertDiscovery: EventBus not yet bootstrapped; "
                    "attach_to_event_bus is a no-op until platform.init runs")
                return False
            bus = registry.get('events')
            # Check-and-set, subscribe, and thread start under ONE lock hold.
            # Two invariants live or die on this atomicity:
            #   * two concurrent attaches must not both pass the flag check
            #     (double bus subscription -> every announce handled twice,
            #     plus TWO health loops);
            #   * the _stop.clear() must be serialized with shutdown()'s
            #     _stop.set(), or an attach/shutdown interleaving can erase
            #     the stop and leave a live loop behind a False flag.
            # Holding our lock across bus.on is safe: EventBus.emit copies its
            # listener list under the BUS lock and releases it before calling
            # callbacks, so there is no hold-and-wait cycle with the callbacks
            # that take self._lock. The early flag check above stays as a
            # cheap fast path.
            with self._lock:
                if self._subscribed:
                    return False
                bus.on(_TOPIC_ANNOUNCE, self._on_announce_event)
                bus.on(_TOPIC_REVOKE, self._on_revoke_event)
                self._subscribed = True
                # Daemon thread, never a pool worker (see __init__). Clear the
                # stop event first so an attach AFTER a shutdown starts a live
                # loop instead of one that exits on its first tick.
                self._stop.clear()
                self._health_thread = threading.Thread(
                    target=self._health_check_loop,
                    name='hive_expert_discovery', daemon=True)
                self._health_thread.start()
            logger.info(
                "HiveExpertDiscovery: subscribed to %s + %s, "
                "health check every %ds",
                _TOPIC_ANNOUNCE, _TOPIC_REVOKE, _HEALTH_CHECK_INTERVAL_S)
            return True
        except Exception as e:
            logger.warning(
                "HiveExpertDiscovery: attach_to_event_bus failed (%s) — "
                "hive routing will not activate this session.", e)
            return False

    def shutdown(self) -> None:
        """Stop the health-check loop and unsubscribe from the EventBus.

        Unsubscribe matters for test isolation and hot-reload paths:
        if a new instance gets constructed after a shutdown, the old
        callbacks would otherwise remain bound to the bus and fire into
        a stopped instance.  ``bus.off`` removes them cleanly so the bus
        only ever talks to the live instance.

        Idempotent, and safe to call without a preceding attach.

        Does NOT unregister already-registered hive backends — leave
        them in place for graceful drain.  ``ModelRegistry.unregister``
        is still callable; callers that need a hard reset can iterate.
        """
        # Flag, stop event, and thread handle change together under the lock —
        # mirroring attach, so an attach/shutdown interleaving can never erase
        # the other's stop-event write (the lost-set race). The bus.off I/O
        # and the join happen OUTSIDE: the health loop briefly takes this lock
        # itself, so joining it while holding the lock could deadlock until
        # the join timeout.
        with self._lock:
            was_subscribed = self._subscribed
            self._subscribed = False
            self._stop.set()
            thread, self._health_thread = self._health_thread, None
        # Unsubscribe so a fresh instance constructed after this shutdown is
        # the only thing the bus talks to.
        if was_subscribed:
            try:
                from core.platform.registry import get_registry
                registry = get_registry()
                if registry.has('events'):
                    bus = registry.get('events')
                    bus.off(_TOPIC_ANNOUNCE, self._on_announce_event)
                    bus.off(_TOPIC_REVOKE, self._on_revoke_event)
            except Exception as e:
                logger.debug(
                    "HiveExpertDiscovery: unsubscribe during shutdown "
                    "raised (%s); continuing", e)
        # Join the health thread so a caller (or a test) that shuts down gets a
        # deterministic "it has stopped", not a racing daemon. BOUNDED: if the
        # loop is mid-ping we do not hold the caller for the ping's full
        # timeout, and because the thread is a daemon a missed join can never
        # keep the process alive — the property the pool version lacked.
        if thread is not None and thread.is_alive():
            thread.join(timeout=_SHUTDOWN_JOIN_TIMEOUT_S)
            if thread.is_alive():
                logger.debug(
                    "HiveExpertDiscovery: health thread still running after "
                    "%ss; it is a daemon and will not block exit",
                    _SHUTDOWN_JOIN_TIMEOUT_S)

    # ── EventBus callbacks (bus passes (topic, data)) ───────────────

    def _on_announce_event(self, topic: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        self.on_peer_announce(data)

    def _on_revoke_event(self, topic: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        peer_id = data.get('peer_id') or ''
        if peer_id:
            self._drop_peer(peer_id, reason='revoke')

    # ── Announce handling ──────────────────────────────────────────

    def on_peer_announce(self, msg: Dict[str, Any]) -> int:
        """Process a single ``peer.capability.announce`` payload.

        Expected payload (producer-side schema documented here so the
        producer daemon has a single source of truth to match):

            {
              "peer_id":          str,         # opaque, stable per node
              "endpoint":         str,         # https://node-x.example
              "auth_token":       str,         # bearer for HTTP calls
              "trust_signature":  str,         # signed by master-key chain
              "models": [
                {
                  "model_id":          str,
                  "display_name":      str,
                  "tier":              "expert" | "balanced" | "fast",
                  "verified_baseline": float,  # 0.0-1.0
                  "specialty":         [str],  # optional
                  "capabilities":      {...},  # optional
                },
                ...
              ]
            }

        Returns the number of expert-tier backends registered (or
        re-registered) for this peer after this announce processes.
        Zero means the announce was rejected (trust / reachability /
        no qualifying models) — caller may log; we don't raise.
        """
        peer_id = (msg.get('peer_id') or '').strip()
        endpoint = (msg.get('endpoint') or '').rstrip('/')
        auth_token = msg.get('auth_token') or ''
        if not peer_id or not endpoint:
            logger.debug(
                "HiveExpertDiscovery: announce missing peer_id/endpoint, "
                "ignored")
            return 0

        # Self-echo guard: when THIS node's HiveCapabilityAdvertiser
        # emits an announce, EventBus fans it out locally before
        # crossbar relays it.  Registering the local models as hive
        # backends would create is_local=False duplicates of entries
        # already present as is_local=True — wrong on every count
        # (dispatcher's _dispatch_expert_langchain would treat them as
        # remote and OpenAI-POST them to the local endpoint instead of
        # using the in-process /chat pipeline).
        # The check belongs here, not in the producer, because any
        # consumer can apply it whether or not the local producer is
        # running.
        local_peer = (os.environ.get('HEVOLVE_NODE_ID') or '').strip()
        if local_peer and local_peer.lower() != 'local' and (
                peer_id == local_peer):
            logger.debug(
                "HiveExpertDiscovery: ignoring self-announce from "
                "peer_id=%r (matches HEVOLVE_NODE_ID)", peer_id)
            return 0

        if not self._verify_peer_trust(msg):
            logger.warning(
                "HiveExpertDiscovery: peer %s trust check failed — "
                "refusing to register advertised expert models",
                peer_id)
            return 0

        # Pre-commit reachability probe: don't register entries we
        # can't actually reach right now.  Producer's next announce
        # will give us another chance.
        latency_ms = self._ping_latency(endpoint, auth_token)
        if latency_ms is None:
            logger.info(
                "HiveExpertDiscovery: peer %s unreachable on first probe "
                "(endpoint=%s); will retry on next announce",
                peer_id, endpoint)
            return 0

        # Compute the new set BEFORE touching the registry so a
        # malformed payload doesn't half-drop the peer.
        new_ids: Set[str] = set()
        new_backends: List[ModelBackend] = []
        for model in (msg.get('models') or []):
            if not isinstance(model, dict):
                continue
            if model.get('tier') != 'expert':
                continue
            model_id = (model.get('model_id') or '').strip()
            if not model_id:
                continue
            try:
                baseline = float(model.get('verified_baseline', 0.0))
            except (TypeError, ValueError):
                continue
            if baseline < _MIN_VERIFIED_BASELINE:
                continue

            backend_id = _backend_id_for(peer_id, model_id)
            new_ids.add(backend_id)
            new_backends.append(ModelBackend(
                model_id=backend_id,
                display_name=(
                    'Hive: ' + (model.get('display_name') or model_id)),
                tier=ModelTier.EXPERT,
                config_list_entry={
                    'model': model_id,
                    'api_key': auth_token or 'hive',
                    'base_url': f'{endpoint}/v1',
                    'price': [0, 0],
                    'specialty': list(model.get('specialty') or []),
                },
                avg_latency_ms=latency_ms,
                accuracy_score=baseline,
                cost_per_1k_tokens=0.0,
                is_local=False,
                hardware_dependent=False,
                gpu_tdp_watts=0.0,
            ))

        # Diff against any existing registration for this peer.  Register
        # the new ones first (so the peer never goes to zero models
        # mid-update), then unregister the ones that disappeared.
        with self._lock:
            prev_ids = self._peer_models.get(peer_id, set())
        for backend in new_backends:
            self._registry.register(backend)
        dropped = prev_ids - new_ids
        for backend_id in dropped:
            self._registry.unregister(backend_id)
        with self._lock:
            self._peer_models[peer_id] = new_ids
            self._fail_count[peer_id] = 0

        if new_ids:
            logger.info(
                "HiveExpertDiscovery: peer %s registered %d expert "
                "backend(s), dropped %d, latency=%.0fms",
                peer_id, len(new_ids), len(dropped), latency_ms)
        elif dropped:
            logger.info(
                "HiveExpertDiscovery: peer %s announced no qualifying "
                "experts; dropped %d prior backend(s)",
                peer_id, len(dropped))
        return len(new_ids)

    # ── Health check ───────────────────────────────────────────────

    def _health_check_loop(self) -> None:
        """Background loop — pings every registered peer's endpoint
        every ``_HEALTH_CHECK_INTERVAL_S``.  Drops peers after
        ``_HEALTH_CHECK_FAIL_BUDGET`` consecutive failures.

        Updates each surviving peer's per-backend ``avg_latency_ms``
        from the live ping so the dispatcher's selector reflects
        current network conditions instead of the original-announce
        snapshot.
        """
        while not self._stop.wait(_HEALTH_CHECK_INTERVAL_S):
            with self._lock:
                peers = list(self._peer_models.keys())
            for peer_id in peers:
                if self._stop.is_set():
                    return
                try:
                    self._check_one_peer(peer_id)
                except Exception as e:
                    logger.debug(
                        "HiveExpertDiscovery: health check raised for "
                        "%s: %s", peer_id, e)

    def _check_one_peer(self, peer_id: str) -> None:
        with self._lock:
            backend_ids = list(self._peer_models.get(peer_id, ()))
        if not backend_ids:
            return
        # Backends from one peer share an endpoint — probe via the
        # first registered backend's config_list_entry base_url.
        first = self._registry.get_model(backend_ids[0])
        if first is None:
            # Backend vanished from under us (manual unregister?) — sync
            # our state and move on.
            self._drop_peer(peer_id, reason='backend_missing')
            return
        cfg = first.config_list_entry or {}
        base_url = (cfg.get('base_url') or '').rstrip('/')
        if base_url.endswith('/v1'):
            endpoint = base_url[:-3]
        else:
            endpoint = base_url
        auth = cfg.get('api_key') or ''

        latency_ms = self._ping_latency(endpoint, auth)
        if latency_ms is None:
            with self._lock:
                self._fail_count[peer_id] = (
                    self._fail_count.get(peer_id, 0) + 1)
                fails = self._fail_count[peer_id]
            if fails >= _HEALTH_CHECK_FAIL_BUDGET:
                self._drop_peer(peer_id, reason=f'health_fail x{fails}')
            return

        # Live latency update — feeds the dispatcher's picker so a
        # peer whose ping latency drifts up loses to faster peers.
        with self._lock:
            self._fail_count[peer_id] = 0
        for backend_id in backend_ids:
            self._registry.record_latency(backend_id, latency_ms)

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _ping_latency(endpoint: str, auth_token: str) -> Optional[float]:
        """HTTP probe matching the same liveness contract llama-server
        speaks (commit 3f9be3be): HTTP 200 = alive, HTTP 503 with
        'Loading' body = alive-but-warming, anything else = dead.

        Returns latency in milliseconds, or None on failure.
        """
        if not endpoint:
            return None
        url = f'{endpoint.rstrip("/")}/health'
        headers = (
            {'Authorization': f'Bearer {auth_token}'} if auth_token else {})
        try:
            t0 = time.time()
            r = requests.get(url, headers=headers, timeout=_PING_TIMEOUT_S)
            elapsed_ms = (time.time() - t0) * 1000
            if r.status_code == 200:
                return elapsed_ms
            if r.status_code == 503:
                body_text = (r.text or '').lower()
                if 'loading' in body_text:
                    return elapsed_ms
            return None
        except requests.RequestException:
            return None
        except Exception:
            return None

    @staticmethod
    def _verify_peer_trust(msg: Dict[str, Any]) -> bool:
        """Trust gate.  Prefer the master-key delegation chain when
        ``security.key_delegation.verify_peer_attestation`` is available.

        Until that API ships, fall back to an env-var allowlist so an
        operator can run the discovery path against a known-trusted
        peer they control (typical for a regional deployment in
        bring-up).  When the API lands, ImportError path goes dead
        without code change here.
        """
        peer_id = (msg.get('peer_id') or '').strip()
        if not peer_id:
            return False
        try:
            from security.key_delegation import (  # type: ignore
                verify_peer_attestation,
            )
            return bool(verify_peer_attestation(
                peer_id=peer_id,
                signature=msg.get('trust_signature', ''),
                payload=msg,
            ))
        except ImportError:
            allowlist_env = os.environ.get('HEVOLVE_HIVE_TRUSTED_PEERS', '')
            trusted = {
                p.strip() for p in allowlist_env.split(',') if p.strip()
            }
            return peer_id in trusted
        except Exception as e:
            logger.warning(
                "HiveExpertDiscovery: trust verification raised for "
                "peer %s: %s — denying", peer_id, e)
            return False

    def _drop_peer(self, peer_id: str, *, reason: str) -> int:
        """Unregister every backend recorded for this peer.

        Returns the count of backends unregistered.  Safe to call
        multiple times — no-op when the peer is unknown.
        """
        with self._lock:
            backend_ids = self._peer_models.pop(peer_id, set())
            self._fail_count.pop(peer_id, None)
        for backend_id in backend_ids:
            self._registry.unregister(backend_id)
        if backend_ids:
            logger.info(
                "HiveExpertDiscovery: dropped %d backend(s) from peer "
                "%s (%s)", len(backend_ids), peer_id, reason)
        return len(backend_ids)


# ─── Module-level singleton ───
_singleton: Optional[HiveExpertDiscovery] = None
_init_lock = threading.Lock()


def get_hive_expert_discovery() -> HiveExpertDiscovery:
    """Module-level singleton accessor — matches ``model_registry``'s
    own singleton pattern (see ``integrations.agent_engine.model_registry``).
    """
    global _singleton
    with _init_lock:
        if _singleton is None:
            _singleton = HiveExpertDiscovery()
    return _singleton
