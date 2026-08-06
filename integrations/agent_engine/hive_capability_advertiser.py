"""Producer side of the hive expert tier — periodically emits
``peer.capability.announce`` gossip so other nodes' ``HiveExpertDiscovery``
can register THIS node's expert-tier models as routable hive backends.

Design intent
=============

The consumer half (``hive_expert_discovery``) listens on the
``peer.capability.*`` EventBus topics and auto-registers the advertising
peer's models.  Without a corresponding producer on each compute-rich
node, the gossip stream is empty — every peer falls back to local
``fast`` and ``served_by`` reads ``local_langchain_bg`` 100% of the
time.  This module is the producer: it announces THIS node's
expert-eligible capabilities so peer routing actually takes the turn.

Lifecycle
---------

  1. ``attach()`` reads node identity (``HEVOLVE_NODE_ID``,
     ``HEVOLVE_HIVE_PUBLIC_ENDPOINT``, ``HEVOLVE_HIVE_AUTH_TOKEN``),
     enumerates qualified models from ``model_registry``, and starts a
     periodic emit loop on a worker thread.
  2. Every ``_ADVERTISE_INTERVAL_S`` (default 300s = 5 min):
       - Re-scan the registry (operator may have hot-installed a model
         since the last advertise)
       - Build the announce payload
       - Emit via ``EventBus.emit('peer.capability.announce', payload)``
         which fans out to local subscribers AND, when ``connect_wamp``
         was called at boot, publishes to Crossbar so other nodes see
         it.
  3. ``shutdown()`` emits a final ``peer.capability.revoke`` so
     consumers drop this node's backends immediately (otherwise they'd
     wait for 3 × 60s health-check failures).
  4. ``atexit.register(shutdown)`` makes a GRACEFUL exit emit that revoke.
     It is best-effort only, and deliberately not the thing that lets the
     process exit: the announce loop runs on a DAEMON thread for that. An
     atexit hook cannot be load-bearing for termination, because
     ``concurrent.futures`` joins its workers from
     ``threading._register_atexit`` — which runs during ``threading._shutdown``,
     BEFORE any atexit callback. While the loop lived on a pool worker, this
     hook never ran at all (measured on 3.12.10) and the process hung; see
     ``hive_expert_discovery.__init__``.

Opt-in by design
----------------

``HEVOLVE_HIVE_ADVERTISE`` defaults to OFF.  An operator has to set it
to ``1`` / ``true`` / ``yes`` / ``on`` to contribute their compute.
Rationale: not every node wants to expose its compute (privacy,
bandwidth, electricity cost).  The crowdsourced-hive vision is
opt-in, not opt-out.

Qualification floor
-------------------

Only models with ``tier in {EXPERT, BALANCED, FAST}``, ``is_local=True``
(don't re-advertise *other* peers' models — that path is for
re-broadcasts which we don't do here), and
``accuracy_score >= _MIN_ADVERTISE_ACCURACY`` get advertised.  Tier
floor is operator-controlled via ``HEVOLVE_HIVE_ADVERTISE_TIER``;
default is ``EXPERT`` so only a node with a 27B-class model
advertises.  An operator with only a 4B can lower the floor to ``FAST``
if their cluster's draft tier is 0.8B and the 4B is genuinely the
expert in that topology.

Self-echo suppression
---------------------

The producer's own ``EventBus.emit`` fires the announce locally, which
would feed back into THIS node's ``HiveExpertDiscovery.on_peer_announce``
and try to register the local models as hive backends (wrong — they're
already in the registry as ``is_local=True``).  The consumer-side fix:
``HiveExpertDiscovery.on_peer_announce`` checks ``peer_id`` against
its own ``HEVOLVE_NODE_ID`` and short-circuits — the actual self-echo
guard lives there because any consumer can apply the check whether or
not this producer ever runs locally.

Reuse
=====

Nothing in this file duplicates existing primitives:

  - ``model_registry`` — single source of truth for local capabilities
  - ``ModelTier`` — same enum the discovery consumer uses
  - ``EventBus.emit`` via ``core.platform.registry.get_registry`` —
    same fan-out path the rest of the platform uses
  - ``HEVOLVE_NODE_ID`` env var — already canonical (see
    ``hart_intelligence_entry.py:9844``)
"""
from __future__ import annotations

import atexit
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from integrations.agent_engine.model_registry import (
    ModelRegistry,
    ModelTier,
    model_registry as _default_registry,
)

logger = logging.getLogger('hevolve_social')

