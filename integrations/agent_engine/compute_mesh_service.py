"""
HART OS Compute Mesh Service — Privacy-Bounded Cross-Device Intelligence.

Same user's devices automatically discover each other and share compute.
Privacy boundary = user_id (Ed25519 keypair). Only YOUR devices can join
YOUR mesh. Different users NEVER share compute through this service.

Discovery:
  LAN   → UDP beacon (port 6780) + device fingerprint
  WAN   → STUN/TURN for NAT traversal, WireGuard tunnel
  Internet → WireGuard over public IP or relay

Task relay protocol:
  POST /mesh/infer  — Offload model inference
  POST /mesh/status — Device health + available compute
  GET  /mesh/peers  — List paired devices
  POST /mesh/pair   — Initiate device pairing (challenge-response)
"""
import hashlib
import json
import logging
from core.subprocess_safe import no_window_kwargs
import os
import threading
import time
from typing import Any, Dict, List, Optional

from core.port_registry import get_port

logger = logging.getLogger('hevolve.compute_mesh')

# Opaque hidden size for the contract-shaped activation ECHO the relay emits.
# HARTOS has no ML code: the echo is a deterministic, correctly-sized stand-in
# for the tensor hevolveai's ShardBackend would produce (shape [1, N, HIDDEN],
# bfloat16 => 2 bytes/element). It proves HART's parse + relay responsibility;
# the real tensor math is the hevolveai boundary.
_ACTIVATION_HIDDEN = 8

# Upper bound on the token/row dimension of an inbound shard frame. The echo
# payload is 2 * prod(shape) bytes, so without this an UNAUTHENTICATED frame
# whose header claims shape [1, 2_000_000_000, 8] would try to materialize ~32 GB
# from ~120 bytes (header-driven amplification DoS). 2^18 rows caps the echo at
# ~4 MB, far above any real sequence length.
_MAX_SHARD_ROWS = 1 << 18

# ═══════════════════════════════════════════════════════════════
# Compute Mesh Service
# ═══════════════════════════════════════════════════════════════

class MeshPeer:
    """Represents a paired device in the compute mesh."""

    def __init__(self, peer_id: str, address: str, public_key: str,
                 capabilities: Optional[dict] = None):
        self.peer_id = peer_id
        self.address = address
        self.public_key = public_key
        self.capabilities = capabilities or {}
        self.last_seen = time.time()  # wall — for the to_dict export/display
        # Monotonic mirror for liveness/age math: a wall-clock jump (#24) must not
        # make a dead peer read as alive (is_stale false-negative -> mesh routes
        # work to a gone peer; false-healthy #6).
        self.last_seen_mono = time.monotonic()
        self.latency_ms: Optional[int] = None
        self.available_compute: float = 0.0  # 0.0 to 1.0
        self.loaded_models: List[str] = []

    def to_dict(self) -> dict:
        return {
            'peer_id': self.peer_id,
            'address': self.address,
            'public_key': self.public_key[:16] + '...',
            'capabilities': self.capabilities,
            'last_seen': self.last_seen,
            'latency_ms': self.latency_ms,
            'available_compute': self.available_compute,
            'loaded_models': self.loaded_models,
            'age_seconds': int(time.monotonic() - self.last_seen_mono),
        }

    def is_stale(self, max_age: int = 300) -> bool:
        """Peer is stale if not seen for max_age seconds (monotonic — a wall-clock
        jump must not make a dead peer read as alive)."""
        return (time.monotonic() - self.last_seen_mono) > max_age


