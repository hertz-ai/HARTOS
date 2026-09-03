"""
HevolveSocial - Decentralized Gossip Peer Discovery
Fully decentralized protocol for HevolveBot instances to discover each other.
No central registry. Peers exchange peer lists via gossip, new nodes propagate automatically.
"""
import os
import uuid
import time
import random
import logging
import threading
import requests
from datetime import datetime, timedelta

from core.http_pool import pooled_get, pooled_post
from core.session_cache import TTLCache  # bounded + TTL dedup (peer churn safe)
from core.ttl_cache import ttl_cached  # hard-TTL discovery cache (gossip counts)

import ipaddress as _ipaddress
from urllib.parse import urlparse as _urlparse

# Docker's default bridge network. A container that advertises its in-bridge
# address (172.17.x) instead of its host's address is unreachable from any
# other node — a config bug, never a routable inter-node peer address.
_DOCKER_BRIDGE = _ipaddress.ip_network('172.17.0.0/16')
# The historical port typo: the real backend port is 6777; drifted configs
# advertised :677, where nothing binds.
_DEAD_PEER_PORT = 677


def is_unroutable_peer_url(url):
    """Is this URL structurally unusable as a DISTINCT peer address — for anyone?

    Returns (True, reason) for addresses that can never be a real distinct peer
    on ANY host: the docker default bridge (172.17.x — a per-host internal
    address, unreachable between nodes), the dead :677 port (the 6777 typo,
    nothing binds it), the unspecified/link-local ranges, and hostless/
    unparseable URLs. A local ping to a docker-bridge row succeeds (the node
    reaches its own bridge), so the age-based health check keeps it 'active'
    forever — hence they must be excluded by CLASS, not aged out. Measured live
    2026-08-25: central's 673-row table held 134 docker-172.17 + 8 dead-:677 of
    exactly this kind, all cert_verified=0.

    Loopback (localhost / 127.x / ::1) is deliberately NOT rejected here: two
    co-located nodes — dev, test (tests/standalone/two_node_collaboration.py),
    single-box multi-process — legitimately reach each other over loopback on
    distinct ports. The production loopback pollution (a REMOTE node that
    advertised its own localhost) is prevented at the SOURCE by
    get_advertisable_base_url (nodes now advertise a routable address); any
    legacy localhost rows on central are a one-time ops cleanup, not worth
    breaking co-located collaboration for. Also does NOT reject ordinary
    private LAN addresses (192.168.x, 10.x, the rest of 172.16/12).

    The one canonical definition of "structurally not a peer"; reuse it, do not
    re-implement per call site.
    """
    if not url:
        return True, 'empty url'
    try:
        parsed = _urlparse(url if '://' in url else 'http://' + url)
    except Exception:
        return True, 'unparseable url'
    host = (parsed.hostname or '').lower()
    if not host:
        return True, 'no host'
    try:
        if parsed.port == _DEAD_PEER_PORT:
            return True, 'dead :677 port'
    except ValueError:
        return True, 'malformed port'
    try:
        ip = _ipaddress.ip_address(host)
    except ValueError:
        # A DNS hostname (incl. 'localhost') — resolution + reachability
        # decide, not this structural gate.
        return False, ''
    if ip.is_unspecified or ip.is_link_local:
        return True, 'unspecified/link-local ip'
    if ip.version == 4 and ip in _DOCKER_BRIDGE:
        return True, 'docker bridge 172.17.x'
    return False, ''

logger = logging.getLogger('hevolve_social')


def _load_or_create_node_id() -> str:
    """Persist node_id under platform_paths.get_data_dir() / 'node_id.json'.

    Returns existing id on subsequent boots so the central side can
    dedupe joins by node_id.  Falls back to a fresh in-memory uuid if
    the data dir is unwritable (degraded environments such as
    cx_Freeze read-only mode).
    """
    import json
    try:
        from core.platform_paths import get_data_dir
        data_dir = get_data_dir()
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, 'node_id.json')
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as fh:
                    payload = json.load(fh)
                nid = payload.get('node_id', '')
                if nid:
                    return nid
            except Exception:
                pass  # Corrupt file → regenerate
        nid = str(uuid.uuid4())
        try:
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump({'node_id': nid, 'created_at': datetime.utcnow().isoformat()}, fh)
        except Exception:
            pass  # Best-effort persist; in-memory id is still valid for this boot
        return nid
    except Exception:
        return str(uuid.uuid4())


# ═══════════════════════════════════════════════════════════════════════
# Bandwidth Profiles - auto-selected by tier, override via env
# ═══════════════════════════════════════════════════════════════════════

BANDWIDTH_PROFILES = {
    'full': {
        'gossip_interval': 60,
        'health_interval': 120,
        'gossip_fanout': 3,
        'payload_mode': 'json',       # Full JSON with all fields
        'stale_threshold': 300,
        'dead_threshold': 900,
    },
    'constrained': {
        'gossip_interval': 300,
        'health_interval': 600,
        'gossip_fanout': 2,
        'payload_mode': 'json_compact',  # Stripped optional fields
        'stale_threshold': 900,
        'dead_threshold': 2700,
    },
    'minimal': {
        'gossip_interval': 900,
        'health_interval': 1800,
        'gossip_fanout': 1,
        'payload_mode': 'msgpack',    # ~60% smaller than JSON
        'stale_threshold': 2700,
        'dead_threshold': 7200,
    },
}

# Bandwidth profile lookup — handles BOTH classification dimensions:
#   1. Capability tier (NodeTierLevel): embedded, observer, lite, standard, full, compute_host
#      → describes what this node CAN do based on hardware
#   2. Topology mode (HEVOLVE_NODE_TIER): flat, regional, central
#      → describes WHERE this node sits in the network hierarchy
# Lookup order: capability_tier first, then topology mode as fallback
# (see GossipProtocol.__init__: cap_tier or self.tier)
_TIER_BANDWIDTH_MAP = {
    # Capability tiers (from security/system_requirements.py NodeTierLevel)
    'embedded': 'minimal',
    'observer': 'constrained',
    'lite': 'constrained',
    'standard': 'full',
    'full': 'full',
    'compute_host': 'full',
    # Topology modes (fallback when capability_tier is not yet resolved)
    'flat': 'full',
    'regional': 'full',
    'central': 'full',
}

# Compact payload: only essential fields for gossip on constrained links
_COMPACT_FIELDS = frozenset({
    'node_id', 'url', 'public_key', 'guardrail_hash', 'code_hash',
    'signature', 'tier', 'capability_tier', 'timestamp', 'hart_tag',
})


@ttl_cached(ttl_seconds=30)
def _cached_node_count(what: str) -> int:
    """Cached agent/post count for gossip advertising.

    Module-level so the cache is keyed by ``what`` (a GLOBAL db count), not by
    the GossipProtocol instance. Gossip advertises these counts every round; a
    <=30s-stale count is fine and saves a COUNT query per tick — the 2026-06-13
    dig caught this loop active in SQL ``fetchall``.
    """
    from .models import get_db, User, Post
    db = get_db()
    try:
        if what == 'agent':
            return db.query(User).filter(User.user_type == 'agent').count()
        elif what == 'post':
            return db.query(Post).filter(Post.is_deleted == False).count()
        return 0
    finally:
        db.close()


