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
