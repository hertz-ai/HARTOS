"""A node the hive admitted may run a SHARED agent on this node over A2A,
proving who it is with the gossip key it already has.  No API key, no new
credential.

OWNER RULING 2026-09-26 (verbatim): "we had trust created in same network and
when auto mode the consent is implicit only a hash verified node is enough
what other creds are we talking about? torrents is the analogy for our
design".

THE DEFECT: 05641511d/01b1b2f65 admitted message/send only when
security.middleware's /chat gate admitted the caller.  peer_reuse.
invoke_peer_agent sends no credential, so on a bundled desktop (NUNBA_BUNDLED)
or a central / keyed node every peer invoke became 401: the remote-invoke
half of cross-node REUSE stopped working for the peers it exists for.

THE FIX: the invoker signs the JSON-RPC body with this node's Ed25519 key
(sender {node_id, public_key}, audience = the receiving node's id, and a
timestamp inside the signed body), and the server also admits a request
signed by a peer this node has VERIFIED (integrity_status 'verified', which
only an answered integrity challenge writes; review of a5364ba66: a bare
PeerNode row is what any stranger gets from the open announce), not banned,
whose key on file is the key that signed, for this node, with a fresh
timestamp.  The real announce + challenge path is driven at the bottom.  The one
rule lives in integrations.social.discovery.admitted_peer_sender.  The
sharing rule (peer_reuse.export_allowed) still applies.  message/get and
task/cancel get the same admission, and a /chat-gate verdict other than 401
(a phone's consent_pending 403) is passed through, not reported as 401.

Everything here drives the REAL Flask app, the REAL API gate, the REAL
jsonrpc view and the REAL invoker; the DB is the suite's real SQLite with
real PeerNode rows.  Only the network (pooled_post -> test_client), the
agent executor (a spy) and the invoker's identity (a fresh key standing for
another node) are stand-ins.
"""
import json
import os
import sys
import time
import types
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask import Flask, jsonify

import integrations.google_a2a.peer_reuse as peer_reuse
import security.node_integrity as ni
from integrations.google_a2a.google_a2a_integration import A2AProtocolServer
from integrations.social import discovery
from integrations.social.integrity_service import WITNESS_TIMESTAMP_MAX_AGE
from integrations.social.models import Base, PeerNode, db_session, get_engine
from integrations.social.sync_engine import SyncEngine
from security.middleware import _apply_api_auth

AGENT = 'livetest_shared_0'
OTHER_AGENT = 'livetest_other_0'
PEER_URL = 'http://node-b:5000'          # the serving node, as the invoker dials it
SERVER_ID = 'livetest-serving-node'       # the serving node's own node_id
INVOKER_URL = 'http://198.51.100.23:6777'  # the invoking node's advertised url
REMOTE = {'REMOTE_ADDR': '198.51.100.23'}
LOCAL = {'REMOTE_ADDR': '127.0.0.1'}

# Both nodes live in this one process; SyncEngine.canonical_node_id answers
# for whichever of them is handling the current call.
_ROLE = {'who': 'invoker'}


class _AsServer:
    def __enter__(self):
        self._prev, _ROLE['who'] = _ROLE['who'], 'server'

    def __exit__(self, *a):
        _ROLE['who'] = self._prev


class _Resp:
    def __init__(self, flask_resp):
        self.status_code = flask_resp.status_code
        self._json = flask_resp.get_json(silent=True)

    def json(self):
        if self._json is None:
            raise ValueError('no json')
        return self._json


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ('NUNBA_BUNDLED', 'HEVOLVE_NODE_TIER', 'HEVOLVE_API_KEY',
              'HEVOLVE_OWNER_USER_ID', 'NUNBA_CI', 'TRUSTED_PROXY'):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv('NUNBA_BUNDLED', '1')
    Base.metadata.create_all(get_engine())
    getattr(discovery, '_seen_peer_signatures', {}).clear()
    yield
    getattr(discovery, '_seen_peer_signatures', {}).clear()
    with db_session() as db:
        db.query(PeerNode).filter(
            PeerNode.node_id.like('livetest-%')).delete(
                synchronize_session=False)


@pytest.fixture
def invoker(monkeypatch):
    """This process signs as ANOTHER node: a fresh Ed25519 key and node_id.
    The server side never uses the process key, only the PeerNode rows."""
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setattr(ni, '_private_key', priv)
    monkeypatch.setattr(ni, '_public_key', priv.public_key())
    node_id = f'livetest-{uuid.uuid4().hex[:12]}'
    ids = {'invoker': node_id, 'server': SERVER_ID}
    monkeypatch.setattr(SyncEngine, 'canonical_node_id',
                        staticmethod(lambda: ids[_ROLE['who']]))
    return types.SimpleNamespace(node_id=node_id,
                                 public_key=ni.get_public_key_hex())


def _other_key():
    return Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()


def _admit(node_id, public_key, **over):
    """A PeerNode row as the SERVING node holds it for the invoker.  Default
    'verified': the state the integrity round writes once the peer answered
    a challenge (the real path is driven in the announce tests below)."""
    row = dict(node_id=node_id, url=INVOKER_URL, public_key=public_key,
               status='active', integrity_status='verified')
    row.update(over)
    with db_session() as db:
        db.add(PeerNode(**row))


