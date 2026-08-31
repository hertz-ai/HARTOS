"""Tests for helper.py agent data backup + info helpers.

Covers two dead-code wirings:

1. backup_agent_data_file now auto-prunes via cleanup_old_backups so
   backups don't accumulate forever. Verify the prune runs and keeps
   only keep_count most-recent files.

2. get_agent_data_info returns a diagnostic dict for admin UI — exists
   flag, size_bytes, modified_at, saved_at, data_keys. The new admin
   endpoint /api/admin/agent-data/<id>/info wraps it.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from flask import Flask


@pytest.fixture
def tmp_agent_data_dir(tmp_path, monkeypatch):
    """Redirect helper's AGENT_DATA_DIR to an isolated tmp dir for every test.

    helper.py uses `current_app.logger` everywhere, which is a Flask
    LocalProxy that needs an application context to resolve. Push a
    minimal Flask app context around every test so those logger calls
    don't blow up with 'Working outside of application context'.
    """
    from hartos import helper
    monkeypatch.setattr(helper, 'AGENT_DATA_DIR', str(tmp_path))
    app = Flask('test_helper_agent_data')
    with app.app_context():
        yield tmp_path


class TestCleanupOldBackups:
    """cleanup_old_backups keeps only the N newest backup files per prompt_id."""

    def _make_backup(self, tmp_path, prompt_id, age_seconds):
        """Create a fake backup file with a modtime age_seconds in the past."""
        fname = f'{prompt_id}_agent_data_backup_{age_seconds}.json'
        fpath = os.path.join(tmp_path, fname)
        with open(fpath, 'w') as f:
            f.write('{}')
        stamp = (datetime.now() - timedelta(seconds=age_seconds)).timestamp()
        os.utime(fpath, (stamp, stamp))
        return fpath

    def test_keeps_newest_five_by_default(self, tmp_agent_data_dir):
        from hartos.helper import cleanup_old_backups
        for age in range(8):
            self._make_backup(tmp_agent_data_dir, prompt_id=42, age_seconds=age)
        deleted = cleanup_old_backups(42, keep_count=5)
        assert deleted == 3
        remaining = [
            f for f in os.listdir(tmp_agent_data_dir)
            if '42_agent_data_backup_' in f
        ]
        assert len(remaining) == 5

    def test_respects_custom_keep_count(self, tmp_agent_data_dir):
        from hartos.helper import cleanup_old_backups
        for age in range(10):
            self._make_backup(tmp_agent_data_dir, prompt_id=99, age_seconds=age)
        deleted = cleanup_old_backups(99, keep_count=2)
        assert deleted == 8
        remaining = [
            f for f in os.listdir(tmp_agent_data_dir)
            if '99_agent_data_backup_' in f
        ]
        assert len(remaining) == 2

    def test_per_prompt_isolation(self, tmp_agent_data_dir):
        """Cleanup for prompt 1 must NOT touch prompt 2's backups."""
        from hartos.helper import cleanup_old_backups
        for age in range(6):
            self._make_backup(tmp_agent_data_dir, prompt_id=1, age_seconds=age)
        for age in range(4):
            self._make_backup(tmp_agent_data_dir, prompt_id=2, age_seconds=age)
        cleanup_old_backups(1, keep_count=2)
        p1 = [f for f in os.listdir(tmp_agent_data_dir) if '1_agent_data_backup_' in f]
        p2 = [f for f in os.listdir(tmp_agent_data_dir) if '2_agent_data_backup_' in f]
        assert len(p1) == 2  # pruned
        assert len(p2) == 4  # untouched

    def test_zero_backups_returns_zero(self, tmp_agent_data_dir):
        """No files to prune is not an error — return 0."""
        from hartos.helper import cleanup_old_backups
        assert cleanup_old_backups(123, keep_count=5) == 0


class TestBackupAutoPrune:
    """backup_agent_data_file now calls cleanup_old_backups so the caller
    doesn't have to remember the two-step dance."""

    def test_backup_triggers_cleanup(self, tmp_agent_data_dir):
        from hartos.helper import backup_agent_data_file, get_agent_data_file_path

        # Create the primary agent data file that backup will copy
        primary = get_agent_data_file_path(7)
        with open(primary, 'w') as f:
            json.dump({'data': {'foo': 'bar'}, 'saved_at': 'test'}, f)

        with patch('hartos.helper.cleanup_old_backups') as mock_prune:
            result = backup_agent_data_file(7, keep_count=3)

        assert result is True
        mock_prune.assert_called_once_with(7, keep_count=3)


class TestGetAgentDataInfo:
    """get_agent_data_info returns a diagnostic dict for the admin UI."""

    def test_missing_file_returns_exists_false(self, tmp_agent_data_dir):
        from hartos.helper import get_agent_data_info
        info = get_agent_data_info(999)
        assert info['exists'] is False
        assert 'path' in info

    def test_existing_file_returns_full_diagnostic(self, tmp_agent_data_dir):
        from hartos.helper import get_agent_data_info, get_agent_data_file_path
        path = get_agent_data_file_path(77)
        payload = {
            'saved_at': '2026-04-10T18:00:00',
            'data': {'history': [], 'persona': {}, 'tools': []},
        }
        with open(path, 'w') as f:
            json.dump(payload, f)

        info = get_agent_data_info(77)

        assert info['exists'] is True
        assert info['path'] == path
        assert info['size_bytes'] > 0
        assert info['saved_at'] == '2026-04-10T18:00:00'
        assert set(info['data_keys']) == {'history', 'persona', 'tools'}
        assert 'modified_at' in info


