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

#: How many live nodes to spawn.  TWO by default, so every existing caller
#: and docs/VERIFICATION.md keep the exact contract they recorded; pass a
#: count to prove a bigger hive:
#:
#:     python tests/standalone/two_node_collaboration.py 3
#:
#: The shape of the proof does not change with N -- N distinct identities,
#: every node announced and accepted, every node's delta counted by the
#: observer, and the observer's OWN census reporting N with N-1 remote.
#: Two was simply the smallest interesting number, not the design.
NODE_COUNT = int(os.environ.get('COLLAB_NODES', '2'))
LABELS = 'ABCDEFGH'


def port_for(i: int) -> int:
    """Node i's port.  A and B keep their historical numbers so a failure
    on this box is comparable with every earlier run recorded against them."""
    return (PORT_A, PORT_B)[i] if i < 2 else PORT_B + (i - 1)


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


def unsigned_reason(rec: dict) -> str:
    """Why this node's own announce record would be refused, or '' if fine.

    hartos-14's requirement and it is not ceremony.  An UNSIGNED node does
    not fail loudly: hard enforcement refuses it, and on a HARTOS-only node
    `_PEER_ADMISSION_ASK` is None (hartos_bootstrap.bootstrap() is called
    only from Nunba's main.py), so the owner is never asked either.  The
    result is a peer that simply never joins, surfacing at census time as a
    number one lower than expected -- which reads like a federation bug and
    is really a setup bug.

    Ask the NODE, not the disk.  The first version of this check looked for
    files under HEVOLVE_KEY_DIR and failed every node, because the keypair
    is not written by the time the node answers /status -- so a run that
    federates perfectly well was reported as having no keys.  A directory
    listing is a proxy for identity; the node's own signed record IS the
    identity, and it is already fetched here to be announced.
    """
    if not rec.get('public_key'):
        return 'no public_key in its own announce record'
    if not rec.get('signature'):
        return 'its own announce record is unsigned'
    return ''