@pytest.fixture
def node(monkeypatch):
    """The serving node: its own gate, its own jsonrpc view, two agents."""
    monkeypatch.setattr(peer_reuse, 'export_allowed',
                        lambda pid: str(pid) != 'livetest_other')
    app = Flask('livetest_serving_node')
    _apply_api_auth(app)
    srv = A2AProtocolServer(app, 'http://node-a')
    ran = []

    async def spy(text, ctx):
        ran.append(text)
        return {'role': 'model', 'parts': [{'text': f'ran:{text}'}]}
    srv.register_agent(AGENT, 'shared', 'd', [{'id': 's'}], spy)
    srv.register_agent(OTHER_AGENT, 'private', 'd', [{'id': 's'}], spy)
    srv.setup_routes()
    client = app.test_client()
    secrets = {'security.secrets_manager': types.SimpleNamespace(
        get_secret=lambda name: __import__('os').environ.get(name, ''))}
    seen = []
    bodies = []

    # The invoker knows the serving node by url and node_id (its peer store).
    with db_session() as db:
        db.add(PeerNode(node_id=SERVER_ID, url=PEER_URL, status='active',
                        public_key=_other_key(), integrity_status='verified'))

    def routed_post(url, json=None, timeout=None, **kw):
        with _AsServer():
            r = client.post(urlsplit(url).path, json=json,
                            environ_base=REMOTE)
        seen.append((r.status_code, r.get_json(silent=True)))
        bodies.append(json or {})
        return _Resp(r)
    monkeypatch.setattr(peer_reuse, 'pooled_post', routed_post)
    with patch.dict(sys.modules, secrets):
        yield types.SimpleNamespace(client=client, ran=ran, seen=seen,
                                    srv=srv, bodies=bodies)


def _post(node, body, agent=AGENT, environ=REMOTE):
    with _AsServer():
        r = node.client.post(f'/a2a/{agent}/jsonrpc', json=body,
                             environ_base=environ)
    return r.status_code, r.get_json()


def _signed(method='message/send', agent=AGENT, text='summarise',
            params=None, **over):
    body = {'jsonrpc': '2.0', 'id': uuid.uuid4().hex, 'method': method,
            'params': params if params is not None else {
                'message': {'messageId': uuid.uuid4().hex,
                            'parts': [{'kind': 'text', 'text': text}]}},
            'agent_id': agent}
    body = discovery.signed_peer_request(body, audience=SERVER_ID)
    body.update(over)
    return body


# ── the ruling: an admitted node runs a shared agent ─────────────────────

@pytest.mark.parametrize('env', [{'NUNBA_BUNDLED': '1'},
                                 {'HEVOLVE_NODE_TIER': 'central'},
                                 {'HEVOLVE_API_KEY': 'livetest-key'}])
