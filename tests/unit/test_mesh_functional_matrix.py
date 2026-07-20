"""Exhaustive FUNCTIONAL matrix for the mesh shard-transport, exercised end to end.

Every test starts one or more REAL stdlib ComputeMesh nodes on 127.0.0.1 (real
loopback TCP, not in-process fakes), pairs over the real /mesh/pair handshake,
and drives real /mesh/shard relays + the real envelope. Nothing is grepped or
string-asserted: each case observes a behaviour (pairing result, echo bytes,
HTTP status, order_index chain, concurrency isolation) and asserts on it.

Complements tests/unit/test_mesh_shard_transport.py (the base 7) with the edge,
failure, multi-hop, concurrency, multi-peer, large-payload and lifecycle
dimensions. Node start/stop reuses the shipped mesh_node_runner helpers so there
is one canonical way to bring a node up (no parallel node impl).
"""
import struct
import threading
import time
import urllib.request
import uuid

import pytest

from integrations.agent_engine.mesh_node_runner import (
    build_ephemeral_mesh,
    build_stdlib_server,
)
from core.shard_runtime.envelope import (
    token_ids_frame,
    frame as build_frame,
    parse_header,
    read_routing,
    payload_view,
    PROTOCOL_VERSION,
)


# ─── node lifecycle helper (reuses shipped runner; one canonical start path) ───

class _Node:
    def __init__(self):
        self.mesh, self.data_dir = build_ephemeral_mesh(port=0)
        self.server = build_stdlib_server(self.mesh, host='127.0.0.1', port=0)
        self.port = self.server.server_address[1]
        self.mesh.task_relay_port = self.port
        self.mesh._start_background_loops()
        self._t = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._t.start()
        assert self._await_health(), 'node /health never came up'

    @property
    def device_id(self):
        return self.mesh._device_id

    @property
    def addr(self):
        return f'127.0.0.1:{self.port}'

    def _await_health(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f'http://{self.addr}/health', timeout=1) as r:
                    if r.status == 200:
                        return True
            except Exception:
                time.sleep(0.03)
        return False

    def get(self, path):
        with urllib.request.urlopen(f'http://{self.addr}{path}', timeout=4) as r:
            return r.status, r.read()

    def post_raw(self, path, body, ctype='application/octet-stream'):
        """POST arbitrary bytes to a route; return (status, body). Never raises on 4xx."""
        req = urllib.request.Request(f'http://{self.addr}{path}', data=body,
                                     headers={'Content-Type': ctype}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=4) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def shutdown(self):
        try:
            self.mesh.stop()          # end background loops deterministically
        except Exception:
            pass
        try:
            self.server.shutdown()
        except Exception:
            pass


@pytest.fixture
def nodes():
    made = []

    def _make(n=1):
        ns = [_Node() for _ in range(n)]
        made.extend(ns)
        return ns if n > 1 else ns[0]

    yield _make
    for n in made:
        n.shutdown()


def _prod(shape):
    out = 1
    for d in shape:
        out *= d
    return out


def _shard_path(mesh):
    """Discover the shard route at RUNTIME from route_table (not by grepping)."""
    for (verb, path) in mesh.route_table().keys():
        if verb == 'POST' and 'shard' in path:
            return path
    raise AssertionError(f'no shard route in {list(mesh.route_table().keys())}')


# ═══ 1. Route surface (exercised, not asserted-by-string) ═══

def test_route_table_exposes_the_expected_verbs(nodes):
    a = nodes(1)
    routes = set(a.mesh.route_table().keys())
    # every route we depend on must actually dispatch
    assert ('GET', '/health') in routes
    assert any(p == '/mesh/status' for (_v, p) in routes)
    assert any(p == '/mesh/pair' for (_v, p) in routes)
    assert any('shard' in p for (_v, p) in routes)


def test_health_returns_200(nodes):
    a = nodes(1)
    status, body = a.get('/health')
    assert status == 200
    assert b'ok' in body


def test_status_reports_real_host_metrics(nodes):
    import json
    a = nodes(1)
    _s, body = a.get('/mesh/status')
    d = json.loads(body)
    assert d['device_id'] == a.device_id
    assert d['local']['cpu_count'] >= 1
    assert d['local']['ram_gb'] > 0


# ═══ 2. Pairing matrix ═══

def test_pair_success_distinct_device_ids(nodes):
    a, b = nodes(2)
    assert a.device_id != b.device_id
    res = a.mesh.pair_device(b.addr)
    assert res.get('status') == 'paired'
    assert res['peer_id'] == b.device_id
    assert b.device_id in a.mesh._peers


