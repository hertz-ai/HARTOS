"""Node identity: a lost race still mints the id, and says so in the log.

security/node_integrity.py had five `except ...: pass` bodies (the security
ratchet's 67 > 62).  Each one covered a race or a missing dependency, and each
now logs instead of swallowing.  These tests drive the REAL functions with a
real Ed25519 key in a temp key dir, force the race (os.replace raising
FileNotFoundError, as when another process moved the record first), and assert
both the observable result (an id is minted and recorded) and the log record.
"""
import json
import logging
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

import security.node_integrity as ni  # noqa: E402


@pytest.fixture
def key_dir(tmp_path, monkeypatch):
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setattr(ni, '_private_key', priv)
    monkeypatch.setattr(ni, '_public_key', priv.public_key())
    monkeypatch.setattr(ni, '_KEY_DIR', str(tmp_path))
    monkeypatch.setattr(ni, 'identity_state', None)
    return tmp_path


_REAL_REPLACE = os.replace


def _raise_not_found(src, dst):
    """Another process moved the record aside a moment before we could: the
    record is gone (moved), and our own os.replace finds nothing to move."""
    _REAL_REPLACE(src, dst)
    raise FileNotFoundError('moved by another process')


def test_take_new_identity_survives_a_concurrent_move(key_dir, monkeypatch,
                                                      caplog):
    rec = key_dir / ni._IDENTITY_FILE
    rec.write_text(json.dumps({'node_id': 'old-id',
                               'public_key_hex': 'someone-else'}),
                   encoding='utf-8')
    monkeypatch.setattr(ni.os, 'replace', _raise_not_found)
    with caplog.at_level(logging.DEBUG, logger='hevolve_security'):
        new_id = ni.take_new_node_identity('test conflict')
    assert new_id and new_id != 'old-id'
    assert ni.identity_state == 'minted'
    assert any('already moved by another process' in r.getMessage()
               for r in caplog.records), 'the lost race left no log line'


def test_mismatched_record_is_replaced_even_when_the_move_races(
        key_dir, monkeypatch, caplog):
    rec = key_dir / ni._IDENTITY_FILE
    rec.write_text(json.dumps({'node_id': 'stale-id',
                               'public_key_hex': 'a-different-key'}),
                   encoding='utf-8')
    monkeypatch.setattr(ni.os, 'replace', _raise_not_found)
    with caplog.at_level(logging.DEBUG, logger='hevolve_security'):
        node_id = ni.load_or_create_node_identity()
    assert node_id != 'stale-id'
    assert ni.identity_state == 'minted'
    msgs = [r.getMessage() for r in caplog.records]
    assert any('already moved by another process' in m for m in msgs), msgs


def test_second_writer_uses_the_record_on_disk_and_logs_it(key_dir, caplog):
    path = str(key_dir / ni._IDENTITY_FILE)
    first = ni._write_identity_once(path, {'node_id': 'first'})
    with caplog.at_level(logging.DEBUG, logger='hevolve_security'):
        second = ni._write_identity_once(path, {'node_id': 'second'})
    assert first['node_id'] == second['node_id'] == 'first'
    assert any('already recorded by another process' in r.getMessage()
               for r in caplog.records)


def test_key_dir_search_warns_when_the_data_root_is_unavailable(
        monkeypatch, caplog):
    monkeypatch.delenv('HEVOLVE_KEY_DIR', raising=False)
    monkeypatch.delenv('HEVOLVE_DB_PATH', raising=False)
    import core.platform_paths as pp

    def _boom():
        raise RuntimeError('no data root')

    monkeypatch.setattr(pp, 'get_identity_data_dir', _boom)
    with caplog.at_level(logging.WARNING, logger='hevolve_security'):
        found = ni.local_key_dir_holding('f' * 16)
    assert found is None
    assert any('not searched' in r.getMessage() for r in caplog.records), (
        'two candidate key dirs were dropped without a word')