def test_an_admitted_peer_runs_a_shared_agent_through_the_real_invoker(
        node, invoker, monkeypatch, env):
    monkeypatch.delenv('NUNBA_BUNDLED', raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _admit(invoker.node_id, invoker.public_key)
    result = peer_reuse.invoke_peer_agent(PEER_URL, AGENT, 'collect metrics')
    assert node.seen[-1][0] == 200, node.seen
    assert result['state'] == 'completed', result
    assert node.ran == ['collect metrics']


def test_the_invoke_is_bound_to_the_node_at_that_url_not_any_peer(
        node, invoker):
    """The invoker's peer store holds other peers too; the audience must be
    the node_id held for the url being dialled."""
    _admit(invoker.node_id, invoker.public_key)
    with db_session() as db:
        db.query(PeerNode).filter_by(node_id=SERVER_ID).delete()
        db.add(PeerNode(node_id='livetest-decoy-peer', url='http://node-c:6777',
                        status='active', public_key=_other_key(),
                        integrity_status='verified'))
    with db_session() as db:
        db.add(PeerNode(node_id=SERVER_ID, url=PEER_URL, status='active',
                        public_key=_other_key(), integrity_status='verified'))
    result = peer_reuse.invoke_peer_agent(PEER_URL, AGENT, 'bound')
    assert result and result['state'] == 'completed', node.seen
    assert node.ran == ['bound']


def _audience_of_invoke(url=PEER_URL):
    captured = {}
    real = peer_reuse.pooled_post

    def capture(u, json=None, timeout=None, **kw):
        captured['body'] = json
        return real(u, json=json, timeout=timeout, **kw)
    with patch.object(peer_reuse, 'pooled_post', capture):
        result = peer_reuse.invoke_peer_agent(url, AGENT, 'x')
    return captured['body'].get('audience'), result


def test_the_audience_is_found_past_a_thousand_other_peers(node, invoker):
    """The lookup scanned admitted_peers(limit=1000): a store holding more
    than 1000 admitted rows ahead of the dialled one signed for audience ''
    and the peer refused the invoke.  Now the url is looked up directly."""
    _admit(invoker.node_id, invoker.public_key)
    with db_session() as db:
        db.query(PeerNode).filter_by(node_id=SERVER_ID).delete()
    with db_session() as db:
        db.add_all([PeerNode(node_id=f'livetest-crowd-{i:04d}',
                             url=f'http://crowd-{i}:6777', status='active',
                             public_key='', integrity_status='unverified')
                    for i in range(1005)])
    with db_session() as db:
        db.add(PeerNode(node_id=SERVER_ID, url=PEER_URL, status='active',
                        public_key=_other_key(), integrity_status='verified'))
    audience, result = _audience_of_invoke()
    assert audience == SERVER_ID
    assert result and result['state'] == 'completed', node.seen


def test_two_identities_at_one_url_sign_for_the_one_heard_from_last(
        node, invoker):
    """A url can carry an older identity too (the node re-keyed): the node
    answering there now is the one that announced most recently."""
    _admit(invoker.node_id, invoker.public_key)
    now = datetime.utcnow()
    with db_session() as db:
        db.query(PeerNode).filter_by(node_id=SERVER_ID).one().last_seen = now
        db.add(PeerNode(node_id='livetest-old-identity', url=PEER_URL + '/',
                        status='active', public_key=_other_key(),
                        integrity_status='verified',
                        last_seen=now - timedelta(days=3)))
    assert _audience_of_invoke()[0] == SERVER_ID
    with db_session() as db:
        db.query(PeerNode).filter_by(
            node_id='livetest-old-identity').one().last_seen = (
                now + timedelta(seconds=5))
    assert _audience_of_invoke()[0] == 'livetest-old-identity'


def _stale_identities_at_the_peer_url(n=4):
    """What the owner's desktop holds (2026-09-26): 106 active urls with more
    than one row; at http://192.168.0.9:6777 four older node_ids sit beside
    230c3115, the node that answers there.  Here they are UNVERIFIED and seen
    MORE recently than the real node (a one-shot identity that announced from
    that address a minute ago), which is what makes a last_seen guess wrong."""
    later = datetime.utcnow() + timedelta(minutes=1)
    with db_session() as db:
        for i in range(n):
            db.add(PeerNode(node_id=f'livetest-stale-{i}', url=PEER_URL,
                            status='active', public_key=_other_key(),
                            integrity_status='unverified', last_seen=later))


def test_reuse_signs_for_the_peer_it_matched_not_a_guess_by_url(
        node, invoker, monkeypatch):
    """try_peer_recipe_reuse already holds the peer it matched: that node_id
    is the audience.  Driven through the REAL admitted_peers, discovery,
    invoker and serving gate; the directory and the refused recipe export
    are the only simulated responses."""
    _admit(invoker.node_id, invoker.public_key)
    _stale_identities_at_the_peer_url()
    ident = {'goal_slug': f'livetest-slug-{uuid.uuid4().hex[:6]}',
             'goal_title': 'collect metrics', 'goal_type': 'ops'}

    class _Get:
        def __init__(self, code, body):
            self.status_code, self._body = code, body

        def json(self):
            return self._body

    def get(url, timeout=None, **kw):
        if url == f'{PEER_URL}/a2a/agents':
            return _Get(200, {'agents': [{
                'agent_id': AGENT, 'status': 'completed', 'flow_id': 0,
                'goal_slug': ident['goal_slug']}]})
        if url.endswith('/recipe'):
            return _Get(403, {'error': 'export_refused'})
        return _Get(404, {})
    monkeypatch.setattr(peer_reuse, 'pooled_get', get)
    monkeypatch.setattr(peer_reuse, '_record_remote_outcome',
                        lambda *a, **k: None)
    # The url lookup is the last resort: with a matched peer in hand it must
    # not decide the audience (the store can change between the sweep and
    # the invoke).  Were it consulted, this answer would be refused.
    monkeypatch.setattr(peer_reuse, 'peer_node_id_for',
                        lambda url: 'livetest-stale-0')
    verdict = peer_reuse.try_peer_recipe_reuse(
        ident, f'livetest-{uuid.uuid4().hex[:8]}',
        deadline=time.monotonic() + 30)
    assert verdict == 'invoked', node.seen
    assert node.ran == ['collect metrics']


def test_the_url_fallback_prefers_the_verified_row(node, invoker):
    """The last resort (no node_id in hand): of the rows at the url, the one
    this node VERIFIED is the node that answered its challenge there."""
    _admit(invoker.node_id, invoker.public_key)
    _stale_identities_at_the_peer_url()
    assert _audience_of_invoke()[0] == SERVER_ID


def test_discovery_sweeps_each_url_once(node, invoker, monkeypatch):
    """Five rows at one url are one peer: the sweep (8 peers per tick) must
    not spend five of its slots asking the same directory."""
    _admit(invoker.node_id, invoker.public_key)
    _stale_identities_at_the_peer_url()
    urls = [p['url'] for p in peer_reuse.admitted_peers()]
    assert urls.count(PEER_URL) == 1, urls
    assert {p['node_id'] for p in peer_reuse.admitted_peers()
            if p['url'] == PEER_URL} == {SERVER_ID}


def test_admitted_peers_fills_its_limit_past_duplicate_urls(node, invoker):
    """The dedup ran on a limit*8 over-fetch, so more than limit*8 rows at
    one url ranked first (verified, most recent) left the sweep with ONE
    peer where eight distinct nodes were admitted (review of 4cf4411d0)."""
    now = datetime.utcnow() + timedelta(minutes=5)
    with db_session() as db:
        db.add_all([PeerNode(node_id=f'livetest-dup-{i:03d}',
                             url='http://dup-host:6777', status='active',
                             public_key=_other_key(),
                             integrity_status='verified', last_seen=now)
                    for i in range(3 * 8 + 1)])
        db.add_all([PeerNode(node_id=f'livetest-distinct-{i}',
                             url=f'http://distinct-{i}:6777', status='active',
                             public_key=_other_key(),
                             integrity_status='verified',
                             last_seen=now - timedelta(seconds=1 + i))
                    for i in range(3)])
    peers = peer_reuse.admitted_peers(limit=3)
    urls = [p['url'] for p in peers]
    assert len(urls) == 3 and len(set(urls)) == 3, urls


def test_reuse_asks_the_node_when_the_matched_row_is_unverified(
        node, invoker, monkeypatch):
    """Nothing this node verified stands at the url: the row the sweep
    matched is a guess (the most recent of several unverified identities),
    and a request signed for it is refused.  The node at that url says who
    it is (peer_node_id_for(ask_the_node=True)); that is the audience
    (review of 4cf4411d0)."""
    _admit(invoker.node_id, invoker.public_key)
    with db_session() as db:
        db.query(PeerNode).filter_by(node_id=SERVER_ID).one(
            ).integrity_status = 'unverified'
    _stale_identities_at_the_peer_url()
    ident = {'goal_slug': f'livetest-slug-{uuid.uuid4().hex[:6]}',
             'goal_title': 'collect metrics', 'goal_type': 'ops'}

    class _Get:
        def __init__(self, code, body):
            self.status_code, self._body = code, body

        def json(self):
            return self._body

    def get(url, timeout=None, **kw):
        if url == f'{PEER_URL}/a2a/agents':
            return _Get(200, {'agents': [{
                'agent_id': AGENT, 'status': 'completed', 'flow_id': 0,
                'goal_slug': ident['goal_slug']}]})
        if url == f'{PEER_URL}/api/social/peers/health':
            return _Get(200, {'node_id': SERVER_ID})
        if url.endswith('/recipe'):
            return _Get(403, {'error': 'export_refused'})
        return _Get(404, {})
    monkeypatch.setattr(peer_reuse, 'pooled_get', get)
    monkeypatch.setattr(peer_reuse, '_record_remote_outcome',
                        lambda *a, **k: None)
    matched = [p for p in peer_reuse.admitted_peers() if p['url'] == PEER_URL]
    assert matched and matched[0]['node_id'] != SERVER_ID, matched
    verdict = peer_reuse.try_peer_recipe_reuse(
        ident, f'livetest-{uuid.uuid4().hex[:8]}',
        deadline=time.monotonic() + 30)
    assert node.bodies[-1].get('audience') == SERVER_ID, node.bodies[-1]
    assert verdict == 'invoked', node.seen
    assert node.ran == ['collect metrics']


def test_a_banned_row_at_the_url_is_not_the_audience(node, invoker):
    """Same admission filter as admitted_peers: a banned row is skipped."""
    _admit(invoker.node_id, invoker.public_key)
    with db_session() as db:
        db.query(PeerNode).filter_by(node_id=SERVER_ID).one(
            ).integrity_status = 'banned'
    assert _audience_of_invoke()[0] == ''


def test_the_invoker_signs_with_the_nodes_gossip_identity(node, invoker):
    _admit(invoker.node_id, invoker.public_key)
    captured = {}
    real = peer_reuse.pooled_post

    def capture(url, json=None, timeout=None, **kw):
        captured['body'] = json
        return real(url, json=json, timeout=timeout, **kw)
    with patch.object(peer_reuse, 'pooled_post', capture):
        peer_reuse.invoke_peer_agent(PEER_URL, AGENT, 'x')
    body = captured['body']
    assert body['sender'] == {'node_id': invoker.node_id,
                              'public_key': invoker.public_key}
    assert body['agent_id'] == AGENT
    assert body['audience'] == SERVER_ID
    assert abs(body['timestamp'] - time.time()) < 5
    assert ni.verify_json_signature(invoker.public_key, body,
                                    body['signature'])


@pytest.mark.parametrize('status', ['unverified', 'claimed', 'suspicious'])
def test_a_peer_this_node_has_not_verified_is_refused(node, invoker, status):
    """Review of a5364ba66: a row is not admission.  Any fresh key gets an
    'unverified' row from the open announce; 'claimed' is a self-reported
    code hash; 'suspicious' is a fraud score over 40.  Only a peer that
    answered this node's integrity challenge ('verified') runs an agent."""
    _admit(invoker.node_id, invoker.public_key, integrity_status=status)
    code, body = _post(node, _signed())
    assert code == 401, body
    assert 'not verified' in body['error']['message']
    assert node.ran == []


def test_a_request_signed_for_another_node_is_refused(node, invoker):
    """The audience is inside the signature: a request captured on its
    way to one node does not run on another that also verified the sender."""
    _admit(invoker.node_id, invoker.public_key)
    body = discovery.signed_peer_request(
        {'jsonrpc': '2.0', 'id': 1, 'method': 'message/send', 'agent_id': AGENT,
         'params': {'message': {'parts': [{'kind': 'text', 'text': 'x'}]}}},
        audience='livetest-some-other-node')
    code, resp = _post(node, body)
    assert code == 401, resp
    assert 'another node' in resp['error']['message']
    assert node.ran == []


# ── what is refused ─────────────────────────────────────────────────────

def test_an_unknown_node_is_refused(node, invoker):
    status, body = _post(node, _signed())
    assert status == 401, body
    assert 'unknown' in body['error']['message']
    assert node.ran == []


@pytest.mark.parametrize('ban_until', [timedelta(hours=1), None])
def test_a_banned_node_is_refused(node, invoker, ban_until):
    _admit(invoker.node_id, invoker.public_key, integrity_status='banned',
           ban_until=ban_until and datetime.utcnow() + ban_until)
    status, body = _post(node, _signed())
    assert status == 401, body
    assert body['error']['message'] == 'peer not admitted: node is banned'
    assert node.ran == []


def test_a_ban_that_has_not_expired_is_refused_whatever_the_status(
        node, invoker):
    _admit(invoker.node_id, invoker.public_key,
           integrity_status='suspicious',
           ban_until=datetime.utcnow() + timedelta(hours=1))
    assert _post(node, _signed())[0] == 401
    assert node.ran == []


def test_a_ban_that_has_expired_no_longer_refuses(node, invoker):
    _admit(invoker.node_id, invoker.public_key,
           integrity_status='verified',
           ban_until=datetime.utcnow() - timedelta(hours=1))
    assert _post(node, _signed())[0] == 200


def test_a_key_other_than_the_one_on_file_is_refused(node, invoker):
    other = _other_key()
    _admit(invoker.node_id, other)
    status, body = _post(node, _signed())
    assert status == 401, body
    assert node.ran == []


def test_a_sender_that_names_the_key_on_file_but_signs_with_another_is_refused(
        node, invoker):
    other = _other_key()
    _admit(invoker.node_id, other)
    body = _signed()
    body['sender'] = {'node_id': invoker.node_id, 'public_key': other}
    assert _post(node, body)[0] == 401
    assert node.ran == []


def test_a_sender_naming_another_key_is_refused_even_signed_by_the_key_on_file(
        node, invoker):
    """The key the sender names must BE the key on file, not merely be
    outvoted by it."""
    _admit(invoker.node_id, invoker.public_key)
    body = _signed()
    body['sender'] = {'node_id': invoker.node_id, 'public_key': _other_key()}
    body['signature'] = ni.sign_json_payload(body)
    status, resp = _post(node, body)
    assert status == 401, resp
    assert 'key on file' in resp['error']['message']
    assert node.ran == []


def test_a_body_without_a_timestamp_is_refused(node, invoker):
    _admit(invoker.node_id, invoker.public_key)
    body = _signed()
    del body['timestamp']
    body['signature'] = ni.sign_json_payload(body)
    status, resp = _post(node, body)
    assert status == 401, resp
    assert 'timestamp' in resp['error']['message']


def test_the_replay_record_forgets_what_has_expired(node, invoker):
    _admit(invoker.node_id, invoker.public_key)
    discovery._seen_peer_signatures['livetest-old'] = time.time() - 1
    assert _post(node, _signed())[0] == 200
    assert 'livetest-old' not in discovery._seen_peer_signatures
    assert len(discovery._seen_peer_signatures) == 1


def test_a_tampered_body_is_refused(node, invoker):
    _admit(invoker.node_id, invoker.public_key)
    body = _signed(text='summarise')
    body['params']['message']['parts'][0]['text'] = 'delete everything'
    status, _ = _post(node, body)
    assert status == 401
    assert node.ran == []


def test_an_unsigned_body_naming_an_admitted_sender_is_refused(node, invoker):
    _admit(invoker.node_id, invoker.public_key)
    body = _signed()
    del body['signature']
    assert _post(node, body)[0] == 401
    assert node.ran == []


def _resigned_at(body, skew):
    """The invoker's own body, validly signed, but dated ``skew`` s off now."""
    body['timestamp'] += skew
    body['signature'] = ni.sign_json_payload(body)
    return body


@pytest.mark.parametrize('sign', [-1, 1])
def test_a_stale_or_future_timestamp_is_refused(node, invoker, sign):
    _admit(invoker.node_id, invoker.public_key)
    body = _resigned_at(_signed(), sign * (WITNESS_TIMESTAMP_MAX_AGE + 30))
    status, resp = _post(node, body)
    assert status == 401, resp
    assert 'timestamp' in resp['error']['message']
    assert node.ran == []


def test_a_timestamp_inside_the_window_is_admitted(node, invoker):
    _admit(invoker.node_id, invoker.public_key)
    body = _resigned_at(_signed(), -(WITNESS_TIMESTAMP_MAX_AGE - 10))
    assert _post(node, body)[0] == 200


def test_a_replayed_request_is_refused(node, invoker):
    _admit(invoker.node_id, invoker.public_key)
    body = _signed()
    assert _post(node, body)[0] == 200
    status, resp = _post(node, body)
    assert status == 401, resp
    assert node.ran == ['summarise']


def test_a_request_signed_for_one_agent_does_not_run_another(node, invoker):
    """agent_id is in the signed body; the URL is not.  A captured request
    re-posted to another agent's path must not run that agent."""
    with patch.object(peer_reuse, 'export_allowed', lambda pid: True):
        _admit(invoker.node_id, invoker.public_key)
        status, _ = _post(node, _signed(agent=AGENT), agent=OTHER_AGENT)
    assert status == 401
    assert node.ran == []


def test_an_unsigned_remote_caller_on_a_bundled_node_is_refused(node):
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'message/send', 'params': {
        'message': {'parts': [{'kind': 'text', 'text': 'x'}]}}}
    status, resp = _post(node, body)
    assert status == 401, resp
    assert node.ran == []