class GossipProtocol:
    """Gossip-based peer discovery for HevolveBot network."""

    @property
    def base_url(self) -> str:
        """This node's reachable base URL, as advertised to every peer.

        LAZY ON PURPOSE.  ``gossip = GossipProtocol()`` runs at module scope
        (bottom of this file), i.e. at IMPORT time — long before Flask binds
        its port.  Resolving in __init__ therefore always loses the port race
        and freezes the cold-boot fallback into `_self_info()['url']` for the
        life of the process, so every peer that discovers us dials a dead port.

        Delegates to the canonical ``core.port_registry.get_advertisable_base_url``
        (Gate 4 — built on the same get_local_backend_url that hartos_bootstrap,
        agent_engine.dispatch, channels.flask_integration and mcp._tool_impls
        already use).  That resolver probes which port is actually LISTENING:
        standalone HARTOS serves on backend (6777), the bundled desktop serves
        HARTOS in-process on the Flask port (5000).

        ADVERTISABLE, not local.  get_local_backend_url answers "where do I
        reach my own backend" and correctly returns loopback; this property is
        published to peers, where loopback is never right.  It is why every row
        in the live peer table read http://localhost:6777, and why
        core/peer_link/nat.py — which dials peer_info['url'] — resolved every
        peer back to the caller's own machine.  get_advertisable_base_url pairs
        the LAN address with the port this resolver just proved is live, and
        HEVOLVE_BASE_URL still wins when set.

        Cached only once it returns a real answer.  While nothing is listening
        the resolver yields its backend fallback, and caching THAT is precisely
        the bug — so we keep re-resolving until a port actually answers.  The
        same test applies to the host: a loopback answer means no LAN address
        was available yet, so it is not final either.
        """
        if self._base_url_final:
            return self._base_url_cached

        from core import port_registry as _pr
        url = _pr.get_advertisable_base_url()
        # Finality is decided by the PORT, never the host form. The previous
        # rule compared against the LOOPBACK fallback string
        # (http://localhost:6777) and called anything else final — but
        # get_advertisable_base_url rewrites the host to the LAN address, so
        # on any networked box the cold-boot fallback arrives as
        # http://<lan>:6777, matched nothing, and was FROZEN. That is the
        # exact regression this property exists to prevent, reintroduced for
        # the LAN path: every peer dialled a dead 6777 for the life of the
        # process. Ask the honest question instead — is the port in the
        # answer actually LISTENING? (Same probe the resolver itself uses;
        # attribute access through the module so test seams keep working.)
        if os.environ.get('HEVOLVE_BASE_URL'):
            self._base_url_final = True
        else:
            _port = url.rsplit(':', 1)[-1]
            if _port.isdigit() and _pr._is_port_listening(int(_port)):
                self._base_url_final = True
        self._base_url_cached = url
        return url

    @base_url.setter
    def base_url(self, value: str):
        """Pin the advertised URL explicitly.

        Turning base_url into a read-only property broke every caller that
        assigned to it, which is most of tests/unit/test_gossip_security.py.
        The lazy resolver exists so nothing freezes the cold-boot fallback, not
        to forbid callers who genuinely know the answer, and a test that needs
        a node to claim a specific address is a legitimate case. An explicit
        assignment is authoritative, so mark it final and stop resolving.
        """
        self._base_url_cached = (value or '').rstrip('/')
        self._base_url_final = bool(self._base_url_cached)

    @base_url.deleter
    def base_url(self):
        """Forget any pinned/cached answer and resume lazy resolution.

        Completes the property contract the setter opened: an explicit
        assignment is authoritative, so its UNDO must exist too. Concretely,
        `patch.object(gossip, 'base_url', ...)` — the pattern
        test_recipe_capability_mesh already uses — assigns through the setter
        on enter and `del`s on exit; without a deleter every such patch died
        at TEARDOWN with "property ... has no deleter"."""
        self._base_url_cached = ''
        self._base_url_final = False

    def __init__(self):
        # Identity — persisted across restarts so the central side can
        # dedupe joins by node_id.  Without persistence, every watchdog
        # restart of the gossip thread fabricates a fresh uuid and the
        # central dashboard sees infinite "new node" rows for a single
        # install (witnessed 2026-04-30: same install logged
        # node=a7ba8cc9 then node=d016058b in one server.log).
        self.node_id = _load_or_create_node_id()
        self.node_name = os.environ.get(
            'HEVOLVE_NODE_NAME', f'hevolve-{self.node_id[:8]}')
        # base_url is a lazy @property (see above) — do NOT resolve it here.
        # This runs at import time, before Flask binds, so any value computed
        # now is the cold-boot fallback.
        self._base_url_cached = ''
        self._base_url_final = False
        self.version = '1.0.0'
        self.started_at = datetime.utcnow()

        # Hierarchy configuration (needed before bandwidth selection)
        try:
            from security.key_delegation import get_node_tier
            self.tier = get_node_tier()
        except ImportError:
            self.tier = 'flat'

        # Bandwidth profile: auto-select from tier, allow env override
        self.bandwidth_profile = os.environ.get('HEVOLVE_GOSSIP_BANDWIDTH', '')
        if not self.bandwidth_profile:
            # Auto-select from capability tier (from system_requirements)
            cap_tier = ''
            try:
                from security.system_requirements import get_capabilities
                caps = get_capabilities()
                if caps:
                    cap_tier = caps.tier.value
            except Exception:
                pass
            self.bandwidth_profile = _TIER_BANDWIDTH_MAP.get(
                cap_tier or self.tier, 'full')
        profile = BANDWIDTH_PROFILES.get(self.bandwidth_profile, BANDWIDTH_PROFILES['full'])

        # Configuration - profile defaults, overridable by env
        self.gossip_interval = int(os.environ.get(
            'HEVOLVE_GOSSIP_INTERVAL', str(profile['gossip_interval'])))
        self.health_interval = int(os.environ.get(
            'HEVOLVE_HEALTH_INTERVAL', str(profile['health_interval'])))
        self.stale_threshold = int(os.environ.get(
            'HEVOLVE_STALE_THRESHOLD', str(profile['stale_threshold'])))
        self.dead_threshold = int(os.environ.get(
            'HEVOLVE_DEAD_THRESHOLD', str(profile['dead_threshold'])))
        self.gossip_fanout = int(os.environ.get(
            'HEVOLVE_GOSSIP_FANOUT', str(profile['gossip_fanout'])))
        self.payload_mode = profile['payload_mode']

        self.central_url = os.environ.get('HEVOLVE_CENTRAL_URL', '').rstrip('/')
        self.regional_url = os.environ.get('HEVOLVE_REGIONAL_URL', '').rstrip('/')

        # Parse seed peers — env override + canonical genesis peers.
        # Genesis peers prevent bootstrap poisoning: even if env var is
        # compromised, the node always knows at least the real network.
        # Sourced from `core.superadmins.ALL_CENTRAL_URLS` (primary +
        # fallback) so gossip can find EITHER the .hevolve.ai central
        # OR the azurekong.hertzai.com fallback — single source of
        # truth, no parallel literals that could drift (Gate 4 / DRY).
        try:
            from core.superadmins import ALL_CENTRAL_URLS
            _GENESIS_PEERS = list(ALL_CENTRAL_URLS)
        except Exception:
            # Degraded environment fallback (cx_Freeze import chain race
            # at very-early boot).  Mirror the canonical default.
            _GENESIS_PEERS = [
                'https://central.hevolve.ai',
                'https://azurekong.hertzai.com',
            ]
        seed_str = os.environ.get('HEVOLVE_SEED_PEERS', '')
        env_peers = [
            u.strip().rstrip('/') for u in seed_str.split(',')
            if u.strip()
        ]
        # Merge: env peers first (user-specified), then genesis (always present)
        seen = set()
        self.seed_peers = []
        for url in env_peers + _GENESIS_PEERS:
            if url not in seen:
                seen.add(url)
                self.seed_peers.append(url)

        # HART node identity (loaded on start)
        self._hart_tag = ''

        # Our public IP as ECHOED back by a peer's announce response
        # (discovery.peer_announce 'observed_ip').  A NAT'd node cannot know
        # this by itself — 0/147 fleet peers advertised a routable URL on
        # 2026-08-07 because every node could only claim its LAN address.
        # When set (and public), _self_info advertises `observed_url` so
        # other peers gain a WAN dial candidate.  In-memory on purpose:
        # re-learned within one announce round, and a moved node (new
        # network) must not persist a stale public IP.
        self._observed_public_ip = ''

        # State
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        # Exponential backoff for unreachable peers
        from core.circuit_breaker import PeerBackoff
        self._peer_backoff = PeerBackoff(initial=10, maximum=300)

        logger.info(
            f"Gossip bandwidth: {self.bandwidth_profile} "
            f"(gossip={self.gossip_interval}s, health={self.health_interval}s, "
            f"fanout={self.gossip_fanout}, payload={self.payload_mode})"
        )

    # ─── Peer Backoff (delegates to core.circuit_breaker.PeerBackoff) ───

    def _is_peer_backed_off(self, peer_url: str) -> bool:
        return self._peer_backoff.is_backed_off(peer_url)

    def _record_peer_failure(self, peer_url: str):
        self._peer_backoff.record_failure(peer_url)

    def _record_peer_success(self, peer_url: str):
        self._peer_backoff.record_success(peer_url)

    # ─── Payload Serialization ───

    def _gossip_self_info(self):
        """Return self info appropriate for current bandwidth profile.

        Re-signs after compaction. _COMPACT_FIELDS keeps `signature` and
        `public_key` but drops name, version, agent_count, post_count,
        x25519_public and current_version, all of which the signature covers.
        The receiver verifies over every field except 'signature', so a
        compacted record carrying the full record's signature can never
        verify, and under enforcement=hard the peer is refused. That made
        every constrained and minimal profile node permanently unverifiable,
        the same defect as signing _self_info before finishing it.

        Signing what is actually sent keeps the invariant in one place. The
        bandwidth saving is unaffected.
        """
        info = self._self_info()
        if self.payload_mode == 'json_compact':
            compact = {k: v for k, v in info.items() if k in _COMPACT_FIELDS}
            if compact.get('public_key'):
                try:
                    from security.node_integrity import sign_json_payload
                    compact.pop('signature', None)
                    compact['signature'] = sign_json_payload(compact)
                except Exception:
                    # No crypto available: send it unsigned rather than with a
                    # signature that cannot match, so the receiver's
                    # enforcement path sees the truth.
                    compact.pop('signature', None)
            return compact
        return info

    def _gossip_peer_list(self):
        """Return peer list appropriate for current bandwidth profile."""
        peers = self.get_peer_list()
        if self.payload_mode == 'json_compact':
            return [{k: v for k, v in p.items() if k in _COMPACT_FIELDS}
                    for p in peers]
        return peers

    @staticmethod
    def _serialize_payload(data) -> bytes:
        """Serialize payload using msgpack if available, else JSON."""
        try:
            import msgpack
            return msgpack.packb(data, use_bin_type=True)
        except ImportError:
            import json
            return json.dumps(data).encode('utf-8')

    @staticmethod
    def _deserialize_payload(raw: bytes):
        """Deserialize payload from msgpack or JSON."""
        try:
            import msgpack
            return msgpack.unpackb(raw, raw=False)
        except ImportError:
            import json
            return json.loads(raw.decode('utf-8'))
        except Exception:
            import json
            return json.loads(raw.decode('utf-8'))

    def start(self):
        """Load peers from DB, announce to seeds/known peers, start background thread."""
        with self._lock:
            if self._running:
                return
            self._running = True

        # Generate HART node identity if not yet established
        self._ensure_hart_identity()

        # Seed peers into DB
        self._seed_initial_peers()

        # Announce self to all known peers (non-blocking)
        threading.Thread(target=self._announce_to_all, daemon=True).start()

        # Start gossip background loop
        self._thread = threading.Thread(target=self._background_loop, daemon=True)
        self._thread.start()
        logger.info(f"Gossip started: node={self.node_id[:8]}, "
                    f"name={self.node_name}, hart_tag={self._hart_tag}, "
                    f"seeds={len(self.seed_peers)}, "
                    f"bandwidth={self.bandwidth_profile}")

        # Start the central report-in loop.  Every install — bundled,
        # Docker, ISO — reports identity to the canonical superadmin
        # allowlist (core.superadmins.SUPERADMIN_CENTRAL_URLS) so the
        # superadmin dashboard sees every node that has ever joined.
        # Offline-tolerant via outbox; cheap when centrals are
        # unreachable (PeerBackoff handles DNS + connect timeouts).
        try:
            from core.superadmin_report import start_background_loop
            start_background_loop(self._self_info)
        except Exception as e:
            logger.debug(f"Superadmin report-in loop not started: {e}")

    def _ensure_hart_identity(self):
        """Generate HART node identity on first startup. Like getting an IP address.

        - Central: picks a unique element → @element
        - Regional: picks a unique spirit → @central.spirit
        - Flat: no node tag needed (users get individual tags)
        """
        try:
            from hartos.hart_onboarding import generate_node_identity, get_onboarding_identity

            # Check if already generated
            existing = get_onboarding_identity()
            if existing and existing.get('node_tag'):
                self._hart_tag = existing['node_tag']
                return

            # Gather known tags from peer network for collision avoidance
            known_tags = set()
            try:
                peers = self._load_peers_from_db(exclude_dead=True)
                for p in peers:
                    tag = p.get('hart_tag', '')
                    if tag:
                        known_tags.add(tag)
            except Exception:
                pass

            # Central element comes from env or the central we connect to
            central_element = os.environ.get('HART_CENTRAL_ELEMENT', '')

            identity = generate_node_identity(
                tier=self.tier,
                central_element=central_element or None,
                known_tags=known_tags,
            )

            self._hart_tag = identity.get('node_tag', '')
        except Exception as e:
            logger.debug(f"HART identity generation skipped: {e}")

    def stop(self):
        """Stop the gossip background thread."""
        with self._lock:
            self._running = False
        if self._thread:
            self._thread.join(timeout=10)

    # ─── Background Loop ───

    def _background_loop(self):
        last_gossip = 0
        last_health = 0
        last_integrity = 0
        integrity_interval = int(os.environ.get('HEVOLVE_INTEGRITY_INTERVAL', '300'))
        while self._running:
            now = time.time()
            # Heartbeat to watchdog
            try:
                from security.node_watchdog import get_watchdog
                wd = get_watchdog()
                if wd:
                    wd.heartbeat('gossip')
            except Exception:
                pass
            # Each round is isolated.  These three used to share ONE
            # try/except, which had two compounding failure modes: a raising
            # _gossip_round() skipped health AND integrity for that tick, and
            # it left last_gossip un-advanced, so the next tick retried gossip
            # immediately, raised again, and starved the other two rounds
            # permanently.  At logger.debug, that is invisible.
            #
            # Measured on central 2026-09-01: the newest integrity challenge
            # was 2026-08-31 04:29, 24h stale, while inbound announces were
            # being served normally (465 peers seen in 10 minutes).  The
            # retention sweep that keeps integrity_challenges from growing
            # unbounded is wired into _integrity_round, so 155,869 rows sat
            # past their window with the code to remove them never reached.
            #
            # last_* is advanced even when a round FAILS: a round that throws
            # every time must retry on its own schedule, not spin every 5s and
            # crowd out its siblings.  Failures are logged at WARNING now,
            # because a silent one cost a day.
            if now - last_gossip >= self.gossip_interval:
                self._run_round('gossip', self._gossip_round)
                last_gossip = now
            if now - last_health >= self.health_interval:
                self._run_round('health', self._health_check_round)
                last_health = now
            if now - last_integrity >= integrity_interval:
                self._run_round('integrity', self._integrity_round)
                last_integrity = now
            time.sleep(5)

    def _run_round(self, name: str, fn) -> bool:
        """Run one background round, isolated from its siblings.

        A failure in one round must never stop a DIFFERENT round from running.
        Returns True if the round completed without raising.
        """
        try:
            fn()
            return True
        except Exception as e:
            logger.warning("Gossip loop: %s round failed (%s: %s); "
                           "other rounds continue on their own schedule",
                           name, type(e).__name__, e)
            return False

    # ─── Gossip Round ───

    def _heartbeat(self):
        """Send heartbeat to watchdog between potentially blocking operations."""
        try:
            from security.node_watchdog import get_watchdog
            wd = get_watchdog()
            if wd:
                wd.heartbeat('gossip')
        except Exception:
            pass

    def _gossip_round(self):
        # Tier-aware gossip: scope targets by tier
        if self.tier == 'flat':
            peers = self._load_peers_from_db(exclude_dead=True)
        else:
            peers = self._load_peers_by_tier()

        if not peers:
            # Bootstrap. Retry seeds, limited to 2 so a cold node does not
            # block for N x 5s when every seed is unreachable.
            #
            # EXCHANGE as well as announce. Announcing only tells the seed we
            # exist; _announce_to_peer discards the response body, so it
            # teaches us nothing. Exchange is the only call that returns the
            # other side's peer list, and it was reached exclusively through
            # the `peers` path below, which requires already having a peer.
            # That was a closed loop: a node with no peers could register
            # itself with central forever and never learn of a single node,
            # so remote_count stayed 0 on every fresh install no matter what
            # else was fixed. Observed live: central accepted this desktop and
            # listed it, while the desktop still reported remote_count 0
            # through repeated gossip rounds.
            #
            # Announce is kept ahead of the exchange rather than replaced.
            # Exchange carries `sender`, so a peer that implements it learns
            # us either way, but a node that predates the endpoint would 404
            # the exchange and we would lose the registration. Two requests to
            # at most two seeds, and only while we have no peers at all.
            for url in list(self.seed_peers)[:2]:
                if not self._running:
                    return
                if self._is_peer_backed_off(url):
                    continue
                self._announce_to_peer(url)
                try:
                    their_peers = self._exchange_with_peer(url)
                    if their_peers:
                        self._merge_peer_list(their_peers)
                except Exception as e:
                    logger.debug(f"Bootstrap exchange with {url} failed: {e}")
                self._heartbeat()
            return

        targets = random.sample(peers, min(self.gossip_fanout, len(peers)))

        # Central seeds join the target set EVERY round, not only when the
        # node is peerless.  Measured live 2026-08-24: a fresh desktop's
        # PeerNode table carries hundreds of stale rows (dead localhost:6777
        # from port drift), all marked `active`, so `peers` is never empty —
        # which meant the `if not peers:` bootstrap-exchange above never ran
        # and the node NEVER exchanged with azurekong.  Result: zero central
        # contacts across an entire boot, the node absent from the hive
        # census, no federation.  Losing the census/federation hub is far
        # worse than one extra request per round to a seed that is, by
        # definition, the aggregation point.  Deduped against `targets` by
        # URL so a seed already sampled isn't hit twice; backoff still
        # applies, so a genuinely down seed is skipped after failures.
        _target_urls = {p.get('url') for p in targets}
        _seed_targets = [{'url': u, 'node_id': None}
                         for u in self.seed_peers if u not in _target_urls]

        for peer in targets + _seed_targets:
            if not self._running:
                return
            peer_url = peer['url']
            if self._is_peer_backed_off(peer_url):
                continue
            try:
                # Seeds get an announce too, so central always relearns us
                # even if our row aged out on its side.
                if peer in _seed_targets:
                    self._announce_to_peer(peer_url)
                their_peers = self._exchange_with_peer(peer_url)
                if their_peers:
                    self._merge_peer_list(their_peers)
                # Backoff is handled inside _exchange_with_peer()
            except Exception as e:
                logger.debug(f"Gossip exchange failed with {peer_url}: {e}")
                self._record_peer_failure(peer_url)
            self._heartbeat()

    def _flush_health_row(self, db, round_name='Health round') -> bool:
        """Commit one peer row's change immediately, before the next probe.

        Exists so a gossip round never holds SQLite's write lock across a
        network call -- see the rationale at the call site in
        ``_health_check_round``.  ``_integrity_round`` uses it too (#71):
        its guardrail audit and challenge steps make one network call per
        active peer and used to commit once after the whole loop.

        A failed commit must NOT abort the round: the outer handler would
        roll back the whole sweep (every status update and every #38 purge)
        because one row lost a lock race. Roll back just this row, say so,
        and let the next peer try on its own.
        """
        try:
            db.commit()
            return True
        except Exception as e:
            db.rollback()
            logger.warning(
                "%s: per-row commit failed (%s: %s); rolled back "
                "that row and continuing with the next peer",
                round_name, type(e).__name__, e)
            return False

    def _health_check_round(self):
        self._peer_backoff.prune_expired()
        from .models import get_db, PeerNode
        db = get_db()
        try:
            peers = db.query(PeerNode).filter(PeerNode.status != 'dead').all()
            now = datetime.utcnow()
            purged = 0
            live_peers = []

            # WALL-CLOCK BUDGET. This loop pings every non-dead peer serially at
            # _ping_peer's 3s timeout. _background_loop runs gossip, health and
            # integrity in sequence, so a long health round does not just delay
            # itself -- it STARVES the integrity round behind it, which is due
            # every 300s.
            #
            # Measured on central 2026-09-01: 566 non-dead rows x 3s = up to ~28
            # minutes for ONE health round, against a 5-minute integrity
            # interval. The integrity round had not produced a challenge in 29h
            # and its retention sweep removed 0 rows across a fresh container,
            # with NOTHING in the logs -- because nothing raised. The round was
            # simply never reached. Calling the sweep by hand on that same
            # container removed exactly 10,000 rows, proving the sweep was fine
            # and only the scheduling was broken.
            #
            # 18c3e3cb isolated the rounds against EXCEPTIONS. This is the other
            # half: isolation against DURATION. A round that cannot finish in
            # its budget yields and resumes next tick instead of holding the
            # loop.
            _budget_s = float(os.environ.get(
                'HEVOLVE_HEALTH_ROUND_BUDGET_S', '30'))
            _started = time.time()
            # Rotate the starting point so a budget cut does not re-check the
            # same prefix forever and never reach the tail of the table.
            if peers:
                _cursor = getattr(self, '_health_cursor', 0) % len(peers)
                peers = peers[_cursor:] + peers[:_cursor]
            _examined = 0

            for peer in peers:
                if not self._running:
                    break
                if time.time() - _started > _budget_s:
                    self._health_cursor = (
                        getattr(self, '_health_cursor', 0) + _examined)
                    logger.warning(
                        "Health round hit its %.0fs budget after %d/%d peers; "
                        "yielding so the integrity round is not starved "
                        "(resumes from this point next round)",
                        _budget_s, _examined, len(peers))
                    break
                _examined += 1
                if peer.node_id == self.node_id:
                    continue
                # #38: a structurally-unroutable row (loopback / docker-bridge /
                # dead :677) can never be a real remote peer, and a local ping
                # to it SUCCEEDS — so the age logic below would keep it 'active'
                # forever and inflate every count. Delete it outright rather
                # than age it out; ingest (_merge_peer) now rejects new ones, so
                # it will not come back.
                _bad_url, _ = is_unroutable_peer_url(peer.url)
                if _bad_url:
                    db.delete(peer)
                    purged += 1
                    self._flush_health_row(db)
                    continue
                live_peers.append(peer)
                reachable = self._ping_peer(peer.url)
                self._heartbeat()
                if reachable:
                    peer.last_seen = now
                    peer.status = 'active'
                else:
                    age = (now - (peer.last_seen or peer.first_seen)).total_seconds()
                    if age > self.dead_threshold:
                        peer.status = 'dead'
                    elif age > self.stale_threshold:
                        peer.status = 'stale'
                # Commit THIS row before probing the next peer.
                #
                # The round previously accumulated every delete + status change
                # in one session and committed only after the whole sweep. The
                # first mutation opens SQLite's single write transaction, so the
                # write lock was then held across all the remaining _ping_peer
                # calls. That is a lock held across network I/O.
                #
                # It wedged central on 2026-09-01: a probe hung (pooled_get's
                # timeout=3 bounds the socket, NOT the wait for a free pooled
                # connection, and a hang raises nothing for the RequestException
                # handler to catch), the loop never advanced, so the wall-clock
                # budget below -- which is only checked BETWEEN peers -- never
                # got to fire. The write lock stayed held for ~2h. The agent
                # daemon's `UPDATE agent_goals SET last_dispatched_at` lost its
                # 5s busy_timeout on every tick, which poisoned its session
                # ("rolled back due to a previous exception during flush") and
                # aborted the tick. Goals were selected and never recorded:
                # max(last_dispatched_at) frozen while the daemon looked alive.
                #
                # Committing per row keeps the lock held for microseconds
                # instead of for the length of a network sweep. A hung probe
                # then stalls only THIS round, which the budget already covers,
                # rather than every writer in the process. It also means the #38
                # purge persists incrementally instead of being rolled back
                # wholesale when the round cannot finish.
                self._flush_health_row(db)
            # Dead is not deleted. The main query above excludes 'dead' rows,
            # so a peer that aged out during an outage was never re-pinged and
            # could only come back via an INBOUND announce — two nodes that
            # age each other out stay partitioned forever on a LAN with no
            # central to reintroduce them (measured 2026-08-25: box9 alive and
            # serving while this node's broadcast skipped it; a manual
            # gossip._announce_to_peer was what healed it). Re-probe a bounded
            # random batch per round: bounded because central's table held 673
            # rows; random so unrevivable rows cannot starve the rotation.
            dead_ids = [r[0] for r in db.query(PeerNode.id)
                        .filter(PeerNode.status == 'dead').all()]
            for _id in random.sample(dead_ids, min(5, len(dead_ids))):
                if not self._running:
                    break
                peer = db.query(PeerNode).filter(PeerNode.id == _id).first()
                if peer is None or peer.node_id == self.node_id:
                    continue
                _bad_url, _ = is_unroutable_peer_url(peer.url)
                if _bad_url:
                    db.delete(peer)
                    purged += 1
                    continue
                if self._ping_peer(peer.url):
                    peer.last_seen = now
                    peer.status = 'active'
                    logger.info("Dead peer revived by re-probe: %s", peer.url)
                self._heartbeat()
            db.commit()
            if purged:
                logger.info(
                    "Peer purge (#38): removed %d unroutable rows "
                    "(loopback / docker-172.17 / dead :677)", purged)
            # Update contribution scores for active/stale peers (live rows only;
            # purged rows are gone from the session).
            try:
                from .hosting_reward_service import HostingRewardService
                for peer in live_peers:
                    if peer.status in ('active', 'stale') and peer.node_id != self.node_id:
                        HostingRewardService.compute_contribution_score(db, peer.node_id)
                db.commit()
            except Exception:
                pass
        except Exception as e:
            db.rollback()
            # WARNING, not debug: this round owns peer liveness and the #38
            # unroutable-row purge. A silent failure here leaves stale peers
            # 'active' and the junk rows in place, which is what inflates every
            # node count.
            logger.warning("Health check round failed (%s: %s)",
                           type(e).__name__, e)
        finally:
            db.close()

    # ─── Announce ───

    def _announce_to_all(self):
        peers = self._load_peers_from_db(exclude_dead=False)
        urls = set(p['url'] for p in peers)
        # Always include seed peers — flat-mode installs MUST be allowed
        # to talk to central.hevolve.ai for the universal peer-join +
        # central report-in spec (memory:
        # project_universal_peer_join_central_report.md).  The previous
        # 2026-04 fix dropped seeds entirely in flat mode to suppress
        # NameResolutionError noise, which silently broke central
        # registration for every desktop install.  PeerBackoff (line ~429)
        # already absorbs DNS failures with exponential backoff, so we
        # get the announce attempt without the log spam.
        urls.update(self.seed_peers)
        for url in urls:
            if not self._running:
                return
            if url != self.base_url:
                # Skip peers that are currently backed off (exponential
                # backoff from PeerBackoff). This prevents unnecessary
                # DNS lookups for hosts that have been unreachable.
                if self._is_peer_backed_off(url):
                    continue
                self._announce_to_peer(url)
                self._heartbeat()

    def _announce_to_peer(self, peer_url):
        try:
            resp = pooled_post(
                f"{peer_url}/api/social/peers/announce",
                json=self._self_info(),
                timeout=5,
            )
            if resp.status_code == 200:
                self._record_peer_success(peer_url)
                self._consume_observed_ip_echo(resp)
                return True
            self._record_peer_failure(peer_url)
            return False
        except requests.RequestException:
            self._record_peer_failure(peer_url)
            return False

    def _consume_observed_ip_echo(self, resp):
        """Learn our own public IP from a peer's announce response.

        peer_announce echoes 'observed_ip' — the source address the RECEIVER
        saw for us.  Only a PUBLIC echo is kept (a LAN peer echoing our LAN
        address teaches nothing WAN-dialable), and only a change is logged.
        Old peers without the field simply don't teach us — no-op.
        """
        try:
            echoed = ((resp.json() or {}).get('observed_ip') or '').strip()
        except Exception:
            return
        if not echoed:
            return
        try:
            from core.peer_link.nat import NATTraversal
            if NATTraversal._is_private_ip(echoed.strip('[]')):
                return
        except Exception:
            return
        if echoed != self._observed_public_ip:
            self._observed_public_ip = echoed
            logger.info(f"Public address learned from announce echo: {echoed}")

    # ─── Exchange ───

    def _exchange_with_peer(self, peer_url):
        try:
            payload = {
                'peers': self._gossip_peer_list(),
                'sender': self._gossip_self_info(),
            }

            # Try PeerLink first (direct WebSocket, no HTTP overhead)
            try:
                peer_id = self._url_to_node_id(peer_url)
                if peer_id:
                    from core.peer_link.link_manager import get_link_manager
                    link = get_link_manager().get_link(peer_id)
                    if link:
                        result = link.send('gossip', payload,
                                          wait_response=True, timeout=10)
                        if result is not None:
                            get_link_manager().record_http_exchange(peer_id)
                            return result.get('peers', [])
            except Exception:
                pass  # Fall through to HTTP

            # HTTP path (original)
            if self.payload_mode == 'msgpack':
                try:
                    import msgpack
                    resp = pooled_post(
                        f"{peer_url}/api/social/peers/exchange",
                        data=self._serialize_payload(payload),
                        headers={'Content-Type': 'application/msgpack'},
                        timeout=10,
                    )
                except ImportError:
                    resp = pooled_post(
                        f"{peer_url}/api/social/peers/exchange",
                        json=payload, timeout=10,
                    )
            else:
                resp = pooled_post(
                    f"{peer_url}/api/social/peers/exchange",
                    json=payload, timeout=10,
                )
            if resp.status_code == 200:
                data = resp.json()
                self._record_peer_success(peer_url)
                # Record exchange for auto-upgrade to PeerLink
                try:
                    peer_id = self._url_to_node_id(peer_url)
                    if peer_id:
                        from core.peer_link.link_manager import get_link_manager
                        get_link_manager().record_http_exchange(peer_id)
                except Exception:
                    pass
                return data.get('peers', [])
            self._record_peer_failure(peer_url)
        except requests.RequestException:
            self._record_peer_failure(peer_url)
        return None

    def _url_to_node_id(self, peer_url: str) -> str:
        """Look up node_id for a peer URL from the DB."""
        try:
            from .models import PeerNode, db_session
            with db_session() as db:
                peer = db.query(PeerNode).filter(
                    PeerNode.url == peer_url).first()
                if peer:
                    return peer.node_id
        except Exception:
            pass
        return ''

    def _ping_peer(self, peer_url):
        if self._is_peer_backed_off(peer_url):
            return False
        try:
            resp = pooled_get(f"{peer_url}/api/social/peers/health", timeout=3)
            if resp.status_code == 200:
                self._record_peer_success(peer_url)
                return True
            self._record_peer_failure(peer_url)
            return False
        except requests.RequestException:
            self._record_peer_failure(peer_url)
            return False

    # ─── Handlers (called by Flask endpoints) ───

    def handle_announce(self, peer_data, reasons=None, observed_ip=''):
        """Process an incoming peer announcement. Returns True if peer was new.

        ``reasons``: optional list; a rejection appends why. False alone is
        ambiguous, it means both "already knew this peer" and "refused it",
        and the caller cannot tell those apart without this.

        ``observed_ip``: the source address the Flask endpoint saw for this
        announce — side-band, NEVER merged into ``peer_data`` (which is
        signed over every field except 'signature').
        """
        from .models import get_db
        db = get_db()
        try:
            is_new = self._merge_peer(db, peer_data, reasons=reasons,
                                      observed_ip=observed_ip)
            db.commit()
            if is_new:
                logger.info(f"New peer discovered: {peer_data.get('name', '')} "
                            f"at {peer_data.get('url', '')}")
            return is_new
        except Exception as e:
            db.rollback()
            logger.debug(f"Announce handler error: {e}")
            if reasons is not None:
                reasons.append(f'announce handler errored: {e}')
            return False
        finally:
            db.close()

    def broadcast(self, message: dict, targets: list = None) -> int:
        """Broadcast a message to active peers via bounded gossip.

        Used by RALT skill distribution, skill queries, resonance deltas,
        upgrade adverts, and world-model events.
        Posts to /api/social/peers/broadcast on each target node.

        Returns number of peers that responded with HTTP 2xx.

        Audit fix #8 (April 2026): previously this counted every HTTP
        response as "sent" — even 404s when the peer didn't implement
        the broadcast endpoint. Callers thought they succeeded when
        they hadn't. Now only 2xx counts, and non-2xx is recorded as
        a peer failure so the back-off path kicks in.

        Bounded fan-out (2026-08-25): this was a SERIAL walk over every
        non-dead peer row at timeout=5 each.  On a node with a polluted
        peer table (desktop: 557 active rows) one call took 30+ minutes,
        and resonance_tick calls it synchronously inside the federation
        tick — the tick thread sat parked in one connect for 80+ minutes
        (py-spy, installed build 13) while agent_daemon's single-flight
        guard blocked every subsequent tick.  Same defect class b8523319
        fixed for broadcast_delta, on the shared primitive it missed.
        Now: class-unroutable rows are skipped, targets=None samples
        gossip_fanout rows (gossip converges over rounds — unsampled
        peers get the next round), delivery is concurrent under one hard
        deadline, and stragglers are abandoned (loss-tolerant).  Explicit
        `targets` skips the sampling — a directed send still reaches
        every named target — but keeps the concurrency and deadline.
        """
        peers = self._load_peers_from_db(exclude_dead=True)
        if targets:
            target_set = set(targets)
            peers = [p for p in peers if p.get('node_id') in target_set]

        rows = []
        for peer in peers:
            url = peer.get('url', '')
            if not url or peer.get('node_id') == self.node_id:
                continue
            unroutable, _reason = is_unroutable_peer_url(url)
            if unroutable:
                continue
            if self._is_peer_backed_off(url):
                continue
            rows.append(url)

        if targets is None and len(rows) > self.gossip_fanout:
            rows = random.sample(rows, self.gossip_fanout)

        def _deliver_one(url):
            try:
                resp = pooled_post(
                    f"{url}/api/social/peers/broadcast",
                    json=message,
                    timeout=5,
                )
                # Only 2xx counts as a successful delivery. 4xx means the
                # peer rejected (rate-limited, missing endpoint, bad
                # payload); 5xx means the peer errored. Either way the
                # payload did NOT land, so don't tell callers it did.
                if 200 <= resp.status_code < 300:
                    self._record_peer_success(url)
                    return True
                self._record_peer_failure(url)
                logger.debug(
                    f"gossip.broadcast to {url}: HTTP "
                    f"{resp.status_code} (type={message.get('type')})")
            except requests.RequestException:
                self._record_peer_failure(url)
            return False

        sent = 0
        if rows:
            from concurrent.futures import ThreadPoolExecutor
            from concurrent.futures import wait as _fwait
            ex = ThreadPoolExecutor(max_workers=min(8, len(rows)))
            try:
                futures = [ex.submit(_deliver_one, u) for u in rows]
                done, not_done = _fwait(futures, timeout=15)
                for f in done:
                    if f.result():
                        sent += 1
                if not_done:
                    logger.debug(
                        f"gossip.broadcast abandoned {len(not_done)} "
                        f"straggler(s) at the 15s deadline "
                        f"(type={message.get('type')})")
            finally:
                ex.shutdown(wait=False, cancel_futures=True)
        return sent

    def handle_exchange(self, their_peers):
        """Process incoming peer list, return our peer list."""
        if their_peers:
            self._merge_peer_list(their_peers)
        return self.get_peer_list()

    # ─── Peer List ───

    def get_peer_list(self):
        """Return all non-dead REMOTE peers, plus this node's LIVE self-info.

        Self is deliberately part of the list (callers render "this node"
        alongside its peers), but it must NEVER be served from the PeerNode
        table.

        Task #596.  The previous version appended _self_info() only when the
        DB did not already contain a row for our own node_id:

            if not any(p.get('node_id') == self.node_id for p in peers):
                peers.append(self_info)

        Once a self-row exists in PeerNode, that guard is False and the fresh
        _self_info() is silently discarded, so the node reports STALE data
        about itself.  Nothing culls such a row either: _load_peers_from_db
        filters on ``PeerNode.status != 'dead'`` — a status column, not a
        last_seen age check — so it survives indefinitely.

        Measured on a live node 2026-08-03: /api/social/peers returned its own
        node_id with endpoint=None, every capability/compute field None, and
        last_seen "2026-04-30T07:16:29" — 95 days stale while the node was
        running and its gossip _send_loop was demonstrably alive.

        Filtering self out of the DB result and unconditionally appending
        _self_info() makes live self-state authoritative, and has the side
        benefit that the self entry now always has ONE shape (_self_info's)
        instead of flip-flopping with PeerNode.to_dict()'s.
        """
        peers = [
            p for p in self._load_peers_from_db(exclude_dead=True)
            if p.get('node_id') != self.node_id
        ]
        peers.append(self._self_info())
        return peers

    def count_remote_peers(self):
        """Number of known non-dead peers EXCLUDING self.

        ``len(get_peer_list())`` is never 0 — self is always in it — so it
        cannot answer "am I actually federated with anyone?".  Callers that
        need that question answered must use this (task #596).
        """
        return sum(
            1 for p in self._load_peers_from_db(exclude_dead=True)
            if p.get('node_id') != self.node_id
        )

    def get_health(self):
        """Return this node's health info for the /health endpoint.

        ``peer_count`` counts REMOTE peers only.  Task #596 follow-up: this
        method loads straight from PeerNode and used to report ``len(peers)``,
        which includes the node's own persisted self-row — so a single machine
        with zero federation partners advertised ``peer_count: 1``.  Measured
        live 2026-08-03: /api/social/peers/health returned peer_count=1 while
        count_remote_peers() returned 0 against the same table.

        That number is user-visible — liquid_ui_service renders
        ``health.peer_count`` directly — and it is also served to other nodes
        over /api/social/peers/health, so the lie propagates across the mesh.
        The key name is kept (consumers depend on it); only the value is
        corrected.
        """
        uptime = (datetime.utcnow() - self.started_at).total_seconds()
        return {
            'node_id': self.node_id,
            'name': self.node_name,
            'version': self.version,
            'uptime_seconds': int(uptime),
            'peer_count': self.count_remote_peers(),
            'agent_count': self._get_count('agent'),
            'post_count': self._get_count('post'),
            'status': 'healthy',
        }

    # ─── Internal Helpers ───

    def _self_info(self):
        info = {
            'node_id': self.node_id,
            'url': self.base_url,
            'name': self.node_name,
            'version': self.version,
            'agent_count': self._get_count('agent'),
            'post_count': self._get_count('post'),
            'timestamp': int(time.time()),
            'tier': self.tier,
            'hart_tag': self._hart_tag,
        }
        # Add cryptographic identity if available
        try:
            from security.node_integrity import get_public_key_hex, compute_code_hash
            info['public_key'] = get_public_key_hex()
            info['code_hash'] = compute_code_hash()
            # Include release manifest info if available
            try:
                from security.master_key import load_release_manifest
                manifest = load_release_manifest()
                if manifest:
                    info['release_version'] = manifest.get('version', '')
                    info['release_manifest_signature'] = manifest.get('master_signature', '')
            except Exception:
                pass
            # Include certificate for regional/central nodes
            try:
                from security.key_delegation import load_node_certificate
                cert = load_node_certificate()
                if cert:
                    info['certificate'] = cert
            except Exception:
                pass
        except Exception:
            pass
        # Include X25519 public key for E2E encryption
        try:
            from security.channel_encryption import get_x25519_public_hex
            info['x25519_public'] = get_x25519_public_hex()
        except Exception:
            pass
        # Include guardrail hash for peer verification
        try:
            from security.hive_guardrails import get_guardrail_hash
            info['guardrail_hash'] = get_guardrail_hash()
        except Exception:
            pass
        # Include HART OS capabilities (contribution tier + enabled features)
        try:
            from security.system_requirements import get_capabilities
            caps = get_capabilities()
            if caps:
                info['capability_tier'] = caps.tier.value
                info['enabled_features'] = caps.enabled_features
                info['hardware_summary'] = {
                    'cpu_cores': caps.hardware.cpu_cores,
                    'ram_gb': caps.hardware.ram_gb,
                    'gpu_vram_gb': caps.hardware.gpu_vram_gb,
                    'disk_free_gb': caps.hardware.disk_free_gb,
                }
        except Exception:
            pass
        # Advertise idle compute availability for distributed task execution
        try:
            from integrations.coding_agent.idle_detection import IdleDetectionService
            from integrations.social.models import get_db
            db = get_db()
            try:
                idle_stats = IdleDetectionService.get_idle_stats(db)
                info['idle_compute'] = {
                    'available': idle_stats.get('currently_idle', 0) > 0,
                    'idle_agents': idle_stats.get('currently_idle', 0),
                    'opted_in': idle_stats.get('total_opted_in', 0),
                }
            finally:
                db.close()
        except Exception:
            pass
        # Advertise version info for autonomous upgrade discovery
        try:
            from integrations.agent_engine.upgrade_orchestrator import get_upgrade_orchestrator
            orch = get_upgrade_orchestrator()
            info['current_version'] = self.version
            status = orch.get_status()
            if status.get('version') and status.get('stage') == 'completed':
                info['available_version'] = status['version']
        except Exception:
            pass
        # Advertise our WAN address when a peer's announce echo taught it to
        # us (_consume_observed_ip_echo — public echoes only).  `url` stays
        # the LAN truth for same-subnet dialing; observed_url is the WAN
        # candidate that nat.py tries next.  MUST sit above the signing
        # block: the signature covers every field except 'signature', so a
        # field added after signing would unverify every announce (the exact
        # defect documented below).
        if self._observed_public_ip:
            try:
                _port = int((self.base_url or '').rsplit(':', 1)[-1])
                _ip = self._observed_public_ip
                _host = f'[{_ip}]' if ':' in _ip else _ip
                info['observed_url'] = f'http://{_host}:{_port}'
            except Exception:
                pass
        # Sign LAST, over the fully populated dict.
        #
        # The signature used to be taken mid-construction, straight after
        # public_key and code_hash were set. Everything below that point
        # (x25519_public, guardrail_hash, capability_tier, enabled_features,
        # hardware_summary, idle_compute, current_version) was added AFTER
        # signing. _merge_peer rebuilds the verification payload as "every
        # field except 'signature'", so it always checked the signature
        # against a superset of what was actually signed, and no announce
        # could ever verify.
        #
        # get_enforcement_mode() defaults to hard, so an unverified peer is
        # rejected outright. That is why the live network held 69 registered
        # nodes while every one of them reported remote_count 0. Nothing
        # surfaced it because peer_announce returns HTTP 200 success:true on
        # all five _merge_peer rejection paths, making a rejection
        # indistinguishable from a duplicate.
        #
        # Signing last makes the sender's covered set exactly equal to the
        # receiver's reconstruction, so unchanged receivers verify these
        # announces. _build_beacon already signs last for the same reason;
        # this brings _self_info onto that one pattern rather than adding
        # another. Same defect and same fix as the federation delta path,
        # where Ed25519 verification ran over a payload that excluded the
        # later-added hmac_signature.
        if info.get('public_key'):
            try:
                from security.node_integrity import sign_json_payload
                info['signature'] = sign_json_payload(info)
            except Exception:
                pass
        return info

    def _seed_initial_peers(self):
        """Insert seed peers into DB if not already present."""
        from .models import get_db, PeerNode
        db = get_db()
        try:
            for url in self.seed_peers:
                existing = db.query(PeerNode).filter(PeerNode.url == url).first()
                if not existing:
                    seed = PeerNode(
                        node_id=f'seed_{uuid.uuid4().hex[:12]}',
                        url=url, name='seed', version='',
                        status='active',
                    )
                    db.add(seed)
            # Also ensure self is in DB
            self_peer = db.query(PeerNode).filter(
                PeerNode.node_id == self.node_id).first()
            if not self_peer:
                self_peer = PeerNode(
                    node_id=self.node_id, url=self.base_url,
                    name=self.node_name, version=self.version,
                    status='active',
                )
                db.add(self_peer)
            db.commit()
        except Exception as e:
            db.rollback()
            logger.debug(f"Seed peers init error: {e}")
        finally:
            db.close()

    def _merge_peer_list(self, peer_list):
        """Merge a list of peer dicts into the DB.

        These are RELAYED records, so they are address hints, not identity
        claims, and are merged with relayed=True. A relayed record physically
        cannot carry proof of who it describes: PeerNode has no signature
        column, so a peer list can only ever republish node_id, url and
        public_key. Judging them by the rules for a direct announce meant
        every third-party record was refused for having no signature, and a
        node could therefore never learn about anyone it had not already
        contacted. The network could only ever be a star around whoever you
        announced to directly.

        Measured against live central: of 72 records returned by
        /api/social/peers/exchange, exactly ONE carried a signature, the
        sender's own live self-info. The other 71 were unusable.

        A hint gets the node an address to try. The direct announce that
        follows carries a live signature and is what actually verifies the
        peer, so nothing is trusted on hearsay: relayed rows land as
        unverified and have to prove themselves on contact.
        """
        from .models import get_db
        db = get_db()
        try:
            new_count = 0
            for p in peer_list:
                if p.get('node_id') and p.get('node_id') != self.node_id:
                    if self._merge_peer(db, p, relayed=True):
                        new_count += 1
            if new_count > 0:
                logger.info(f"Gossip: merged {new_count} new peers")
            db.commit()
        except Exception as e:
            db.rollback()
            logger.debug(f"Merge peer list error: {e}")
        finally:
            db.close()

    @staticmethod
    def _observed_url_for(peer_url: str, observed_ip: str) -> str:
        """The dialable-from-here hint for a peer: observed source IP paired
        with the peer's CLAIMED service port.

        Returns '' when the hint adds nothing: no observed ip, loopback
        (says nothing about where the peer lives), or observed host equal to
        the claimed host (the claim is already the truth — writing it again
        every 30s beacon would only churn the row).  NAT port mapping means
        the claimed port is only guaranteed correct for port-forwarded or
        full-cone peers — which is exactly the population this hint exists
        to make dialable.  It is a dial CANDIDATE, never a trust input.
        """
        if not observed_ip:
            return ''
        ip = observed_ip.strip().strip('[]')
        if ip in ('127.0.0.1', '::1', 'localhost') or ip.startswith('127.'):
            return ''
        try:
            from urllib.parse import urlparse
            parsed = urlparse(peer_url)
            if (parsed.hostname or '').lower() == ip.lower():
                return ''
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        except Exception:
            return ''
        host = f'[{ip}]' if ':' in ip else ip
        return f'http://{host}:{port}'

    @staticmethod
    def _capacity_from_announce(peer_data):
        """Map a signed announce's ``hardware_summary`` onto PeerNode.compute_*.

        WHY THIS EXISTS: ``_self_info`` has always shipped ``hardware_summary``
        (cpu_cores / ram_gb / gpu_vram_gb / disk_free_gb) inside the SIGNED
        payload, and its siblings from the same block — ``capability_tier`` and
        ``enabled_features`` — were persisted. The hardware sub-dict alone was
        never mapped, so ``PeerNode.compute_*`` stayed NULL fleet-wide (0 of 107
        active peers reporting, measured 2026-08-22).

        The consequence is not "no data", it is WRONG data:
        ``ComputeDemocracy.compute_effective_weight`` defaults a missing value to
        1 GPU / 8 GB, so ``raw = 1 * (8/8) = 1`` and EVERY node scores exactly
        1.0. Compute democracy has been a uniform no-op, and ``adjusted_reward``
        scales every payout identically — the 90/9/1 split cannot discriminate
        between a contributor and a freeloader.

        SELF-REPORTED IS ACCEPTABLE HERE, and that is a decision this function
        inherits rather than invents. The code-hash gate three screens up records
        exactly the same reasoning: an unrecognised hash is "recorded, not fatal"
        because rejecting "partitioned the entire network: 69 registered nodes,
        none federating", and because "code_hash is self-reported — the signature
        authenticates that the sender's key asserted the value, not that it
        matches the code actually running". Capacity is the same shape of claim,
        so it gets the same treatment: record it, let provenance carry the doubt.

        The lie is bounded BY DESIGN. ``compute_effective_weight`` is
        ``log2(gpus * ram/8) + 1`` clamped to ``MAX_INFLUENCE_WEIGHT``: a node
        claiming 100x hardware earns ~3x, then hits the cap. And the standing
        rule is that a gate may refuse on evidence of badness, never on absence
        of evidence — refusing every claim because none can be proven is the
        partition failure, not the safe default. Delivered work
        (``gpu_hours_served``, ``total_inferences``) accrues separately and is
        the ground truth that outlives any claim.

        Returns ``{}`` when the announce carries no hardware block, so callers
        can apply it unconditionally without clobbering a known value with None.
        """
        hw = (peer_data or {}).get('hardware_summary') or {}
        if not isinstance(hw, dict) or not hw:
            return {}
        out = {}
        if hw.get('cpu_cores') is not None:
            out['compute_cpu_cores'] = hw['cpu_cores']
        if hw.get('ram_gb') is not None:
            out['compute_ram_gb'] = hw['ram_gb']
        # gpu_vram_gb is VRAM, NOT a device count, and PeerNode has no VRAM
        # column. Mapping one onto the other would be the '#91 wrong-keys' error
        # this codebase already memorialises, so derive PRESENCE only: >0 VRAM
        # means at least one usable GPU. Under-reporting a multi-GPU node is
        # honest; inventing a count from a capacity number is not.
        vram = hw.get('gpu_vram_gb')
        if vram is not None:
            out['compute_gpu_count'] = 1 if (vram or 0) > 0 else 0
        return out

    def _merge_peer(self, db, peer_data, reasons=None, relayed=False,
                    observed_ip=''):
        """Upsert a single peer into PeerNode table. Returns True if new.
        Verifies Ed25519 signature if present. Rejects banned nodes.

        ``observed_ip``: side-band source address from the receiving
        endpoint (never part of the signed payload).  Stored as
        ``metadata_json['observed_url']`` so gossip carries a dialable WAN
        hint for NAT'd peers — the registry held 0/147 routable URLs on
        2026-08-07 because only claimed addresses ever existed.

        ``relayed``: the record came from another node's peer list rather than
        from the node it describes. It is an address hint. A relayed record
        cannot carry a signature (PeerNode stores none), and it is not the
        subject asserting anything, so the gates that test what a node claims
        about ITSELF do not apply: absent signature, and the certificate
        required of a regional or central tier. Those are enforced on the
        direct announce, which is the node speaking for itself. A relayed row
        is always recorded unverified.

        The checks that still apply to a hint are the ones about whether the
        record is safe to hold at all: banned nodes, the per-host Sybil cap,
        a guardrail hash that disagrees with ours, and a signature that IS
        present but does not verify, which is worse than none.

        ``reasons``: optional list. When supplied, a rejection appends the
        reason to it. The return value stays a plain bool so existing callers
        and their assertions are untouched.

        Why this exists: every rejection below returns the same False that a
        duplicate announce returns, and peer_announce reports HTTP 200
        success:true either way. A node could therefore be refused by all five
        gates without anything observable happening, which is exactly how the
        announce-signing defect survived across the whole network. Recording
        the reason costs nothing and makes the next one a single request to
        diagnose instead of a bisect.
        """
        def _reject(reason):
            if reasons is not None:
                reasons.append(reason)
            return False

        from .models import PeerNode
        node_id = peer_data.get('node_id')
        url = peer_data.get('url', '').rstrip('/')
        if not node_id or not url:
            return _reject('node_id and url are both required')
        if node_id == self.node_id:
            return _reject('announcement is from this node itself')

        # Structural gate: a peer whose URL is loopback / docker-bridge / :677
        # can never be dialed as a REMOTE node (it points at self or nowhere).
        # This is the source of the localhost:6777 flood — the Sybil check
        # below deliberately EXEMPTS loopback, so without this gate those rows
        # accumulate without limit and then fool the age-based health check
        # (a local ping to them succeeds). Reject at ingest; #38.
        _bad_url, _bad_why = is_unroutable_peer_url(url)
        if _bad_url:
            return _reject('unroutable peer url (%s)' % _bad_why)

        # Sybil protection: max 5 nodes per IP/hostname.
        # Loopback addresses are exempt - single-user dev installs
        # naturally accumulate many node_ids on localhost (one per
        # reboot / data-dir reset / clean-install), and rejecting
        # them as Sybil is a false positive that floods WARNING logs
        # AND blocks legitimate self-peer registration during testing.
        # Real Sybil attacks come from distinct external IPs.
        try:
            from urllib.parse import urlparse
            host = (urlparse(url).hostname or '').lower()
            _is_loopback = host in (
                'localhost', '127.0.0.1', '::1', '0.0.0.0',
            ) or host.startswith('127.')
            if host and not _is_loopback:
                from .models import PeerNode
                same_host_count = db.query(PeerNode).filter(
                    PeerNode.url.contains(host),
                    PeerNode.integrity_status != 'banned',
                ).count()
                max_per_ip = int(os.environ.get('HEVOLVE_MAX_PEERS_PER_IP', '5'))
                if same_host_count >= max_per_ip:
                    logger.warning(f"Sybil limit: {same_host_count} nodes from {host}, rejecting {node_id[:8]}")
                    return _reject(
                        f'sybil limit: {same_host_count} nodes already '
                        f'registered from {host}, max {max_per_ip}')
        except Exception:
            pass  # URL parsing failed — proceed with other checks

        # Reject banned nodes
        existing = db.query(PeerNode).filter(PeerNode.node_id == node_id).first()
        if existing and existing.integrity_status == 'banned':
            logger.debug(f"Rejecting banned node: {node_id[:8]}")
            return _reject('node is banned')

        # Verify signature if present (backward-compatible: unsigned peers accepted as 'unverified')
        signature = peer_data.get('signature')
        public_key = peer_data.get('public_key')
        signature_valid = False
        if signature and public_key:
            try:
                from security.node_integrity import verify_json_signature
                # Build payload without signature for verification
                payload = {k: v for k, v in peer_data.items() if k != 'signature'}
                signature_valid = verify_json_signature(public_key, payload, signature)
                if not signature_valid:
                    logger.warning(f"Invalid signature from node {node_id[:8]} at {url}")
                    return _reject(
                        'signature does not verify over the announced payload '
                        '(receiver checks every field except "signature"; a '
                        'sender that signs before appending fields fails here)')
            except ImportError:
                pass  # crypto module not available, accept unsigned
            except Exception as e:
                logger.warning(f"Unexpected error verifying signature for {node_id[:8]}: {e}")
                return _reject(f'signature verification errored: {e}')

        integrity_status = 'verified' if signature_valid else 'unverified'

        # Enforcement gate: reject unsigned peers in hard mode.
        # Skipped for relayed hints, which cannot carry one by construction.
        if not signature_valid and not relayed:
            try:
                from security.master_key import get_enforcement_mode
                enforcement = get_enforcement_mode()
                if enforcement == 'hard':
                    logger.warning(f"Rejecting unsigned peer {node_id[:8]} (enforcement=hard)")
                    return _reject(
                        'peer carries no usable signature and enforcement '
                        'mode is hard')
                elif enforcement == 'soft':
                    logger.info(f"Unsigned peer {node_id[:8]} accepted (enforcement=soft)")
            except ImportError:
                pass  # No enforcement module = dev mode, accept all

        # Guardrail hash verification: reject peers with different guardrail values
        peer_guardrail_hash = peer_data.get('guardrail_hash', '')
        if peer_guardrail_hash:
            try:
                from security.hive_guardrails import get_guardrail_hash
                local_guardrail_hash = get_guardrail_hash()
                if peer_guardrail_hash != local_guardrail_hash:
                    logger.warning(
                        f"Rejecting peer {node_id[:8]}: guardrail hash mismatch")
                    return _reject(
                        f'guardrail hash mismatch: peer '
                        f'{peer_guardrail_hash[:16]}, local '
                        f'{local_guardrail_hash[:16]}')
            except Exception:
                pass

        # Code hash verification: check against release hash registry (multi-version)
        # then fall back to current manifest
        master_key_verified = False
        hash_trusted_source = 'untrusted'
        peer_code_hash = peer_data.get('code_hash', '')
        if peer_code_hash:
            # Priority 1: Release hash registry (supports rolling upgrades)
            try:
                from security.release_hash_registry import get_release_hash_registry
                registry = get_release_hash_registry()
                if registry.is_known_release_hash(peer_code_hash):
                    master_key_verified = True
                    hash_trusted_source = 'registry'
            except Exception:
                pass

            # Priority 2: Current release manifest (fallback if registry unavailable)
            if not master_key_verified:
                try:
                    from security.master_key import load_release_manifest
                    manifest = load_release_manifest()
                    if manifest:
                        expected_hash = manifest.get('code_hash', '')
                        if peer_code_hash == expected_hash:
                            master_key_verified = True
                            hash_trusted_source = 'manifest'
                except Exception:
                    pass

            # An unrecognised code hash is recorded, not fatal.
            #
            # This gate used to reject under enforcement=hard and it
            # partitioned the entire network: 69 registered nodes, none
            # federating. Three separate things make it unworkable as a hard
            # gate, and none of them are fixed by publishing more hashes.
            #
            # It cannot bootstrap. _KNOWN_HASHES ships empty, so a stock
            # desktop knows no hashes and refuses everyone. The runtime
            # discovery path only learns from peers that already passed, so
            # nothing can ever be the first to pass.
            #
            # It cannot span builds. Central signs a release manifest at
            # deploy time covering its own container, so its only trusted
            # hash is its own. A frozen desktop bundle is a different file
            # set and hashes differently by construction, so central would
            # refuse every desktop no matter how many GA hashes were
            # published. Live proof: central 61c1dd94, this desktop 2d66b241.
            #
            # It never stopped an attacker anyway. code_hash is self-reported.
            # The signature authenticates that the sender's key asserted the
            # value, not that it matches the code actually running, so a
            # hostile node simply claims a known-good hash. The gate only ever
            # turned away honest nodes on unpublished builds.
            #
            # So the peer is admitted with master_key_verified False and
            # hash_trusted_source untrusted, exactly the values it would have
            # carried before. Every downstream trust decision (visibility
            # tier, contribution scoring, fraud_score, and the
            # challenge/attestation endpoints, which DO prove running code)
            # sees what it saw before and can still act on it. Nothing that
            # was verified becomes unverified; something that was refused
            # becomes recorded-and-untrusted.
            #
            # Operators who genuinely want strict provenance, e.g. a locked
            # regional cluster where every node runs one published build, set
            # HEVOLVE_REQUIRE_KNOWN_CODE_HASH=1. That is guarded by
            # has_trust_basis() so it cannot be switched on into a vacuum and
            # silently partition the cluster it was meant to protect.
            if not master_key_verified:
                _strict = os.environ.get(
                    'HEVOLVE_REQUIRE_KNOWN_CODE_HASH', '').lower() in (
                        '1', 'true', 'yes')
                try:
                    from security.release_hash_registry import (
                        get_release_hash_registry as _reg)
                    _have_basis = _reg().has_trust_basis()
                except Exception:
                    _have_basis = False
                if _strict and _have_basis:
                    logger.warning(
                        f"Rejecting peer {node_id[:8]}: unknown code hash "
                        f"{peer_code_hash[:16]}... "
                        f"(HEVOLVE_REQUIRE_KNOWN_CODE_HASH=1)")
                    return _reject(
                        f'code hash {peer_code_hash[:16]} is not in the '
                        f'release hash registry or current manifest, and '
                        f'HEVOLVE_REQUIRE_KNOWN_CODE_HASH is set')
                logger.info(
                    f"Peer {node_id[:8]} code hash {peer_code_hash[:16]} is "
                    f"unrecognised; admitting as untrusted "
                    f"(master_key_verified=False)")

        # Certificate verification for peers claiming regional/central tier
        peer_tier = peer_data.get('tier', 'flat')
        certificate = peer_data.get('certificate')
        certificate_verified = False
        if peer_tier in ('regional', 'central') and not certificate and not relayed:
            logger.warning(f"Rejecting {node_id[:8]}: {peer_tier} tier requires certificate")
            return _reject(f'tier {peer_tier} requires a certificate, none sent')
        if peer_tier in ('regional', 'central') and certificate:
            try:
                from security.key_delegation import verify_certificate_chain
                from security.master_key import get_enforcement_mode
                chain_result = verify_certificate_chain(certificate)
                certificate_verified = chain_result['valid']
                enforcement = get_enforcement_mode()
                if not certificate_verified:
                    # Always reject invalid certificates for regional/central tiers
                    if peer_tier in ('regional', 'central'):
                        logger.warning(f"Rejecting peer {node_id[:8]}: {peer_tier} tier requires valid certificate")
                        return _reject(
                            f'tier {peer_tier} certificate failed chain '
                            f'verification')
                    if enforcement == 'hard':
                        logger.warning(f"Rejecting peer {node_id[:8]}: invalid certificate "
                                      f"for tier={peer_tier} (enforcement=hard)")
                        return _reject(
                            f'certificate invalid for tier {peer_tier} and '
                            f'enforcement mode is hard')
                    else:
                        logger.warning(f"Peer {node_id[:8]} has invalid certificate "
                                      f"for tier={peer_tier} (enforcement={enforcement})")
            except Exception as e:
                logger.debug(f"Certificate verification error for {node_id[:8]}: {e}")

        if relayed:
            # A hint carries no proof of anything, so it must not inherit
            # trust from what it happens to assert. The code hash and
            # certificate above were read from a record some OTHER node
            # republished; treating either as established would let a peer
            # list launder trust for nodes nobody has spoken to. These get
            # set for real when the node announces itself and its live
            # signature verifies.
            master_key_verified = False
            certificate_verified = False
            integrity_status = 'unverified'

        if existing:
            existing.last_seen = datetime.utcnow()
            existing.url = url
            existing.name = peer_data.get('name', existing.name)
            existing.version = peer_data.get('version', existing.version)
            existing.agent_count = peer_data.get('agent_count', existing.agent_count)
            existing.post_count = peer_data.get('post_count', existing.post_count)
            # Update integrity fields
            if public_key:
                existing.public_key = public_key
            if peer_data.get('code_hash'):
                existing.code_hash = peer_data['code_hash']
            if peer_data.get('version'):
                existing.code_version = peer_data['version']
            if signature_valid:
                existing.integrity_status = 'verified'
            # A relayed hint must not DOWNGRADE a peer that already proved
            # itself with a direct signed announce. It carries no evidence
            # either way, and letting hearsay clear master_key_verified,
            # certificate_verified or tier would mean any node could strip a
            # verified peer's standing just by republishing a stale row.
            # Hints may add a peer we did not know; they may not unmake one.
            if not relayed:
                existing.master_key_verified = master_key_verified
                existing.tier = peer_tier
                if certificate:
                    existing.certificate_json = certificate
                    existing.certificate_verified = certificate_verified
                # Capacity, from the node speaking for ITSELF only. Same gate as
                # the fields above and for the same reason: a relayed hint is a
                # third party republishing a row, so letting it set compute_*
                # would let any node inflate (or zero out) a peer's standing in
                # ComputeDemocracy just by gossiping about it.
                for _k, _v in self._capacity_from_announce(peer_data).items():
                    setattr(existing, _k, _v)
            if peer_data.get('release_version'):
                existing.release_version = peer_data['release_version']
            # Update capability tier from HART OS equilibrium
            if peer_data.get('capability_tier'):
                existing.capability_tier = peer_data['capability_tier']
            if peer_data.get('enabled_features'):
                existing.enabled_features_json = peer_data['enabled_features']
            # Update X25519 public key for E2E encryption
            if peer_data.get('x25519_public'):
                existing.x25519_public = peer_data['x25519_public']
            if existing.status == 'dead':
                # Only resurrect if announcement is recent (not stale gossip)
                if (datetime.utcnow() - existing.last_seen).total_seconds() < 60:
                    existing.status = 'active'
            # Direct announces update the observed-address hint; relayed
            # records carry the RELAYER's vantage, not the subject's, so
            # they must not overwrite what a direct announce established.
            if not relayed:
                _obs = self._observed_url_for(url, observed_ip)
                _meta = existing.metadata_json or {}
                if _obs and _meta.get('observed_url') != _obs:
                    # Reassign (not mutate) so SQLAlchemy sees the change.
                    existing.metadata_json = {**_meta, 'observed_url': _obs}
            return False

        _obs = '' if relayed else self._observed_url_for(url, observed_ip)
        _new_meta = dict(peer_data.get('metadata', {}) or {})
        if _obs:
            _new_meta['observed_url'] = _obs
        new_peer = PeerNode(
            node_id=node_id, url=url,
            name=peer_data.get('name', ''),
            version=peer_data.get('version', ''),
            status='active',
            agent_count=peer_data.get('agent_count', 0),
            post_count=peer_data.get('post_count', 0),
            metadata_json=_new_meta,
            public_key=public_key or '',
            code_hash=peer_data.get('code_hash', ''),
            code_version=peer_data.get('version', ''),
            integrity_status=integrity_status,
            master_key_verified=master_key_verified,
            release_version=peer_data.get('release_version', ''),
            tier=peer_tier,
            certificate_json=certificate,
            certificate_verified=certificate_verified,
            capability_tier=peer_data.get('capability_tier'),
            enabled_features_json=peer_data.get('enabled_features'),
            x25519_public=peer_data.get('x25519_public', ''),
            # Capacity only from a DIRECT announce — see _capacity_from_announce
            # and the `if not relayed` gate in the update branch above. A first
            # sighting via someone else's peer list is an address hint; it must
            # not seed ComputeDemocracy weighting with hearsay.
            **({} if relayed else self._capacity_from_announce(peer_data)),
        )
        db.add(new_peer)

        # ─── Auto-federation ───
        # Peer passed verification, so follow it now rather than waiting for an
        # operator to do it by hand. Trust is re-checked by the integrity round
        # below, which can mark the peer dead later.
        threading.Thread(
            target=self._auto_federate_peer,
            args=(node_id, url),
            daemon=True,
        ).start()

        return True

    def _auto_federate_peer(self, peer_node_id: str, peer_url: str):
        """Auto-follow a newly accepted peer so its content starts flowing.
        Runs on its own thread; failures are logged and dropped."""
        try:
            from .models import get_db
            from .federation import federation
            db = get_db()
            try:
                # Follow them (we receive their content)
                federation.follow_instance(db, self.node_id, peer_node_id, peer_url)
                db.commit()
                logger.info(f"Mind merge: auto-federated with {peer_node_id[:8]} at {peer_url}")
            except Exception as e:
                db.rollback()
                logger.debug(f"Auto-federation failed for {peer_node_id[:8]}: {e}")
            finally:
                db.close()
        except Exception:
            pass

    # ─── Integrity Round ───

    def _integrity_round(self):
        """Periodic integrity check: continuous audit using ALL active nodes.
        Every node audits every other node it can reach - not just one random peer.
        Valid connections are a breeze; continuous audit is the price of trust."""
        # Self-check: verify own code integrity before challenging others
        try:
            from security.runtime_monitor import is_code_healthy
            if not is_code_healthy():
                logger.critical("Integrity round: local code tampered, stopping gossip")
                self.stop()
                return
        except Exception:
            pass

        # Self-check: verify own guardrail integrity
        try:
            from security.hive_guardrails import verify_guardrail_integrity
            if not verify_guardrail_integrity():
                logger.critical("Integrity round: guardrail integrity failed, stopping gossip")
                self.stop()
                return
        except Exception:
            pass

        from .models import get_db, PeerNode
        db = get_db()
        try:
            # 0. Forgiveness sweep — decay fraud scores and release EXPIRED
            #    bans.  This round has always run the punitive half
            #    (challenges, guardrail audits) every integrity_interval while
            #    apply_fraud_score_decay — whose docstring believed it ran
            #    "every ~5 minutes via AgentDaemon integrity tick" — had no
            #    scheduled caller at all: its only route was a manually-hit
            #    admin endpoint.  Measured live 2026-08-07: this node held 72
            #    'banned' rows with ban_until dates back to MARCH, the LAN
            #    peer held 69, and one on-demand audit released them all.
            #    Bans that outlive their own expiry strangled every
            #    federation path (banned peers' announces AND inbox posts are
            #    rejected, so a ban can never heal itself).  Runs FIRST so a
            #    just-released peer rejoins this same round's audit.
            try:
                from .integrity_service import IntegrityService as _IS
                _decay = _IS.apply_fraud_score_decay(db)
                if _decay.get('unbanned_count'):
                    logger.info(
                        f"Integrity round: released "
                        f"{_decay['unbanned_count']} expired ban(s), "
                        f"decayed {_decay.get('decayed_count', 0)} score(s)")
                db.commit()
            except Exception as _e:
                db.rollback()
                # WARNING, not debug. The hevolve_social logger runs at WARNING
                # in production, so a debug line here is invisible and a sweep
                # that throws on every round looks identical to one that has
                # nothing to do.
                logger.warning("Integrity round decay sweep failed (%s: %s)",
                               type(_e).__name__, _e)

            # 0b. Retention sweep — integrity_challenges is append-only and had
            #     no expiry, so it grew to 300,092 rows / 386 MB (plus ~60 MB of
            #     indexes) by 2026-09-01: 72% of central's 618 MB database.
            #     That size is what broke the write path — the WAL checkpointer
            #     could not finish against a file that large under continuous
            #     daemon writes, so every writer blocked past busy_timeout and
            #     the daemon logged "database is locked" on each tick, leaving
            #     goal status updates unpersisted and goals re-dispatching
            #     forever.  Sits here, beside the decay sweep, because that is
            #     the one scheduled maintenance point this round has.
            try:
                from .integrity_service import IntegrityService as _IS2
                _pruned = _IS2.prune_challenge_history(db)
                if _pruned.get('deleted_total'):
                    logger.info(
                        f"Integrity round: pruned "
                        f"{_pruned['deleted_total']} resolved challenge(s)"
                        + (" (backlog remains)"
                           if _pruned.get('more_remaining') else ""))
            except Exception as _e:
                db.rollback()
                # WARNING, not debug — and this one bit me directly. On central
                # the sweep ran twice (300,092 -> 290,092 -> 280,092) and then
                # stopped, with 135,869 rows still eligible and NOTHING in the
                # logs either way: the success line is INFO and this failure
                # line was debug, both invisible at the production WARNING
                # level. The only observable was a row count that would not
                # move, which is not a diagnosis.
                #
                # I wrote that blind spot into this file today while telling
                # other agents that absence of an INFO log proves nothing.
                logger.warning("Integrity round retention sweep failed (%s: %s)",
                               type(_e).__name__, _e)

            active_peers = db.query(PeerNode).filter(
                PeerNode.status == 'active',
                PeerNode.node_id != self.node_id,
                PeerNode.integrity_status != 'banned',
            ).all()

            if active_peers:
                from .integrity_service import IntegrityService

                # 1. Guardrail audit: re-verify ALL active peers' guardrail hashes.
                #    This is the continuous audit - every node checks every other node.
                #
                #    Commit after EVERY peer, in this loop and the next.  Both
                #    make one network call per active peer and used to commit
                #    once after the whole loop, so from the first fraud-score
                #    flush onward the round held SQLite's single write lock
                #    across every remaining probe (5s GET here, a 30s POST
                #    below).  Measured on a desktop 2026-09-02: 525 active
                #    peers, most unroutable, one round ran for hours,
                #    integrity_interval is 300s, and the agent daemon logged
                #    "database is locked" on every tick with zero goal updates
                #    persisted (#71).  Same shape, same fix as the health
                #    round (374f5ab6); create_challenge also commits its own
                #    row before it POSTs.
                for peer in active_peers:
                    self._audit_peer_guardrails(db, peer)
                    self._flush_health_row(db, round_name='Integrity round')
                    self._heartbeat()

                # 2. Deep challenge: cycle through challenge types across all peers.
                #    Each peer gets a different challenge type per round (round-robin).
                challenge_types = ['agent_count_verify', 'code_hash_check',
                                   'stats_probe', 'guardrail_verify']
                for i, peer in enumerate(active_peers):
                    challenge_type = challenge_types[i % len(challenge_types)]
                    try:
                        IntegrityService.create_challenge(
                            db, self.node_id, peer.node_id,
                            peer.url, challenge_type)
                    except Exception as e:
                        logger.debug(f"Challenge to {peer.node_id[:8]} failed: {e}")
                    self._flush_health_row(db, round_name='Integrity round')
                    self._heartbeat()
                try:
                    db.commit()
                except Exception:
                    db.rollback()

                # 3. Run full fraud detection on ALL active peers
                for peer in active_peers:
                    try:
                        IntegrityService.detect_impression_anomaly(db, peer.node_id)
                        IntegrityService.detect_score_jump(db, peer.node_id)
                        IntegrityService.detect_collusion(db, peer.node_id)
                    except Exception as e:
                        logger.debug(f"Fraud detection for {peer.node_id[:8]} failed: {e}")
                try:
                    db.commit()
                except Exception:
                    db.rollback()

            # 4. Verify audit compute dominance — no node can outcompute its auditors
            try:
                from .integrity_service import IntegrityService
                for peer in active_peers:
                    IntegrityService.verify_audit_dominance(db, peer.node_id)
                db.commit()
            except Exception as e:
                db.rollback()
                logger.debug(f"Audit dominance check failed: {e}")

            # 5. Pull registry ban list if configured
            registry_url = os.environ.get('HEVOLVE_REGISTRY_URL', '')
            if registry_url:
                try:
                    from .integrity_service import IntegrityService
                    banned_ids = IntegrityService.check_registry_ban_list(registry_url)
                    if banned_ids:
                        for nid in banned_ids:
                            peer = db.query(PeerNode).filter_by(node_id=nid).first()
                            if peer and peer.integrity_status != 'banned':
                                peer.integrity_status = 'banned'
                                logger.info(f"Node {nid[:8]} banned via registry")
                        db.commit()
                except Exception as e:
                    logger.debug(f"Registry ban list check failed: {e}")

        except Exception as e:
            logger.debug(f"Integrity round error: {e}")
        finally:
            db.close()

    def _audit_peer_guardrails(self, db, peer):
        """Re-verify a peer's guardrail hash by directly querying it.
        This is the continuous audit — every node verifies every other node."""
        try:
            resp = pooled_get(
                f"{peer.url}/api/social/integrity/guardrail-hash",
                timeout=5,
            )
            if resp.status_code != 200:
                return  # Endpoint might not exist on older nodes

            data = resp.json()
            peer_hash = data.get('guardrail_hash', '')
            if not peer_hash:
                return

            from security.hive_guardrails import get_guardrail_hash
            local_hash = get_guardrail_hash()

            if peer_hash != local_hash:
                logger.warning(
                    f"Continuous audit: guardrail drift detected on "
                    f"{peer.node_id[:8]} — disconnecting")
                from .integrity_service import IntegrityService
                IntegrityService.increase_fraud_score(
                    db, peer.node_id, 50.0,
                    'Guardrail hash drift detected during continuous audit',
                    {'expected': local_hash[:16], 'got': peer_hash[:16]})
                # Severe: unfollow from federation immediately
                try:
                    from .federation import federation
                    federation.unfollow_instance(db, self.node_id, peer.node_id)
                except Exception:
                    pass
            else:
                # Peer passed — reward good behavior
                from .integrity_service import IntegrityService
                IntegrityService.decrease_fraud_score(
                    db, peer.node_id, 1.0,
                    'Guardrail audit passed')
        except requests.RequestException:
            pass  # Network issue — will catch it next round
        except Exception as e:
            logger.debug(f"Guardrail audit for {peer.node_id[:8]} error: {e}")

    def _load_peers_by_tier(self):
        """Load gossip targets scoped to this node's tier."""
        from .models import get_db
        db = get_db()
        try:
            from .hierarchy_service import HierarchyService
            return HierarchyService.get_gossip_targets(db, self.node_id, self.tier)
        except Exception:
            return []
        finally:
            db.close()

    def _load_peers_from_db(self, exclude_dead=True):
        from .models import get_db, PeerNode
        db = get_db()
        try:
            q = db.query(PeerNode)
            if exclude_dead:
                q = q.filter(PeerNode.status != 'dead')
            peers = q.all()
            return [p.to_dict() for p in peers]
        except Exception:
            return []
        finally:
            db.close()

    def _get_count(self, what):
        # Delegates to module-level _cached_node_count (30s hard TTL) so repeated
        # gossip rounds don't re-run the COUNT query every tick (2026-06-13 dig).
        try:
            return _cached_node_count(what)
        except Exception:
            return 0


