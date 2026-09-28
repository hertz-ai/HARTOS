"""#45 — AdminAPI identity + workflows (+ channels) survive a restart.

_save_config used to dump an always-empty self._config, so the live state
(channels/workflows/identity) was lost on every restart.  Now it serializes the
real attrs.  Verified by a save-then-fresh-load round-trip on a temp file.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_identity_workflows_channels_survive_restart(tmp_path, monkeypatch):
    try:
        from integrations.channels.admin.api import AdminAPI
        from integrations.channels.admin.schemas import (
            WorkflowSchema, IdentityConfigSchema)
    except Exception as e:
        pytest.skip(f"admin api/schemas unavailable: {e}")

    cfg = str(tmp_path / 'admin_config.json')
    monkeypatch.setattr(AdminAPI, '_config_path', lambda self: cfg)

    a = AdminAPI()  # no file yet → empty
    a._channels = {'discord': {'bot_token': 'tok', 'announce_chat_id': '123'}}
    a._workflows = {'w1': WorkflowSchema(id='w1', name='Greet', enabled=True,
                                         nodes=[{'t': 'start'}])}
    a._identity = IdentityConfigSchema(agent_id='ag1', display_name='Nunba',
                                       bio='local mind', personality={'tone': 'warm'})
    a._save_config()
    assert os.path.exists(cfg)

    # Simulate a restart: a brand-new instance loads from the same file.
    b = AdminAPI()
    assert b._channels == {'discord': {'bot_token': 'tok', 'announce_chat_id': '123'}}
    assert 'w1' in b._workflows
    assert b._workflows['w1'].name == 'Greet'
    assert b._workflows['w1'].nodes == [{'t': 'start'}]
    assert b._identity is not None
    assert b._identity.agent_id == 'ag1'
    assert b._identity.bio == 'local mind'
    assert b._identity.personality == {'tone': 'warm'}


def test_missing_config_file_is_safe(tmp_path, monkeypatch):
    from integrations.channels.admin.api import AdminAPI
    monkeypatch.setattr(AdminAPI, '_config_path',
                        lambda self: str(tmp_path / 'nope.json'))
    a = AdminAPI()  # no file → no crash, empty state
    assert a._workflows == {} and a._identity is None


def test_config_lives_in_the_user_data_dir(tmp_path, monkeypatch):
    """The config used to be written next to the package, which in the
    installed app is C:\Program Files (x86)\...\site-packages\agent_data:
    not the user's to write.  It belongs in the user data dir."""
    from core import platform_paths
    from integrations.channels.admin.api import AdminAPI
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(platform_paths, '_cached_data_dir', None)

    path = AdminAPI()._config_path()
    assert path == os.path.join(platform_paths.get_agent_data_dir(),
                                'admin_config.json')
    assert os.path.abspath(path).startswith(str(tmp_path))


def test_a_refused_save_gives_up_at_once(tmp_path, monkeypatch):
    """Live 2026-09-25: a consent Allow ran _save_config, tempfile.mkstemp got
    PermissionError on every try and, on Windows, retries up to 2**31 times
    while os.access says the dir is writable -- 597,941 tries in, the request
    was still spinning and held the SQLite write lock, so every later write in
    the app failed with "database is locked".  A save that is refused must
    fail once, log, and return."""
    import builtins
    from integrations.channels.admin.api import AdminAPI
    monkeypatch.setattr(AdminAPI, '_config_path',
                        lambda self: str(tmp_path / 'admin_config.json'))
    api = AdminAPI()
    attempts = []
    real_open = builtins.open

    def refusing_open(file, mode='r', *a, **k):
        if 'w' in mode and str(tmp_path) in str(file):
            attempts.append(file)
            raise PermissionError(13, 'Access is denied', str(file))
        return real_open(file, mode, *a, **k)

    def refusing_os_open(file, flags, *a, **k):
        attempts.append(file)
        raise PermissionError(13, 'Access is denied', str(file))

    monkeypatch.setattr(builtins, 'open', refusing_open)
    monkeypatch.setattr(os, 'open', refusing_os_open)
    api._save_config()  # must return, not spin
    assert 1 <= len(attempts) <= 2
