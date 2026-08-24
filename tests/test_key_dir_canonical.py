"""One resolver for "where does this node's key material live" (#632).

The last-resort used to be a RELATIVE 'agent_data': the same machine
minted a DIFFERENT Ed25519 identity per working directory, and under a
read-only install root (Program Files) the key write failed outright.
channel_encryption carried a byte-copy of the resolver (X25519 must
persist alongside Ed25519 — a copy can drift them into different dirs),
and key_delegation's inline default had ALREADY drifted (it missed the
HEVOLVE_DB_PATH branch).
"""

import importlib
import os

import pytest


@pytest.fixture
def clean_env(monkeypatch):
    """Clear resolver env vars; restore module state after the test."""
    saved = {v: os.environ.get(v) for v in ('HEVOLVE_KEY_DIR',
                                            'HEVOLVE_DB_PATH')}
    for var in saved:
        monkeypatch.delenv(var, raising=False)
    yield monkeypatch
    for var, val in saved.items():
        if val is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = val
    import security.node_integrity as ni
    import security.channel_encryption as ce
    import security.key_delegation as kd
    importlib.reload(ni)
    importlib.reload(ce)
    importlib.reload(kd)


def _patch_data_dir(monkeypatch, path):
    import core.platform_paths as pp
    monkeypatch.setattr(pp, 'get_data_dir', lambda: str(path))


def test_last_resort_is_stable_not_cwd(clean_env, tmp_path):
    clean_env.chdir(tmp_path)
    stable_root = tmp_path / 'stable'
    _patch_data_dir(clean_env, stable_root)
    import security.node_integrity as ni
    ni = importlib.reload(ni)
    assert os.path.isabs(ni._KEY_DIR), (
        f"key dir {ni._KEY_DIR!r} is CWD-relative — identity forks per "
        "working directory (#632)")
    assert ni._KEY_DIR.startswith(str(stable_root))


def test_legacy_cwd_keypair_adopted(clean_env, tmp_path):
    legacy = tmp_path / 'agent_data'
    legacy.mkdir()
    (legacy / 'node_private_key.pem').write_bytes(b'PRIV-LEGACY')
    (legacy / 'node_public_key.pem').write_bytes(b'PUB-LEGACY')
    clean_env.chdir(tmp_path)
    stable_root = tmp_path / 'stable'
    _patch_data_dir(clean_env, stable_root)
    import security.node_integrity as ni
    ni = importlib.reload(ni)
    adopted = os.path.join(ni._KEY_DIR, 'node_private_key.pem')
    assert os.path.isfile(adopted), (
        "legacy CWD-relative keypair was not adopted — the node would "
        "mint a NEW identity on upgrade (#632)")
    with open(adopted, 'rb') as fh:
        assert fh.read() == b'PRIV-LEGACY'


def test_channel_encryption_shares_resolver(clean_env):
    import security.node_integrity as ni
    import security.channel_encryption as ce
    ni = importlib.reload(ni)
    ce = importlib.reload(ce)
    assert ce._resolve_key_dir is ni.resolve_key_dir, (
        "channel_encryption carries its own resolver copy — X25519 and "
        "Ed25519 keys can drift into different dirs (#632)")


def test_cert_path_follows_resolver(clean_env, tmp_path):
    clean_env.chdir(tmp_path)
    _patch_data_dir(clean_env, tmp_path / 'stable')
    import security.node_integrity as ni
    import security.key_delegation as kd
    ni = importlib.reload(ni)
    kd = importlib.reload(kd)
    assert os.path.dirname(kd._DEFAULT_CERT_PATH) == ni._KEY_DIR
    assert os.path.isabs(kd._DEFAULT_CERT_PATH), (
        "node_certificate.json resolved CWD-relatively (#632)")