def test_the_desktops_own_caller_still_runs_unsigned(node):
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'message/send', 'params': {
        'message': {'parts': [{'kind': 'text', 'text': 'local'}]}}}
    assert _post(node, body, environ=LOCAL)[0] == 200
    assert node.ran == ['local']


def test_an_admitted_peer_cannot_run_an_agent_this_node_does_not_share(
        node, invoker):
    _admit(invoker.node_id, invoker.public_key)
    status, resp = _post(node, _signed(agent=OTHER_AGENT), agent=OTHER_AGENT)
    assert status == 403, resp
    assert node.ran == []


def test_a_db_failure_refuses(node, invoker, monkeypatch):
    _admit(invoker.node_id, invoker.public_key)

    def broken(*a, **k):
        raise RuntimeError('db down')
    monkeypatch.setattr(discovery, 'admitted_peer_sender', broken)
    status, _ = _post(node, _signed())
    assert status == 503
    assert node.ran == []


# ── message/get and task/cancel: same admission ─────────────────────────

def _run_one_locally(node):
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'message/send', 'params': {
        'message': {'messageId': 'livetest_task_1',
                    'parts': [{'kind': 'text', 'text': 'x'}]}}}
    assert _post(node, body, environ=LOCAL)[0] == 200
    return 'livetest_task_1'


