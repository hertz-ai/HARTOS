"""End-to-end tests for the shard-orchestrator + the ShardBackend seam.

Real loopback mesh nodes (not fakes): the orchestrator drives a multi-hop relay
across them over real TCP, and the seam is proven by pointing
HART_SHARD_BACKEND_URL at a REAL reference backend HTTP server that implements
/v1/shard/forward per the frozen contract. That reference server is the
executable contract stub hevolveai's C3 replaces with torch; it MARKS its output
(order_index += 100) so the test proves the frame came from the backend, not the
node's built-in stand-in.
"""
import http.server
import threading
import time
import urllib.request
import uuid

import pytest

from integrations.agent_engine.mesh_node_runner import (
    build_ephemeral_mesh,
    build_stdlib_server,
)
from core.shard_runtime.orchestrator import ShardOrchestrator
from core.shard_runtime.envelope import (
    parse_header,
    payload_view,
    frame as build_frame,
    PROTOCOL_VERSION,
)


def _prod(shape):
    out = 1
    for d in shape:
        out *= d
    return out


def _await_health(port, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.03)
    return False


class _MeshNode:
    """One real stdlib mesh node on 127.0.0.1 (reuses the shipped runner)."""

    def __init__(self):
        self.mesh, self.data_dir = build_ephemeral_mesh(port=0)
        self.server = build_stdlib_server(self.mesh, host='127.0.0.1', port=0)
        self.port = self.server.server_address[1]
        self.mesh.task_relay_port = self.port
        self.mesh._start_background_loops()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        assert _await_health(self.port), 'node /health never came up'

    def shutdown(self):
        try:
            self.mesh.stop()
        except Exception:
            pass
        try:
            self.server.shutdown()
        except Exception:
            pass


class _RefShardBackend:
    """A REAL HTTP server implementing /v1/shard/forward per the frozen contract.

    Returns a contract-valid activation with order_index += 100 and model_id
    prefixed 'REF-', so a test can prove the frame was produced by the backend
    (via the seam) and NOT by the node's stand-in (which does order_index + 1).
    This is the executable contract stub; C3 replaces the body with real torch.
    """

    def __init__(self):
        class _H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):
                return

            def do_POST(self):
                if self.path != '/v1/shard/forward':
                    self.send_response(404)
                    self.end_headers()
                    return
                ln = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(ln) if ln else b''
                hdr, _ = parse_header(raw)
                out = build_frame(
                    {'v': PROTOCOL_VERSION,
                     'model_id': 'REF-' + str(hdr['model_id']),
                     'request_id': hdr['request_id'],
                     'order_index': int(hdr['order_index']) + 100,
                     'seq_pos': 0, 'dtype': 'bfloat16',
                     'shape': [1, 1, 8], 'kind': 'activation'},
                    b'\x00' * (2 * 1 * 8))
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}'

    def shutdown(self):
        try:
            self.server.shutdown()
        except Exception:
            pass


@pytest.fixture
def nodes():
    made = []

    def _make(n):
        ns = [_MeshNode() for _ in range(n)]
        made.extend(ns)
        return ns

    yield _make
    for x in made:
        x.shutdown()


# ═══ Orchestrator drives a real multi-hop pipeline ═══

def test_orchestrator_drives_multihop_pipeline(nodes):
    """A(entry) -> B -> C -> D over real TCP; order_index chains 0->3, id stable."""
    a, b, c, d = nodes(4)
    for peer in (b, c, d):
        res = a.mesh.pair_device(f'127.0.0.1:{peer.port}')
        assert res.get('status') == 'paired'
    peer_ids = [b.mesh._device_id, c.mesh._device_id, d.mesh._device_id]
    assert len(set(peer_ids)) == 3

    orch = ShardOrchestrator(a.mesh)
    req = str(uuid.uuid4())
    ids = [3, 5, 7, 9]
    out = orch.run('pipe', ids, peer_ids, request_id=req)

    hdr = orch.final_header(out)
    assert hdr['request_id'] == req                    # stable across every hop
    assert hdr['order_index'] == 3                     # 0 -> B(1) -> C(2) -> D(3)
    assert hdr['kind'] == 'activation'                 # logits once a real last-shard exists
    assert hdr['shape'][1] == len(ids)                 # token dim preserved down the ring
    assert len(payload_view(out)) == 2 * _prod(hdr['shape'])


def test_orchestrator_empty_cluster_raises(nodes):
    a, = nodes(1)
    orch = ShardOrchestrator(a.mesh)
    with pytest.raises(ValueError):
        orch.run('m', [1, 2, 3], [])


# ═══ The ShardBackend seam (node calls a REAL backend when configured) ═══

def test_seam_forwards_to_real_backend(nodes, monkeypatch):
    """With HART_SHARD_BACKEND_URL set, the node forwards to the reference backend
    and returns ITS response (order_index += 100 + 'REF-' model), proving the seam
    rather than the built-in stand-in (which does order_index + 1)."""
    ref = _RefShardBackend()
    try:
        monkeypatch.setenv('HART_SHARD_BACKEND_URL', ref.url)
        a, b = nodes(2)
        a.mesh.pair_device(f'127.0.0.1:{b.port}')

        orch = ShardOrchestrator(a.mesh)
        req = str(uuid.uuid4())
        out = orch.run('m', [1, 2, 3], [b.mesh._device_id], request_id=req)

        hdr = orch.final_header(out)
        assert hdr['order_index'] == 100          # backend marker, NOT stand-in's +1
        assert hdr['model_id'] == 'REF-m'         # produced by the reference backend
        assert hdr['request_id'] == req
    finally:
        ref.shutdown()


def test_seam_degrades_to_standin_on_backend_error(nodes, monkeypatch):
    """A configured-but-dead backend must degrade to the stand-in, not drop the
    frame (degrade-not-die). Stand-in produces order_index + 1."""
    monkeypatch.setenv('HART_SHARD_BACKEND_URL', 'http://127.0.0.1:9')  # nothing listens
    a, b = nodes(2)
    a.mesh.pair_device(f'127.0.0.1:{b.port}')
    orch = ShardOrchestrator(a.mesh)
    out = orch.run('m', [1, 2, 3], [b.mesh._device_id])
    hdr = orch.final_header(out)
    assert hdr['order_index'] == 1                 # stand-in fallback, not the backend
    assert hdr['kind'] == 'activation'


def test_no_backend_uses_standin(nodes, monkeypatch):
    """No HART_SHARD_BACKEND_URL -> the transport-only stand-in (unchanged path)."""
    monkeypatch.delenv('HART_SHARD_BACKEND_URL', raising=False)
    a, b = nodes(2)
    a.mesh.pair_device(f'127.0.0.1:{b.port}')
    orch = ShardOrchestrator(a.mesh)
    out = orch.run('m', [1, 2, 3, 4, 5], [b.mesh._device_id])
    hdr = orch.final_header(out)
    assert hdr['order_index'] == 1
    assert hdr['shape'][1] == 5