def test_pair_is_idempotent_on_repair(nodes):
    a, b = nodes(2)
    a.mesh.pair_device(b.addr)
    a.mesh.pair_device(b.addr)  # second time must not error or duplicate
    peer_ids = [pid for pid in a.mesh._peers]
    assert peer_ids.count(b.device_id) == 1


def test_pair_unreachable_peer_fails_gracefully(nodes):
    a = nodes(1)
    res = a.mesh.pair_device('127.0.0.1:9')  # nothing listens on :9
    assert res.get('status') != 'paired'


def test_node_can_pair_with_two_peers(nodes):
    a, b, c = nodes(3)
    a.mesh.pair_device(b.addr)
    a.mesh.pair_device(c.addr)
    assert b.device_id in a.mesh._peers
    assert c.device_id in a.mesh._peers
    assert len({a.device_id, b.device_id, c.device_id}) == 3


# ═══ 3. Shard relay happy variants ═══

@pytest.mark.parametrize('ids', [[7], [1, 2, 3, 4], list(range(64))])
def test_relay_token_ids_round_trips_activation(nodes, ids):
    a, b = nodes(2)
    a.mesh.pair_device(b.addr)
    req = str(uuid.uuid4())
    frame = token_ids_frame('m', req, ids, order_index=0)
    echo = a.mesh.relay_shard(b.device_id, frame)
    hdr, _ = parse_header(echo)
    assert hdr['kind'] == 'activation'
    assert hdr['request_id'] == req                       # request_id survives the wire
    assert hdr['order_index'] == 1                        # inbound 0 + 1
    assert hdr['shape'][1] == len(ids)                    # token dim preserved
    assert len(payload_view(echo)) == 2 * _prod(hdr['shape'])  # 2 bytes/bf16 elem


def test_relay_large_token_list_round_trips(nodes):
    a, b = nodes(2)
    a.mesh.pair_device(b.addr)
    ids = list(range(4096))
    req = str(uuid.uuid4())
    echo = a.mesh.relay_shard(b.device_id, token_ids_frame('big', req, ids, order_index=0))
    hdr, _ = parse_header(echo)
    assert hdr['request_id'] == req
    assert hdr['shape'][1] == 4096


# ═══ 4. Multi-hop pipeline (the actual sharding topology) ═══

def test_three_hop_pipeline_order_index_chains(nodes):
    """token_ids -> A relays to B (order 1) -> relay B's activation to C (order 2).

    Proves a real K-node pipeline: order_index chains 0->1->2 and request_id is
    stable across every hop, which is exactly how contiguous layer ranges pass
    activations down the line.
    """
    a, b, c = nodes(3)
    a.mesh.pair_device(b.addr)
    a.mesh.pair_device(c.addr)
    req = str(uuid.uuid4())

    hop1 = a.mesh.relay_shard(b.device_id, token_ids_frame('pipe', req, [5, 6, 7], order_index=0))
    h1, _ = parse_header(hop1)
    assert h1['order_index'] == 1 and h1['request_id'] == req and h1['kind'] == 'activation'

    hop2 = a.mesh.relay_shard(c.device_id, hop1)  # feed B's activation into C
    h2, _ = parse_header(hop2)
    assert h2['order_index'] == 2, 'order_index must chain across hops'
    assert h2['request_id'] == req, 'request_id must be stable across the whole pipeline'
    assert h2['kind'] == 'activation'


# ═══ 5. Fail-closed on the shard route (raw bad bytes -> 4xx, never an activation) ═══

def test_truncated_frame_is_rejected(nodes):
    b = nodes(1)
    status, body = b.post_raw(_shard_path(b.mesh), b'\x02')  # < 4-byte length prefix
    assert status >= 400
    # must NOT have been treated as a valid activation
    with pytest.raises(Exception):
        parse_header(body)


def test_bad_json_header_is_rejected(nodes):
    b = nodes(1)
    bad = b'not-json-header'
    frame = struct.pack('<I', len(bad)) + bad + b'\x00\x00'
    status, _ = b.post_raw(_shard_path(b.mesh), frame)
    assert status >= 400