# How often to (re-)announce.  Long enough that gossip volume stays
# reasonable; short enough that a freshly-joined consumer doesn't wait
# minutes to see this node.  Health-check failure budget on the
# consumer side is 3 × 60s = 3 min, so 5 min announce cadence means
# at most one missed cycle before the consumer drops us — operator
# can shorten via env var if their cluster has tight latency budgets.
_ADVERTISE_INTERVAL_S = 300
# How long shutdown() waits for the announce thread to notice `_stop`. The loop
# wakes immediately on the event, so this only absorbs an iteration that is
# mid-announce; the thread is a daemon, so overrunning it is safe.
_SHUTDOWN_JOIN_TIMEOUT_S = 5

# Producer's qualification floor for accuracy.  Lower than the
# consumer's _MIN_VERIFIED_BASELINE (0.5) so the consumer's floor
# can be tightened independently — producer ships its best estimate,
# consumer makes the trust decision.
_MIN_ADVERTISE_ACCURACY = 0.5

_TOPIC_ANNOUNCE = 'peer.capability.announce'
_TOPIC_REVOKE = 'peer.capability.revoke'

# Default tier floor: EXPERT.  An operator running only a 4B can
# override with HEVOLVE_HIVE_ADVERTISE_TIER=fast (or balanced).
_DEFAULT_ADVERTISE_TIER = ModelTier.EXPERT


def _enabled() -> bool:
    """Operator opt-in switch.  Default OFF — a node only contributes
    its compute to the hive when the operator says so.

    Mirrors the consumer's ``HEVOLVE_DISPATCH_LANGCHAIN_BG`` shape so
    the env-var grammar is consistent across the dispatcher layer.
    """
    raw = os.environ.get('HEVOLVE_HIVE_ADVERTISE', '').strip().lower()
    return raw in ('1', 'true', 'yes', 'on')


def _resolve_tier_floor() -> ModelTier:
    """Read ``HEVOLVE_HIVE_ADVERTISE_TIER`` and return the corresponding
    ``ModelTier``.  Falls back to ``EXPERT`` on missing / unrecognised
    values."""
    raw = os.environ.get('HEVOLVE_HIVE_ADVERTISE_TIER', '').strip().lower()
    for tier in ModelTier:
        if tier.value == raw:
            return tier
    return _DEFAULT_ADVERTISE_TIER


def _local_peer_id() -> str:
    """Stable identifier for THIS node across announces.

    Uses ``HEVOLVE_NODE_ID`` when set to a non-trivial value (matches
    ``hart_intelligence_entry.py:9844``'s convention).  Falls back to a
    generated UUID held on the singleton instance — stable for the
    process lifetime, but not across restarts (operator should set
    ``HEVOLVE_NODE_ID`` for production deployment).
    """
    node_id = (os.environ.get('HEVOLVE_NODE_ID') or '').strip()
    if node_id and node_id.lower() != 'local':
        return node_id
    return ''  # sentinel — caller substitutes per-instance UUID


def _local_endpoint() -> str:
    """Public-facing URL peers can hit to dispatch work to this node.

    Single source of truth: ``HEVOLVE_HIVE_PUBLIC_ENDPOINT`` env var.
    Returns empty string when unset — the advertiser refuses to emit
    in that case (no point advertising an unreachable address).
    """
    return (os.environ.get('HEVOLVE_HIVE_PUBLIC_ENDPOINT') or '').strip().rstrip('/')


def _local_auth_token() -> str:
    """Bearer token peers attach when dispatching to this node.

    Operator-supplied via ``HEVOLVE_HIVE_AUTH_TOKEN``.  Empty string is
    valid (open hive — discouraged in production); the consumer's
    ``HiveExpertDiscovery._dispatch_expert_langchain`` skips the
    Authorization header when the token is empty.
    """
    return (os.environ.get('HEVOLVE_HIVE_AUTH_TOKEN') or '').strip()


