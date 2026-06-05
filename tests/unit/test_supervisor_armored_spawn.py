"""Reconciled canonical armor path (2026-06-01) — supersedes the flag-gated
Phase-2 design.

The hevolveai SERVER subprocess installs the Hevolvearmor import hook via the
SAME mechanism the in-process loader (security/native_hive_loader) uses — the
HEVOLVE_ARMORED_DIR / HEVOLVE_ARMOR_KEY_FILE env vars that app.py exports + the
canonical ``hevolvearmor.install`` API — with NO separate flag and NO second
loader entry.  There is ONE armor path, two call sites (in-process + this
subprocess), not a parallel build path.

These tests pin:
  * ONE boot path (no flag fork); the armor snippet runs BEFORE the api_server
    import.
  * the SAME env-var contract as the in-process loader (no drift).
  * the dropped parallel path is GONE (no HEVOLVE_HEVOLVEAI_ARMORED flag, no
    _loader.install_loader in the boot).
  * BEHAVIOUR: with a real armored bundle + those env vars, the snippet installs
    the hook and an armored module imports (42); with no bundle it is a silent
    no-op so the dev boot is unchanged.
"""
from __future__ import annotations

import os
import sys
import shutil
import tempfile
import subprocess

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.agent_engine import hevolveai_supervisor as hs  # noqa: E402


def _sup():
    sup = hs._Supervisor.__new__(hs._Supervisor)
    sup.python_exe = sys.executable
    return sup


def test_single_boot_path_snippet_before_import():
    cmd = _sup()._build_cmd()
    assert cmd[0] == sys.executable and cmd[1] == '-c'
    boot = cmd[2]
    # armor-install runs BEFORE the api_server import (so .enc shadows any .pyd)
    assert 'install_loader(' in boot
    assert 'from hevolveai.server.api_server import app' in boot
    assert (boot.index('install_loader(')
            < boot.index('from hevolveai.server.api_server import app'))
    assert "uvicorn.run(app, host='0.0.0.0'" in boot
    # one launch path, never -m (breaks the .pyd bundle)
    assert '-m' not in cmd


def test_boot_uses_canonical_env_contract_not_parallel_path():
    """The subprocess uses the SAME env vars + install API as
    security/native_hive_loader, and NONE of the dropped parallel path."""
    boot = _sup()._build_cmd()[2]
    # canonical env vars (shared with native_hive_loader + exported by app.py)
    assert 'HEVOLVE_ARMORED_DIR' in boot
    assert 'HEVOLVE_ARMOR_KEY_FILE' in boot
    # raw-key loader (matches the producer's random _key.bin)
    assert 'install_loader(' in boot
    # the dropped parallel path (per-supervisor on/off flag) must NOT reappear
    assert 'HEVOLVE_HEVOLVEAI_ARMORED' not in boot


def test_flag_no_longer_consulted(monkeypatch):
    """The old on/off flag is gone — toggling it does not change the boot."""
    monkeypatch.delenv('HEVOLVE_HEVOLVEAI_ARMORED', raising=False)
    off = _sup()._build_cmd()
    monkeypatch.setenv('HEVOLVE_HEVOLVEAI_ARMORED', '1')
    on = _sup()._build_cmd()
    assert off == on, "armor boot must be flag-independent (single path)"


def test_source_guard_native_hive_loader_shares_env_vars():
    """Source guard (DRY enforcement across files): the in-process loader and
    the subprocess snippet reference the SAME env-var names, so the single
    mechanism cannot silently fork into two.  Behavioural coverage is below;
    this only pins the shared contract."""
    nhl = os.path.join(ROOT, 'security', 'native_hive_loader.py')
    with open(nhl, 'r', encoding='utf-8') as f:
        src = f.read()
    for var in ('HEVOLVE_ARMORED_DIR', 'HEVOLVE_ARMOR_KEY_FILE'):
        assert var in src, f"{var} missing from native_hive_loader"
        assert var in hs._ARMOR_INSTALL_SNIPPET, f"{var} missing from snippet"


# -- behavioural: the snippet really installs the hook against a real bundle --

def _armor_available():
    try:
        if os.path.join(ROOT, 'hevolvearmor') not in sys.path:
            sys.path.insert(0, os.path.join(ROOT, 'hevolvearmor'))
        import hevolvearmor  # noqa: F401
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        return True
    except Exception:
        return False


def _load_producer():
    import importlib.util as ilu
    spec = ilu.spec_from_file_location(
        'armor_hevolveai', os.path.join(ROOT, 'scripts', 'armor_hevolveai.py'))
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(not _armor_available(),
                    reason="hevolvearmor native / cryptography not available")
def test_snippet_installs_hook_and_imports_real_bundle():
    """Drive the EXACT supervisor snippet against a producer-made bundle: the
    canonical hevolvearmor.install hook decrypts + imports an armored module."""
    ah = _load_producer()
    tmp = tempfile.mkdtemp(prefix='armor_snip_')
    try:
        pkg = os.path.join(tmp, 'src', 'armed_fixture')
        os.makedirs(pkg)
        with open(os.path.join(pkg, '__init__.py'), 'w') as f:
            f.write('FROM_INIT = 1\n')
        with open(os.path.join(pkg, 'bar.py'), 'w') as f:
            f.write('ANSWER = 42\n')

        key = ah.generate_key()
        mods = os.path.join(tmp, 'modules')
        stats = ah.armor_package(pkg, os.path.join(mods, 'armed_fixture'), key, False)
        assert stats['failed'] == 0
        key_file = os.path.join(tmp, '_key.bin')
        with open(key_file, 'wb') as f:
            f.write(key)

        env = dict(os.environ)
        env['HEVOLVE_ARMORED_DIR'] = mods
        env['HEVOLVE_ARMOR_KEY_FILE'] = key_file
        env['PYTHONPATH'] = (os.path.join(ROOT, 'hevolvearmor')
                             + os.pathsep + env.get('PYTHONPATH', ''))
        prog = hs._ARMOR_INSTALL_SNIPPET + (
            "import armed_fixture.bar as b\n"
            "print('RESULT', b.ANSWER)\n"
        )
        r = subprocess.run([sys.executable, '-c', prog],
                           capture_output=True, text=True, env=env, timeout=120)
        assert 'RESULT 42' in r.stdout, (r.stdout + '\n' + r.stderr)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.skipif(not _armor_available(),
                    reason="hevolvearmor native / cryptography not available")
def test_snippet_is_noop_without_bundle():
    """Dev path: no HEVOLVE_ARMORED_DIR → the snippet does nothing, no error,
    so the api_server import that follows loads the plain on-disk package."""
    env = dict(os.environ)
    env.pop('HEVOLVE_ARMORED_DIR', None)
    env.pop('HEVOLVE_ARMOR_KEY_FILE', None)
    env['PYTHONPATH'] = (os.path.join(ROOT, 'hevolvearmor')
                         + os.pathsep + env.get('PYTHONPATH', ''))
    prog = hs._ARMOR_INSTALL_SNIPPET + "print('OK_NOOP')\n"
    r = subprocess.run([sys.executable, '-c', prog],
                       capture_output=True, text=True, env=env, timeout=60)
    assert 'OK_NOOP' in r.stdout and r.returncode == 0, (r.stdout + '\n' + r.stderr)