# ═══════════════════════════════════════════════════════════════════════
# AutoDiscovery — Zero-Config LAN Peer Finding via UDP Broadcast
# ═══════════════════════════════════════════════════════════════════════

class AutoDiscovery:
    """LAN-based zero-config peer discovery using UDP broadcast.

    After boot verification, broadcasts a signed beacon every 30s on UDP port 6780.
    Listens for beacons from other nodes on the same network.
    Discovered peers are fed into GossipProtocol as additional seeds.

    This is ADDITIVE — works alongside seed peers and registry.
    """

    BEACON_MAGIC = b'HEVOLVE_DISCO_V1'
    MAX_PACKET_SIZE = 2048

    def __init__(self, gossip_protocol: GossipProtocol,
                 port: int = None, beacon_interval: int = None):
        self._gossip = gossip_protocol
        from core.port_registry import get_port
        # Legacy env var takes precedence for backward compat
        _legacy = os.environ.get('HEVOLVE_DISCOVERY_PORT')
        self._port = port or (int(_legacy) if _legacy else get_port('discovery'))
        self._beacon_interval = beacon_interval or int(
            os.environ.get('HEVOLVE_DISCOVERY_INTERVAL', '30'))
        self._running = False
        self._send_thread = None
        self._recv_thread = None
        self._lock = threading.Lock()
        # First-beacon dedup (suppresses re-logging + re-gossiping the same
        # node every ~30s beacon).  Was an unbounded `set` that only ever grew
        # — on a long-running node in a churny LAN (peers cycling through new
        # node_ids) it leaked memory without bound (#83).  TTLCache caps size
        # (FIFO-evicts the oldest) and expires entries after a TTL, so a node
        # absent for the TTL is simply re-discovered (re-logged once) if it
        # returns — correct + bounded.  Accessed only from the single recv
        # thread, so no extra locking needed.
        self._discovered_nodes = TTLCache(
            ttl_seconds=int(os.environ.get('HEVOLVE_DISCOVERY_DEDUP_TTL', '3600')),
            max_size=int(os.environ.get('HEVOLVE_DISCOVERY_DEDUP_MAX', '2048')),
            name='peer_discovered_nodes')
        self._sock = None
        # Cached list of broadcast addresses (one per usable IPv4 NIC).
        # Refreshed on start; iterated each beacon send.  Replaces the
        # naive `<broadcast>` (255.255.255.255) target which on multi-NIC
        # Windows boxes (Wi-Fi + Hyper-V + VMware + Docker virtual NICs)
        # leaves the box on a single OS-chosen interface — usually a
        # virtual subnet, not the physical LAN where peers actually live.
        self._broadcast_targets: list = []

    def start(self) -> None:
        """Start beacon sender and listener threads."""
        import socket as _socket
        with self._lock:
            if self._running:
                return
            self._running = True

        try:
            self._sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            self._sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_BROADCAST, 1)
            self._sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            self._sock.bind(('', self._port))
            self._sock.settimeout(2.0)
        except OSError as e:
            logger.warning(f"AutoDiscovery: cannot bind UDP port {self._port}: {e}")
            self._running = False
            return

        # Enumerate per-NIC broadcast addresses.  Always include the
        # limited-broadcast 255.255.255.255 as a fallback.
        self._broadcast_targets = self._enumerate_broadcast_targets()
        logger.info(f"AutoDiscovery broadcast targets: "
                    f"{', '.join(self._broadcast_targets) or '<broadcast>'}")

        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()
        self._send_thread = threading.Thread(target=self._send_loop, daemon=True)
        self._send_thread.start()
        logger.info(f"AutoDiscovery started on UDP port {self._port} "
                    f"(interval={self._beacon_interval}s)")

    # NIC name patterns that indicate a virtual/tunnel adapter we
    # should NOT broadcast onto.  Case-insensitive substring match.
    # Catches WSL/Hyper-V vSwitch, VMware/VirtualBox host-only adapters,
    # Bluetooth PAN, Docker bridges, and Windows loopback.
    _VIRTUAL_NIC_HINTS = (
        'loopback', 'pseudo', 'bluetooth', 'vethernet', 'wsl',
        'hyper-v', 'vmware', 'virtualbox', 'vbox', 'docker',
        'tap-', 'tun', 'npcap',
    )

    @staticmethod
    def _derive_broadcast(addr: str, netmask: str) -> str:
        """Compute IPv4 broadcast = addr | ~netmask.  Returns '' on parse failure."""
        try:
            a = [int(x) for x in addr.split('.')]
            m = [int(x) for x in netmask.split('.')]
            if len(a) != 4 or len(m) != 4:
                return ''
            bcast = [(a[i] | (~m[i] & 0xFF)) for i in range(4)]
            return '.'.join(str(b) for b in bcast)
        except Exception:
            return ''

    def _enumerate_broadcast_targets(self) -> list:
        """Return one broadcast address per usable IPv4 NIC.

        On Windows, ``sock.sendto((b'…', '<broadcast>', port))`` only
        traverses the OS-chosen default-route interface.  On boxes with
        multiple physical/virtual NICs this is roulette — the beacon
        often leaves on a virtual NIC the LAN peers aren't on.

        Implementation notes:
        - psutil returns ``snic.broadcast = None`` on Windows even for
          NICs with valid IPv4 addresses, so we derive broadcast from
          ``address | ~netmask`` ourselves.
        - We skip virtual / tunnel NICs by name (Bluetooth, vEthernet,
          WSL, Hyper-V, VMware, Docker, loopback) so a beacon never
          leaks into a virtual subnet our LAN peers aren't on.
        - Fallback: if no usable NIC is found, return the limited
          broadcast so degraded environments still emit something.
        """
        try:
            import psutil
        except ImportError:
            return ['255.255.255.255']
        targets = []
        try:
            stats = {}
            try:
                stats = psutil.net_if_stats()
            except Exception:
                pass
            for nic_name, addrs in psutil.net_if_addrs().items():
                # Skip virtual/tunnel NICs by name pattern.
                lower_name = nic_name.lower()
                if any(hint in lower_name for hint in self._VIRTUAL_NIC_HINTS):
                    continue
                # Skip if the NIC is down (when stats are available).
                nic_stat = stats.get(nic_name)
                if nic_stat is not None and not nic_stat.isup:
                    continue
                for snic in addrs:
                    if getattr(snic, 'family', None) is None:
                        continue
                    fam_val = int(snic.family) if hasattr(snic.family, 'value') else snic.family
                    if fam_val != 2:  # AF_INET
                        continue
                    addr = snic.address or ''
                    netmask = snic.netmask or ''
                    bcast = snic.broadcast or ''
                    # Skip loopback (127.x) and APIPA (169.254.x).
                    if addr.startswith('127.') or addr.startswith('169.254.'):
                        continue
                    # Derive broadcast on Windows (psutil leaves it None).
                    if not bcast and netmask:
                        bcast = self._derive_broadcast(addr, netmask)
                    if not bcast:
                        continue
                    if bcast in ('0.0.0.0', '255.255.255.255'):
                        continue  # Treat as "no useful broadcast"
                    if bcast not in targets:
                        targets.append(bcast)
        except Exception as e:
            logger.debug(f"AutoDiscovery NIC enumeration error: {e}")
        # Always keep limited broadcast as a final fallback so a host
        # without psutil-readable NICs (rare degraded environments) still
        # gets at least one outbound packet.
        if '255.255.255.255' not in targets:
            targets.append('255.255.255.255')
        return targets

    def stop(self) -> None:
        """Stop discovery threads and close socket."""
        with self._lock:
            self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

    def _build_beacon(self) -> bytes:
        """Build a signed beacon packet: MAGIC + JSON payload."""
        import json as _json
        payload = {
            'type': 'hevolve-discovery',
            'node_id': self._gossip.node_id,
            'url': self._gossip.base_url,
            'name': self._gossip.node_name,
            'version': self._gossip.version,
            'tier': self._gossip.tier,
            'timestamp': int(time.time()),
        }
        try:
            from security.hive_guardrails import get_guardrail_hash
            payload['guardrail_hash'] = get_guardrail_hash()
        except Exception:
            pass
        try:
            from security.node_integrity import (
                get_public_key_hex, compute_code_hash, sign_json_payload,
            )
            payload['public_key'] = get_public_key_hex()
            payload['code_hash'] = compute_code_hash()
        except Exception:
            pass
        # Include release version if manifest available
        try:
            from security.master_key import load_release_manifest
            manifest = load_release_manifest()
            if manifest:
                payload['release_version'] = manifest.get('version', '')
        except Exception:
            pass
        # Include X25519 public key for E2E encryption
        try:
            from security.channel_encryption import get_x25519_public_hex
            payload['x25519_public'] = get_x25519_public_hex()
        except Exception:
            pass
        # Include robot capabilities for fleet dispatch
        try:
            from integrations.robotics.capability_advertiser import (
                get_capability_advertiser,
            )
            adv = get_capability_advertiser()
            payload['robot_capabilities'] = adv.get_gossip_payload()
        except Exception:
            pass
        try:
            from security.node_integrity import sign_json_payload
            payload['signature'] = sign_json_payload(payload)
        except Exception:
            pass

        json_bytes = _json.dumps(payload, separators=(',', ':')).encode('utf-8')
        return self.BEACON_MAGIC + json_bytes

    def _parse_beacon(self, data: bytes) -> dict:
        """Parse and verify a beacon packet. Returns payload dict or empty dict."""
        import json as _json
        if not data.startswith(self.BEACON_MAGIC):
            return {}
        try:
            json_bytes = data[len(self.BEACON_MAGIC):]
            payload = _json.loads(json_bytes.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return {}

        # Untrusted UDP input: a valid-JSON NON-dict (e.g. b'[1,2,3]' or b'"x"')
        # would sail past the except above, then the payload.get(...) calls below
        # raise AttributeError — which is NOT caught here and would bubble into
        # the recv loop. Reject anything that isn't a dict explicitly.
        if not isinstance(payload, dict):
            return {}

        if payload.get('type') != 'hevolve-discovery':
            return {}
        # node_id is REQUIRED by the beacon spec and is used downstream as the
        # dedup key and as `node_id[:8]` in the recv-loop log line. A beacon
        # missing it (or carrying a non-string) would otherwise sail past here
        # and crash the recv loop at node_id[:8]; reject it now, same as the
        # type guard above (completes the malformed-beacon hardening).
        node_id = payload.get('node_id')
        if not isinstance(node_id, str) or not node_id:
            return {}
        if node_id == self._gossip.node_id:
            return {}

        # Verify guardrail hash
        peer_hash = payload.get('guardrail_hash', '')
        if peer_hash:
            try:
                from security.hive_guardrails import get_guardrail_hash
                if peer_hash != get_guardrail_hash():
                    logger.debug(f"AutoDiscovery: rejecting beacon from "
                                 f"{payload.get('node_id', '?')[:8]}: guardrail mismatch")
                    return {}
            except Exception:
                pass

        # Verify code hash against release hash registry
        peer_code_hash = payload.get('code_hash', '')
        if peer_code_hash:
            try:
                from security.release_hash_registry import get_release_hash_registry
                from security.master_key import get_enforcement_mode
                registry = get_release_hash_registry()
                if not registry.is_known_release_hash(peer_code_hash):
                    enforcement = get_enforcement_mode()
                    if enforcement == 'hard':
                        logger.warning(
                            f"AutoDiscovery: rejecting beacon from "
                            f"{payload.get('node_id', '?')[:8]}: "
                            f"unknown code hash {peer_code_hash[:16]}...")
                        return {}
                    elif enforcement in ('soft', 'warn'):
                        logger.info(
                            f"AutoDiscovery: unknown code hash from "
                            f"{payload.get('node_id', '?')[:8]} "
                            f"(enforcement={enforcement})")
            except Exception:
                pass

        # Verify Ed25519 signature
        sig = payload.get('signature')
        pubkey = payload.get('public_key')
        if sig and pubkey:
            try:
                from security.node_integrity import verify_json_signature
                clean = {k: v for k, v in payload.items() if k != 'signature'}
                if not verify_json_signature(pubkey, clean, sig):
                    logger.warning(f"AutoDiscovery: invalid signature from "
                                   f"{payload.get('node_id', '?')[:8]}")
                    return {}
            except Exception:
                pass

        # Reject stale beacons (> 5 minutes old)
        ts = payload.get('timestamp', 0)
        if abs(time.time() - ts) > 300:
            return {}

        return payload

    def _send_loop(self) -> None:
        """Periodically broadcast beacon on LAN.

        Sleeps via ``NodeWatchdog.sleep_with_heartbeat`` so an interval
        longer than the watchdog's frozen threshold (30s × 10 = 300s by
        default) can't age the heartbeat out mid-sleep. The default
        beacon interval is well below the threshold, but running with
        HEVOLVE_BEACON_INTERVAL=600 (or similar operator overrides) used
        to trigger the restart cascade documented in the 2026-04-11
        incident.
        """
        while self._running:
            try:
                beacon = self._build_beacon()
                # Send the beacon to every usable per-NIC broadcast
                # address (Win11 multi-NIC: Wi-Fi + Hyper-V + VMware +
                # Docker virtuals).  A failure on one NIC must not
                # abort the round.
                targets = self._broadcast_targets or ['255.255.255.255']
                for tgt in targets:
                    try:
                        self._sock.sendto(beacon, (tgt, self._port))
                    except Exception as e:
                        logger.debug(f"AutoDiscovery send to {tgt} failed: {e}")
            except Exception as e:
                logger.debug(f"AutoDiscovery send error: {e}")
            try:
                from security.node_watchdog import get_watchdog
                wd = get_watchdog()
                if wd is not None:
                    wd.sleep_with_heartbeat(
                        'auto_discovery', self._beacon_interval,
                        stop_check=lambda: not self._running,
                    )
                    continue
            except Exception:
                pass
            # Fallback path if watchdog is unavailable: plain sleep +
            # best-effort heartbeat. Preserves original behavior.
            time.sleep(self._beacon_interval)

    def _recv_loop(self) -> None:
        """Listen for beacons from other nodes on the network.

        The socket has a 2s timeout (set in start()) so recvfrom wakes
        regularly and we can heartbeat between calls. The heartbeat is
        now emitted on BOTH timeout and successful receipt — the
        previous code only heartbeated on timeout, so a node that kept
        receiving packets every 2s could still let the heartbeat age
        if the recv path itself blocked past the frozen threshold.
        """
        import socket as _socket

        def _wd_heartbeat_safe():
            try:
                from security.node_watchdog import get_watchdog
                wd = get_watchdog()
                if wd:
                    wd.heartbeat('auto_discovery')
            except Exception:
                pass

        while self._running:
            try:
                data, addr = self._sock.recvfrom(self.MAX_PACKET_SIZE)
            except _socket.timeout:
                _wd_heartbeat_safe()
                continue
            except OSError:
                if not self._running:
                    break
                continue
            # Successful receipt — refresh heartbeat before processing
            # the payload, which involves JSON parsing + gossip handoff
            # and could itself block for a noticeable fraction of a second.
            _wd_heartbeat_safe()

            payload = self._parse_beacon(data)
            if not payload:
                continue

            node_id = payload.get('node_id')
            if node_id in self._discovered_nodes:   # TTL-aware membership
                continue

            self._discovered_nodes[node_id] = True   # bounded; FIFO-evicts oldest
            url = payload.get('url', '')
            logger.info(f"AutoDiscovery: found node "
                        f"{payload.get('name', node_id[:8])} at {url} via LAN")

            # Feed into gossip
            try:
                self._gossip.handle_announce(payload)
            except Exception:
                pass
            try:
                self._gossip._announce_to_peer(url)
            except Exception:
                pass


# Module-level singletons
gossip = GossipProtocol()
auto_discovery = AutoDiscovery(gossip)


def get_peer_discovery() -> GossipProtocol:
    """Return the singleton GossipProtocol instance.

    Canonical accessor for callers that want the gossip singleton
    without importing the module-level binding directly (e.g.
    `integrations.agent_engine.compute_borrowing` for compute-offer
    broadcast).  Same object every call — gossip identity is stable.
    """
    return gossip


def get_auto_discovery() -> AutoDiscovery:
    """Return the singleton AutoDiscovery instance.

    Canonical accessor used by standalone runners (systemd unit,
    NixOS module) so they don't have to instantiate a new
    AutoDiscovery with their own gossip — they pick up the same
    singleton wired here.
    """
    return auto_discovery
