"""Phase 2 of HEVOLVEAI_ARMOR_CANONICAL_PLAN.md: supervisor armored-spawn,
flag-gated, with a BYTE-IDENTICAL plain fallback.

ZERO-REGRESSION guard (the load-bearing test): with the flag off — or on but no
armored bundle staged — _build_cmd MUST return the exact pre-Phase-2 plain boot
command, character-for-character.  The armored branch only activates under
HEVOLVE_HEVOLVEAI_ARMORED + a present bundle (unverified until Phase 3 — that's
fine, it's opt-in).
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.agent_engine import hevolveai_supervisor as hs  # noqa: E402

# The exact plain boot that shipped before Phase 2 — the fallback must equal this.
PLAIN_BOOT = (
    "import os, uvicorn;"
    "from hevolveai.server.api_server import app;"
    "uvicorn.run(app, host='0.0.0.0',"
    " port=int(os.environ.get('HEVOLVEAI_PORT', '8000')),"
    " log_level='info')"
)


def _sup():
    sup = hs._Supervisor.__new__(hs._Supervisor)
    sup.python_exe = 'python'
    return sup


def test_flag_off_is_byte_identical_plain(monkeypatch):
    monkeypatch.delenv('HEVOLVE_HEVOLVEAI_ARMORED', raising=False)
    sup = _sup()
    cmd = sup._build_cmd()
    assert cmd == ['python', '-c', PLAIN_BOOT], (
        "flag-off MUST be the exact pre-Phase-2 plain boot (zero-regression)")


def test_flag_on_but_no_bundle_falls_back_plain(monkeypatch):
    monkeypatch.setenv('HEVOLVE_HEVOLVEAI_ARMORED', '1')
    sup = _sup()
    # No bundle present → _armored_bundle_dir returns None → plain fallback.
    monkeypatch.setattr(sup, '_armored_bundle_dir', lambda: None)
    assert sup._build_cmd() == ['python', '-c', PLAIN_BOOT], (
        "flag-on but no staged bundle MUST fall back to the exact plain boot")


def test_armored_boot_when_flag_on_and_bundle_present(monkeypatch):
    monkeypatch.setenv('HEVOLVE_HEVOLVEAI_ARMORED', '1')
    sup = _sup()
    monkeypatch.setattr(sup, '_armored_bundle_dir',
                        lambda: os.path.join('X:', 'app', 'vendor', 'hevolveai_armored'))
    cmd = sup._build_cmd()
    assert cmd[0] == 'python' and cmd[1] == '-c'
    boot = cmd[2]
    # Installs the armored loader BEFORE importing api_server, same uvicorn run.
    assert 'from hevolvearmor._loader import install_loader' in boot
    assert 'install_loader(' in boot
    assert 'modules' in boot and '_key.bin' in boot
    # api_server import + uvicorn boot are preserved after the loader install.
    assert boot.index('install_loader(') < boot.index('from hevolveai.server.api_server import app')
    assert "uvicorn.run(app, host='0.0.0.0'" in boot


def test_armored_dir_gate_flag_off_returns_none(monkeypatch):
    monkeypatch.delenv('HEVOLVE_HEVOLVEAI_ARMORED', raising=False)
    assert _sup()._armored_bundle_dir() is None


def test_armored_dir_resolves_when_present(monkeypatch):
    monkeypatch.setenv('HEVOLVE_HEVOLVEAI_ARMORED', 'true')
    monkeypatch.setenv('HEVOLVE_HEVOLVEAI_ARMORED_DIR', os.path.join('Y:', 'bundle'))
    sup = _sup()
    # Simulate the override dir having modules/ + _key.bin.
    monkeypatch.setattr(os.path, 'isdir', lambda p: p.endswith('modules'))
    monkeypatch.setattr(os.path, 'isfile', lambda p: p.endswith('_key.bin'))
    got = sup._armored_bundle_dir()
    assert got == os.path.join('Y:', 'bundle')


def test_armored_dir_none_when_bundle_absent(monkeypatch):
    monkeypatch.setenv('HEVOLVE_HEVOLVEAI_ARMORED', '1')
    monkeypatch.delenv('HEVOLVE_HEVOLVEAI_ARMORED_DIR', raising=False)
    sup = _sup()
    monkeypatch.setattr(os.path, 'isdir', lambda p: False)
    monkeypatch.setattr(os.path, 'isfile', lambda p: False)
    assert sup._armored_bundle_dir() is None
