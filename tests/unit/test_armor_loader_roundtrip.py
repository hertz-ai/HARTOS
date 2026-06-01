"""Phase 3 of HEVOLVEAI_ARMOR_CANONICAL_PLAN.md — the loader GATE, as a
permanent regression test.

Proves the Hevolvearmor mechanism round-trips END TO END: the HARTOS producer
(scripts/armor_hevolveai.py: compile .py -> .pyc -> AES-256-GCM -> .enc) and the
loader (hevolvearmor._loader.install_loader -> native armor_decrypt -> exec) are
COMPATIBLE — a module armored by the producer imports + runs through the loader.

This is the invariant the whole canonical plan rests on: if it ever breaks,
flag-flipping the supervisor to the armored path (Phase 5) would crash every
deployment.  Verified live 2026-06-01 (foo.bar.ANSWER==42 through the loader).

Skips gracefully where the toolchain isn't present (no cryptography, or the
hevolvearmor native ext doesn't match the interpreter ABI) so it never
false-fails on a thin CI image.
"""
from __future__ import annotations

import os
import sys
import shutil
import tempfile
import importlib
import importlib.util as ilu

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _load_producer():
    spec = ilu.spec_from_file_location(
        'armor_hevolveai', os.path.join(ROOT, 'scripts', 'armor_hevolveai.py'))
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _loader_available():
    armor_pkg = os.path.join(ROOT, 'hevolvearmor')
    if armor_pkg not in sys.path:
        sys.path.insert(0, armor_pkg)
    try:
        importlib.import_module('hevolvearmor._loader')
        importlib.import_module('hevolvearmor._native')  # ABI-matched native decrypt
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _loader_available(),
                    reason="hevolvearmor native ext / cryptography not available in this env")
def test_producer_armored_module_imports_through_loader():
    ah = _load_producer()
    from hevolvearmor._loader import install_loader, uninstall_loader

    tmp = tempfile.mkdtemp(prefix='armor_rt_')
    try:
        pkg = os.path.join(tmp, 'src', 'armed_fixture')
        os.makedirs(pkg)
        with open(os.path.join(pkg, '__init__.py'), 'w') as f:
            f.write('FROM_INIT = 1\n')
        with open(os.path.join(pkg, 'bar.py'), 'w') as f:
            f.write('ANSWER = 42\n\n\ndef greet():\n    return "hi from armored"\n')

        key = ah.generate_key()
        assert len(key) == 32
        mods = os.path.join(tmp, 'modules')
        stats = ah.armor_package(pkg, os.path.join(mods, 'armed_fixture'), key, False)
        assert stats['failed'] == 0 and stats['encrypted'] >= 2

        # .enc produced, no plain .py/.pyc leaked into the armored output
        enc = [f for _r, _d, fs in os.walk(os.path.join(mods, 'armed_fixture')) for f in fs]
        assert any(f.endswith('.enc') for f in enc)
        assert not any(f.endswith(('.py', '.pyc')) for f in enc), \
            "armored output must contain no decompilable .py/.pyc"

        install_loader(mods, key, ['armed_fixture'])
        try:
            bar = importlib.import_module('armed_fixture.bar')
            assert bar.ANSWER == 42
            assert bar.greet() == 'hi from armored'
        finally:
            uninstall_loader()
            for m in ('armed_fixture', 'armed_fixture.bar'):
                sys.modules.pop(m, None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