@pytest.mark.parametrize('method', ['message/get', 'task/cancel'])
def test_task_reads_and_cancels_need_the_same_admission(node, method):
    task_id = _run_one_locally(node)
    body = {'jsonrpc': '2.0', 'id': 2, 'method': method,
            'params': {'taskId': task_id}}
    status, resp = _post(node, body)
    assert status == 401, resp
    assert 'result' not in resp


def test_an_admitted_peer_cannot_read_the_local_callers_task(node, invoker):
    """Admission lets a peer ask; the task still answers only the caller
    that started it (review finding M5: any admitted caller could read or
    cancel any task by id)."""
    task_id = _run_one_locally(node)
    _admit(invoker.node_id, invoker.public_key)
    body = discovery.signed_peer_request({
        'jsonrpc': '2.0', 'id': 3, 'method': 'message/get',
        'params': {'taskId': task_id}, 'agent_id': AGENT}, audience=SERVER_ID)
    status, resp = _post(node, body)
    assert status == 200, resp
    assert 'not found' in resp['result']['error']['message'], resp


def test_the_local_caller_reads_a_task(node):
    task_id = _run_one_locally(node)
    status, resp = _post(node, {'jsonrpc': '2.0', 'id': 4,
                                'method': 'message/get',
                                'params': {'taskId': task_id}},
                         environ=LOCAL)
    assert status == 200
    assert resp['result']['id'] == task_id


# ── the gate's own verdict passes through ───────────────────────────────

def test_a_phones_consent_pending_is_reported_as_403_not_401(
        node, monkeypatch):
    from integrations.social import consent_service as cs
    from tests.unit.test_device_access_gate import Phone
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'livetest-owner')
    phone = Phone()
    body = {'jsonrpc': '2.0', 'id': 5, 'method': 'message/send',
            'params': {'message': {'parts': [{'kind': 'text', 'text': 'x'}]}}}
    with patch.object(cs, '_emit'):
        r = node.client.post(
            f'/a2a/{AGENT}/jsonrpc', json=body, environ_base=REMOTE,
            headers={'Authorization': f'Bearer {phone.token()}'})
    try:
        assert r.status_code == 403, r.get_json()
        assert r.get_json()['error']['message'] == 'consent_pending'
        assert node.ran == []
    finally:
        from integrations.social.models import UserConsent
        with db_session() as db:
            db.query(UserConsent).filter_by(user_id='livetest-owner').delete(
                synchronize_session=False)


# ── through the REAL admission path: announce, then the integrity challenge ──
#
# No planted rows: the invoker announces itself through the real
# gossip.handle_announce (the one admission path), and becomes 'verified' only
# by answering the serving node's real IntegrityService challenge with its own
# real handle_challenge, signed by its own key.