class TestAdminEndpointAuthGate:
    """Every /api/admin/* route MUST require authentication on every
    tier except bundled desktop — admin ops modify persistent state so
    LAN trust is NOT sufficient. Two distinct regressions have to be
    blocked here:

      (a) The ADMIN_PATHS tuple must contain '/api/admin' so new
          admin routes inherit the gate automatically (the 'forgot
          to decorate' failure mode).

      (b) The regional-tier enforcement branch must actually fire.
          The previous iteration of this test only checked tuple
          membership in the source, so a middleware change that
          *had* the tuple but bypassed regional (the exact bug the
          reviewer caught) still passed. This version uses a real
          Flask test client to hit the route on each tier and
          assert the response status.
    """

    def test_admin_path_in_ADMIN_PATHS_tuple(self):
        """Regression guard (cheap, source-level): '/api/admin' must
        stay in security.middleware.ADMIN_PATHS so any new route with
        that prefix inherits the auth gate without the author having
        to remember a decorator."""
        from security import middleware
        assert '/api/admin' in middleware.ADMIN_PATHS, (
            "'/api/admin' was removed from ADMIN_PATHS — new admin "
            "routes will ship unguarded. See tests/unit/test_helper_"
            "agent_data.py::TestAdminEndpointAuthGate."
        )

    @pytest.mark.parametrize('tier', ['central', 'regional', 'flat'])
    def test_admin_route_requires_auth_on_every_tier(self, tier, monkeypatch):
        """Behavior-level guard: whether the tier is central, regional,
        or flat, hitting /api/admin/agent-data/<id>/info without a
        Bearer token must return 401. Bundled/desktop mode is a
        separate path that stays trusted."""
        from flask import Flask
        from security.middleware import _apply_api_auth
        monkeypatch.setenv('HEVOLVE_NODE_TIER', tier)
        monkeypatch.delenv('HEVOLVE_API_KEY', raising=False)
        monkeypatch.delenv('NUNBA_BUNDLED', raising=False)

        app = Flask('test_admin_auth')
        _apply_api_auth(app)

        @app.route('/api/admin/ping')
        def _admin_ping():
            return {'ok': True}

        client = app.test_client()
        unauth = client.get('/api/admin/ping')
        assert unauth.status_code == 401, (
            f"Tier={tier}: /api/admin/ping returned "
            f"{unauth.status_code} without Bearer token — admin routes "
            f"MUST be auth-gated on every tier, not just central."
        )
        # A valid JWT-shaped Bearer token should get past the middleware.
        # The middleware now does JWT decoding — "Bearer garbage" is rejected.
        # Use a mock-valid JWT shape with patched decode.
        from unittest.mock import patch
        with patch('integrations.social.auth.decode_jwt',
                   return_value={'user_id': 'test', 'tier': tier}):
            ok = client.get('/api/admin/ping',
                            headers={'Authorization': 'Bearer valid.jwt.shape'})
        assert ok.status_code == 200, (
            f"Tier={tier}: Valid JWT was rejected by the gate "
            f"({ok.status_code})."
        )

    def test_bundled_desktop_mode_skips_gate(self, monkeypatch):
        """Nunba desktop (NUNBA_BUNDLED=1) runs an in-process Flask
        test client with no network exposure — the gate must NOT fire
        or every desktop chat breaks."""
        from flask import Flask
        from security.middleware import _apply_api_auth
        monkeypatch.setenv('NUNBA_BUNDLED', '1')
        monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')

        app = Flask('test_bundled_mode')
        _apply_api_auth(app)

        @app.route('/api/admin/ping')
        def _admin_ping():
            return {'ok': True}

        client = app.test_client()
        response = client.get('/api/admin/ping')
        assert response.status_code == 200

    def test_regional_tier_user_facing_paths_stay_open(self, monkeypatch):
        """Regional tier's USER-FACING paths (/chat, /prompts) must
        stay open so LAN-only deployments keep working. Only admin
        paths are unconditionally gated — this lock stops a future
        'protect everything on regional' change that would break
        every small-office deployment."""
        from flask import Flask
        from security.middleware import _apply_api_auth
        monkeypatch.setenv('HEVOLVE_NODE_TIER', 'regional')
        monkeypatch.delenv('HEVOLVE_API_KEY', raising=False)
        monkeypatch.delenv('NUNBA_BUNDLED', raising=False)

        app = Flask('test_regional_user_paths')
        _apply_api_auth(app)

        @app.route('/chat', methods=['POST'])
        def _chat():
            return {'ok': True}

        @app.route('/prompts/mine')
        def _prompts():
            return {'ok': True}

        client = app.test_client()
        # Both user-facing routes must be reachable without auth on
        # regional tier (LAN trust model).
        assert client.post('/chat').status_code == 200
        assert client.get('/prompts/mine').status_code == 200
