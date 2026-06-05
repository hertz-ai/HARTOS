"""Regression tests for the in-process armored-hevolveai loader fix (task #67).

Before the fix, native_hive_loader called ``hevolvearmor.install(modules_dir,
bytes(key))`` — but that Rust API's 2nd positional arg is a *passphrase string*,
so passing a raw 32-byte key raised TypeError, which was caught and swallowed:
in-process armor NEVER actually installed and silently fell back to the plain
.pyd.

The fix routes through the RAW-KEY loader
``hevolvearmor._loader.install_loader(dir, raw_key)`` (the same entry the
supervisor subprocess + producer round-trip use), and adds a plain-package
fallback if a bad/stale .enc fails to import.

These mock the hevolvearmor boundary (no native ext / bundle needed) and assert
the real function's observable behaviour (call args + module state).
"""
from __future__ import annotations

import os
import sys
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import security.native_hive_loader as nhl  # noqa: E402


def _reset():
    nhl._armor_install_tried = False
    nhl._armor_install_ok = False
    nhl._armor_finder = None


def _fake_hevolvearmor(monkeypatch, install_calls, uninstall_calls):
    """Inject a hevolvearmor with ONLY the raw-key _loader API — deliberately no
    .install attribute, so a regression back to the passphrase API would fail."""
    arm = types.ModuleType('hevolvearmor')
    loader = types.ModuleType('hevolvearmor._loader')

    def install_loader(d, k, package_names=None):
        install_calls.append((d, bytes(k), package_names))
        return ('FINDER', d)

    def uninstall_loader(f=None):
        uninstall_calls.append(f)

    loader.install_loader = install_loader
    loader.uninstall_loader = uninstall_loader
    arm._loader = loader
    monkeypatch.setitem(sys.modules, 'hevolvearmor', arm)
    monkeypatch.setitem(sys.modules, 'hevolvearmor._loader', loader)


def test_install_uses_raw_key_loader_not_passphrase_install(monkeypatch, tmp_path):
    """The fix: install_loader(dir, raw_key) — NEVER hevolvearmor.install (which
    is passphrase-based and would TypeError on a raw key)."""
    _reset()
    mods = tmp_path / 'modules'
    (mods / 'hevolveai').mkdir(parents=True)
    (mods / 'hevolveai' / '__init__.enc').write_bytes(b'\x00')
    keyf = tmp_path / '_key.bin'
    keyf.write_bytes(b'K' * 32)
    monkeypatch.setenv('HEVOLVE_ARMORED_DIR', str(mods))
    monkeypatch.setenv('HEVOLVE_ARMOR_KEY_FILE', str(keyf))
    monkeypatch.delenv('HEVOLVE_ARMOR_PASSPHRASE', raising=False)

    install_calls, uninstall_calls = [], []
    _fake_hevolvearmor(monkeypatch, install_calls, uninstall_calls)
    # a hevolvearmor WITHOUT .install proves we never call the passphrase API
    assert not hasattr(sys.modules['hevolvearmor'], 'install')

    ok, msg = nhl._try_install_hevolvearmor()
    assert ok, msg
    assert len(install_calls) == 1, install_calls
    called_dir, called_key, _names = install_calls[0]
    assert called_dir == str(mods)
    assert called_key == b'K' * 32  # raw key handed straight to the raw-key loader
    assert nhl._armor_finder == ('FINDER', str(mods))


def test_uninstall_armor_removes_hook(monkeypatch):
    """The fallback safety net drops the installed finder so a bad .enc can't
    wedge imports onto the broken hook."""
    _reset()
    install_calls, uninstall_calls = [], []
    _fake_hevolvearmor(monkeypatch, install_calls, uninstall_calls)
    nhl._armor_finder = 'SENTINEL_FINDER'
    nhl._armor_install_ok = True
    nhl._uninstall_armor()
    assert uninstall_calls == ['SENTINEL_FINDER']
    assert nhl._armor_finder is None
    assert nhl._armor_install_ok is False


def test_no_bundle_is_noop(monkeypatch, tmp_path):
    """No HEVOLVE_ARMORED_DIR + no bundle under HART_ROOT -> (False, ...) and
    nothing installed; in-process stays on the plain package (dev unchanged)."""
    _reset()
    monkeypatch.delenv('HEVOLVE_ARMORED_DIR', raising=False)
    # point HART_ROOT at an empty tmp dir so the default candidate paths miss
    monkeypatch.setattr(nhl, '_HART_ROOT', str(tmp_path))
    install_calls, uninstall_calls = [], []
    _fake_hevolvearmor(monkeypatch, install_calls, uninstall_calls)
    ok, msg = nhl._try_install_hevolvearmor()
    assert ok is False
    assert install_calls == []
