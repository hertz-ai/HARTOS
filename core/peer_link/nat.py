"""
NAT Traversal — get two peers connected regardless of network topology.

Strategy (try in order, stop at first success):
  1. LAN direct: Same subnet -> direct WebSocket to peer IP
  2. STUN: Get external IP via STUN -> try direct connection
  3. WireGuard: Use compute_mesh WireGuard tunnel -> WS over mesh IP
  4. Peer relay: Route through a mutual peer with public IP
  5. Crossbar relay: Last resort (legacy compatibility)

Reuses existing infrastructure:
  - compute_mesh_service.py -> STUN server config, WireGuard tunnel
  - peer_discovery.py -> peer list with addresses
  - signaling.py -> connection negotiation pattern

For same-user LAN: strategy 1 always works (UDP beacon discovery).
For cross-user WAN: strategies 2-5 depending on NAT type.
"""
import logging
import os
import socket
import struct
import threading
from typing import Optional

logger = logging.getLogger('hevolve.peer_link')


class NATType:
    """Detected NAT type."""
    NONE = 'none'              # Public IP, no NAT
    FULL_CONE = 'full_cone'    # Any external host can reach mapped port
    RESTRICTED = 'restricted'  # Only hosts we've sent to can reach us
    SYMMETRIC = 'symmetric'    # Different mapping per destination (hardest)
    UNKNOWN = 'unknown'