class HiveCapabilityAdvertiser:
    """Emits ``peer.capability.announce`` every
    ``_ADVERTISE_INTERVAL_S`` seconds with this node's expert-eligible
    models, plus a ``peer.capability.revoke`` on shutdown.

    Thread-safety: a single worker emits.  No shared state mutated from
    callers — ``attach()`` and ``shutdown()`` are idempotent and
    protected by ``self._lock``.
    """

    def __init__(self, registry: Optional[ModelRegistry] = None):
        self._registry = registry or _default_registry
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # DAEMON thread, not a ThreadPoolExecutor worker: _advertise_loop is
        # infinite, and a non-daemon worker running an infinite task is joined
        # at interpreter exit and never returns. See the long note in
        # hive_expert_discovery.__init__ (the consumer half of this pair) —
        # same bug, measured on 3.12.10, and there it hung a whole CI shard.
        #
        # The `atexit.register(self.shutdown)` below does set `_stop`, so it
        # LOOKS sufficient. It is not: concurrent.futures joins its workers
        # from threading._register_atexit, which runs during
        # threading._shutdown() — BEFORE any atexit callback — so the join
        # blocks first and the hook never runs. That is why this module's
        # header claim that atexit "covers process termination" was wrong.
        self._thread: Optional[threading.Thread] = None
        self._attached = False
        self._atexit_registered = False
        # Per-instance UUID for nodes that haven't set HEVOLVE_NODE_ID.
        # Generated lazily on first announce to avoid wasted entropy
        # when the advertiser never runs.
        self._fallback_peer_id: Optional[str] = None

    # ── Public API ───────────────────────────────────────────────

    def attach(self) -> bool:
        """Start the periodic announce loop.

        No-op + returns False when:
          - ``HEVOLVE_HIVE_ADVERTISE`` is not opted-in
          - ``HEVOLVE_HIVE_PUBLIC_ENDPOINT`` is unset (nothing to
            advertise routes TO)
          - already attached (idempotent)
        Returns True on first successful attach.
        """
        if not _enabled():
            logger.info(
                "HiveCapabilityAdvertiser: HEVOLVE_HIVE_ADVERTISE not "
                "set; node will not contribute compute to the hive.")
            return False
        endpoint = _local_endpoint()
        if not endpoint:
            logger.warning(
                "HiveCapabilityAdvertiser: HEVOLVE_HIVE_PUBLIC_ENDPOINT "
                "is unset — refusing to advertise an unreachable peer.")
            return False
        with self._lock:
            if self._attached:
                return False
            self._attached = True
            # The event + thread handle are written under the SAME lock hold
            # that flips the flag. Split (flag under lock, clear/start after),
            # a concurrent shutdown() could interleave: it sees attached=True,
            # proceeds, sets _stop — and THEN this clear erases the stop and
            # starts a loop shutdown never saw, leaving _attached=False with a
            # live announce loop advertising a revoked peer. Serialized, the
            # last lock holder's flag and event always agree.
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._advertise_loop, name='hive_advertiser', daemon=True)
            self._thread.start()
        # Best-effort revoke on graceful exit so other peers don't have
        # to wait 3 × 60s health checks before dropping us.
        if not self._atexit_registered:
            try:
                atexit.register(self.shutdown)
                self._atexit_registered = True
            except Exception:
                pass
        logger.info(
            "HiveCapabilityAdvertiser: attached, advertising every %ds "
            "as peer_id=%r endpoint=%r",
            _ADVERTISE_INTERVAL_S,
            self._peer_id(),
            endpoint,
        )
        return True

    def shutdown(self) -> None:
        """Stop the announce loop, emit a final revoke, join the thread.

        Idempotent.  Safe to call from atexit + manual teardown +
        signal handler.  Resets ``_attached`` so a second call (e.g.
        atexit firing after a manual shutdown) is a no-op rather than
        a duplicate revoke.
        """
        with self._lock:
            if not self._attached:
                # Never started, or already shut — nothing to revoke
                # or close.
                return
            self._attached = False
            # Set + handle-swap under the lock, mirroring attach (see there
            # for the lost-stop interleaving this prevents). The revoke and
            # the join stay OUTSIDE: one is network I/O, the other a wait,
            # and the announce loop briefly takes this lock itself.
            self._stop.set()
            thread, self._thread = self._thread, None
        try:
            self._emit_revoke()
        except Exception as e:
            logger.debug(
                "HiveCapabilityAdvertiser: final revoke raised (%s); "
                "consumers will drop us via health-check fail budget", e)
        # Bounded join so a caller gets a deterministic stop; the thread is a
        # daemon, so overrunning it can never keep the process alive.
        if thread is not None and thread.is_alive():
            thread.join(timeout=_SHUTDOWN_JOIN_TIMEOUT_S)
            if thread.is_alive():
                logger.debug(
                    "HiveCapabilityAdvertiser: announce thread still running "
                    "after %ss; it is a daemon and will not block exit",
                    _SHUTDOWN_JOIN_TIMEOUT_S)

    # ── Internals ────────────────────────────────────────────────

    def _peer_id(self) -> str:
        """Resolve the peer_id, generating a stable per-process UUID
        when ``HEVOLVE_NODE_ID`` isn't set."""
        explicit = _local_peer_id()
        if explicit:
            return explicit
        with self._lock:
            if self._fallback_peer_id is None:
                self._fallback_peer_id = f'auto-{uuid.uuid4().hex[:12]}'
            return self._fallback_peer_id

    def _advertise_loop(self) -> None:
        """Worker loop: emit immediately, then every interval until
        ``_stop`` is set.  Each iteration is wrapped so a single
        failure (e.g. EventBus not yet bootstrapped, registry empty,
        WAMP transient) doesn't kill the loop."""
        # Initial emit happens right away so a freshly-attached
        # advertiser surfaces on the network without waiting 5 minutes.
        try:
            self._emit_announce()
        except Exception as e:
            logger.debug(
                "HiveCapabilityAdvertiser: initial announce failed "
                "(%s); will retry on next tick", e)
        while not self._stop.wait(_ADVERTISE_INTERVAL_S):
            try:
                self._emit_announce()
            except Exception as e:
                logger.debug(
                    "HiveCapabilityAdvertiser: announce tick raised "
                    "(%s); continuing", e)

    def _enumerate_models(self) -> List[Dict[str, Any]]:
        """Build the ``models`` list for the announce payload.

        Filter: local (``is_local=True``) AND tier at-or-above the
        operator-set floor AND ``accuracy_score`` ≥
        ``_MIN_ADVERTISE_ACCURACY``.  All advertised models are
        stamped ``tier='expert'`` regardless of their local tier — the
        consumer's ``HiveExpertDiscovery`` only registers ``expert``
        tier entries, and for cross-peer routing the model's role IS
        "the expert this peer is offering".  Local tier is what THIS
        node uses for its own scheduling; what we advertise is what
        peers should treat the model as.
        """
        floor = _resolve_tier_floor()
        # Map tier ordering for the floor comparison.
        order = {
            ModelTier.DRAFT: 0,
            ModelTier.FAST: 1,
            ModelTier.BALANCED: 2,
            ModelTier.EXPERT: 3,
        }
        floor_rank = order.get(floor, order[_DEFAULT_ADVERTISE_TIER])

        models: List[Dict[str, Any]] = []
        with self._registry._lock:  # noqa: SLF001 — registry is a singleton
            backends = list(self._registry._models.values())
        for backend in backends:
            if not getattr(backend, 'is_local', False):
                continue
            if order.get(backend.tier, -1) < floor_rank:
                continue
            if backend.accuracy_score < _MIN_ADVERTISE_ACCURACY:
                continue
            models.append({
                'model_id': backend.model_id,
                'display_name': backend.display_name,
                'tier': 'expert',
                'verified_baseline': float(backend.accuracy_score),
                'specialty': list(
                    (backend.config_list_entry or {}).get(
                        'specialty') or []),
            })
        return models

    def _build_payload(self) -> Optional[Dict[str, Any]]:
        """Compose the announce payload.  Returns ``None`` when there's
        nothing to advertise (no qualified models) — caller skips the
        emit."""
        models = self._enumerate_models()
        if not models:
            return None
        endpoint = _local_endpoint()
        if not endpoint:
            return None
        return {
            'peer_id': self._peer_id(),
            'endpoint': endpoint,
            'auth_token': _local_auth_token(),
            # Trust signature: empty for now.  The env-var allowlist on
            # the consumer side is what gates trust in this rollout.
            # When ``security.key_delegation.sign_peer_announce`` lands,
            # this becomes ``sign_peer_announce(payload)``.
            'trust_signature': '',
            'models': models,
            'announced_at': time.time(),
        }

    def _emit_announce(self) -> bool:
        payload = self._build_payload()
        if payload is None:
            logger.debug(
                "HiveCapabilityAdvertiser: no qualified models; "
                "skipping announce tick")
            return False
        return self._emit(_TOPIC_ANNOUNCE, payload)

    def _emit_revoke(self) -> bool:
        return self._emit(_TOPIC_REVOKE, {
            'peer_id': self._peer_id(),
            'revoked_at': time.time(),
        })

    def _emit(self, topic: str, payload: Dict[str, Any]) -> bool:
        """Emit a topic via the platform EventBus.

        ``EventBus.emit`` fans out to local subscribers AND, when
        ``connect_wamp`` has run, auto-publishes to
        ``com.hartos.event.<topic>`` so peers on Crossbar receive the
        gossip.

        Returns True if the bus accepted the emit.  Failures (bus not
        bootstrapped, exception during fan-out) degrade to a debug log
        line — the chat path keeps working.
        """
        try:
            from core.platform.registry import get_registry
            registry = get_registry()
            if not registry.has('events'):
                logger.debug(
                    "HiveCapabilityAdvertiser: EventBus not "
                    "bootstrapped; cannot emit %s", topic)
                return False
            bus = registry.get('events')
            bus.emit(topic, payload)
            logger.debug(
                "HiveCapabilityAdvertiser: emitted %s "
                "(peer_id=%r, models=%d)",
                topic, payload.get('peer_id'),
                len(payload.get('models', [])),
            )
            return True
        except Exception as e:
            logger.debug(
                "HiveCapabilityAdvertiser: emit %s raised (%s)",
                topic, e)
            return False


# ─── Module-level singleton ───
_singleton: Optional[HiveCapabilityAdvertiser] = None
_init_lock = threading.Lock()


def get_hive_capability_advertiser() -> HiveCapabilityAdvertiser:
    """Singleton accessor — matches the discovery side's
    ``get_hive_expert_discovery()`` pattern."""
    global _singleton
    with _init_lock:
        if _singleton is None:
            _singleton = HiveCapabilityAdvertiser()
    return _singleton