def test_version_mismatch_is_rejected(nodes):
    b = nodes(1)
    ids = [1, 2, 3]
    good = token_ids_frame('v', str(uuid.uuid4()), ids, order_index=0)
    # flip the version byte inside the JSON header by rebuilding with v=999
    import json
    hdr = {'v': 999, 'model_id': 'v', 'request_id': str(uuid.uuid4()),
           'order_index': 0, 'seq_pos': 0, 'dtype': 'int32', 'shape': [1, 3],
           'kind': 'token_ids'}
    hb = json.dumps(hdr).encode()
    frame = struct.pack('<I', len(hb)) + hb + struct.pack('<3i', *ids)
    status, _ = b.post_raw(_shard_path(b.mesh), frame)
    assert status >= 400


def test_empty_body_is_rejected(nodes):
    b = nodes(1)
    status, _ = b.post_raw(_shard_path(b.mesh), b'')
    assert status >= 400


def test_random_garbage_is_rejected(nodes):
    b = nodes(1)
    status, body = b.post_raw(_shard_path(b.mesh), b'\xde\xad\xbe\xef' * 32)
    assert status >= 400
    with pytest.raises(Exception):
        parse_header(body)


def test_unknown_route_404(nodes):
    b = nodes(1)
    status, _ = b.post_raw('/mesh/does-not-exist', b'x')
    assert status == 404


def test_oversized_activation_shape_rejected_not_ooming(nodes):
    """A ~120-byte frame claiming 50M rows must 4xx, not materialize ~800MB.

    Regression for the header-driven amplification DoS: n came straight from the
    attacker header shape with no bound before allocating 2 * n * hidden bytes.
    """
    b = nodes(1)
    huge = build_frame(
        {'v': PROTOCOL_VERSION, 'model_id': 'x', 'request_id': 'r', 'order_index': 0,
         'seq_pos': 0, 'dtype': 'bfloat16', 'shape': [1, 50_000_000, 8],
         'kind': 'activation'}, b'')
    status, body = b.post_raw(_shard_path(b.mesh), huge)
    assert status >= 400
    with pytest.raises(Exception):
        parse_header(body)          # must NOT be a valid activation echo


def test_malformed_shape_type_rejected_4xx_not_500(nodes):
    """A non-list shape must fail-closed to a client error, not crash to 500."""
    import json
    b = nodes(1)
    hdr = {'v': PROTOCOL_VERSION, 'model_id': 'x', 'request_id': 'r', 'order_index': 0,
           'seq_pos': 0, 'dtype': 'bfloat16', 'shape': 'notalist', 'kind': 'activation'}
    hb = json.dumps(hdr).encode()
    fr = struct.pack('<I', len(hb)) + hb + b''
    status, _ = b.post_raw(_shard_path(b.mesh), fr)
    assert 400 <= status < 500


# ═══ 6. Concurrency (no cross-request contamination) ═══

def test_concurrent_relays_do_not_cross_contaminate(nodes):
    a, b = nodes(2)
    a.mesh.pair_device(b.addr)
    results = {}
    errors = []

    def one(i):
        req = f'req-{i}-{uuid.uuid4()}'
        ids = [i, i + 1, i + 2]
        try:
            echo = a.mesh.relay_shard(b.device_id, token_ids_frame('c', req, ids, order_index=0))
            hdr, _ = parse_header(echo)
            results[i] = (hdr['request_id'], hdr['shape'][1])
        except Exception as e:
            errors.append((i, str(e)))

    threads = [threading.Thread(target=one, args=(i,)) for i in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, f'concurrent relay errors: {errors}'
    assert len(results) == 24
    for i, (req_id, n) in results.items():
        assert req_id == results[i][0]          # each got ITS OWN request_id back
        assert f'req-{i}-' in req_id             # not another thread's frame
        assert n == 3


# ═══ 7. Envelope-level frame variants over the real wire ═══

def test_activation_frame_relays_and_increments(nodes):
    """A middle-shard style hop: feed an activation frame directly, get order+1."""
    a, b = nodes(2)
    a.mesh.pair_device(b.addr)
    req = str(uuid.uuid4())
    act_in = build_frame(
        {'v': PROTOCOL_VERSION, 'model_id': 'mid', 'request_id': req,
         'order_index': 3, 'seq_pos': 0, 'dtype': 'bfloat16',
         'shape': [1, 4, 8], 'kind': 'activation'},
        b'\x00' * (2 * 1 * 4 * 8))
    echo = a.mesh.relay_shard(b.device_id, act_in)
    hdr, _ = parse_header(echo)
    assert hdr['request_id'] == req
    assert hdr['order_index'] == 4
    assert hdr['kind'] == 'activation'
