"""
HevolveSocial - zero-config LAN peer finding over a UDP broadcast beacon.

AutoDiscovery used to sit in peer_discovery.py beside GossipProtocol. It is a
separate concern: a LAN beacon that FEEDS the gossip protocol (it hands each
new node to GossipProtocol.handle_announce), never the protocol itself, so it
has its own module. peer_discovery re-exports the class and still owns the
``auto_discovery`` singleton and ``get_auto_discovery()``, so every existing
import path (``from integrations.social.peer_discovery import AutoDiscovery``,
``peer_discovery.auto_discovery``) keeps working unchanged.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import TYPE_CHECKING

from core.session_cache import TTLCache  # bounded + TTL dedup (peer churn safe)

if TYPE_CHECKING:  # annotation only: peer_discovery imports THIS module
    from integrations.social.peer_discovery import GossipProtocol

logger = logging.getLogger('hevolve_social')


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
                                 f"{node_id[:8]}: guardrail mismatch")
                    return {}
            except Exception as e:
                # Reading OUR OWN guardrail hash failed, which says nothing
                # about the peer, so the beacon still passes -- refuse on
                # evidence of badness, never on absence of evidence.  But say
                # so: this used to be `pass`, and a node whose guardrail hash
                # could not be read checked no beacon against it and reported
                # that nowhere.
                logger.warning(
                    f"AutoDiscovery: guardrail hash unreadable ({e}); the "
                    f"beacon from {node_id[:8]} was NOT checked against it")

        # Code hash: a TRUST SIGNAL, not an admission gate.
        #
        # This used to drop the beacon outright whenever the hash was
        # unrecognised and enforcement was 'hard' -- which is the DEFAULT, so
        # it fired on every node nobody had configured.  That is the
        # REJECT-ON-UNKNOWN-HASH pattern release_hash_registry.py's header
        # documents as SUPERSEDED by ADMIT-AND-RECORD, for reasons that all
        # still hold here:
        #
        #   * code_hash is SELF-REPORTED.  The signature proves this key
        #     asserted this value, never that it is the code running, so a
        #     hostile node simply claims a known-good hash.  The gate only
        #     ever turned away honest nodes on unpublished builds.
        #   * _KNOWN_HASHES is baked into each build and a tree cannot
        #     contain its own hash, so a build only ever learns the hashes of
        #     builds BEFORE it.  Every node on a newer build is unknown to
        #     every older one by construction.
        #   * Measured: it held the live network at ZERO federating peers out
        #     of 69 registered nodes.
        #
        # The HTTP admission path already got this right 700 lines above and
        # carries the long note; this beacon path kept the old behaviour and
        # pre-empted it, because a dropped beacon never reaches
        # handle_announce at all.  Measured on the owner's desktop 2026-09-21:
        # one LAN peer refused 88 times, invisible to them, while the very
        # same enforcement flag read 'hard' here and (before c3be1b660) read
        # permissive in PeerLink -- one setting, two answers, on one machine.
        #
        # So the beacon is admitted and the hash recorded as what it is.  The
        # real decision stays where it is documented: handle_announce sets
        # master_key_verified=False and hash_trusted_source='untrusted', and
        # every downstream consumer (canary selection, revenue credit, moat
        # scoring, visibility tier, fraud_score) sees exactly what it saw
        # before.  Authentication is untouched -- the Ed25519 signature check
        # below still runs, and a guardrail mismatch above still refuses.
        #
        # Strict provenance for a locked cluster is still available, through
        # the same opt-in the admission site uses so the two cannot disagree:
        # HEVOLVE_REQUIRE_KNOWN_CODE_HASH=1, guarded by has_trust_basis() so
        # it cannot be switched on into a vacuum and partition the cluster it
        # was meant to protect.
        peer_code_hash = payload.get('code_hash', '')
        if peer_code_hash:
            try:
                from security.release_hash_registry import get_release_hash_registry
                registry = get_release_hash_registry()
                if not registry.is_known_release_hash(peer_code_hash):
                    _strict = os.environ.get(
                        'HEVOLVE_REQUIRE_KNOWN_CODE_HASH', '').lower() in (
                            '1', 'true', 'yes')
                    if _strict and registry.has_trust_basis():
                        logger.warning(
                            f"AutoDiscovery: rejecting beacon from "
                            f"{node_id[:8]}: unknown code hash "
                            f"{peer_code_hash[:16]}... "
                            f"(HEVOLVE_REQUIRE_KNOWN_CODE_HASH=1)")
                        return {}
                    logger.info(
                        f"AutoDiscovery: beacon from {node_id[:8]} carries "
                        f"unrecognised code hash {peer_code_hash[:16]}; "
                        f"admitting as untrusted, the way the announce path "
                        f"does")
            except Exception as e:
                logger.warning(
                    f"AutoDiscovery: code hash registry unreadable ({e}); the "
                    f"beacon from {node_id[:8]} was NOT checked against it")

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
            except ImportError as e:
                # OUR verifier is missing, not the peer's fault.  Refusing
                # here would partition this node off the LAN over a local
                # packaging fault, which is the thing security must not do.
                # Admit unverified and SAY SO -- the same choice, and the
                # same wording, as the code-hash branch above.
                logger.error(
                    f"AutoDiscovery: signature verifier unavailable ({e}); "
                    f"the beacon from {node_id[:8]} was NOT verified. "
                    f"Admitting as untrusted, the way the announce path does")
            except Exception as e:
                # The signature and public key came from the packet, so an
                # exception here is the SENDER's malformed input -- a
                # malformed pubkey that makes the verifier raise would
                # otherwise skip verification entirely and be admitted.
                # That is evidence of badness, so refuse.
                #
                # This gulp was `except Exception: pass`, which returned the
                # payload with no verification and NO log line.  d6c686197
                # (mine) demoted the code-hash gate from an admission gate
                # to a trust signal -- correctly -- which left this check as
                # the ONLY thing in front of admission, so the silence here
                # went from bad to load-bearing.  Found by hartos-14.
                logger.warning(
                    f"AutoDiscovery: signature check RAISED for "
                    f"{node_id[:8]} ({e}); the packet's own key or signature "
                    f"is malformed. Refusing -- an unverifiable signature "
                    f"must not read as a valid one")
                return {}

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

            # Feed into gossip.  handle_announce is where admission is really
            # decided (trust signals, tier, certificate); the beacon only gets
            # the payload to it.  Both calls used to swallow everything, so a
            # peer that reached this line and was then refused, or crashed the
            # handler, looked identical to one that joined.
            try:
                # addr[0] is where the beacon actually came from: the
                # measured vantage address_evidence judges the url against.
                self._gossip.handle_announce(payload, observed_ip=addr[0])
            except Exception as e:
                logger.warning(
                    f"AutoDiscovery: handing {node_id[:8]} to gossip failed: "
                    f"{e}")
            try:
                self._gossip._announce_to_peer(url)
            except Exception as e:
                logger.info(
                    f"AutoDiscovery: could not announce back to "
                    f"{node_id[:8]} at {url}: {e}")
