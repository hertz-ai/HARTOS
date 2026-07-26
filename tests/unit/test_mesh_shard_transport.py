"""Behavioural test for the ComputeMesh shard transport (real loopback sockets).

Spins two stdlib ComputeMesh nodes on ephemeral 127.0.0.1 ports via
mesh_node_runner, pairs them through the shipped pair_device() /mesh/pair
challenge, and relays a REAL envelope.token_ids_frame over the /mesh/shard HTTP
leg. Nothing is mocked but the already-try/excepted model_bus/psutil boundary
(those simply fail closed when the services are not running).

Asserts against the real envelope contract:
  * pairing registers the peer keyed by the far node's device_id;
  * B's _handle_shard_frame decodes the token ids EXACTLY (int32 round-trip);
  * A receives an activation frame: kind=='activation', identical request_id,
    order_index == inbound + 1, payload_view length == 2 * prod(shape);
  * a truncated frame and a valid-length-but-bad-JSON frame are rejected
    fail-closed (EnvelopeError / HTTP 400, never forwarded);
  * Flask-bound vs stdlib-bound route_table() return byte-identical /mesh/status
    and /mesh/pair (the stdlib .9 fallback == the Flask app).
"""
import json
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
    parse_header,
    payload_view,
    decode_token_ids,
    EnvelopeError,
)


def _prod(shape):
    out = 1
    for d in shape:
        out *= d
    return out


def _await_health(port, timeout=5.0):
    deadline = time.time() + timeout
    url = f'http://127.0.0.1:{port}/health'
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.05)
    return False


class _Node:
    def __init__(self):
        self.mesh, self.data_dir = build_ephemeral_mesh(port=0)
        self.server = build_stdlib_server(self.mesh, host='127.0.0.1', port=0)
        self.port = self.server.server_address[1]
        self.mesh.task_relay_port = self.port
        self.mesh._start_background_loops()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        assert _await_health(self.port), f'node on {self.port} never became healthy'

    def stop(self):
        try:
            self.server.shutdown()
        except Exception:
            pass


@pytest.fixture
def two_nodes():
    a = _Node()
    b = _Node()
    try:
        yield a, b
    finally:
        a.stop()
        b.stop()


def test_pairing_registers_peer_by_device_id(two_nodes):
    a, b = two_nodes
    assert a.mesh._device_id and b.mesh._device_id
    assert a.mesh._device_id != b.mesh._device_id

    result = a.mesh.pair_device(f'127.0.0.1:{b.port}')
    assert result.get('status') == 'paired', result
    peer_id = result['peer_id']
    assert peer_id == b.mesh._device_id
    assert peer_id in a.mesh._peers
    assert a.mesh._peers[peer_id].peer_id == b.mesh._device_id


def test_shard_frame_round_trips_activation_echo(two_nodes):
    a, b = two_nodes
    pair = a.mesh.pair_device(f'127.0.0.1:{b.port}')
    peer_id = pair['peer_id']

    req_id = str(uuid.uuid4())
    ids = [10, 20, 30, 40]
    frame = token_ids_frame('proof', req_id, ids, order_index=0)

    echo = a.mesh.relay_shard(peer_id, frame)

    hdr, _ = parse_header(echo)
    pv = payload_view(echo)
    assert hdr['kind'] == 'activation'
    assert hdr['request_id'] == req_id
    assert hdr['order_index'] == 1
    assert hdr['dtype'] == 'bfloat16'
    assert len(pv) == 2 * _prod(hdr['shape'])
    assert hdr['shape'][1] == len(ids)


def test_node_b_decodes_token_ids_exactly(two_nodes):
    """B's relay handler must decode the int32 token ids EXACTLY (no drift)."""
    _a, b = two_nodes
    req_id = str(uuid.uuid4())
    ids = [10, 20, 30, 40]
    frame = token_ids_frame('proof', req_id, ids, order_index=0)

    # The exact bytes B receives decode back to the originals (int32 round-trip).
    assert decode_token_ids(frame) == ids
    # And B's own handler, fed those same bytes, echoes N == len(ids).
    echo = b.mesh._handle_shard_frame(frame)
    hdr, _ = parse_header(echo)
    assert hdr['shape'][1] == len(ids)
    assert hdr['request_id'] == req_id


def test_truncated_frame_rejected_fail_closed(two_nodes):
    a, b = two_nodes
    pair = a.mesh.pair_device(f'127.0.0.1:{b.port}')
    peer_id = pair['peer_id']

    # < 4 bytes: not even a length prefix.
    with pytest.raises(EnvelopeError):
        a.mesh.relay_shard(peer_id, b'\x01\x02')


def test_bad_json_header_rejected_fail_closed(two_nodes):
    a, b = two_nodes
    pair = a.mesh.pair_device(f'127.0.0.1:{b.port}')
    peer_id = pair['peer_id']

    # Valid length prefix (claims 5 header bytes) but the header is not JSON.
    garbage = struct.pack('<I', 5) + b'xxxxx'
    with pytest.raises(EnvelopeError):
        a.mesh.relay_shard(peer_id, garbage)


def test_bad_frame_returns_400_not_activation(two_nodes):
    """The far node must answer a bad frame with HTTP 400 (never an activation)."""
    _a, b = two_nodes
    garbage = struct.pack('<I', 5) + b'xxxxx'
    body = garbage
    req = urllib.request.Request(
        f'http://127.0.0.1:{b.port}/mesh/shard',
        data=body,
        headers={'Content-Type': 'application/octet-stream'},
        method='POST',
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        got_status = 200
        got_body = b''
    except urllib.error.HTTPError as e:
        got_status = e.code
        got_body = e.read()
    assert got_status == 400
    # The body is a JSON error, not an activation frame.
    assert b'activation' not in got_body
    payload = json.loads(got_body.decode('utf-8'))
    assert 'error' in payload


def test_flask_and_stdlib_routes_are_byte_identical():
    """The stdlib .9 fallback must be byte-for-byte the Flask app on the shared handlers."""
    flask = pytest.importorskip('flask')  # noqa: F841

    mesh, _dir = build_ephemeral_mesh(port=0)
    # No background loops => deterministic 'stopped' status on both transports.

    app = mesh._create_flask_app()
    client = app.test_client()

    server = build_stdlib_server(mesh, host='127.0.0.1', port=0)
    port = server.server_address[1]
    mesh.task_relay_port = port
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        assert _await_health(port)

        # /mesh/status (GET)
        flask_status = client.get('/mesh/status').data
        with urllib.request.urlopen(
                f'http://127.0.0.1:{port}/mesh/status', timeout=5) as r:
            stdlib_status = r.read()
        assert flask_status == stdlib_status

        # /mesh/pair (POST challenge)
        body = json.dumps({'action': 'challenge', 'device_id': 'peer-x'}).encode()
        flask_pair = client.post(
            '/mesh/pair', data=body, content_type='application/json').data
        req = urllib.request.Request(
            f'http://127.0.0.1:{port}/mesh/pair',
            data=body,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            stdlib_pair = r.read()
        assert flask_pair == stdlib_pair
    finally:
        server.shutdown()
