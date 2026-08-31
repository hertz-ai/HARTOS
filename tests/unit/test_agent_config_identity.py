"""Casual/fast-path agent identity: load the active agent's config (#81).

The casual + fast chat paths build the agent's identity via
agent_identity.build_identity_prompt(agent_config, owner_name, user_details).
agent_config used to come from thread_local_data.get_agent_config() — a method
that never existed (hasattr-guarded → always None) — so the agent's persona was
silently dropped from casual replies (the full-agent path injects it via a
different, working mechanism, build_personality_prompt).

Now it's loaded from prompts/{prompt_id}.json via the mtime-cached, self-
refreshing core.cache_loaders.load_agent_config ("cache on set": a write bumps
the file mtime, so the next read reflects the edit — no stale persona).

Behavioural: real files + real cache + real prompt builder. No grep tests.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── load_agent_config: read + mtime cache + cache-on-set refresh ─────────

def test_load_agent_config_reads_and_cache_hits(tmp_path, monkeypatch):
    from core import cache_loaders
    monkeypatch.setattr(cache_loaders, 'PROMPTS_DIR', str(tmp_path))
    cache_loaders._agent_config_cache.clear()
    (tmp_path / '42.json').write_text(
        json.dumps({'name': 'Vijai', 'personality': {'tone': 'warm'}}))

    cfg = cache_loaders.load_agent_config(42)
    assert cfg['name'] == 'Vijai'
    # Unchanged file → cache HIT returns the very same object (no re-read);
    # also proves int/str prompt_id normalize to one key.
    assert cache_loaders.load_agent_config('42') is cfg


def test_load_agent_config_refreshes_when_file_is_written(tmp_path, monkeypatch):
    """'Cache on set': editing the config (bumping mtime) makes the next read
    reflect the new persona — a stale persona is never served after an edit."""
    from core import cache_loaders
    monkeypatch.setattr(cache_loaders, 'PROMPTS_DIR', str(tmp_path))
    cache_loaders._agent_config_cache.clear()
    p = tmp_path / '7.json'
    p.write_text(json.dumps({'name': 'Before'}))
    assert cache_loaders.load_agent_config(7)['name'] == 'Before'

    p.write_text(json.dumps({'name': 'After'}))
    st = p.stat()
    os.utime(p, (st.st_atime + 100, st.st_mtime + 100))  # deterministic mtime bump
    assert cache_loaders.load_agent_config(7)['name'] == 'After'


def test_load_agent_config_none_for_absent_unsafe_or_nondict(tmp_path, monkeypatch):
    from core import cache_loaders
    monkeypatch.setattr(cache_loaders, 'PROMPTS_DIR', str(tmp_path))
    cache_loaders._agent_config_cache.clear()
    assert cache_loaders.load_agent_config(None) is None
    assert cache_loaders.load_agent_config(999) is None          # no file
    assert cache_loaders.load_agent_config('../secrets') is None  # path traversal
    (tmp_path / '5.json').write_text('[1, 2, 3]')                 # not a dict
    assert cache_loaders.load_agent_config(5) is None


def test_load_agent_config_drops_cache_when_file_removed(tmp_path, monkeypatch):
    from core import cache_loaders
    monkeypatch.setattr(cache_loaders, 'PROMPTS_DIR', str(tmp_path))
    cache_loaders._agent_config_cache.clear()
    p = tmp_path / '9.json'
    p.write_text(json.dumps({'name': 'Gone'}))
    assert cache_loaders.load_agent_config(9)['name'] == 'Gone'
    p.unlink()
    assert cache_loaders.load_agent_config(9) is None
    assert '9' not in cache_loaders._agent_config_cache


# ── build_identity_prompt: persona injection + non-dict hardening ────────

def test_identity_includes_persona_and_owner_from_dict_config():
    from hartos.agent_identity import build_identity_prompt
    out = build_identity_prompt(
        {'name': 'Vijai', 'goal': 'help you ship',
         'personality': {'primary_traits': ['kind', 'precise'], 'tone': 'warm'}},
        owner_name='Sam', user_details='')
    assert 'Vijai' in out and 'kind' in out and 'warm' in out
    assert 'Sam' in out  # owner-awareness layer


def test_identity_tolerates_string_personality_no_crash():
    """Regression guard: some configs store `personality` as a plain string;
    this branch now runs for real (fed by load_agent_config) and must not raise
    on the chat hot path. Falls back to name-only."""
    from hartos.agent_identity import build_identity_prompt
    out = build_identity_prompt({'name': 'Vijai', 'personality': 'vijai'},
                                owner_name='', user_details='')
    assert 'Vijai' in out  # no AttributeError; name still surfaces


def test_identity_generic_when_no_config():
    from hartos.agent_identity import build_identity_prompt
    out = build_identity_prompt(None, owner_name='', user_details='')
    assert 'Hevolve' in out  # generic platform identity, no persona layer


# ── extract_owner_name: single regex source (chat path + channel announce) ──

def test_extract_owner_name():
    from hartos.agent_identity import extract_owner_name
    assert extract_owner_name('name: Alice\nrole: admin') == 'Alice'
    assert extract_owner_name('Name: Bob, age 30') == 'Bob'
    assert extract_owner_name('') == ''
    assert extract_owner_name(None) == ''
