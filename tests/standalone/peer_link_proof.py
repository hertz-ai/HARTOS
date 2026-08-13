"""
Two-node proof that an inbound PeerLink actually forms.

Not a mock. This starts a REAL second node in a subprocess — its own Ed25519
identity (own HEVOLVE_KEY_DIR), real Hypercorn, the real
`peer_link_asgi(AsyncioWSGIMiddleware(flask_app))` stack from
hart_intelligence_entry — and dials it with the real
`PeerLinkManager.upgrade_peer`, the same call `_try_auto_upgrade` makes after
three gossip exchanges.

It proves, in order:
  1. the server answers a websocket upgrade on /peer_link          (was: 404)
  2. the Ed25519 handshake verifies in both directions
  3. X25519 ECDH derives a session key, so the link is encrypted
  4. the CLIENT holds a live link            -> broadcast() has a peer
  5. the SERVER holds a live link            -> accept_inbound registered it
  6. a message crosses and reaches a channel handler on the far side

Steps 4 and 5 are the ones that mattered: `PeerLinkManager._links` was empty on
every node in the fleet, and skill broadcast, the distributed coder and shard
fan-out all read that dict.

Run:  python tests/standalone/peer_link_proof.py
Exit code 0 = proven, 1 = not.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))


# --------------------------------------------------------------------------
# The server node (child process)
# --------------------------------------------------------------------------

def serve(port: int) -> None:
    """Run a node that accepts inbound PeerLinks, exactly as production does."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from flask import Flask, jsonify
    from hypercorn.asyncio import serve as hypercorn_serve
    from hypercorn.config import Config
    from hypercorn.middleware import AsyncioWSGIMiddleware

    from core.peer_link.link_manager import get_link_manager
    from core.peer_link.server import peer_link_asgi
    from security.node_integrity import get_node_identity

    received: list = []

    manager = get_link_manager()
    manager.start()
    # Registered BEFORE any peer connects, so accept_inbound's
    # _apply_channel_handlers has something to attach.
    manager.register_channel_handler(
        'gossip', lambda ch, data, peer: received.append({'ch': ch, 'd': data,
                                                          'peer': peer}))

    app = Flask('peer_link_proof_node')

    @app.get('/proof/identity')
    def identity():
        return jsonify({'node_id': get_node_identity().get('node_id', '')})

    @app.get('/proof/state')
    def state():
        status = manager.get_status()
        return jsonify({
            'active_links': status['active_links'],
            'encrypted_links': status['encrypted_links'],
            'links': {pid[:8]: s['trust'] for pid, s in status['links'].items()},
            'received': received,
        })

    config = Config()
    config.bind = [f'127.0.0.1:{port}']
    config.accesslog = None
    config.errorlog = '-'

    asgi_app = peer_link_asgi(AsyncioWSGIMiddleware(app))

    async def _runner():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(
            ThreadPoolExecutor(max_workers=16, thread_name_prefix='proof'))
        await hypercorn_serve(asgi_app, config)

    print(f'SERVE {port}', flush=True)
    asyncio.run(_runner())


# --------------------------------------------------------------------------
# The client node (this process)
# --------------------------------------------------------------------------

def _http_json(url: str, timeout: float = 5.0):
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _wait_for_server(port: int, deadline: float = 60.0):
    start = time.time()
    last = None
    while time.time() - start < deadline:
        try:
            return _http_json(f'http://127.0.0.1:{port}/proof/identity', 2.0)
        except Exception as exc:
            last = exc
            time.sleep(0.5)
    raise RuntimeError(f'server never came up on {port}: {last}')


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def drive() -> int:
    port = _free_port()

    # A DIFFERENT node identity for the child, so the handshake verifies a
    # genuinely foreign Ed25519 key rather than our own.
    server_keys = tempfile.mkdtemp(prefix='peerlink-proof-server-')
    env = dict(os.environ)
    env['HEVOLVE_KEY_DIR'] = server_keys
    env['PYTHONPATH'] = str(_REPO) + os.pathsep + env.get('PYTHONPATH', '')
    env.pop('HEVOLVE_ENFORCEMENT_MODE', None)

    child = subprocess.Popen(
        [sys.executable, __file__, '--serve', '--port', str(port)],
        env=env, cwd=str(_REPO),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    failures = []
    try:
        ident = _wait_for_server(port)
        server_node_id = ident['node_id']
        print(f'[1] server up on {port}, node_id={server_node_id[:8]}')

        from core.peer_link.link import TrustLevel
        from core.peer_link.link_manager import get_link_manager

        manager = get_link_manager()
        manager.start()

        t0 = time.time()
        ok = manager.upgrade_peer(
            peer_id=server_node_id,
            address=f'127.0.0.1:{port}',
            trust=TrustLevel.PEER,
        )
        elapsed = time.time() - t0
        print(f'[2] upgrade_peer -> {ok} in {elapsed:.2f}s')
        if not ok:
            failures.append('upgrade_peer returned False — no link formed')
            return _report(failures, child)

        link = manager.get_link(server_node_id)
        if link is None or not link.is_connected:
            failures.append('client has no connected link after upgrade_peer')
        else:
            print(f'[3] client link: trust={link.trust.value} '
                  f'encrypted={link.is_encrypted}')
            if not link.is_encrypted:
                failures.append('PEER-trust link is not encrypted '
                                '(X25519 ECDH did not derive a session key)')

        # Give the far side a moment to finish registering.
        for _ in range(20):
            state = _http_json(f'http://127.0.0.1:{port}/proof/state')
            if state['active_links'] >= 1:
                break
            time.sleep(0.25)

        print(f'[4] server state: active_links={state["active_links"]} '
              f'encrypted={state["encrypted_links"]} links={state["links"]}')
        if state['active_links'] < 1:
            failures.append('server registered no link — accept_inbound did '
                            'not reach PeerLinkManager._links')

        # Message across the wire, on a channel the server has a handler for.
        link = manager.get_link(server_node_id)
        if link is not None:
            link.send('gossip', {'proof': 'hello-from-client'})

        got = []
        for _ in range(20):
            state = _http_json(f'http://127.0.0.1:{port}/proof/state')
            got = state['received']
            if got:
                break
            time.sleep(0.25)

        print(f'[5] server received: {got}')
        if not got:
            failures.append('message sent but no channel handler fired on the '
                            'server — the receive loop is not reading')
        elif got[0]['d'].get('proof') != 'hello-from-client':
            failures.append(f'payload corrupted in transit: {got[0]}')

        return _report(failures, child)
    finally:
        try:
            child.terminate()
            child.wait(timeout=10)
        except Exception:
            child.kill()


def _report(failures, child) -> int:
    print()
    if failures:
        print('NOT PROVEN:')
        for f in failures:
            print(f'  - {f}')
        try:
            child.terminate()
            out = child.communicate(timeout=5)[0]
            tail = '\n'.join((out or '').strip().splitlines()[-25:])
            if tail:
                print('\n--- server log tail ---')
                print(tail)
        except Exception:
            pass
        return 1
    print('PROVEN: inbound PeerLink forms, is encrypted, and carries messages.')
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    if args.serve:
        serve(args.port)
    else:
        sys.exit(drive())
