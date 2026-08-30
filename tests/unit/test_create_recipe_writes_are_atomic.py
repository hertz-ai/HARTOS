"""Every recipe-state write in create_recipe.py must be crash-safe.

A recipe file written with a bare ``open(path, 'w')`` + ``json.dump`` is corrupted
if the process dies mid-write — and recipes are persisted state read back on the
next turn, so a half-written file is a hard failure. The canonical
``core.file_cache.atomic_json_write`` (tmp → fsync → ``os.replace`` → tmp cleanup)
is the ONE crash-safe writer.

Two complementary guards (a behavioural test AND a source guard — the source
guard is NOT the only test, per CLAUDE.md Gate 5 / feedback_no_grep_tests):

  1. BEHAVIOURAL: the writer actually leaves the original intact and leaves no
     ``.tmp`` behind when the rename fails — the real crash the fix prevents.
  2. SOURCE: no bare ``json.dump`` call survives in create_recipe.py, so every
     recipe write is routed through the atomic writer, not just one that happened
     to be scanned.
"""
import ast
import json
import os
from pathlib import Path

import pytest

from core.file_cache import atomic_json_write

_SRC_PATH = Path(__file__).resolve().parents[2] / 'hartos/create_recipe.py'


# ── 1. Behavioural: the atomicity this file is named for ─────────────────────

def test_original_survives_and_no_tmp_leaks_when_rename_fails(tmp_path, monkeypatch):
    """If ``os.replace`` fails after the temp write (the crash window), the
    existing file must be untouched and the temp file must be cleaned up — the
    exact leak the hand-rolled tmp+rename this fix replaced used to have."""
    target = tmp_path / 'recipe.json'
    target.write_text('{"original": true}')

    def boom(*_a, **_k):
        raise RuntimeError('rename failed mid-write')

    monkeypatch.setattr(os, 'replace', boom)

    with pytest.raises(RuntimeError):
        atomic_json_write(str(target), {'new': True, 'clobbered': 'no'})

    # original recipe intact — never half-written, never clobbered
    assert json.loads(target.read_text()) == {'original': True}
    # and no stray .tmp left behind
    assert not list(tmp_path.glob('*.tmp')), 'atomic writer leaked a temp file'


def test_happy_path_writes_readable_json(tmp_path):
    target = tmp_path / 'sub' / 'recipe.json'  # also proves makedirs
    atomic_json_write(str(target), {'a': 1, 'b': [2, 3]}, indent=4)
    assert json.loads(target.read_text()) == {'a': 1, 'b': [2, 3]}


# ── 2. Source guard: no raw write can creep back in ──────────────────────────

def test_no_raw_json_dump_remains_in_create_recipe():
    tree = ast.parse(_SRC_PATH.read_text(encoding='utf-8'))
    offenders = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == 'dump'
        and isinstance(node.func.value, ast.Name) and node.func.value.id == 'json'
    ]
    assert not offenders, (
        f'raw json.dump(...) in create_recipe.py at lines {offenders} — recipe '
        f'writes must route through core.file_cache.atomic_json_write')
