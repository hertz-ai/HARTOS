"""
PeerLink Manager — manages all active peer connections.

Auto-upgrade policy:
  After N successful gossip HTTP exchanges with a peer, offer PeerLink upgrade.

Connection budget (tier-based):
  flat:     max 10 links (bandwidth-conscious home devices)
  regional: max 50 links (relay capacity)
  central:  max 200 links (hub capacity)

Idle pruning: close links with <1 message in 5 minutes.
Priority: keep links to peers with GPU, loaded models.

HTTP fallback: send(peer_id, channel, data) tries PeerLink first,
falls back to HTTP POST if no link available.
"""
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .link import PeerLink, TrustLevel, LinkState

logger = logging.getLogger('hevolve.peer_link')

# Connection budget by node tier
_MAX_LINKS = {
    'flat': 10,
    'regional': 50,
    'central': 200,
}

# Auto-upgrade: after this many successful HTTP exchanges, offer PeerLink
_UPGRADE_THRESHOLD = 3

# Idle timeout: close links idle for this many seconds
_IDLE_TIMEOUT = 300  # 5 minutes

# Reconnect backoff
_RECONNECT_MIN = 5
_RECONNECT_MAX = 120


class PeerLinkManager:
    """Manages all peer connections. Singleton via get_link_manager()."""

    def __init__(self):
        self._links: Dict[str, PeerLink] = {}  # peer_id -> PeerLink
        self._lock = threading.Lock()
        self._running = False
        self._maintenance_thread: Optional[threading.Thread] = None
        self._http_exchange_counts: Dict[str, int] = {}  # peer_id -> successful exchanges
        self._channel_handlers: Dict[str, List[Callable]] = {}
        self._reconnect_backoff: Dict[str, float] = {}  # peer_id -> next retry time

        # Determine connection budget from tier
        try:
            from security.key_delegation import get_node_tier
            tier = get_node_tier()
        except ImportError:
            tier = 'flat'
        self._max_links = _MAX_LINKS.get(tier, 10)
        self._tier = tier

    def start(self):
        """Start the link manager background maintenance."""
        if self._running:
            return
        self._running = True
        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop, daemon=True,
            name='peerlink-maintenance')
        self._maintenance_thread.start()
        logger.info(f"PeerLinkManager started (tier={self._tier}, "
                   f"max_links={self._max_links})")

    def stop(self):
        """Stop manager and close all links."""
        self._running = False
        with self._lock:
            for link in list(self._links.values()):
                link.close()
            self._links.clear()
        if self._maintenance_thread:
            self._maintenance_thread.join(timeout=10)

    # --- Link Access ---------------------------------------------------

    def get_link(self, peer_id: str) -> Optional[PeerLink]:
        """Get active link to a peer, or None."""
        with self._lock:
            link = self._links.get(peer_id)
            if link and link.is_connected:
                return link
            return None

    def has_link(self, peer_id: str) -> bool:
        """Check if an active link exists to a peer."""
        return self.get_link(peer_id) is not None

    # --- Send with Fallback --------------------------------------------

    def send(self, peer_id: str, channel: str, data: dict,
             peer_url: str = '', wait_response: bool = False,
             timeout: float = 30.0) -> Optional[dict]:
        """Send message to peer. PeerLink first, HTTP fallback.

        Args:
            peer_id: Target peer node_id
            channel: Channel name
            data: Message payload
            peer_url: HTTP URL for fallback (if no PeerLink)
            wait_response: Block until response
            timeout: Max wait time

        Returns:
            Response dict if wait_response=True, else None
        """
        # Try PeerLink first
        link = self.get_link(peer_id)
        if link:
            result = link.send(channel, data, wait_response=wait_response,
                              timeout=timeout)
            if result is not None or not wait_response:
                return result
            # PeerLink send failed, fall through to HTTP

        # HTTP fallback
        if not peer_url:
            return None

        return self._http_fallback(peer_url, channel, data, timeout)

    def broadcast(self, channel: str, data: dict,
                  trust_filter: Optional[TrustLevel] = None) -> int:
        """Broadcast message to all connected peers.

        Args:
            channel: Channel name
            data: Message payload
            trust_filter: Only send to links with this trust level

        Returns:
            Number of peers successfully sent to
        """
        sent = 0
        with self._lock:
            links = list(self._links.values())

        for link in links:
            if not link.is_connected:
                continue
            if trust_filter and link.trust != trust_filter:
                continue
            try:
                link.send(channel, data)
                sent += 1
            except Exception:
                pass
        return sent

    def collect(self, channel: str, timeout_ms: int = 1000) -> List[dict]:
        """Broadcast and collect responses from all peers.

        Used by HiveMind for distributed thought fusion.
        """
        responses = []

        with self._lock:
            links = list(self._links.values())

        # Send query and collect responses
        timeout_s = timeout_ms / 1000.0
        for link in links:
            if not link.is_connected:
                continue
            try:
                result = link.send(channel, {'type': 'query'},
                                  wait_response=True, timeout=timeout_s)
                if result:
                    responses.append(result)
            except Exception:
                pass

        return responses

    # --- Link Management -----------------------------------------------

    def upgrade_peer(self, peer_id: str, address: str,
                     trust: TrustLevel,
                     x25519_public: str = '',
                     ed25519_public: str = '') -> bool:
        """Upgrade a peer from HTTP to persistent PeerLink.

        Called when auto-upgrade threshold reached or manually.
        """
        with self._lock:
            # Check budget
            active = sum(1 for l in self._links.values() if l.is_connected)
            if active >= self._max_links:
                # Try to evict least useful link
                if not self._evict_weakest_link():
                    logger.debug(f"Link budget full ({active}/{self._max_links}), "
                               f"cannot upgrade {peer_id[:8]}")
                    return False

            # Check if already linked
            existing = self._links.get(peer_id)
            if existing and existing.is_connected:
                return True

        # Create and connect
        link = PeerLink(
            peer_id=peer_id,
            address=address,
            trust=trust,
            x25519_public_hex=x25519_public,
            ed25519_public_hex=ed25519_public,
        )

        # Register channel handlers
        for channel, handlers in self._channel_handlers.items():
            for handler in handlers:
                link.on_message(channel, handler)

        if link.connect():
            with self._lock:
                self._links[peer_id] = link
            return True
        return False

    def close_link(self, peer_id: str):
        """Close and remove a link."""
        with self._lock:
            link = self._links.pop(peer_id, None)
        if link:
            link.close()

    def register_channel_handler(self, channel: str, handler: Callable):
        """Register handler for incoming messages on a channel.

        Applied to all current and future links.
        """
        if channel not in self._channel_handlers:
            self._channel_handlers[channel] = []
        self._channel_handlers[channel].append(handler)

        # Apply to existing links
        with self._lock:
            for link in self._links.values():
                link.on_message(channel, handler)

    def record_http_exchange(self, peer_id: str):
        """Record a successful HTTP exchange with a peer.

        When threshold reached, auto-upgrade to PeerLink.
        Called by gossip, federation, etc. after successful HTTP call.
        """
        count = self._http_exchange_counts.get(peer_id, 0) + 1
        self._http_exchange_counts[peer_id] = count

        if count >= _UPGRADE_THRESHOLD:
            # Try to auto-upgrade
            self._http_exchange_counts[peer_id] = 0
            self._try_auto_upgrade(peer_id)

    # --- Status --------------------------------------------------------

    def get_status(self) -> dict:
        with self._lock:
            links = {pid: l.get_stats() for pid, l in self._links.items()}

        active = sum(1 for s in links.values() if s['state'] == 'connected')
        encrypted = sum(1 for s in links.values() if s.get('encrypted'))

        return {
            'running': self._running,
            'tier': self._tier,
            'max_links': self._max_links,
            'active_links': active,
            'encrypted_links': encrypted,
            'total_links': len(links),
            'links': links,
        }

    # --- Internal ------------------------------------------------------

    def _maintenance_loop(self):
        """Background: prune idle links, attempt reconnects, key rotation."""
        while self._running:
            try:
                self._prune_idle_links()
                self._attempt_reconnects()
            except Exception as e:
                logger.debug(f"Maintenance error: {e}")

            # Sleep in small increments to allow clean shutdown
            for _ in range(30):  # 30 seconds
                if not self._running:
                    break
                time.sleep(1)

    def _prune_idle_links(self):
        """Close links that haven't had activity in _IDLE_TIMEOUT seconds."""
        to_close = []
        with self._lock:
            for peer_id, link in self._links.items():
                if link.is_connected and link.idle_seconds > _IDLE_TIMEOUT:
                    to_close.append(peer_id)

        for peer_id in to_close:
            logger.info(f"Pruning idle link: {peer_id[:8]}")
            self.close_link(peer_id)

    def _attempt_reconnects(self):
        """Try to reconnect links that dropped."""
        now = time.time()
        with self._lock:
            disconnected = [
                (pid, link) for pid, link in self._links.items()
                if link.state == LinkState.DISCONNECTED
            ]

        for peer_id, link in disconnected:
            retry_at = self._reconnect_backoff.get(peer_id, 0)
            if now < retry_at:
                continue

            if link.connect():
                self._reconnect_backoff.pop(peer_id, None)
            else:
                # Exponential backoff
                current = self._reconnect_backoff.get(peer_id, _RECONNECT_MIN)
                self._reconnect_backoff[peer_id] = now + min(current * 2, _RECONNECT_MAX)

    def _evict_weakest_link(self) -> bool:
        """Evict the least useful connected link to make room."""
        with self._lock:
            candidates = [
                (pid, link) for pid, link in self._links.items()
                if link.is_connected
            ]

        if not candidates:
            return False

        # Score: lower = less useful
        def score(item):
            pid, link = item
            s = 0
            if link.capabilities.get('gpu'):
                s += 10  # GPU peers are valuable
            s -= link.idle_seconds / 60  # Penalize idle
            s += link._messages_received / 100  # Active peers are valuable
            return s

        candidates.sort(key=score)
        weakest_id = candidates[0][0]
        self.close_link(weakest_id)
        return True

    def _try_auto_upgrade(self, peer_id: str):
        """Auto-upgrade a peer from HTTP to PeerLink."""
        # Look up peer info from gossip
        try:
            from integrations.social.peer_discovery import gossip
            peers = gossip.get_peer_list()
            peer_info = next((p for p in peers if p.get('node_id') == peer_id), None)
            if not peer_info:
                return

            address = peer_info.get('url', '').replace('http://', '').replace('https://', '').rstrip('/')
            if not address:
                return

            # Determine trust level based on user identity, not network
            # Same user = same authenticated user_id across ANY network
            trust = TrustLevel.PEER  # Default to encrypted
            try:
                # Check compute_mesh (same-user device registry)
                from integrations.agent_engine.compute_mesh_service import get_compute_mesh
                mesh = get_compute_mesh()
                if mesh and peer_id in (mesh._peers or {}):
                    trust = TrustLevel.SAME_USER
            except Exception:
                pass

            if trust != TrustLevel.SAME_USER:
                try:
                    # Check if peer's user_id matches ours (regional/WAN same-user)
                    peer_user_id = peer_info.get('user_id', '')
                    if peer_user_id:
                        local_user_id = os.environ.get('HEVOLVE_USER_ID', '')
                        if not local_user_id:
                            from security.node_integrity import get_node_identity
                            local_user_id = get_node_identity().get('user_id', '')
                        if local_user_id and peer_user_id == local_user_id:
                            trust = TrustLevel.SAME_USER
                except Exception:
                    pass

            self.upgrade_peer(
                peer_id=peer_id,
                address=address,
                trust=trust,
                x25519_public=peer_info.get('x25519_public', ''),
                ed25519_public=peer_info.get('public_key', ''),
            )
        except Exception as e:
            logger.debug(f"Auto-upgrade failed for {peer_id[:8]}: {e}")

    @staticmethod
    def _http_fallback(peer_url: str, channel: str, data: dict,
                       timeout: float = 30.0) -> Optional[dict]:
        """Send via HTTP when no PeerLink available."""
        try:
            from core.http_pool import pooled_post
            resp = pooled_post(
                f'{peer_url}/api/peer-link/message',
                json={'ch': channel, 'd': data},
                timeout=timeout,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None


# --- Singleton ---------------------------------------------------------

_manager: Optional[PeerLinkManager] = None
_manager_lock = threading.Lock()


def get_link_manager() -> PeerLinkManager:
    """Get or create the singleton PeerLinkManager."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = PeerLinkManager()
    return _manager


def reset_link_manager():
    """Reset singleton (testing only)."""
    global _manager
    if _manager:
        _manager.stop()
    _manager = None