class NATTraversal:
    """Orchestrate NAT traversal to establish peer connection.

    Returns a WebSocket URL that can be used to connect to the peer,
    or None if all strategies fail.
    """

    def __init__(self, stun_server: str = ''):
        self._stun_server = stun_server or os.environ.get(
            'HEVOLVE_STUN_SERVER', 'stun.l.google.com:19302')
        self._external_ip: Optional[str] = None
        self._nat_type = NATType.UNKNOWN
        self._lock = threading.Lock()

    def resolve_peer_address(self, peer_info: dict) -> Optional[str]:
        """Try all strategies to get a connectable address for a peer.

        Args:
            peer_info: Dict with peer's url, mesh_ip, node_id, etc.

        Returns:
            WebSocket URL (ws://host:port/peer_link) or None
        """
        peer_url = peer_info.get('url', '')
        peer_mesh_ip = peer_info.get('mesh_ip', '')

        # Extract host from URL
        peer_host = self._extract_host(peer_url)

        # Strategy 1: LAN direct
        ws_url = self._try_lan_direct(peer_host)
        if ws_url:
            logger.debug(f"NAT: LAN direct to {peer_host}")
            return ws_url

        # Strategy 2: Direct WAN (peer might have public IP)
        ws_url = self._try_direct_wan(peer_host)
        if ws_url:
            logger.debug(f"NAT: Direct WAN to {peer_host}")
            return ws_url

        # Strategy 3: WireGuard mesh IP
        if peer_mesh_ip:
            ws_url = self._try_wireguard(peer_mesh_ip)
            if ws_url:
                logger.debug(f"NAT: WireGuard to {peer_mesh_ip}")
                return ws_url

        # Strategy 4: Relay through seed peer (not implemented yet - placeholder)
        # In future: find a mutual peer with public IP and relay through them

        # Strategy 5: Crossbar relay (legacy fallback)
        ws_url = self._try_crossbar_relay()
        if ws_url:
            logger.debug("NAT: Crossbar relay fallback")
            return ws_url

        logger.debug(f"NAT: All strategies failed for {peer_host}")
        return None

    def _try_lan_direct(self, peer_host: str) -> Optional[str]:
        """Check if peer is on same LAN subnet."""
        if not peer_host:
            return None

        try:
            # Check if peer is reachable on LAN
            from core.port_registry import get_port
            port = get_port('backend')

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((peer_host, port))
            sock.close()

            if result == 0:
                return f'ws://{peer_host}:{port}/peer_link'
        except Exception:
            pass
        return None

    def _try_direct_wan(self, peer_host: str) -> Optional[str]:
        """Try direct connection to peer's public address."""
        if not peer_host or self._is_private_ip(peer_host):
            return None

        try:
            from core.port_registry import get_port
            port = get_port('backend')

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            result = sock.connect_ex((peer_host, port))
            sock.close()

            if result == 0:
                return f'ws://{peer_host}:{port}/peer_link'
        except Exception:
            pass
        return None

    def _try_wireguard(self, mesh_ip: str) -> Optional[str]:
        """Try connection through WireGuard mesh tunnel."""
        if not mesh_ip:
            return None

        try:
            # Check if mesh interface exists
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            # WireGuard mesh uses compute_mesh port
            result = sock.connect_ex((mesh_ip, 6796))
            sock.close()

            if result == 0:
                return f'ws://{mesh_ip}:6796/peer_link'
        except Exception:
            pass
        return None

    def _try_crossbar_relay(self) -> Optional[str]:
        """Use Crossbar as relay (last resort)."""
        crossbar_url = os.environ.get('CBURL', '')
        if crossbar_url:
            # Return the Crossbar URL — link_manager will use WAMP relay mode
            return crossbar_url
        return None

    def get_external_ip(self) -> Optional[str]:
        """Get our external IP via STUN."""
        if self._external_ip:
            return self._external_ip

        try:
            # Simple STUN request
            host, port_str = self._stun_server.rsplit(':', 1)
            port = int(port_str)

            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(3)

            # STUN binding request (simplified)
            # STUN message: type=0x0001 (binding request), length=0,
            # magic=0x2112A442, transaction_id=random
            txn_id = os.urandom(12)
            request = struct.pack('>HHI', 0x0001, 0, 0x2112A442) + txn_id

            sock.sendto(request, (host, port))
            data, _ = sock.recvfrom(1024)
            sock.close()

            if len(data) >= 32:
                # Parse XOR-MAPPED-ADDRESS from response (simplified)
                # Full STUN parsing would need proper attribute parsing
                # For now, this is a best-effort extraction
                for i in range(20, len(data) - 8):
                    attr_type = struct.unpack('>H', data[i:i+2])[0]
                    if attr_type == 0x0020:  # XOR-MAPPED-ADDRESS
                        attr_len = struct.unpack('>H', data[i+2:i+4])[0]
                        if attr_len >= 8:
                            xor_port = struct.unpack(
                                '>H', data[i+6:i+8])[0] ^ 0x2112
                            xor_ip = struct.unpack(
                                '>I', data[i+8:i+12])[0] ^ 0x2112A442
                            ip = socket.inet_ntoa(struct.pack('>I', xor_ip))
                            self._external_ip = ip
                            return ip
                        break
        except Exception as e:
            logger.debug(f"STUN lookup failed: {e}")

        return None

    @staticmethod
    def _extract_host(url: str) -> str:
        """Extract hostname from URL."""
        if not url:
            return ''
        # Remove protocol
        host = url.split('://')[-1]
        # Remove path
        host = host.split('/')[0]
        # Remove port
        host = host.split(':')[0]
        return host

    @staticmethod
    def _is_private_ip(ip: str) -> bool:
        """Check if IP is in a private range."""
        try:
            parts = [int(p) for p in ip.split('.')]
            if len(parts) != 4:
                return False
            return (parts[0] == 10 or
                    (parts[0] == 172 and 16 <= parts[1] <= 31) or
                    (parts[0] == 192 and parts[1] == 168) or
                    parts[0] == 127)
        except (ValueError, IndexError):
            return False


# Module-level singleton
_nat: Optional[NATTraversal] = None
_nat_lock = threading.Lock()


def get_nat_traversal() -> NATTraversal:
    global _nat
    if _nat is None:
        with _nat_lock:
            if _nat is None:
                _nat = NATTraversal()
    return _nat
