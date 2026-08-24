#!/usr/bin/env python3
"""One real HARTOS peer node, for a two-process live federation check.

Run two of these and watch hive-census go from 1 to 2. That is the end-to-end
assertion the in-process tests cannot make, and it is what proved the delta
signature ordering bug end to end:

    pre-fix   POST delta -> "invalid signature"  -> nodes_reporting 1
    post-fix  POST delta -> "accepted"           -> nodes_reporting 2

Usage (two terminals, from the HARTOS root):

    python tests/standalone/live_peer_node.py 7801 /tmp/nodeA
    python tests/standalone/live_peer_node.py 7802 /tmp/nodeB

Then drive it (node B is the observer):

    curl -XPOST localhost:7802/dev/seed-local-delta
    curl -XPOST "localhost:7802/dev/handshake?with=http://127.0.0.1:7801"
    curl -XPOST "localhost:7801/dev/send-delta?to=http://127.0.0.1:7802"
    curl        localhost:7802/api/social/hive-census

Isolation matters and is easy to get wrong. THREE things must differ per node
or the census merges them into one local entry:

    NUNBA_DATA_DIR       gossip node_id.json
    HEVOLVE_KEY_DIR      Ed25519 keypair, which stamps the delta node_id
    HEVOLVE_AGENT_DATA   per-node HMAC secret

Set from the data_dir argument below, so passing two different dirs is enough.

Original docstring follows.

One real HARTOS peer node: the production blueprints over real HTTP.

Not a mock. This registers integrations.social.discovery and
api_hive_census exactly as the app does, so /api/social/peers/announce,
/peers/federation-delta and /hive-census are the real handlers, and the delta
travels as JSON over a socket between two OS processes.

Usage: live_node.py <port> <data_dir>

NUNBA_DATA_DIR gives each process its own node_id.json, which is what makes two
processes on one machine two distinct nodes to the protocol.

The /dev/* endpoints are harness scaffolding, not production code. They stand in
for the agent engine, which in a real node is what produces the local delta and
drives the broadcast loop. Everything they call is the production path:
extract_local_delta, _sign_delta, and the real HTTP endpoint on the peer.
"""
import os
import sys

port = int(sys.argv[1])
data_dir = sys.argv[2]

os.environ['NUNBA_DATA_DIR'] = data_dir
# Isolate the whole identity, not just the gossip node_id. HEVOLVE_KEY_DIR owns
# the Ed25519 keypair and HEVOLVE_AGENT_DATA the per-node HMAC secret; without
# both, two processes on one machine share an aggregator identity and the
# census merges them into a single local entry.
os.environ['HEVOLVE_KEY_DIR'] = os.path.join(data_dir, 'keys')
os.environ['HEVOLVE_AGENT_DATA'] = os.path.join(data_dir, 'agent_data')
# The social DB is the FOURTH thing to isolate. Without a per-node path both
# processes open the SAME sqlite file, and their concurrent writers (each node's
# health check + gossip + the announce/delta INSERTs) collide past the 3s
# busy_timeout with "database is locked" — which fails the announce and looks
# like a federation bug. Each node gets its own DB, exactly as two separate
# machines would in production.
os.environ['HEVOLVE_DB_PATH'] = os.path.join(data_dir, 'social.db')
os.environ['HEVOLVE_ENFORCEMENT_MODE'] = 'hard'   # production default
os.environ.setdefault('HEVOLVE_BASE_URL', f'http://127.0.0.1:{port}')

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

import requests  # noqa: E402
from flask import Flask, jsonify, request  # noqa: E402
from integrations.social.discovery import discovery_bp  # noqa: E402
from integrations.social.api_hive_census import hive_census_bp  # noqa: E402
from integrations.social.models import init_db  # noqa: E402

# Create the schema in THIS node's isolated DB (HEVOLVE_DB_PATH, set above).
# Without an explicit init the fresh per-node sqlite file has no tables and the
# announce INSERT fails with "no such table: peer_nodes" — the shared-default-DB
# path only worked because a prior run had already created the tables there.
init_db()

app = Flask(__name__)
app.register_blueprint(discovery_bp)
app.register_blueprint(hive_census_bp)


def _agg():
    # Same accessor api_hive_census uses, so the census and these endpoints
    # operate on ONE aggregator instance rather than two.
    from integrations.agent_engine.federated_aggregator import (
        get_federated_aggregator)
    return get_federated_aggregator()


@app.get('/whoami')
def whoami():
    from integrations.social.peer_discovery import gossip
    return jsonify({'node_id': gossip.node_id, 'base_url': gossip.base_url})


@app.post('/dev/seed-local-delta')
def seed_local_delta():
    """Stand in for the agent engine holding this node's own delta."""
    agg = _agg()
    agg._local_delta = agg.extract_local_delta()
    return jsonify({'ok': bool(agg._local_delta)})


@app.post('/dev/send-delta')
def send_delta():
    """Produce this node's delta the way broadcast_delta does, and POST it to
    the peer's real federation-delta endpoint."""
    from integrations.agent_engine.federated_aggregator import _sign_delta
    target = request.args.get('to', '')
    agg = _agg()
    delta = agg.extract_local_delta()
    if not delta:
        return jsonify({'ok': False, 'error': 'no local delta'}), 500
    _sign_delta(delta)          # exactly what broadcast_delta does before POST
    r = requests.post(f'{target}/api/social/peers/federation-delta',
                      json=delta, timeout=15)
    return jsonify({'ok': r.ok, 'status': r.status_code,
                    'peer_said': r.json() if r.content else None,
                    'sent_node_id': delta.get('node_id', '')})


@app.get('/dev/my-hmac')
def my_hmac():
    """What this node offers during the federation handshake.

    get_hmac_secret_for_handshake is the production accessor for exactly this
    exchange; in a real handshake the value travels signed by the node's
    Ed25519 key.
    """
    from integrations.agent_engine.federated_aggregator import (
        get_hmac_secret_for_handshake, get_federated_aggregator)
    # Report the SAME id the delta carries. The aggregator has no node_id
    # attribute; extract_local_delta stamps it from the node identity, so take
    # it from there or the handshake registers a secret under an empty key and
    # verification silently never matches.
    agg = get_federated_aggregator()
    d = agg.extract_local_delta() or {}
    return jsonify({'node_id': d.get('node_id', ''),
                    'secret': get_hmac_secret_for_handshake()})


@app.post('/dev/handshake')
def handshake():
    """Complete the handshake with a peer: fetch its id and secret, store them.

    This is the step that was never happening in production, because peers
    could not reach each other. Without it a delta arrives and fails HMAC,
    which is the honest and correct rejection.
    """
    from integrations.agent_engine.federated_aggregator import (
        register_peer_hmac_secret)
    target = request.args.get('with', '')
    r = requests.get(f'{target}/dev/my-hmac', timeout=15)
    peer = r.json()
    register_peer_hmac_secret(peer.get('node_id', ''), peer.get('secret', ''))
    return jsonify({'ok': True, 'learned_node_id': peer.get('node_id', '')})


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)
