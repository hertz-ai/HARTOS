"""Phase 1 of the canonical hevolveai-armor plan (2026-06-01):
ensure_hevolveai_armored() — a flag-gated, default-OFF canonical entry that
reuses the existing CLI as the single produce path.

ZERO-REGRESSION pins:
  * default (flag unset) → no-op, NO subprocess spawned → nothing changes for
    any existing build until explicitly opted in.
  * flag on → invokes THIS module's CLI (the one produce path), reports ok.
  * force=True → runs regardless of flag.
  * never raises into the caller.
See docs/architecture/HEVOLVEAI_ARMOR_CANONICAL_PLAN.md.
"""
from __future__ import annotations

import os
import sys
import importlib.util
from unittest.mock import patch, MagicMock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_ARMOR = os.path.join(ROOT, 'scripts', 'armor_hevolveai.py')


def _load():
    spec = importlib.util.spec_from_file_location('armor_hevolveai', _ARMOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_default_off_is_noop(monkeypatch):
    monkeypatch.delenv('HEVOLVE_HEVOLVEAI_ARMORED', raising=False)
    mod = _load()
    with patch('subprocess.run') as mk:
        out = mod.ensure_hevolveai_armored()
    assert out['enabled'] is False
    assert not mk.called, "default-off MUST NOT spawn the producer (zero-regression)"


def test_flag_on_invokes_cli_once(monkeypatch):
    monkeypatch.setenv('HEVOLVE_HEVOLVEAI_ARMORED', '1')
    mod = _load()
    fake = MagicMock(returncode=0, stderr='', stdout='')
    with patch('subprocess.run', return_value=fake) as mk:
        out = mod.ensure_hevolveai_armored()
    assert out == {'enabled': True, 'ok': True}
    assert mk.called
    argv = mk.call_args[0][0]
    assert argv[0] == sys.executable and argv[1].endswith('armor_hevolveai.py'), argv
    assert '-q' in argv  # reuses the CLI quietly


def test_force_runs_even_when_flag_off(monkeypatch):
    monkeypatch.delenv('HEVOLVE_HEVOLVEAI_ARMORED', raising=False)
    mod = _load()
    with patch('subprocess.run', return_value=MagicMock(returncode=0, stderr='', stdout='')) as mk:
        out = mod.ensure_hevolveai_armored(force=True)
    assert out['ok'] is True and mk.called


def test_producer_failure_reported_not_raised(monkeypatch):
    monkeypatch.setenv('HEVOLVE_HEVOLVEAI_ARMORED', 'true')
    mod = _load()
    with patch('subprocess.run', return_value=MagicMock(returncode=1, stderr='boom', stdout='')):
        out = mod.ensure_hevolveai_armored()
    assert out['enabled'] is True and out['ok'] is False and 'boom' in out['error']


def test_truthy_values(monkeypatch):
    mod = _load()
    for v in ('1', 'true', 'TRUE', 'yes', 'on'):
        monkeypatch.setenv('HEVOLVE_HEVOLVEAI_ARMORED', v)
        with patch('subprocess.run', return_value=MagicMock(returncode=0, stderr='', stdout='')):
            assert mod.ensure_hevolveai_armored()['enabled'] is True, v
    for v in ('', '0', 'false', 'no', 'off'):
        monkeypatch.setenv('HEVOLVE_HEVOLVEAI_ARMORED', v)
        with patch('subprocess.run') as mk:
            assert mod.ensure_hevolveai_armored()['enabled'] is False, v
            assert not mk.called