@pytest.fixture
def gossip(monkeypatch):
    from integrations.social.peer_discovery import gossip as g
    # A new row starts an auto-follow thread that dials the peer: not this test.
    monkeypatch.setattr(g, '_auto_federate_peer', lambda *a, **k: None)
    return g


def _announce(gossip, invoker, **fields):
    info = {'node_id': invoker.node_id, 'url': INVOKER_URL,
            'name': 'livetest-invoker', 'version': '1.0.0',
            'public_key': invoker.public_key,
            'timestamp': int(time.time()), 'tier': 'flat'}
    info.update(fields)
    info['signature'] = ni.sign_json_payload(info)
    reasons = []
    new = gossip.handle_announce(info, reasons=reasons)
    return new, reasons


def _row(node_id):
    with db_session() as db:
        r = db.query(PeerNode).filter_by(node_id=node_id).first()
        return r and types.SimpleNamespace(
            integrity_status=r.integrity_status, public_key=r.public_key)


def _challenge(monkeypatch, invoker, challenge_type='guardrail_verify',
               tamper=None):
    """The serving node challenges the invoker, over the real protocol.  The
    invoker answers with its real handle_challenge (signed by its key);
    ``tamper`` lets a dishonest node change its answer and re-sign it."""
    from integrations.social import integrity_service as isvc
    from integrations.social.integrity_service import IntegrityService

    def answer(url, json=None, timeout=None, **kw):
        assert url == f'{INVOKER_URL}/api/social/integrity/challenge'
        with db_session() as peer_db:
            out = IntegrityService.handle_challenge(peer_db, json)
        if tamper:
            out['response'] = tamper(dict(out['response']))
            out['signature'] = ni.sign_json_payload(out['response'])
        return types.SimpleNamespace(status_code=200,
                                     json=lambda: {'success': True, **out})
    monkeypatch.setattr(isvc, 'pooled_post', answer)
    with _AsServer(), db_session() as db:
        return IntegrityService.create_challenge(
            db, SERVER_ID, invoker.node_id, INVOKER_URL, challenge_type)


def _hive_hashes():
    from security.hive_guardrails import get_guardrail_hash
    return {'guardrail_hash': get_guardrail_hash(),
            'code_hash': 'livetest-unregistered-desktop-build'}


def test_a_stranger_that_announced_itself_cannot_run_an_agent(
        node, invoker, gossip):
    """The reviewer's probe, as a test: a fresh key, no guardrail hash, an
    unknown code hash.  The open announce admits the row ('unverified');
    that row alone must not run anything."""
    new, reasons = _announce(gossip, invoker)
    assert new is True, reasons
    assert _row(invoker.node_id).integrity_status == 'unverified'
    result = peer_reuse.invoke_peer_agent(PEER_URL, AGENT,
                                          'open notepad and type hi')
    assert result is None
    assert node.seen[-1][0] == 401, node.seen
    assert 'not verified' in node.seen[-1][1]['error']['message']
    assert node.ran == []


def test_a_node_with_another_guardrail_hash_is_refused_at_announce(
        node, invoker, gossip):
    new, reasons = _announce(gossip, invoker, guardrail_hash='0' * 64)
    assert new is False and 'guardrail hash mismatch' in reasons[0]
    assert _row(invoker.node_id) is None
    assert peer_reuse.invoke_peer_agent(PEER_URL, AGENT, 'x') is None
    assert node.ran == []


def test_a_peer_that_passed_the_real_verification_runs_the_agent(
        node, invoker, gossip, monkeypatch):
    new, reasons = _announce(gossip, invoker, **_hive_hashes())
    assert new is True, reasons
    assert _row(invoker.node_id).integrity_status == 'unverified'
    verdict = _challenge(monkeypatch, invoker)
    assert verdict['passed'] is True, verdict
    assert _row(invoker.node_id).integrity_status == 'verified'
    result = peer_reuse.invoke_peer_agent(PEER_URL, AGENT, 'collect metrics')
    assert result and result['state'] == 'completed', node.seen
    assert node.ran == ['collect metrics']


def test_a_peer_that_answers_with_another_guardrail_hash_is_not_admitted(
        node, invoker, gossip, monkeypatch):
    """Announced without a hash (so the announce could not compare one), then
    answered the challenge with a guardrail hash that is not the hive's."""
    _announce(gossip, invoker)

    def other_values(resp):
        resp['guardrail_hash'] = resp['guardrail_hash_live'] = '0' * 64
        return resp
    verdict = _challenge(monkeypatch, invoker, tamper=other_values)
    assert verdict['passed'] is False, verdict
    assert _row(invoker.node_id).integrity_status != 'verified'
    assert peer_reuse.invoke_peer_agent(PEER_URL, AGENT, 'x') is None
    assert node.ran == []


def test_a_verified_peer_that_later_fails_a_challenge_is_refused_again(
        node, invoker, gossip, monkeypatch):
    _announce(gossip, invoker, **_hive_hashes())
    assert _challenge(monkeypatch, invoker)['passed'] is True

    def other_values(resp):
        resp['guardrail_hash'] = resp['guardrail_hash_live'] = '0' * 64
        return resp
    assert _challenge(monkeypatch, invoker, tamper=other_values)['passed'] is False
    assert _row(invoker.node_id).integrity_status == 'claimed'
    assert peer_reuse.invoke_peer_agent(PEER_URL, AGENT, 'x') is None
    assert node.ran == []


# ── M2: a remote turn nobody waits for is cancelled, not orphaned ────────
#
# Review finding M2 (2026-09-26): the caller waited min(30, remaining) of a
# 10 s peer budget while the server waited up to 30 s for its one LLM permit
# and then ran a whole /chat turn: the caller timed out, fell back to local
# CREATE, and the server finished a turn nobody read, holding the permit.
# Now message/send with configuration.blocking=false returns the task at
# once, the caller polls message/get inside its budget and sends task/cancel
# when it gives up, and the cancel reaches the executor (which frees the
# permit before the turn starts: dispatch.local_chat_dispatch below).