def main():
    n = NODE_COUNT
    if len(sys.argv) > 1:
        n = int(sys.argv[1])
    if not 2 <= n <= len(LABELS):
        raise SystemExit(f'node count must be 2..{len(LABELS)}, got {n}')

    procs, dirs, nodes = [], [], []
    try:
        for i in range(n):
            label, port = LABELS[i], port_for(i)
            d = tempfile.mkdtemp(prefix=f'hive_{label}_')
            dirs.append(d)
            env = dict(os.environ)
            env['PYTHONPATH'] = PROJECT_ROOT
            env['HEVOLVE_ENFORCEMENT_MODE'] = 'hard'
            procs.append(subprocess.Popen(
                [sys.executable, HARNESS, str(port), d],
                env=env, cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
            nodes.append({'label': label, 'port': port, 'dir': d,
                          'url': f'http://127.0.0.1:{port}'})

        print(f'=== 1. {n} nodes, distinct identities ===')
        for nd in nodes:
            nd.update(wait_up(nd['port'], nd['label']))
        ids = [nd['node_id'] for nd in nodes]
        if len(set(ids)) != n:
            raise SystemExit(
                'SHARED IDENTITY: isolation failed, everything below would '
                'be vacuous. NUNBA_DATA_DIR / HEVOLVE_KEY_DIR / '
                'HEVOLVE_AGENT_DATA must differ per node.')
        print(f'  distinct node_ids: yes ({n})')

        print()

        observer = nodes[-1]
        others = nodes[:-1]
        O = observer['url']

        print(f'=== 2. every node announces to {observer["label"]}, '
              f'are they accepted? ===')
        for nd in others:
            self_view = get(f'{nd["url"]}/api/social/peers')
            rec = next(p for p in self_view['peers']
                       if p['node_id'] == self_view['node_id'])
            # Assert the identity BEFORE announcing it, so an unsigned node
            # fails here, by name and with a reason, instead of silently
            # never joining and surfacing as a short census later.
            why = unsigned_reason(rec)
            if why:
                raise SystemExit(
                    f'NODE {nd["label"]} IS UNSIGNED: {why}. Under hard '
                    f'enforcement it will be refused, and on a HARTOS-only '
                    f'node nothing asks the owner either, so the census '
                    f'would just come up short with no stated reason. '
                    f'Fix setup, not federation.')
            st, body = post(f'{O}/api/social/peers/announce', rec)
            print(f'  {nd["label"]} -> {observer["label"]}: HTTP {st} '
                  f'accepted={body.get("accepted")} is_new={body.get("is_new")}')
            if body.get('reason'):
                print(f'    reason: {body["reason"]}')
            if body.get('accepted') is False:
                raise SystemExit(
                    f'{observer["label"]} refused {nd["label"]}, '
                    f'collaboration cannot follow')
        print()

        print(f'=== 3. does {observer["label"]} know them all? ===')
        obs_peers = get(f'{O}/api/social/peers')
        known = {p['node_id'] for p in obs_peers['peers']}
        for nd in others:
            print(f'  knows {nd["label"]}: {nd["node_id"] in known}')
        missing = [nd['label'] for nd in others if nd['node_id'] not in known]
        if missing:
            raise SystemExit(f'{observer["label"]} does not know '
                             f'{",".join(missing)}')
        print(f'  remote_count: {obs_peers.get("remote_count")}\n')

        print('=== 4. every node produces a learning delta and sends it ===')
        for nd in nodes:
            print(f'  seed {nd["label"]} local delta:',
                  post(f'{nd["url"]}/dev/seed-local-delta')[1])
        for nd in others:
            print(f'  handshake {observer["label"]}<-{nd["label"]}:',
                  post(f'{O}/dev/handshake?with={nd["url"]}')[1])
            st, sent = post(f'{nd["url"]}/dev/send-delta?to={O}')
            print(f'    HTTP {st} peer_said='
                  f'{json.dumps(sent.get("peer_said"))} '
                  f'node_id={sent.get("sent_node_id")}')
        print()

        print(f'=== 5. {observer["label"]} census, read FROM '
              f'{observer["label"]} ===')
        cen = get(f'{O}/api/social/hive-census')
        print(f'  nodes_reporting: {cen.get("nodes_reporting")}  '
              f'status: {cen.get("status")}')
        per = cen.get('per_node') or {}
        for k, v in per.items():
            print(f'    {k}  local={v.get("local")}  stale={v.get("stale")}')
        print()

        reporting = cen.get('nodes_reporting') or 0
        remotes = sum(1 for v in per.values() if not v.get('local'))
        if reporting >= n and remotes >= n - 1:
            print(f'RESULT: {n} nodes collaborating. Every delta was '
                  f'accepted and counted by {observer["label"]} '
                  f'({remotes} remote).')
            return 0
        print(f'RESULT: NOT proven -- reporting={reporting} (want >={n}), '
              f'remote={remotes} (want >={n - 1}). peer_said above carries '
              f'the reason.')
        return 1
    finally:
        for p in procs:
            try:
                p.terminate()
            except Exception:                               # noqa: BLE001
                pass
        for d in dirs:
            # A CLEANUP failure must never be reported as a RUN failure.
            # shutil.rmtree raises AttributeError on this box even with
            # ignore_errors=True (the venv's 3.12.3 binary loads a newer
            # stdlib whose rmtree passes os._walk_symlinks_as_files, which
            # that interpreter does not define) -- and because the lookup
            # happens before any error handling, ignore_errors cannot help.
            # It fired AFTER a fully successful run and turned exit 0 into
            # a traceback, which is exactly the "the exit code is a proxy"
            # trap this file exists to avoid.
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception as e:                          # noqa: BLE001
                print(f'  (cleanup of {d} failed, ignored: '
                      f'{type(e).__name__}: {e})')


if __name__ == '__main__':
    sys.exit(main())
