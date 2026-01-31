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

logger = logging.getLogger('hevolve_social')


class GossipProtocol:
    """Gossip-based peer discovery for HevolveBot network."""

    def __init__(self):
        # Identity
        self.node_id = str(uuid.uuid4())
        self.node_name = os.environ.get(
            'HEVOLVE_NODE_NAME', f'hevolve-{self.node_id[:8]}')
        self.base_url = os.environ.get(
            'HEVOLVE_BASE_URL', 'http://localhost:6777').rstrip('/')
        self.version = '1.0.0'
        self.started_at = datetime.utcnow()

        # Configuration
        self.gossip_interval = int(os.environ.get('HEVOLVE_GOSSIP_INTERVAL', '60'))
        self.health_interval = int(os.environ.get('HEVOLVE_HEALTH_INTERVAL', '120'))
        self.stale_threshold = int(os.environ.get('HEVOLVE_STALE_THRESHOLD', '300'))
        self.dead_threshold = int(os.environ.get('HEVOLVE_DEAD_THRESHOLD', '900'))
        self.gossip_fanout = int(os.environ.get('HEVOLVE_GOSSIP_FANOUT', '3'))

        # Parse seed peers
        seed_str = os.environ.get('HEVOLVE_SEED_PEERS', '')
        self.seed_peers = [
            u.strip().rstrip('/') for u in seed_str.split(',')
            if u.strip()
        ]

        # State
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

    def start(self):
        """Load peers from DB, announce to seeds/known peers, start background thread."""
        with self._lock:
            if self._running:
                return
            self._running = True

        # Seed peers into DB
        self._seed_initial_peers()

        # Announce self to all known peers (non-blocking)
        threading.Thread(target=self._announce_to_all, daemon=True).start()

        # Start gossip background loop
        self._thread = threading.Thread(target=self._background_loop, daemon=True)
        self._thread.start()
        logger.info(f"Gossip started: node={self.node_id[:8]}, "
                    f"name={self.node_name}, seeds={len(self.seed_peers)}")

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
        while self._running:
            now = time.time()
            try:
                if now - last_gossip >= self.gossip_interval:
                    self._gossip_round()
                    last_gossip = now
                if now - last_health >= self.health_interval:
                    self._health_check_round()
                    last_health = now
            except Exception as e:
                logger.debug(f"Gossip loop error: {e}")
            time.sleep(5)

    # ─── Gossip Round ───

    def _gossip_round(self):
        peers = self._load_peers_from_db(exclude_dead=True)
        if not peers:
            # Retry seeds if we have no peers
            for url in self.seed_peers:
                self._announce_to_peer(url)
            return

        targets = random.sample(peers, min(self.gossip_fanout, len(peers)))
        for peer in targets:
            try:
                their_peers = self._exchange_with_peer(peer['url'])
                if their_peers:
                    self._merge_peer_list(their_peers)
            except Exception as e:
                logger.debug(f"Gossip exchange failed with {peer['url']}: {e}")

    def _health_check_round(self):
        from .models import get_db, PeerNode
        db = get_db()
        try:
            peers = db.query(PeerNode).filter(PeerNode.status != 'dead').all()
            now = datetime.utcnow()
            for peer in peers:
                if peer.node_id == self.node_id:
                    continue
                reachable = self._ping_peer(peer.url)
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
            if url != self.base_url:
                self._announce_to_peer(url)

    def _announce_to_peer(self, peer_url):
        try:
            resp = requests.post(
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
            resp = requests.post(
                f"{peer_url}/api/social/peers/exchange",
                json={'peers': self.get_peer_list(), 'sender': self._self_info()},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get('peers', [])
        except requests.RequestException:
            pass
        return None

    def _ping_peer(self, peer_url):
        try:
            resp = requests.get(f"{peer_url}/api/social/peers/health", timeout=3)
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
        return {
            'node_id': self.node_id,
            'url': self.base_url,
            'name': self.node_name,
            'version': self.version,
            'agent_count': self._get_count('agent'),
            'post_count': self._get_count('post'),
        }

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
        """Upsert a single peer into PeerNode table. Returns True if new."""
        from .models import PeerNode
        node_id = peer_data.get('node_id')
        url = peer_data.get('url', '').rstrip('/')
        if not node_id or not url or node_id == self.node_id:
            return False

        existing = db.query(PeerNode).filter(PeerNode.node_id == node_id).first()
        if existing:
            existing.last_seen = datetime.utcnow()
            existing.url = url
            existing.name = peer_data.get('name', existing.name)
            existing.version = peer_data.get('version', existing.version)
            existing.agent_count = peer_data.get('agent_count', existing.agent_count)
            existing.post_count = peer_data.get('post_count', existing.post_count)
            if existing.status == 'dead':
                existing.status = 'active'  # resurrect
            return False

        new_peer = PeerNode(
            node_id=node_id, url=url,
            name=peer_data.get('name', ''),
            version=peer_data.get('version', ''),
            status='active',
            agent_count=peer_data.get('agent_count', 0),
            post_count=peer_data.get('post_count', 0),
            metadata_json=peer_data.get('metadata', {}),
        )
        db.add(new_peer)
        return True

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


# Module-level singleton
gossip = GossipProtocol()
