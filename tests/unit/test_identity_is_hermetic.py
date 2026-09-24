"""A test run never reads or writes the owner's real node identity.

On 2026-09-23 an uncommitted identity change ran under test and replaced the
owner's desktop node_id.json (46329c87, the id central had verified) with a
fresh id. Importing integrations.social.peer_discovery builds GossipProtocol at
module level, and its __init__ loads-or-creates node_id.json under the real
data root; the Ed25519 key dir resolved there too.

These tests stand in a decoy home for the real one, so a failure writes into
tmp_path, never into ~/Documents/Nunba.
"""
import importlib
import os
import subprocess
import sys

import pytest

import core.platform_paths as pp

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_OVERRIDES = ('NUNBA_DATA_DIR', 'HARTOS_DATA_DIR', 'HEVOLVE_KEY_DIR',
              'HEVOLVE_DB_PATH', 'XDG_DATA_HOME')


@pytest.fixture
def decoy_home(tmp_path, monkeypatch):
    """Point the platform default data root at tmp_path, with no overrides."""
    home = tmp_path / 'home'
    home.mkdir()
    for var in ('HOME', 'USERPROFILE'):
        monkeypatch.setenv(var, str(home))
    for var in _OVERRIDES:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(pp, '_cached_data_dir', None)
    monkeypatch.setattr(pp, '_pytest_identity_dir', None)
    real_root = pp._platform_default_data_dir()
    assert real_root.startswith(str(home)), real_root
    return real_root


def test_node_id_is_not_written_under_the_real_root(decoy_home):
    from integrations.social import peer_discovery

    node_id = peer_discovery._load_or_create_node_id()

    assert node_id
    assert not os.path.exists(os.path.join(decoy_home, 'node_id.json'))


def test_key_dir_is_not_the_real_root(decoy_home):
    import security.node_integrity as ni

    key_dir = os.path.abspath(ni._resolve_key_dir())

    assert not key_dir.startswith(os.path.abspath(decoy_home)), key_dir


def test_a_data_dir_the_test_chose_is_used_as_given(decoy_home, tmp_path, monkeypatch):
    mine = tmp_path / 'mine'
    monkeypatch.setenv('NUNBA_DATA_DIR', str(mine))
    monkeypatch.setattr(pp, '_cached_data_dir', None)
    from integrations.social import peer_discovery
    import security.node_integrity as ni

    key_dir = ni._resolve_key_dir()
    monkeypatch.setattr(ni, '_KEY_DIR', key_dir)
    monkeypatch.setattr(ni, '_private_key', None)
    monkeypatch.setattr(ni, '_public_key', None)

    node_id = peer_discovery._load_or_create_node_id()

    assert os.path.abspath(key_dir) == str(mine / 'agent_data')
    assert (mine / 'agent_data' / 'node_identity.json').read_text().count(node_id) == 1


def test_the_same_temp_root_holds_for_the_whole_process(decoy_home):
    first = pp.get_identity_data_dir()

    assert first == pp.get_identity_data_dir()
    assert not first.startswith(os.path.abspath(decoy_home))


def test_no_shipped_module_imports_pytest():
    # get_identity_data_dir keys off "pytest is imported". A shipped module
    # that imported it would put a real node on a temp identity.
    import re
    pattern = re.compile(r'^\s*(import|from)\s+_?pytest\b', re.M)
    offenders = []
    for top in ('core', 'integrations', 'security'):
        for dirpath, dirnames, filenames in os.walk(os.path.join(_REPO, top)):
            dirnames[:] = [d for d in dirnames if d not in ('tests', 'test')]
            for name in filenames:
                if not name.endswith('.py') or name.startswith('test_') \
                        or name.endswith('_test.py') or name == 'conftest.py':
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding='utf-8', errors='replace') as fh:
                    if pattern.search(fh.read()):
                        offenders.append(os.path.relpath(path, _REPO))

    assert offenders == []


def test_outside_pytest_the_real_root_is_used(decoy_home):
    # A real node must keep its identity where it always was.
    code = ('import sys, core.platform_paths as pp; '
            'assert "pytest" not in sys.modules; '
            'print(pp.get_identity_data_dir() == pp._platform_default_data_dir())')
    env = {k: v for k, v in os.environ.items() if k not in _OVERRIDES}
    out = subprocess.run([sys.executable, '-c', code], cwd=_REPO, env=env,
                         capture_output=True, text=True, timeout=60)

    assert out.stdout.strip() == 'True', out.stderr
