"""#44 — MCP token rotation must not leave stale per-worker caches.

The bug: _ensure_mcp_token cached the first read forever, so after a rotation
(new token written to disk by one Flask worker) OTHER workers kept honouring the
old token until restart.  Fix: mtime-keyed cache — re-read whenever the token
file changes, so a rotation in ANY worker is seen by EVERY worker on its next
call.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _bridge(monkeypatch, tmp_path):
    try:
        import integrations.mcp.mcp_http_bridge as B
    except Exception as e:
        pytest.skip(f"mcp bridge unavailable: {e}")
    tok = tmp_path / 'mcp.token'
    monkeypatch.setattr(B, '_mcp_token_path', lambda: str(tok))
    monkeypatch.delenv('HARTOS_MCP_TOKEN', raising=False)
    monkeypatch.delenv('HARTOS_MCP_TOKEN_FILE', raising=False)
    B._MCP_TOKEN_CACHE = None
    B._MCP_TOKEN_CACHE_KEY = None
    return B, tok


def test_reread_on_file_change(monkeypatch, tmp_path):
    B, tok = _bridge(monkeypatch, tmp_path)
    tok.write_text('TOKEN_ONE', encoding='utf-8')
    os.utime(str(tok), (1000, 1000))
    assert B._ensure_mcp_token() == 'TOKEN_ONE'
    # Another worker rotates the file → new content + new mtime.
    tok.write_text('TOKEN_TWO', encoding='utf-8')
    os.utime(str(tok), (2000, 2000))
    assert B._ensure_mcp_token() == 'TOKEN_TWO', "must re-read, not serve the stale cache"


def test_rotation_invalidates_stale_worker_cache(monkeypatch, tmp_path):
    B, tok = _bridge(monkeypatch, tmp_path)
    t1 = B._ensure_mcp_token()           # creates the token file
    new = B.rotate_mcp_token()           # "worker A" rotates
    assert new and new != t1
    # "worker B" still holds a stale cache (old token + a stale mtime key):
    B._MCP_TOKEN_CACHE = t1
    B._MCP_TOKEN_CACHE_KEY = (str(tok), 1.0)
    assert B._ensure_mcp_token() == new, "stale worker must pick up the rotated token"


def test_env_var_token_is_static(monkeypatch, tmp_path):
    B, _ = _bridge(monkeypatch, tmp_path)
    monkeypatch.setenv('HARTOS_MCP_TOKEN', 'ENV_TOKEN')
    assert B._ensure_mcp_token() == 'ENV_TOKEN'
