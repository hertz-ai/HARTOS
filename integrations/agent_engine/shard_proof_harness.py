"""Shard-relay proof driver — two modes over the SAME shipped code.

  selftest : start two stdlib ComputeMesh nodes on 127.0.0.1:portA/portB (real
             loopback TCP, not in-process calls), pair A -> B via pair_device(),
             build a REAL envelope.token_ids_frame() on A, relay_shard() it to B,
             and assert the activation echo round-trips. Tear both down.

  remote   : --peer HOST[:PORT] — preflight GET /health, pair with an
             already-running node, relay one real frame over the LAN, print
             evidence (distinct device_ids, sent/recv byte lengths, routing
             fields, activation shape, RTT).

This driver implements NO socket / pairing / envelope logic of its own — it only
orchestrates ComputeMeshService.pair_device() + relay_shard(), mesh_node_runner's
stdlib server, and core.shard_runtime.envelope.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import uuid

from integrations.agent_engine.mesh_node_runner import (
    build_ephemeral_mesh,
    build_stdlib_server,
)
from core.shard_runtime.envelope import (
    token_ids_frame,
    parse_header,
    read_routing,
    payload_view,
)


def _prod(shape):
    out = 1
    for d in shape:
        out *= d
    return out


def _start_stdlib_node(port: int = 0):
    """Start one stdlib mesh node on 127.0.0.1 in a daemon thread.

    Returns (mesh, server, port, data_dir). port=0 lets the OS pick a free port.
    """
    mesh, data_dir = build_ephemeral_mesh(port=port or 0)
    server = build_stdlib_server(mesh, host='127.0.0.1', port=port or 0)
    actual = server.server_address[1]
    mesh.task_relay_port = actual
    mesh._start_background_loops()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return mesh, server, actual, data_dir


def _await_health(host: str, port: int, timeout: float = 5.0) -> bool:
    """Poll GET /health until it answers or timeout (loopback readiness)."""
    import urllib.request
    deadline = time.time() + timeout
    url = f'http://{host}:{port}/health'
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.05)
    return False


def run_selftest() -> int:
    print('=== shard relay selftest: two stdlib mesh nodes over loopback TCP ===',
          flush=True)
    node_a = node_b = None
    try:
        mesh_a, srv_a, port_a, dir_a = _start_stdlib_node()
        node_a = srv_a
        mesh_b, srv_b, port_b, dir_b = _start_stdlib_node()
        node_b = srv_b

        assert _await_health('127.0.0.1', port_a), 'node A /health never came up'
        assert _await_health('127.0.0.1', port_b), 'node B /health never came up'

        print(f'node A: 127.0.0.1:{port_a} device_id={mesh_a._device_id}', flush=True)
        print(f'node B: 127.0.0.1:{port_b} device_id={mesh_b._device_id}', flush=True)
        assert mesh_a._device_id and mesh_b._device_id, 'device_id undefined'
        assert mesh_a._device_id != mesh_b._device_id, 'device_ids must be distinct'

        # (1) pair A -> B over real HTTP /mesh/pair
        pair = mesh_a.pair_device(f'127.0.0.1:{port_b}')
        print(f'pair A->B: {pair}', flush=True)
        assert pair.get('status') == 'paired', f'pairing failed: {pair}'
        peer_id = pair['peer_id']
        assert peer_id == mesh_b._device_id, 'paired peer_id != B device_id'
        assert peer_id in mesh_a._peers, 'peer not registered in A._peers'

        # (2) build a REAL token_ids frame on A and relay it to B
        req_id = str(uuid.uuid4())
        ids = [10, 20, 30, 40]
        frame = token_ids_frame('proof', req_id, ids, order_index=0)
        sent_routing = read_routing(frame)
        print(f'sent token_ids_frame request_id={req_id} ids={ids} '
              f'bytes={len(frame)} routing={sent_routing}', flush=True)

        t0 = time.time()
        echo = mesh_a.relay_shard(peer_id, frame)
        rtt_ms = (time.time() - t0) * 1000.0

        # (3) parse the activation echo B returned
        hdr, _ = parse_header(echo)
        pv = payload_view(echo)
        print(f'recv activation bytes={len(echo)} kind={hdr["kind"]} '
              f'request_id={hdr["request_id"]} order_index={hdr["order_index"]} '
              f'dtype={hdr["dtype"]} shape={hdr["shape"]} '
              f'payload_bytes={len(pv)} rtt_ms={rtt_ms:.1f}', flush=True)

        assert hdr['kind'] == 'activation', 'echo not an activation frame'
        assert hdr['request_id'] == req_id, 'request_id did not survive the wire'
        assert hdr['order_index'] == 1, 'order_index must be inbound + 1'
        assert len(pv) == 2 * _prod(hdr['shape']), 'payload != 2 * prod(shape)'
        assert hdr['shape'][1] == len(ids), 'N (token dim) mismatch in echo'

        print('PROOF: PASS', flush=True)
        return 0
    except AssertionError as e:
        print(f'PROOF: FAIL — {e}', flush=True)
        return 1
    finally:
        for s in (node_a, node_b):
            if s is not None:
                try:
                    s.shutdown()
                except Exception:
                    pass


def run_remote(peer: str) -> int:
    print(f'=== shard relay remote proof: peer {peer} ===', flush=True)
    if ':' in peer:
        host, port_s = peer.rsplit(':', 1)
        port = int(port_s)
    else:
        from core.port_registry import get_port
        host, port = peer, get_port('mesh_relay')

    # Preflight reachability so a network failure is diagnosed as network, not code.
    if not _await_health(host, port, timeout=5.0):
        print(f'PROOF: FAIL — preflight GET http://{host}:{port}/health unreachable '
              f'(network/firewall, not the relay code)', flush=True)
        return 2
    print(f'preflight: GET http://{host}:{port}/health -> 200', flush=True)

    # Local node A (identity only; no inbound server needed for an outbound proof).
    mesh_a, _dir_a = build_ephemeral_mesh(port=port)
    print(f'node A (local): device_id={mesh_a._device_id}', flush=True)

    pair = mesh_a.pair_device(f'{host}:{port}')
    print(f'pair A->B: {pair}', flush=True)
    if pair.get('status') != 'paired':
        print(f'PROOF: FAIL — pairing rejected: {pair}', flush=True)
        return 1
    peer_id = pair['peer_id']
    print(f'paired peer_id={peer_id} registered={peer_id in mesh_a._peers}', flush=True)
    if peer_id == mesh_a._device_id:
        print('PROOF: FAIL — remote device_id == local (same node, not two nodes)',
              flush=True)
        return 1

    req_id = str(uuid.uuid4())
    ids = [11, 22, 33, 44, 55]
    frame = token_ids_frame('proof', req_id, ids, order_index=0)
    print(f'sent token_ids_frame request_id={req_id} ids={ids} bytes={len(frame)} '
          f'routing={read_routing(frame)}', flush=True)

    t0 = time.time()
    try:
        echo = mesh_a.relay_shard(peer_id, frame)
    except Exception as e:
        print(f'PROOF: FAIL — relay error: {e}', flush=True)
        return 1
    rtt_ms = (time.time() - t0) * 1000.0

    hdr, _ = parse_header(echo)
    pv = payload_view(echo)
    print(f'recv activation bytes={len(echo)} kind={hdr["kind"]} '
          f'request_id={hdr["request_id"]} order_index={hdr["order_index"]} '
          f'dtype={hdr["dtype"]} shape={hdr["shape"]} payload_bytes={len(pv)} '
          f'rtt_ms={rtt_ms:.1f}', flush=True)

    ok = (hdr['kind'] == 'activation'
          and hdr['request_id'] == req_id
          and hdr['order_index'] == 1
          and len(pv) == 2 * _prod(hdr['shape'])
          and hdr['shape'][1] == len(ids))
    print('PROOF: PASS' if ok else 'PROOF: FAIL — echo did not match contract',
          flush=True)
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description='Shard-relay proof driver.')
    ap.add_argument('--selftest', action='store_true',
                    help='Two localhost stdlib nodes: pair + relay round-trip.')
    ap.add_argument('--peer', default=None,
                    help='HOST[:PORT] of an already-running node (LAN proof).')
    args = ap.parse_args(argv)

    if args.peer:
        return run_remote(args.peer)
    # default action is the selftest
    return run_selftest()


if __name__ == '__main__':
    sys.exit(main())