class ComputeMeshService:
    """Same-user device compute aggregation.

    get_available_peers() and score() provide the interface used by
    CodingAgentOrchestrator for trust-based hive offload of coding tasks.
    """

    def __init__(
        self,
        task_relay_port: int = 6796,
        wg_port: int = 6795,
        max_offload_percent: int = 50,
        allow_wan: bool = True,
        stun_server: str = 'stun:stun.l.google.com:19302',
        mesh_interface: str = 'hart-mesh0',
        mesh_subnet: str = '10.99.0.0/16',
        auto_accept: bool = True,
    ):
        self.task_relay_port = task_relay_port
        self.wg_port = wg_port
        self.max_offload_percent = max_offload_percent
        self.allow_wan = allow_wan
        self.stun_server = stun_server
        self.mesh_interface = mesh_interface
        self.mesh_subnet = mesh_subnet
        self.auto_accept = auto_accept

        self._peers: Dict[str, MeshPeer] = {}
        self._lock = threading.Lock()
        self._running = False
        self._device_id: Optional[str] = None
        self._mesh_ip: Optional[str] = None

        # Load device identity
        self._load_identity()

        logger.info(
            f"ComputeMeshService initialized: relay_port={task_relay_port}, "
            f"wg_port={wg_port}, max_offload={max_offload_percent}%"
        )

    def _load_identity(self):
        """Load mesh device identity from filesystem."""
        data_dir = os.environ.get('HEVOLVE_DATA_DIR', '/var/lib/hart')
        key_dir = os.path.join(data_dir, 'mesh', 'keys')

        try:
            mesh_ip_file = os.path.join(key_dir, 'mesh_ip')
            if os.path.exists(mesh_ip_file):
                with open(mesh_ip_file) as f:
                    self._mesh_ip = f.read().strip()
                logger.info(f"Mesh IP: {self._mesh_ip}")

            pub_key_file = os.path.join(key_dir, 'public.key')
            if os.path.exists(pub_key_file):
                with open(pub_key_file) as f:
                    pub_key = f.read().strip()
                self._device_id = hashlib.sha256(pub_key.encode()).hexdigest()[:16]
                logger.info(f"Device ID: {self._device_id}")

            # Load node identity for user verification
            node_key_file = os.path.join(data_dir, 'node_public.key')
            if os.path.exists(node_key_file):
                with open(node_key_file, 'rb') as f:
                    self._node_public_key = f.read()
            else:
                self._node_public_key = None
        except Exception as e:
            logger.warning(f"Could not load mesh identity: {e}")

    # ─── Peer Discovery ──────────────────────────────────────

    def discover_peers(self) -> List[Dict[str, Any]]:
        """Find same-user devices via discovery service."""
        from core.http_pool import pooled_get
        from urllib.parse import urlparse

        discovered = []

        # Query local discovery service for peers
        try:
            resp = pooled_get(f'http://localhost:{get_port("backend")}/api/social/peers', timeout=5)
            if resp.status_code == 200:
                peers = resp.json().get('peers', [])
                for peer in peers:
                    # Only mesh with same-user devices
                    # In production, verify user_id via Ed25519 signature
                    peer_url = peer.get('url', '')
                    peer_address = ''
                    if peer_url:
                        try:
                            peer_address = urlparse(peer_url).hostname or ''
                        except Exception:
                            pass
                    peer_id = peer.get('node_id', '')

                    if peer_address and peer_id:
                        # Check if this peer supports mesh
                        try:
                            mesh_resp = pooled_get(
                                f'{self._peer_base_url(peer_address)}/mesh/status',
                                timeout=3,
                            )
                            if mesh_resp.status_code == 200:
                                mesh_data = mesh_resp.json()
                                with self._lock:
                                    if peer_id not in self._peers:
                                        self._peers[peer_id] = MeshPeer(
                                            peer_id=peer_id,
                                            address=peer_address,
                                            public_key=peer.get('public_key', ''),
                                            capabilities=mesh_data.get('capabilities', {}),
                                        )
                                    else:
                                        self._peers[peer_id].last_seen = time.time()
                                        self._peers[peer_id].last_seen_mono = time.monotonic()
                                        self._peers[peer_id].capabilities = mesh_data.get('capabilities', {})
                                        self._peers[peer_id].available_compute = mesh_data.get('available_compute', 0)
                                        self._peers[peer_id].loaded_models = mesh_data.get('loaded_models', [])

                                discovered.append(self._peers[peer_id].to_dict())
                        except Exception:
                            pass  # Peer doesn't support mesh
        except Exception as e:
            logger.debug(f"Peer discovery error: {e}")

        return discovered

    # ─── Task Offload ────────────────────────────────────────

    def offload_inference(
        self,
        peer_id: str,
        model_type: str,
        prompt: str,
        options: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """Send inference request to a mesh peer — PeerLink first, HTTP fallback."""
        from core.http_pool import pooled_post

        with self._lock:
            peer = self._peers.get(peer_id)

        if not peer:
            return {'error': f'Unknown peer: {peer_id}'}

        if peer.is_stale():
            age = int(time.monotonic() - peer.last_seen_mono)
            return {'error': f'Peer {peer_id} is stale (last seen {age}s ago)'}

        payload = {
            'model_type': model_type,
            'prompt': prompt,
            'options': options or {},
            'source_device': self._device_id,
        }

        # Try PeerLink first (encrypted for cross-user, plain for same-user)
        try:
            from core.peer_link.link_manager import get_link_manager
            link = get_link_manager().get_link(peer_id)
            if link:
                result = link.send('compute', payload,
                                  wait_response=True,
                                  timeout=(options or {}).get('timeout', 120))
                if result and 'error' not in result:
                    result['offloaded_to'] = peer_id
                    result['peer_address'] = peer.address
                    result['transport'] = 'peerlink'
                    return result
        except Exception:
            pass

        # HTTP fallback
        try:
            resp = pooled_post(
                f'{self._peer_base_url(peer.address)}/mesh/infer',
                json=payload,
                timeout=(options or {}).get('timeout', 120),
            )

            if resp.status_code == 200:
                result = resp.json()
                result['offloaded_to'] = peer_id
                result['peer_address'] = peer.address
                return result
            else:
                return {'error': f'Peer returned status {resp.status_code}'}
        except Exception as e:
            return {'error': f'Offload to {peer_id} failed: {str(e)}'}

    def offload_to_best_peer(
        self, model_type: str, prompt: str, options: Optional[dict] = None
    ) -> Dict[str, Any]:
        """Offload inference to the best available mesh peer."""
        with self._lock:
            candidates = [
                p for p in self._peers.values()
                if not p.is_stale() and p.available_compute > 0.1
            ]

        if not candidates:
            return {'error': 'No mesh peers available for offload'}

        # Sort by: model already loaded > available compute > lowest latency
        def score(peer):
            model_bonus = 10 if model_type in peer.loaded_models else 0
            return model_bonus + peer.available_compute * 5 - (peer.latency_ms or 500) / 100

        candidates.sort(key=score, reverse=True)
        best = candidates[0]

        logger.info(
            f"Offloading {model_type} to peer {best.peer_id} "
            f"(compute={best.available_compute:.1%}, models={best.loaded_models})"
        )

        return self.offload_inference(best.peer_id, model_type, prompt, options)

    # ─── Shard relay (torch-free activation-envelope forwarding) ──

    def _handle_shard_frame(self, raw: bytes) -> bytes:
        """Relay handler for one shard-runtime frame — parse + torch-free echo.

        Reads the inbound frame's routing fields (``read_routing`` fails closed
        on any malformed/unroutable frame), proves the int32 round-trip for the
        one frame HART itself produces (``token_ids`` -> ``decode_token_ids``),
        then emits a CONTRACT-VALID ``activation`` echo: same ``request_id`` and
        ``model_id``, ``order_index + 1``, dtype ``bfloat16``, shape
        ``[1, N, HIDDEN]`` with an opaque deterministic payload sized exactly
        ``2 * prod(shape)`` (bf16 = 2 bytes/element). The tensor CONTENTS are
        the hevolveai ShardBackend boundary; here they are an honest,
        correctly-sized stand-in. Raises ``EnvelopeError`` (mapped to HTTP 400
        by the route) so an unroutable frame is never forwarded.
        """
        from core.shard_runtime.envelope import (
            read_routing, parse_header, decode_token_ids, frame, PROTOCOL_VERSION,
            EnvelopeError,
        )

        routing = read_routing(raw)          # EnvelopeError => fail closed
        header, _ = parse_header(raw)
        kind = header.get('kind')

        if kind == 'token_ids':
            n = len(decode_token_ids(raw))   # prove int32 round-trip
        else:
            shape = header.get('shape')
            # Validate the attacker-controlled shape BEFORE deriving a row count:
            # a bad TYPE would raise ValueError/TypeError (escaping the 400 path as
            # a 500), and an unbounded N amplifies a tiny header into a multi-GB
            # allocation. Fail closed to EnvelopeError -> HTTP 400.
            if not isinstance(shape, (list, tuple)) or not (2 <= len(shape) <= 3):
                raise EnvelopeError(
                    f"activation frame needs a 2- or 3-element shape, got {shape!r}")
            try:
                dims = [int(d) for d in shape]
            except (TypeError, ValueError):
                raise EnvelopeError(f"activation shape dims must be ints, got {shape!r}")
            if any(d < 0 for d in dims):
                raise EnvelopeError(f"activation shape dims must be non-negative, got {dims}")
            # rows count is the token dimension (last dim of [1, N] / middle of [1, N, H])
            n = dims[-1] if len(dims) == 2 else dims[1]

        if n > _MAX_SHARD_ROWS:
            raise EnvelopeError(f"shard row count {n} exceeds max {_MAX_SHARD_ROWS}")

        # ── Real ShardBackend seam ───────────────────────────────
        # If a ShardBackend (hevolveai's layer-slice executor, reached via the
        # Model Bus) is configured, forward the VALIDATED frame to it and return
        # its real activation/logits. The stand-in below runs ONLY when no backend
        # is present (standalone / proof nodes), so one relay path serves both the
        # real executor and the transport-only stand-in with zero forks.
        backend_url = os.environ.get('HART_SHARD_BACKEND_URL')
        if backend_url:
            out = self._forward_to_shard_backend(raw, routing, backend_url)
            if out is not None:
                return out
            # backend unreachable / errored -> degrade to the stand-in below.

        hidden = _ACTIVATION_HIDDEN
        out_shape = [1, n, hidden]
        nbytes = 2 * n * hidden               # 2 * prod(out_shape)

        # Deterministic opaque payload keyed on request_id (repeatable, no RNG).
        seed = hashlib.sha256(str(routing['request_id']).encode()).digest()
        payload = (seed * ((nbytes // len(seed)) + 1))[:nbytes] if nbytes else b''

        out_header = {
            'v': PROTOCOL_VERSION,
            'model_id': routing['model_id'],
            'request_id': routing['request_id'],
            'order_index': int(routing['order_index']) + 1,
            'seq_pos': int(routing.get('seq_pos', 0) or 0),
            'dtype': 'bfloat16',
            'shape': out_shape,
            'kind': 'activation',
        }
        return frame(out_header, payload)

    def _forward_to_shard_backend(self, raw: bytes, routing: dict, backend_url: str):
        """Forward the frame to the real ShardBackend; return response bytes or None.

        The ONE seam where hevolveai's layer-slice forward (reached via the Model
        Bus ``/v1/shard/forward``) replaces the stand-in. Body is the activation
        envelope (opaque bytes); per the frozen contract the backend keys its
        loaded layer-range by ``request_id`` and returns the response envelope
        (an activation, or logits on the last shard). Returns None on ANY error so
        the caller degrades to the stand-in rather than dropping the frame
        (degrade-not-die).
        """
        try:
            from core.http_pool import pooled_post
            resp = pooled_post(
                backend_url.rstrip('/') + '/v1/shard/forward',
                data=raw,
                headers={
                    'Content-Type': 'application/octet-stream',
                    'X-Shard-Request-Id': str(routing['request_id']),
                    'X-Shard-Order-Index': str(routing['order_index']),
                    'X-Shard-Model-Id': str(routing['model_id']),
                },
                timeout=120,
            )
            if resp.status_code == 200 and resp.content:
                return resp.content
            logger.warning("ShardBackend %s returned %s; using stand-in",
                           backend_url, getattr(resp, 'status_code', '?'))
        except Exception as e:
            logger.warning("ShardBackend %s error; using stand-in: %s", backend_url, e)
        return None

    def relay_shard(
        self,
        peer_id: str,
        frame_bytes: bytes,
        timeout: int = 120,
        wait_response: bool = True,
    ) -> bytes:
        """Relay one shard-runtime frame to a peer — PeerLink first, HTTP fallback.

        Sibling of ``offload_inference`` using the same two-leg shape:

          * PeerLink 'compute' channel (0x01) ``send_binary`` first. This leg is
            fire-and-forget (PeerLink binary is one-way in this build), so it is
            used only for a forward that needs no synchronous answer
            (``wait_response=False`` — the multi-hop relay case). Its try/except
            falls through when no live link exists (the zero-pip / no-websockets
            path), exactly like ``offload_inference``.
          * HTTP ``POST /mesh/shard`` (application/octet-stream) via ``pooled_post``
            — the request/response leg that returns the ``activation`` echo. This
            is the leg the localhost self-test and the LAN proof exercise.

        Returns the raw response frame bytes. Raises ``EnvelopeError`` on a
        transport failure or a non-200 (a rejected/unroutable frame is surfaced,
        never silently dropped).
        """
        from core.shard_runtime.envelope import EnvelopeError

        with self._lock:
            peer = self._peers.get(peer_id)
        address = peer.address if peer else peer_id  # allow host:port passthrough

        # Leg 1 — PeerLink binary forward (only when no echo is required).
        if not wait_response:
            try:
                from core.peer_link.link_manager import get_link_manager
                link = get_link_manager().get_link(peer_id)
                if link and link.send_binary('compute', frame_bytes):
                    return b''
            except Exception:
                pass

        # Leg 2 — HTTP POST /mesh/shard (request/response, returns the echo).
        from core.http_pool import pooled_post
        try:
            resp = pooled_post(
                f'{self._peer_base_url(address)}/mesh/shard',
                data=frame_bytes,
                headers={'Content-Type': 'application/octet-stream'},
                timeout=timeout,
            )
        except Exception as e:
            raise EnvelopeError(f'shard relay transport to {peer_id} failed: {e}')

        if resp.status_code == 200:
            return resp.content
        raise EnvelopeError(
            f'shard relay to {peer_id} rejected: HTTP {resp.status_code}')

    def get_available_peers(self) -> List[Dict[str, Any]]:
        """Return non-stale peers as dicts (used by CodingAgentOrchestrator)."""
        with self._lock:
            return [p.to_dict() for p in self._peers.values() if not p.is_stale()]

    def score(self, peer: Dict) -> float:
        """Score a peer dict by compute availability and latency."""
        compute = peer.get('available_compute', 0)
        latency = peer.get('latency_ms') or 500
        return compute * 5 - latency / 100

    # ─── Device Pairing ──────────────────────────────────────

    def _peer_base_url(self, address: str) -> str:
        """Build the http base URL for a peer address.

        ``address`` may be a bare host (``'192.168.0.9'``) or already carry a
        port (``'127.0.0.1:6796'``). A bare host gets this node's mesh relay
        port appended (the shipped same-user discovery flow). An address that
        already names a port is used verbatim — this is what lets two nodes on
        the SAME host bind different ports (the localhost two-node self-test)
        and lets the LAN proof target an explicit ``host:port`` without
        assuming both sides share a port. Behaviour-preserving for the existing
        host-only callers.
        """
        has_port = (':' in address) and not address.startswith('[')
        if has_port:
            return f'http://{address}'
        return f'http://{address}:{self.task_relay_port}'

    def pair_device(self, peer_address: str) -> Dict[str, Any]:
        """Initiate pairing with a new device."""
        from core.http_pool import pooled_post

        try:
            # Send pairing challenge
            challenge = hashlib.sha256(os.urandom(32)).hexdigest()
            resp = pooled_post(
                f'{self._peer_base_url(peer_address)}/mesh/pair',
                json={
                    'action': 'challenge',
                    'challenge': challenge,
                    'device_id': self._device_id,
                    'mesh_ip': self._mesh_ip,
                },
                timeout=10,
            )

            if resp.status_code == 200:
                result = resp.json()
                if result.get('accepted'):
                    peer_id = result.get('device_id', peer_address)
                    with self._lock:
                        self._peers[peer_id] = MeshPeer(
                            peer_id=peer_id,
                            address=peer_address,
                            public_key=result.get('public_key', ''),
                            capabilities=result.get('capabilities', {}),
                        )
                    logger.info(f"Paired with device: {peer_id} at {peer_address}")
                    return {'status': 'paired', 'peer_id': peer_id}
                else:
                    return {'status': 'rejected', 'reason': result.get('reason', 'unknown')}
            else:
                return {'error': f'Pairing failed: HTTP {resp.status_code}'}
        except Exception as e:
            return {'error': f'Pairing failed: {str(e)}'}

    # ─── Status ──────────────────────────────────────────────

    def get_mesh_status(self) -> Dict[str, Any]:
        """Get aggregate compute inventory across all paired devices."""
        with self._lock:
            active_peers = [p for p in self._peers.values() if not p.is_stale()]

        # Get local capabilities
        local_caps = self._get_local_capabilities()

        return {
            'status': 'running' if self._running else 'stopped',
            'device_id': self._device_id,
            'mesh_ip': self._mesh_ip,
            'peer_count': len(active_peers),
            'total_peers_known': len(self._peers),
            'local': local_caps,
            'peers': [p.to_dict() for p in active_peers],
            'aggregate': {
                'total_compute': local_caps.get('available_compute', 0) + sum(
                    p.available_compute for p in active_peers
                ),
                'total_models': list(set(
                    local_caps.get('loaded_models', []) +
                    [m for p in active_peers for m in p.loaded_models]
                )),
            },
            'max_offload_percent': self.max_offload_percent,
            'allow_wan': self.allow_wan,
        }

    def _get_local_capabilities(self) -> dict:
        """Detect local compute capabilities."""
        import shutil

        caps = {
            'cpu_count': os.cpu_count() or 1,
            'available_compute': 1.0 - (self.max_offload_percent / 100.0),
            'loaded_models': [],
        }

        # Detect GPU (delegate to VRAMManager — single source of truth)
        try:
            from integrations.service_tools.vram_manager import vram_manager
            gpu_info = vram_manager.detect_gpu()
            if gpu_info.get('cuda_available'):
                caps['gpu'] = f"{gpu_info.get('name', 'GPU')}, {gpu_info.get('total_gb', 0)}GB"
            else:
                caps['gpu'] = gpu_info.get('name') or None
        except Exception:
            pass

        # Detect total RAM cross-platform. psutil first; then POSIX sysconf
        # (Linux + macOS); then Windows GlobalMemoryStatusEx; then /proc/meminfo.
        # A psutil-less WINDOWS node previously reported NO ram_gb (only a Linux
        # /proc fallback existed), so the capability meter under-reported the hive.
        ram_bytes = None
        try:
            import psutil
            ram_bytes = psutil.virtual_memory().total
        except Exception:
            pass
        if not ram_bytes:
            try:  # POSIX (Linux + macOS)
                ram_bytes = os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
            except (ValueError, AttributeError, OSError):
                pass
        if not ram_bytes:
            try:  # macOS without psutil / where SC_PHYS_PAGES is absent: sysctl
                import sys
                if sys.platform == 'darwin':
                    import subprocess
                    _o = subprocess.run(['sysctl', '-n', 'hw.memsize'],
                                        capture_output=True, text=True, timeout=3, **no_window_kwargs())
                    if _o.returncode == 0 and _o.stdout.strip().isdigit():
                        ram_bytes = int(_o.stdout.strip())
            except Exception:
                pass
        if not ram_bytes and os.name == 'nt':  # Windows, no psutil
            try:
                import ctypes

                class _MEMSTATUS(ctypes.Structure):
                    _fields_ = [('dwLength', ctypes.c_ulong),
                                ('dwMemoryLoad', ctypes.c_ulong),
                                ('ullTotalPhys', ctypes.c_ulonglong),
                                ('ullAvailPhys', ctypes.c_ulonglong),
                                ('ullTotalPageFile', ctypes.c_ulonglong),
                                ('ullAvailPageFile', ctypes.c_ulonglong),
                                ('ullTotalVirtual', ctypes.c_ulonglong),
                                ('ullAvailVirtual', ctypes.c_ulonglong),
                                ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]

                ms = _MEMSTATUS()
                ms.dwLength = ctypes.sizeof(_MEMSTATUS)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                    ram_bytes = ms.ullTotalPhys
            except Exception:
                pass
        if not ram_bytes:
            try:  # last resort: parse /proc/meminfo
                with open('/proc/meminfo') as f:
                    for line in f:
                        if line.startswith('MemTotal:'):
                            ram_bytes = int(line.split()[1]) * 1024
                            break
            except Exception:
                pass
        if ram_bytes:
            caps['ram_gb'] = round(ram_bytes / (1024 ** 3), 1)

        # loaded_models is read from a CACHE refreshed OFF the hot path by
        # _refresh_loaded_models() (kicked + looped in _start_background_loops).
        # Probing the Model Bus synchronously here blocked get_mesh_status AND
        # every /mesh/pair handshake by the pooled_get timeout (~4s measured)
        # whenever the bus is not up, which is every standalone / relay node.
        caps['loaded_models'] = list(getattr(self, '_loaded_models_cache', []) or [])

        return caps

    def _refresh_loaded_models(self):
        """Best-effort refresh of the loaded-models cache (BACKGROUND ONLY).

        Never call on the /mesh/status or /mesh/pair hot path: pooled_get to a
        down Model Bus blocks ~4s. Runs from _start_background_loops (one-shot at
        start + every discovery tick). A node without `requests` (the zero-dep
        stdlib relay vehicle) degrades to an empty list instead of raising.
        """
        models = []
        try:
            from core.http_pool import pooled_get as _pooled_get
            resp = _pooled_get(
                f'http://localhost:{get_port("model_bus")}/v1/models', timeout=2)
            if resp.status_code == 200:
                models = [m.get('type', 'unknown')
                          for m in resp.json().get('models', [])]
        except Exception:
            pass
        self._loaded_models_cache = models

    # ─── HTTP Server ─────────────────────────────────────────

    # Route handlers below are the SINGLE source of route truth. Each is a pure
    # function of the raw request body -> (status:int, content_type:str,
    # body:bytes). Both the Flask app (_create_flask_app) and the stdlib runner
    # (mesh_node_runner) dispatch from route_table() so the two transports are
    # bindings over ONE handler set, never parallel route tables.

    @staticmethod
    def _json_response(obj: Any, status: int = 200):
        return (status, 'application/json', json.dumps(obj).encode('utf-8'))

    def _route_status(self, body: bytes):
        return self._json_response(self.get_mesh_status())

    def _route_peers(self, body: bytes):
        with self._lock:
            peers = [p.to_dict() for p in self._peers.values()]
        return self._json_response({'peers': peers})

    def _route_pair(self, body: bytes):
        try:
            data = json.loads(body.decode('utf-8')) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json_response({'error': 'Invalid JSON'}, 400)
        if not isinstance(data, dict):
            return self._json_response({'error': 'Invalid pairing request'}, 400)

        if data.get('action') == 'challenge':
            # Incoming pairing request
            if self.auto_accept:
                # Auto-accept same-user devices
                return self._json_response({
                    'accepted': True,
                    'device_id': self._device_id,
                    'public_key': '',  # WireGuard public key
                    'capabilities': self._get_local_capabilities(),
                })
            return self._json_response(
                {'accepted': False, 'reason': 'manual approval required'})
        elif 'peer_address' in data:
            # Outgoing pairing request
            return self._json_response(self.pair_device(data['peer_address']))
        return self._json_response({'error': 'Invalid pairing request'}, 400)

    def _compute_contribute_consented(self) -> bool:
        """Fail-CLOSED gate for serving HIVE compute (the compute_contribute consent).

        Contributing THIS device's compute to the hive is EXPLICIT OPT-IN (privacy-
        first): a peer's inference/shard work is served ONLY if a compute_contribute
        consent has been granted. Reuses the canonical UserConsent table — NO new
        store. Device-level: any granted row authorises the device. ANY failure (no
        consent system / cold table / no db) returns False, so the device NEVER
        contributes compute the human did not authorise (humans-always-in-control).
        Closes the audited gap: compute_contribute was DEFINED but enforced NOWHERE."""
        try:
            from integrations.social.models import db_session, UserConsent
            with db_session(commit=False) as db:
                return db.query(UserConsent).filter(
                    UserConsent.consent_type == 'compute_contribute',
                    UserConsent.granted == True,
                ).first() is not None
        except Exception:
            return False

    def _route_infer(self, body: bytes):
        # compute_contribute gate (opt-in, fail-closed): never run a PEER's inference
        # on this device without the owner's explicit consent.
        if not self._compute_contribute_consented():
            return self._json_response(
                {'error': 'compute_contribute consent not granted — this device does '
                          'not serve hive compute (opt-in required)',
                 'code': 'consent_required'}, 403)
        try:
            data = json.loads(body.decode('utf-8')) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json_response({'error': 'Invalid JSON'}, 400)
        model_type = data.get('model_type', 'llm')
        prompt = data.get('prompt', '')

        # Forward to local Model Bus
        from core.http_pool import pooled_post as _pooled_post
        try:
            resp = _pooled_post(
                f'http://localhost:{get_port("model_bus")}/v1/chat',
                json={'prompt': prompt, 'model_type': model_type},
                timeout=120,
            )
            if resp.status_code == 200:
                result = resp.json()
                result['served_by'] = self._device_id
                return self._json_response(result)
            return self._json_response(
                {'error': f'Local inference failed: {resp.status_code}'}, 502)
        except Exception as e:
            return self._json_response(
                {'error': f'Local inference error: {str(e)}'}, 502)

    def _route_shard(self, body: bytes):
        """Relay one shard-runtime frame. Fail-closed to HTTP 400 on a bad frame."""
        # compute_contribute gate: serving a sharded-model frame contributes this
        # device's compute to the hive, so it is opt-in + fail-closed, same as
        # _route_infer (one gate, no parallel consent path).
        if not self._compute_contribute_consented():
            return self._json_response(
                {'error': 'compute_contribute consent not granted', 'code': 'consent_required'}, 403)
        from core.shard_runtime.envelope import EnvelopeError
        try:
            out = self._handle_shard_frame(body)
        except EnvelopeError as e:
            return self._json_response({'error': str(e)}, 400)
        return (200, 'application/octet-stream', out)

    def _route_health(self, body: bytes):
        return self._json_response({'status': 'ok', 'service': 'compute-mesh'})

    def route_table(self) -> Dict:
        """Single source of route truth: {(verb, path): handler}.

        handler(body: bytes) -> (status:int, ctype:str, body:bytes).
        Consumed by BOTH _create_flask_app() and the stdlib mesh_node_runner so
        the Flask app and the zero-dependency stdlib fallback expose byte-for-byte
        identical endpoints (an alternate transport binding, not a second router).
        """
        return {
            ('GET', '/mesh/status'): self._route_status,
            ('POST', '/mesh/status'): self._route_status,
            ('GET', '/mesh/peers'): self._route_peers,
            ('POST', '/mesh/pair'): self._route_pair,
            ('POST', '/mesh/infer'): self._route_infer,
            ('POST', '/mesh/shard'): self._route_shard,
            ('GET', '/health'): self._route_health,
        }

    def _create_flask_app(self):
        """Create Flask app for task relay HTTP API (registers FROM route_table)."""
        from flask import Flask, request, Response

        app = Flask(__name__)

        def _make_view(handler):
            def _view(**_kw):
                raw = request.get_data() or b''
                status, ctype, out = handler(raw)
                return Response(out, status=status, mimetype=ctype)
            return _view

        for (verb, path), handler in self.route_table().items():
            app.add_url_rule(
                path,
                endpoint=f'{verb}:{path}',
                view_func=_make_view(handler),
                methods=[verb],
            )

        return app

    # ─── Serve ───────────────────────────────────────────────

    def _start_background_loops(self):
        """Start discovery + peer-health background threads (idempotent-ish).

        Extracted from serve_forever so the Flask path AND the stdlib runner
        start identical background work. Sets _running = True (get_mesh_status
        reports 'running' once loops are live).
        """
        with self._lock:
            if self._running:
                return
            self._running = True
            if not hasattr(self, '_loaded_models_cache'):
                self._loaded_models_cache = []
        # Prime the loaded-models cache OFF the hot path (see _get_local_capabilities).
        threading.Thread(target=self._refresh_loaded_models, daemon=True).start()

        # Background: periodic peer discovery
        def _discovery_loop():
            while self._running:
                try:
                    self.discover_peers()
                    self._refresh_loaded_models()
                except Exception as e:
                    logger.debug(f"Peer discovery error: {e}")
                time.sleep(30)

        # Background: peer health check
        def _health_loop():
            while self._running:
                time.sleep(60)
                with self._lock:
                    stale = [pid for pid, p in self._peers.items() if p.is_stale(600)]
                    for pid in stale:
                        logger.info(f"Removing stale peer: {pid}")
                        del self._peers[pid]

        threading.Thread(target=_discovery_loop, daemon=True).start()
        threading.Thread(target=_health_loop, daemon=True).start()

    def stop(self):
        """Signal the background loops to exit (idempotent).

        Flips _running so get_mesh_status reports 'stopped' and the discovery /
        health loops end on their next wake. Loops are daemon threads; callers
        that spin up many ephemeral nodes (tests, the proof harness) call this to
        stop background work deterministically.
        """
        self._running = False

    def serve_forever(self, host: str = '0.0.0.0'):
        """Start the Compute Mesh service (Flask transport).

        host defaults to 0.0.0.0 for the integrated app; the standalone runner
        passes loopback by default (the pairing handshake is not yet
        signature-authenticated, so LAN exposure is an explicit opt-in).
        """
        self._start_background_loops()

        # Start Flask HTTP server for task relay
        app = self._create_flask_app()
        logger.info(f"Compute Mesh task relay starting on {host}:{self.task_relay_port}")

        try:
            from waitress import serve
            serve(app, host=host, port=self.task_relay_port, threads=4)
        except ImportError:
            app.run(host=host, port=self.task_relay_port, threaded=True)


# ─── Module-level singleton ─────────────────────────────────
_mesh_instance: Optional[ComputeMeshService] = None
_mesh_lock = threading.Lock()


def get_compute_mesh() -> ComputeMeshService:
    """Get or create the singleton ComputeMeshService."""
    global _mesh_instance
    if _mesh_instance is None:
        with _mesh_lock:
            if _mesh_instance is None:
                _mesh_instance = ComputeMeshService()
    return _mesh_instance