SLOW = 'livetest_slow_0'


def _register_slow(node, release):
    seen = {}

    async def slow(text, ctx, cancel_event=None):
        seen['cancel_event'] = cancel_event
        import asyncio
        for _ in range(200):
            if release.is_set():
                return {'role': 'model', 'parts': [{'text': f'slow:{text}'}]}
            if cancel_event is not None and cancel_event.is_set():
                seen['cancelled_at'] = time.monotonic()
                raise RuntimeError('cancelled before the turn started')
            await asyncio.sleep(0.05)
        raise RuntimeError('never released')
    node.srv.register_agent(SLOW, 'slow', 'd', [{'id': 's'}], slow)
    return seen


def test_a_non_blocking_send_returns_before_the_turn_finishes(node, invoker):
    import threading
    _admit(invoker.node_id, invoker.public_key)
    release = threading.Event()
    _register_slow(node, release)
    started = time.monotonic()
    code, body = _post(node, _signed(agent=SLOW, params={
        'message': {'parts': [{'kind': 'text', 'text': 'later'}]},
        'configuration': {'blocking': False}}), agent=SLOW)
    assert code == 200, body
    assert time.monotonic() - started < 2
    assert body['result']['state'] in ('submitted', 'working'), body
    release.set()
    task_id = body['result']['id']
    for _ in range(100):
        code, got = _post(node, _signed(method='message/get', agent=SLOW,
                                        params={'taskId': task_id}), agent=SLOW)
        if got['result'].get('state') == 'completed':
            break
        time.sleep(0.05)
    assert got['result']['state'] == 'completed', got
    assert got['result']['content']['parts'][0]['text'] == 'slow:later'


def test_a_send_without_configuration_still_blocks(node, invoker):
    _admit(invoker.node_id, invoker.public_key)
    code, body = _post(node, _signed())
    assert code == 200 and body['result']['state'] == 'completed', body


def test_the_caller_that_gives_up_cancels_the_remote_turn(node, invoker):
    """The real invoker against the real server: a budget shorter than the
    server's wait makes the invoker send task/cancel, and the executor sees
    it before any turn starts."""
    import threading
    _admit(invoker.node_id, invoker.public_key)
    release = threading.Event()
    seen = _register_slow(node, release)
    started = time.monotonic()
    result = peer_reuse.invoke_peer_agent(PEER_URL, SLOW, 'x', timeout=1.5)
    waited = time.monotonic() - started
    assert result is None
    assert waited < 4, waited
    methods = [b.get('method') for b in node.bodies]
    assert methods[0] == 'message/send' and 'task/cancel' in methods, methods
    for _ in range(100):
        if 'cancelled_at' in seen:
            break
        time.sleep(0.05)
    assert 'cancelled_at' in seen, seen
    task_id = node.seen[0][1]['result']['id']
    code, got = _post(node, _signed(method='message/get', agent=SLOW,
                                    params={'taskId': task_id}), agent=SLOW)
    assert got['result']['state'] == 'failed', got
    assert 'cancel' in got['result'].get('error', ''), got


def test_a_cancelled_task_is_not_overwritten_by_a_late_result(node, invoker):
    """A turn that was already running finishes, but the task keeps the
    cancelled verdict: nobody asked for that answer any more."""
    import threading
    _admit(invoker.node_id, invoker.public_key)
    release = threading.Event()
    node.srv.register_agent(
        SLOW, 'slow', 'd', [{'id': 's'}],
        _late_executor(release))
    code, body = _post(node, _signed(agent=SLOW, params={
        'message': {'parts': [{'kind': 'text', 'text': 'x'}]},
        'configuration': {'blocking': False}}), agent=SLOW)
    task_id = body['result']['id']
    code, out = _post(node, _signed(method='task/cancel', agent=SLOW,
                                    params={'taskId': task_id}), agent=SLOW)
    assert code == 200 and out['result'].get('success'), out
    release.set()
    time.sleep(0.5)
    code, got = _post(node, _signed(method='message/get', agent=SLOW,
                                    params={'taskId': task_id}), agent=SLOW)
    assert got['result']['state'] == 'failed', got


def _late_executor(release):
    async def run(text, ctx):
        import asyncio
        while not release.is_set():
            await asyncio.sleep(0.02)
        return {'role': 'model', 'parts': [{'text': 'late'}]}
    return run


# ── M4: `hart a2a send` speaks the server's contract ─────────────────────
#
# Review finding M4: the command read result['artifacts'] (the server
# returns 'content') and sent no signature, so against a bundled, central
# or keyed node it printed "Task state: ?" and nothing else.  It now signs
# with this node's gossip key (the peer invoke's path), prints the reply's
# text, and says plainly why a 401 happened.

def _cli(monkeypatch, *args):
    from click.testing import CliRunner
    from hartos import hart_cli
    monkeypatch.setattr(hart_cli, 'pooled_post', peer_reuse.pooled_post)
    return CliRunner().invoke(hart_cli.hart, list(args), catch_exceptions=False)


def test_hart_a2a_send_prints_the_agents_reply(node, invoker, monkeypatch):
    _admit(invoker.node_id, invoker.public_key)
    out = _cli(monkeypatch, 'a2a', 'send', f'{PEER_URL}/a2a/{AGENT}',
               'collect metrics')
    assert out.exit_code == 0, out.output
    assert 'Task state: completed' in out.output
    assert 'ran:collect metrics' in out.output
    assert node.ran == ['collect metrics']
    body = node.bodies[-1]
    assert body['sender']['node_id'] == invoker.node_id
    assert body['audience'] == SERVER_ID and body['agent_id'] == AGENT


def test_hart_a2a_send_explains_a_refusal(node, invoker, monkeypatch):
    _admit(invoker.node_id, invoker.public_key, integrity_status='unverified')
    out = _cli(monkeypatch, 'a2a', 'send', f'{PEER_URL}/a2a/{AGENT}', 'x')
    assert out.exit_code != 0
    assert '401' in out.output and 'not verified' in out.output, out.output
    assert 'integrity challenge' in out.output, out.output
    assert node.ran == []


