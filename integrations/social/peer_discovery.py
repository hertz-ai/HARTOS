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

logger = logging.getLogger('hevolve_social')


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


class GossipProtocol:
    """Gossip-based peer discovery for HevolveBot network."""

    def __init__(self):
        # Identity
        self.node_id = str(uuid.uuid4())
        self.node_name = os.environ.get(
            'HEVOLVE_NODE_NAME', f'hevolve-{self.node_id[:8]}')
        from core.port_registry import get_port
        self.base_url = os.environ.get(
            'HEVOLVE_BASE_URL', f'http://localhost:{get_port("backend")}').rstrip('/')
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

        # Parse seed peers — env override + hardcoded genesis peers.
        # Genesis peers prevent bootstrap poisoning: even if env var is
        # compromised, the node always knows at least the real network.
        _GENESIS_PEERS = [
            'https://central.hevolve.ai',
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

        # State
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        logger.info(
            f"Gossip bandwidth: {self.bandwidth_profile} "
            f"(gossip={self.gossip_interval}s, health={self.health_interval}s, "
            f"fanout={self.gossip_fanout}, payload={self.payload_mode})"
        )

    # ─── Payload Serialization ───

    def _gossip_self_info(self):
        """Return self info appropriate for current bandwidth profile."""
        info = self._self_info()
        if self.payload_mode == 'json_compact':
            return {k: v for k, v in info.items() if k in _COMPACT_FIELDS}
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

    def _ensure_hart_identity(self):
        """Generate HART node identity on first startup. Like getting an IP address.

        - Central: picks a unique element → @element
        - Regional: picks a unique spirit → @central.spirit
        - Flat: no node tag needed (users get individual tags)
        """
        try:
            from hart_onboarding import generate_node_identity, get_node_identity

            # Check if already generated
            existing = get_node_identity()
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
            try:
                if now - last_gossip >= self.gossip_interval:
                    self._gossip_round()
                    last_gossip = now
                if now - last_health >= self.health_interval:
                    self._health_check_round()
                    last_health = now
                if now - last_integrity >= integrity_interval:
                    self._integrity_round()
                    last_integrity = now
            except Exception as e:
                logger.debug(f"Gossip loop error: {e}")
            time.sleep(5)

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
            # Retry seeds if we have no peers — limit to 2 seeds max
            # to avoid blocking for N × 5s when all seeds are unreachable
            for url in list(self.seed_peers)[:2]:
                if not self._running:
                    return
                self._announce_to_peer(url)
                self._heartbeat()
            return

        targets = random.sample(peers, min(self.gossip_fanout, len(peers)))
        for peer in targets:
            if not self._running:
                return
            try:
                their_peers = self._exchange_with_peer(peer['url'])
                if their_peers:
                    self._merge_peer_list(their_peers)
            except Exception as e:
                logger.debug(f"Gossip exchange failed with {peer['url']}: {e}")
            self._heartbeat()

    def _health_check_round(self):
        from .models import get_db, PeerNode
        db = get_db()
        try:
            peers = db.query(PeerNode).filter(PeerNode.status != 'dead').all()
            now = datetime.utcnow()
            for peer in peers:
                if not self._running:
                    break
                if peer.node_id == self.node_id:
                    continue
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
            db.commit()
            # Update contribution scores for active/stale peers
            try:
                from .hosting_reward_service import HostingRewardService
                for peer in peers:
                    if peer.status in ('active', 'stale') and peer.node_id != self.node_id:
                        HostingRewardService.compute_contribution_score(db, peer.node_id)
                db.commit()
            except Exception:
                pass
        except Exception as e:
            db.rollback()
            logger.debug(f"Health check error: {e}")
        finally:
            db.close()

    # ─── Announce ───

    def _announce_to_all(self):
        peers = self._load_peers_from_db(exclude_dead=False)
        urls = set(p['url'] for p in peers)
        urls.update(self.seed_peers)
        for url in urls:
            if not self._running:
                return
            if url != self.base_url:
                self._announce_to_peer(url)
                self._heartbeat()

    def _announce_to_peer(self, peer_url):
        try:
            resp = pooled_post(
                f"{peer_url}/api/social/peers/announce",
                json=self._self_info(),
                timeout=5,
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False

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
                # Record exchange for auto-upgrade to PeerLink
                try:
                    peer_id = self._url_to_node_id(peer_url)
                    if peer_id:
                        from core.peer_link.link_manager import get_link_manager
                        get_link_manager().record_http_exchange(peer_id)
                except Exception:
                    pass
                return data.get('peers', [])
        except requests.RequestException:
            pass
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
        try:
            resp = pooled_get(f"{peer_url}/api/social/peers/health", timeout=3)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    # ─── Handlers (called by Flask endpoints) ───

    def handle_announce(self, peer_data):
        """Process an incoming peer announcement. Returns True if peer was new."""
        from .models import get_db
        db = get_db()
        try:
            is_new = self._merge_peer(db, peer_data)
            db.commit()
            if is_new:
                logger.info(f"New peer discovered: {peer_data.get('name', '')} "
                            f"at {peer_data.get('url', '')}")
            return is_new
        except Exception as e:
            db.rollback()
            logger.debug(f"Announce handler error: {e}")
            return False
        finally:
            db.close()

    def broadcast(self, message: dict, targets: list = None) -> int:
        """Broadcast a message to active peers via gossip.

        Used by RALT skill distribution and skill queries.
        Posts to /api/social/peers/broadcast on each target node.

        Returns number of successfully contacted peers.
        """
        peers = self._load_peers_from_db(exclude_dead=True)
        if targets:
            target_set = set(targets)
            peers = [p for p in peers if p.get('node_id') in target_set]

        sent = 0
        for peer in peers:
            url = peer.get('url', '')
            if not url or peer.get('node_id') == self.node_id:
                continue
            try:
                pooled_post(
                    f"{url}/api/social/peers/broadcast",
                    json=message,
                    timeout=5,
                )
                sent += 1
            except requests.RequestException:
                pass
        return sent

    def handle_exchange(self, their_peers):
        """Process incoming peer list, return our peer list."""
        if their_peers:
            self._merge_peer_list(their_peers)
        return self.get_peer_list()

    # ─── Peer List ───

    def get_peer_list(self):
        """Return all non-dead peers as dicts, including self."""
        peers = self._load_peers_from_db(exclude_dead=True)
        # Include self
        self_info = self._self_info()
        if not any(p.get('node_id') == self.node_id for p in peers):
            peers.append(self_info)
        return peers

    def get_health(self):
        """Return this node's health info for the /health endpoint."""
        uptime = (datetime.utcnow() - self.started_at).total_seconds()
        peers = self._load_peers_from_db(exclude_dead=True)
        return {
            'node_id': self.node_id,
            'name': self.node_name,
            'version': self.version,
            'uptime_seconds': int(uptime),
            'peer_count': len(peers),
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
            from security.node_integrity import get_public_key_hex, compute_code_hash, sign_json_payload
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
            info['signature'] = sign_json_payload(info)
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
        """Merge a list of peer dicts into the DB."""
        from .models import get_db
        db = get_db()
        try:
            new_count = 0
            for p in peer_list:
                if p.get('node_id') and p.get('node_id') != self.node_id:
                    if self._merge_peer(db, p):
                        new_count += 1
            if new_count > 0:
                logger.info(f"Gossip: merged {new_count} new peers")
            db.commit()
        except Exception as e:
            db.rollback()
            logger.debug(f"Merge peer list error: {e}")
        finally:
            db.close()

    def _merge_peer(self, db, peer_data):
        """Upsert a single peer into PeerNode table. Returns True if new.
        Verifies Ed25519 signature if present. Rejects banned nodes."""
        from .models import PeerNode
        node_id = peer_data.get('node_id')
        url = peer_data.get('url', '').rstrip('/')
        if not node_id or not url or node_id == self.node_id:
            return False

        # Sybil protection: max 5 nodes per IP/hostname
        try:
            from urllib.parse import urlparse
            host = urlparse(url).hostname or ''
            if host:
                from .models import PeerNode
                same_host_count = db.query(PeerNode).filter(
                    PeerNode.url.contains(host),
                    PeerNode.integrity_status != 'banned',
                ).count()
                max_per_ip = int(os.environ.get('HEVOLVE_MAX_PEERS_PER_IP', '5'))
                if same_host_count >= max_per_ip:
                    logger.warning(f"Sybil limit: {same_host_count} nodes from {host}, rejecting {node_id[:8]}")
                    return False
        except Exception:
            pass  # URL parsing failed — proceed with other checks

        # Reject banned nodes
        existing = db.query(PeerNode).filter(PeerNode.node_id == node_id).first()
        if existing and existing.integrity_status == 'banned':
            logger.debug(f"Rejecting banned node: {node_id[:8]}")
            return False

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
                    return False
            except ImportError:
                pass  # crypto module not available, accept unsigned
            except Exception as e:
                logger.warning(f"Unexpected error verifying signature for {node_id[:8]}: {e}")
                return False  # Reject on unexpected verification errors

        integrity_status = 'verified' if signature_valid else 'unverified'

        # Enforcement gate: reject unsigned peers in hard mode
        if not signature_valid:
            try:
                from security.master_key import get_enforcement_mode
                enforcement = get_enforcement_mode()
                if enforcement == 'hard':
                    logger.warning(f"Rejecting unsigned peer {node_id[:8]} (enforcement=hard)")
                    return False
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
                    return False
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

            # Enforcement: reject unknown hashes in hard mode
            if not master_key_verified:
                try:
                    from security.master_key import get_enforcement_mode
                    enforcement = get_enforcement_mode()
                    if enforcement == 'hard':
                        logger.warning(
                            f"Rejecting peer {node_id[:8]}: unknown code hash "
                            f"{peer_code_hash[:16]}... (enforcement=hard)")
                        return False
                    elif enforcement == 'soft':
                        logger.warning(
                            f"Peer {node_id[:8]} unknown code hash "
                            f"(enforcement=soft, allowing)")
                except Exception:
                    pass

        # Certificate verification for peers claiming regional/central tier
        peer_tier = peer_data.get('tier', 'flat')
        certificate = peer_data.get('certificate')
        certificate_verified = False
        if peer_tier in ('regional', 'central') and not certificate:
            logger.warning(f"Rejecting {node_id[:8]}: {peer_tier} tier requires certificate")
            return False
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
                        return False
                    if enforcement == 'hard':
                        logger.warning(f"Rejecting peer {node_id[:8]}: invalid certificate "
                                      f"for tier={peer_tier} (enforcement=hard)")
                        return False
                    else:
                        logger.warning(f"Peer {node_id[:8]} has invalid certificate "
                                      f"for tier={peer_tier} (enforcement={enforcement})")
            except Exception as e:
                logger.debug(f"Certificate verification error for {node_id[:8]}: {e}")

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
            existing.master_key_verified = master_key_verified
            if peer_data.get('release_version'):
                existing.release_version = peer_data['release_version']
            # Update tier/certificate fields
            existing.tier = peer_tier
            if certificate:
                existing.certificate_json = certificate
                existing.certificate_verified = certificate_verified
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
            return False

        new_peer = PeerNode(
            node_id=node_id, url=url,
            name=peer_data.get('name', ''),
            version=peer_data.get('version', ''),
            status='active',
            agent_count=peer_data.get('agent_count', 0),
            post_count=peer_data.get('post_count', 0),
            metadata_json=peer_data.get('metadata', {}),
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
        )
        db.add(new_peer)

        # ─── Seamless Mind Merge ───
        # Valid peer accepted - auto-federate so minds merge without friction.
        # Connection is a breeze; the audit layer handles trust continuously.
        threading.Thread(
            target=self._auto_federate_peer,
            args=(node_id, url),
            daemon=True,
        ).start()

        return True

    def _auto_federate_peer(self, peer_node_id: str, peer_url: str):
        """Auto-follow a newly accepted peer for seamless mind merge.
        Valid peers get instant bidirectional content sharing - no manual step."""
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
            active_peers = db.query(PeerNode).filter(
                PeerNode.status == 'active',
                PeerNode.node_id != self.node_id,
                PeerNode.integrity_status != 'banned',
            ).all()

            if active_peers:
                from .integrity_service import IntegrityService

                # 1. Guardrail audit: re-verify ALL active peers' guardrail hashes.
                #    This is the continuous audit - every node checks every other node.
                for peer in active_peers:
                    self._audit_peer_guardrails(db, peer)

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
        try:
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
        self._discovered_nodes: set = set()
        self._sock = None

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

        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()
        self._send_thread = threading.Thread(target=self._send_loop, daemon=True)
        self._send_thread.start()
        logger.info(f"AutoDiscovery started on UDP port {self._port} "
                    f"(interval={self._beacon_interval}s)")

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

        if payload.get('type') != 'hevolve-discovery':
            return {}
        if payload.get('node_id') == self._gossip.node_id:
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
        """Periodically broadcast beacon on LAN."""
        import socket as _socket
        while self._running:
            try:
                beacon = self._build_beacon()
                self._sock.sendto(beacon, ('<broadcast>', self._port))
            except Exception as e:
                logger.debug(f"AutoDiscovery send error: {e}")
            # Heartbeat to watchdog
            try:
                from security.node_watchdog import get_watchdog
                wd = get_watchdog()
                if wd:
                    wd.heartbeat('auto_discovery')
            except Exception:
                pass
            time.sleep(self._beacon_interval)

    def _recv_loop(self) -> None:
        """Listen for beacons from other nodes on the network."""
        import socket as _socket
        while self._running:
            try:
                data, addr = self._sock.recvfrom(self.MAX_PACKET_SIZE)
            except _socket.timeout:
                # Heartbeat on timeout so watchdog knows we're alive
                try:
                    from security.node_watchdog import get_watchdog
                    wd = get_watchdog()
                    if wd:
                        wd.heartbeat('auto_discovery')
                except Exception:
                    pass
                continue
            except OSError:
                if not self._running:
                    break
                continue

            payload = self._parse_beacon(data)
            if not payload:
                continue

            node_id = payload.get('node_id')
            if node_id in self._discovered_nodes:
                continue

            self._discovered_nodes.add(node_id)
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
