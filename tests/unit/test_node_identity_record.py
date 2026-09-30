"""A node's id is stored next to the key that proves it (#140 B1).

The id used to live in <data root>/node_id.json while the key came from a
separate resolver, so the owner's desktop held one id (46329c87) and two
keypairs: data/ (the key central stores for that id) and agent_data/. Whichever
process wrote last could pair the id with the wrong key. Every test here runs in
tmp_path; the owner's files are never read.
"""
import json
import os
import threading
import types

import pytest

import core.platform_paths as pp
import security.node_integrity as ni

LEGACY_ID = '46329c87-cbb6-4ca1-bad5-816f6007b6a0'


def _make_keypair(key_dir, monkeypatch):
    """A real Ed25519 keypair in key_dir; returns its public key hex."""
    os.makedirs(key_dir, exist_ok=True)
    monkeypatch.setattr(ni, '_KEY_DIR', str(key_dir))
    monkeypatch.setattr(ni, '_private_key', None)
    monkeypatch.setattr(ni, '_public_key', None)
    pub = ni.get_public_key_hex()
    monkeypatch.setattr(ni, '_private_key', None)
    monkeypatch.setattr(ni, '_public_key', None)
    return pub


@pytest.fixture
def key_dir(tmp_path, monkeypatch):
    d = tmp_path / 'keys'
    pub = _make_keypair(d, monkeypatch)
    monkeypatch.setattr(ni, '_KEY_DIR', str(d))
    monkeypatch.setattr(ni, 'identity_state', None)
    return types.SimpleNamespace(path=d, pub=pub)


def _record(kd):
    return json.loads((kd.path / 'node_identity.json').read_text())


def test_a_new_node_records_its_id_with_its_key(key_dir):
    node_id = ni.load_or_create_node_identity()

    assert ni.identity_state == 'minted'
    assert _record(key_dir) == {**_record(key_dir), 'node_id': node_id,
                                'public_key_hex': key_dir.pub}
    assert ni.load_or_create_node_identity() == node_id
    assert ni.identity_state == 'recorded'


def test_a_replaced_key_gets_a_new_id_and_the_old_record_is_kept(key_dir, caplog):
    (key_dir.path / 'node_identity.json').write_text(json.dumps(
        {'node_id': 'old-id', 'public_key_hex': 'ab' * 32}))

    node_id = ni.load_or_create_node_identity()

    assert node_id != 'old-id'
    assert _record(key_dir)['public_key_hex'] == key_dir.pub
    kept = list(key_dir.path.glob('node_identity.superseded.*.json'))
    assert len(kept) == 1 and json.loads(kept[0].read_text())['node_id'] == 'old-id'
    assert 'does not match the key' in caplog.text


def test_a_legacy_id_is_used_but_not_claimed(key_dir, tmp_path):
    legacy = tmp_path / 'node_id.json'
    legacy.write_text(json.dumps({'node_id': LEGACY_ID, 'created_at': 'x'}))
    before = legacy.read_bytes()

    node_id = ni.load_or_create_node_identity(str(legacy))

    assert node_id == LEGACY_ID
    assert ni.identity_state == 'legacy_provisional'
    assert not (key_dir.path / 'node_identity.json').exists()
    assert legacy.read_bytes() == before


def test_a_record_wins_over_the_legacy_file(key_dir, tmp_path):
    legacy = tmp_path / 'node_id.json'
    legacy.write_text(json.dumps({'node_id': LEGACY_ID}))
    mine = ni.load_or_create_node_identity()

    assert ni.load_or_create_node_identity(str(legacy)) == mine


def test_two_writers_end_with_the_first_writers_id(key_dir):
    path = str(key_dir.path / 'node_identity.json')

    first = ni._write_identity_once(path, {'node_id': 'A', 'public_key_hex': key_dir.pub})
    second = ni._write_identity_once(path, {'node_id': 'B', 'public_key_hex': key_dir.pub})

    assert first['node_id'] == second['node_id'] == 'A'
    assert not [p for p in os.listdir(key_dir.path) if p.endswith('.tmp')]


def test_processes_booting_together_agree_on_one_id(key_dir):
    ni.get_public_key_hex()  # load the key once; the race is on the record
    seen = []
    barrier = threading.Barrier(8)

    def boot():
        barrier.wait()
        seen.append(ni.load_or_create_node_identity())

    threads = [threading.Thread(target=boot) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(seen)) == 1 and seen[0] == _record(key_dir)['node_id']


@pytest.fixture
def identity_root(tmp_path, monkeypatch):
    root = tmp_path / 'root'
    root.mkdir()
    for var in ('HEVOLVE_KEY_DIR', 'HEVOLVE_DB_PATH'):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(pp, 'get_identity_data_dir', lambda: str(root))
    return root


def test_an_existing_data_keypair_wins_over_agent_data(identity_root, monkeypatch):
    # R2: the desktop shape. Both dirs hold keys and no DB_PATH is set.
    _make_keypair(identity_root / 'data', monkeypatch)
    _make_keypair(identity_root / 'agent_data', monkeypatch)

    assert ni._resolve_key_dir() == str(identity_root / 'data')


def test_agent_data_is_still_used_when_data_holds_no_keypair(identity_root, monkeypatch):
    _make_keypair(identity_root / 'agent_data', monkeypatch)
    (identity_root / 'data').mkdir()
    (identity_root / 'data' / 'node_public_key.pem').write_text('half a pair')

    assert ni._resolve_key_dir() == str(identity_root / 'agent_data')


def test_explicit_key_dir_still_wins(identity_root, tmp_path, monkeypatch):
    _make_keypair(identity_root / 'data', monkeypatch)
    monkeypatch.setenv('HEVOLVE_KEY_DIR', str(tmp_path / 'explicit'))

    assert ni._resolve_key_dir() == str(tmp_path / 'explicit')


def test_the_desktop_keeps_its_id_and_writes_nothing(identity_root, monkeypatch):
    # The owner's desktop on first boot of this code: a legacy node_id.json,
    # keys in data/ AND agent_data/. Every process resolves data/, uses the
    # legacy id, and nothing is written until a peer confirms the pairing.
    data_pub = _make_keypair(identity_root / 'data', monkeypatch)
    _make_keypair(identity_root / 'agent_data', monkeypatch)
    legacy = identity_root / 'node_id.json'
    legacy.write_text(json.dumps({'node_id': LEGACY_ID}))
    monkeypatch.setattr(ni, '_KEY_DIR', ni._resolve_key_dir())

    node_id = ni.load_or_create_node_identity(str(legacy))

    assert node_id == LEGACY_ID
    assert ni.get_public_key_hex() == data_pub
    assert not list(identity_root.rglob('node_identity*.json'))
