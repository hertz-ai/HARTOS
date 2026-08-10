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
import ipaddress
import logging
import os
import socket
import struct
import threading
from typing import Optional

logger = logging.getLogger('hevolve.peer_link')

# Non-public IPv4 blocks the WAN dial gate (_is_private_ip) must refuse.
# This is the SSRF surface: a hostile peer controls the strings in its
# advertised/observed URL, so every non-routable-public v4 block is listed
# explicitly. Deliberately EXCLUDES the RFC 5737 documentation ranges
# (192.0.2/24, 198.51.100/24, 203.0.113/24) — the stdlib now flags those
# is_private, but the historical gate treated them as ordinary public hosts
# and peers advertise them, so they must keep dialing as before.
_PRIVATE_V4_NETS = (
    ipaddress.ip_network('0.0.0.0/8'),       # "this network" / unspecified
    ipaddress.ip_network('10.0.0.0/8'),      # RFC 1918
    ipaddress.ip_network('100.64.0.0/10'),   # RFC 6598 CGNAT shared space
    ipaddress.ip_network('127.0.0.0/8'),     # loopback
    ipaddress.ip_network('169.254.0.0/16'),  # link-local (cloud metadata)
    ipaddress.ip_network('172.16.0.0/12'),   # RFC 1918
    ipaddress.ip_network('192.168.0.0/16'),  # RFC 1918
)


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

        # Extract host AND port from the peer's advertised URL.
        #
        # _extract_host deliberately drops the port, and strategies 1 and 2
        # then re-added get_port('backend') -- OUR local backend port -- as if
        # every peer served on the same one we do. That is wrong twice: a
        # bundled peer serves HARTOS in-process on the flask port, and any peer
        # behind a port-forward or in a container is reachable on whatever port
        # it advertised. The peer already told us in its URL, so use it and
        # only fall back to our local default when it did not.
        peer_host = self._extract_host(peer_url)
        peer_port = self._extract_port(peer_url)

        # Strategy 1: LAN direct
        ws_url = self._try_lan_direct(peer_host, peer_port)
        if ws_url:
            logger.debug(f"NAT: LAN direct to {peer_host}:{peer_port}")
            return ws_url

        # Strategy 2: Direct WAN (peer might have public IP)
        ws_url = self._try_direct_wan(peer_host, peer_port)
        if ws_url:
            logger.debug(f"NAT: Direct WAN to {peer_host}:{peer_port}")
            return ws_url

        # Strategy 2b: OBSERVED address — the WAN candidate discovery
        # recorded from the peer's actual announce source (or the peer
        # advertised after learning it from an announce echo).  The claimed
        # URL above is what the peer BELIEVES its address is; behind NAT
        # that is a private IP and strategy 2 refuses it, which as of
        # 2026-08-07 was every single one of central's 147 registered peers.
        # observed_url is what some receiver actually SAW.  Same reachability
        # probe, same public-only rule — _try_direct_wan refuses private
        # hosts, so a LAN-vantage observation can never fake a WAN rung.
        observed = (peer_info.get('observed_url')
                    or (peer_info.get('metadata') or {}).get('observed_url')
                    or '')
        if observed:
            obs_host = self._extract_host(observed)
            obs_port = self._extract_port(observed)
            if obs_host and obs_host != peer_host:
                ws_url = self._try_direct_wan(obs_host, obs_port)
                if ws_url:
                    logger.debug(f"NAT: Observed WAN to {obs_host}:{obs_port}")
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

    def _try_lan_direct(self, peer_host: str, peer_port: int = 0) -> Optional[str]:
        """Check if the peer answers on the LAN.

        peer_port comes from the peer's advertised URL. Falls back to our local
        backend port only when the peer advertised none.
        """
        if not peer_host:
            return None

        try:
            # Check if peer is reachable on LAN
            from core.port_registry import get_port
            port = peer_port or get_port('backend')

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((peer_host, port))
            sock.close()

            if result == 0:
                return f'ws://{peer_host}:{port}/peer_link'
        except Exception:
            pass
        return None

    def _try_direct_wan(self, peer_host: str, peer_port: int = 0) -> Optional[str]:
        """Try direct connection to the peer's public address.

        peer_port comes from the peer's advertised URL, which matters more here
        than on the LAN: a peer reachable from the WAN is usually behind a
        port-forward on a port that is not our local default.
        """
        if not peer_host or self._is_private_ip(peer_host):
            return None

        try:
            from core.port_registry import get_port
            port = peer_port or get_port('backend')

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
    def _extract_port(url: str) -> int:
        """Extract the port from a peer's advertised URL, 0 if absent.

        Companion to _extract_host, which drops the port. Callers used to
        discard it and substitute their own backend port, which assumes every
        peer serves where we do. Returns 0 rather than a default so the caller
        decides the fallback.
        """
        if not url:
            return 0
        hostport = url.split('://')[-1].split('/')[0]
        if ':' not in hostport:
            return 0
        try:
            return int(hostport.rsplit(':', 1)[1])
        except (ValueError, IndexError):
            return 0

    @staticmethod
    def _is_private_ip(ip: str) -> bool:
        """Check if an IP is private / not a genuine public host.

        This is the SSRF gate for the WAN dial strategies (_try_direct_wan
        and the observed_url rung of resolve_peer_address). A hostile peer
        fully controls its advertised and observed URLs, so any address that
        is not a real routable public host must be refused, otherwise an
        observed_url pointing at the cloud-metadata endpoint, loopback, CGNAT
        or an IPv6 link-local host would be dialed as a WAN rung.

        Refused: RFC 1918, loopback, IPv4 link-local (169.254.0.0/16 — the
        cloud-metadata 169.254.169.254 endpoint), CGNAT shared space
        (100.64.0.0/10), the 0.0.0.0/8 "this network" block, and every
        non-global IPv6 host (loopback ::1, link-local fe80::/10, unique-local
        fc00::/7, plus IPv4-mapped ::ffff:a.b.c.d forms, classified by the
        embedded v4 address).

        Malformed / non-literal hosts (bare DNS names, empty, None) return
        False: they are not classified here and are resolved by the OS at
        dial time — preserving the historical contract that this method only
        vets IP literals.
        """
        if not ip:
            return False
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False

        # IPv4-mapped IPv6 (::ffff:a.b.c.d) is exactly as (un)safe as the
        # embedded IPv4 address — classify by that so a metadata IP cannot be
        # smuggled through an IPv6 literal.
        mapped = getattr(addr, 'ipv4_mapped', None)
        if mapped is not None:
            addr = mapped

        if addr.version == 6:
            # No public-IPv6 dial path is exercised today; refuse every
            # non-global v6 host. (is_private covers ULA + documentation.)
            return (addr.is_private or addr.is_loopback or addr.is_link_local
                    or addr.is_unspecified or addr.is_reserved
                    or addr.is_multicast)

        # IPv4: explicit non-public blocks (see _PRIVATE_V4_NETS).
        return any(addr in net for net in _PRIVATE_V4_NETS)


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