def test_hart_a2a_send_json_is_the_raw_reply(node, invoker, monkeypatch):
    _admit(invoker.node_id, invoker.public_key)
    out = _cli(monkeypatch, '--json', 'a2a', 'send',
               f'{PEER_URL}/a2a/{AGENT}', 'collect metrics')
    assert out.exit_code == 0, out.output
    assert json.loads(out.output)['result']['state'] == 'completed'


def test_hart_a2a_send_refuses_a_url_that_names_no_agent(monkeypatch):
    out = _cli(monkeypatch, 'a2a', 'send', 'http://node-b:5000', 'x')
    assert out.exit_code != 0
    assert '/a2a/<agent_id>' in out.output, out.output


# ── review of a4ea04651, finding 3: the invoker's poll ───────────────────

def _scripted_peer(monkeypatch, replies):
    """pooled_post answering each JSON-RPC method from ``replies`` (a dict
    of method -> list of results, consumed in order)."""
    calls = []

    class _R:
        status_code = 200

        def __init__(self, body):
            self._b = body

        def json(self):
            return self._b

    def post(url, json=None, timeout=None, **kw):
        m = json['method']
        calls.append(m)
        seq = replies[m]
        if callable(seq):
            res = seq(calls)
        else:
            res = seq.pop(0) if len(seq) > 1 else seq[0]
        return _R({'jsonrpc': '2.0', 'id': json['id'], 'result': res})
    monkeypatch.setattr(peer_reuse, 'pooled_post', post)
    return calls


def test_a_failed_task_is_returned_not_cancelled(monkeypatch):
    calls = _scripted_peer(monkeypatch, {
        'message/send': [{'id': 't1', 'state': 'working'}],
        'message/get': [{'id': 't1', 'state': 'failed', 'error': 'boom'}],
        'task/cancel': [{'success': True}]})
    res = peer_reuse.invoke_peer_agent(PEER_URL, AGENT, 'x', timeout=5,
                                       peer_node_id=SERVER_ID)
    assert res == {'id': 't1', 'state': 'failed', 'error': 'boom'}, res
    assert 'task/cancel' not in calls, calls


def test_a_refused_cancel_gets_one_last_look(monkeypatch):
    """The task finished while the cancel was on its way: the cancel is
    refused, and the finished answer is still used."""
    done = {'id': 't1', 'state': 'completed',
            'content': {'parts': [{'text': 'late but done'}]}}
    calls = _scripted_peer(monkeypatch, {
        'message/send': [{'id': 't1', 'state': 'working'}],
        'message/get': lambda calls: (done if 'task/cancel' in calls
                                      else {'id': 't1', 'state': 'working'}),
        'task/cancel': [{'error': {'code': -32600,
                                   'message': 'Cannot cancel task in state completed'}}]})
    monkeypatch.setattr(peer_reuse, '_POLL_INTERVAL_S', 0.05)
    res = peer_reuse.invoke_peer_agent(PEER_URL, AGENT, 'x', timeout=0.6,
                                       peer_node_id=SERVER_ID)
    assert calls[-2:] == ['task/cancel', 'message/get'], calls
    assert res == done, res


def test_the_poll_backs_off(monkeypatch):
    calls = _scripted_peer(monkeypatch, {
        'message/send': [{'id': 't1', 'state': 'working'}],
        'message/get': [{'id': 't1', 'state': 'working'}],
        'task/cancel': [{'success': True}]})
    started = time.monotonic()
    assert peer_reuse.invoke_peer_agent(PEER_URL, AGENT, 'x', timeout=6.9,
                                        peer_node_id=SERVER_ID) is None
    # The whole budget is spent before the cancel: the last sleep is cut to
    # what is left, not skipped (a backed-off 2 s step would stop at ~5.8).
    elapsed = time.monotonic() - started
    assert elapsed >= 6.7
    # ...and not overrun: the cancel goes out at the budget, not a backed-off
    # step past it (mutant P2: the last sleep not cut to what is left).
    assert elapsed <= 7.6, elapsed
    assert calls.count('message/get') <= 8, calls
    assert calls[-1] == 'task/cancel' or calls[-2] == 'task/cancel', calls


# ── review of a4ea04651, deferred minors ─────────────────────────────────

def test_the_audience_is_asked_of_the_node_when_the_store_has_no_row(
        node, monkeypatch, caplog):
    """peer_node_id_for(url, ask_the_node=True): the store first, then the
    node's own /api/social/peers/health; a failed ask is LOGGED, not
    swallowed (it used to live in hart_cli as a silent except)."""
    import logging
    with db_session() as db:
        db.query(PeerNode).filter_by(node_id=SERVER_ID).delete()

    class _R:
        def json(self):
            return {'node_id': 'livetest-asked'}
    monkeypatch.setattr(peer_reuse, 'pooled_get', lambda url, **k: _R())
    assert peer_reuse.peer_node_id_for(PEER_URL) == ''
    assert peer_reuse.peer_node_id_for(PEER_URL, ask_the_node=True) == \
        'livetest-asked'

    def boom(url, **k):
        raise ConnectionError('down')
    monkeypatch.setattr(peer_reuse, 'pooled_get', boom)
    with caplog.at_level(logging.INFO, logger='hevolve_social'):
        assert peer_reuse.peer_node_id_for(PEER_URL, ask_the_node=True) == ''
    assert any('down' in r.getMessage() for r in caplog.records), caplog.text


def test_open_task_states_are_the_protocols():
    from integrations.google_a2a.google_a2a_integration import TaskState
    assert set(peer_reuse._OPEN_TASK_STATES) == {
        TaskState.SUBMITTED.value, TaskState.WORKING.value}


def test_source_guard_hart_cli_uses_public_peer_reuse_helpers():
    """hart_cli imported _peer_node_id_for and _result_text: private names
    across a package boundary drift the moment peer_reuse renames them."""
    import ast
    tree = ast.parse(open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), 'hartos', 'hart_cli.py'),
        encoding='utf-8').read())
    private = [a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
               and (n.module or '').endswith('peer_reuse')
               for a in n.names if a.name.startswith('_')]
    assert private == [], private
