#!/usr/bin/env python
"""Prove two peer nodes collaborate: work made on A is merged and counted by B.

Not a mock and not a unit test. Spawns two live_peer_node.py processes with
fully isolated identities, talks to them over real HTTP, and reads the result
from the RECEIVING node rather than inferring it from the sender's POST
returning 200.

    python tests/standalone/two_node_collaboration.py

Exit 0 only if B's hive-census reports two nodes, one of them remote.

Isolation is the part that is easy to get wrong. Three things must differ per
process or the two share an identity and the census merges them into a single
entry, which looks like success right up until you read `local`:

    NUNBA_DATA_DIR      node_id.json, the gossip identity
    HEVOLVE_KEY_DIR     the Ed25519 keypair
    HEVOLVE_AGENT_DATA  the per-node HMAC secret

live_peer_node.py sets all three from its data-dir argument.

What each step establishes:

  1. Distinct node_ids. Without this everything below is vacuous.
  2. B ACCEPTS A's announce. This is the gate that could not pass before the
     announce-signing fix: _self_info() signed mid-construction while the
     receiver verifies every field except 'signature', so no announce
     verified anywhere and enforcement=hard refused the peer.
  3. B knows A, from B's own peer list.
  4. A extracts a federated learning delta, signs it exactly as
     broadcast_delta does, and POSTs it to B's real federation-delta
     endpoint. This is the collaboration: knowledge learned on one node
     merged into another node's aggregate.
  5. B's census reports both, with A's entry marked local=False.

Both nodes must seed a local delta. A node with no local contribution is not
a second reporting node, so seeding only A yields nodes_reporting 1 with a
single remote entry, which is the harness being incomplete rather than the
merge failing.

Run under HEVOLVE_ENFORCEMENT_MODE=hard, the production default, so the
signature and enforcement gates are genuinely exercised.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(HERE))
HARNESS = os.path.join(HERE, 'live_peer_node.py')

PORT_A = int(os.environ.get('COLLAB_PORT_A', '7801'))
PORT_B = int(os.environ.get('COLLAB_PORT_B', '7802'))


def get(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def post(url, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else b''
    req = urllib.request.Request(
        url, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {'raw': body}


def wait_up(port, label, tries=40):
    for _ in range(tries):
        try:
            d = get(f'http://127.0.0.1:{port}/whoami', timeout=5)
            print(f'  {label} up: node_id={d["node_id"][:16]} '
                  f'url={d["base_url"]}')
            return d
        except Exception:                                   # noqa: BLE001
            time.sleep(2)
    raise SystemExit(f'{label} never came up on :{port}')


def main():
    procs, dirs = [], []
    try:
        for port, label in ((PORT_A, 'A'), (PORT_B, 'B')):
            d = tempfile.mkdtemp(prefix=f'hive_{label}_')
            dirs.append(d)
            env = dict(os.environ)
            env['PYTHONPATH'] = PROJECT_ROOT
            env['HEVOLVE_ENFORCEMENT_MODE'] = 'hard'
            procs.append(subprocess.Popen(
                [sys.executable, HARNESS, str(port), d],
                env=env, cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT))

        print('=== 1. two nodes, distinct identities ===')
        a = wait_up(PORT_A, 'A')
        b = wait_up(PORT_B, 'B')
        if a['node_id'] == b['node_id']:
            raise SystemExit('SHARED IDENTITY: isolation failed, '
                             'everything below would be vacuous')
        print('  distinct node_ids: yes\n')

        A = f'http://127.0.0.1:{PORT_A}'
        B = f'http://127.0.0.1:{PORT_B}'

        print('=== 2. A announces to B, does B accept? ===')
        a_self = get(f'{A}/api/social/peers')
        a_rec = next(p for p in a_self['peers']
                     if p['node_id'] == a_self['node_id'])
        st, body = post(f'{B}/api/social/peers/announce', a_rec)
        print(f'  HTTP {st} accepted={body.get("accepted")} '
              f'is_new={body.get("is_new")}')
        if body.get('reason'):
            print(f'  reason: {body["reason"]}')
        if body.get('accepted') is False:
            raise SystemExit('B refused A, collaboration cannot follow')
        print()

        print('=== 3. does B know A? ===')
        b_peers = get(f'{B}/api/social/peers')
        knows = any(p['node_id'] == a['node_id'] for p in b_peers['peers'])
        print(f'  B remote_count: {b_peers.get("remote_count")}')
        print(f'  B knows A: {knows}\n')

        print('=== 4. A produces a learning delta and sends it to B ===')
        print('  seed A local delta:', post(f'{A}/dev/seed-local-delta')[1])
        print('  seed B local delta:', post(f'{B}/dev/seed-local-delta')[1])
        print('  handshake B<-A    :', post(f'{B}/dev/handshake?with={A}')[1])
        st, sent = post(f'{A}/dev/send-delta?to={B}')
        print(f'  HTTP {st} peer_said={json.dumps(sent.get("peer_said"))}')
        print(f'  delta node_id     : {sent.get("sent_node_id")}\n')

        print('=== 5. B census, read FROM B ===')
        cen = get(f'{B}/api/social/hive-census')
        print(f'  nodes_reporting: {cen.get("nodes_reporting")}  '
              f'status: {cen.get("status")}')
        per = cen.get('per_node') or {}
        for k, v in per.items():
            print(f'    {k}  local={v.get("local")}')
        print()

        reporting = cen.get('nodes_reporting') or 0
        has_remote = any(not v.get('local') for v in per.values())
        if reporting >= 2 and has_remote:
            print('RESULT: two nodes collaborating. A delta was accepted and '
                  'counted by B.')
            return 0
        print('RESULT: NOT proven. peer_said above carries the reason.')
        return 1
    finally:
        for p in procs:
            try:
                p.terminate()
            except Exception:                               # noqa: BLE001
                pass
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
