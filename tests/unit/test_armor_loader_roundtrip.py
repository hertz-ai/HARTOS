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

# The armor loader round-trips through the hevolvearmor NATIVE extension. A
# runner whose install skipped building it (CI does `pip install -e .
# --no-deps` without the ext toolchain) cannot execute the round-trip at all:
# skip honestly rather than fail on the missing binary.
pytest.importorskip('hevolvearmor._native',
    reason='hevolvearmor native extension not built in this environment')

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


def test_exec_module_sets_dunder_file_from_origin():
    """An armored module that references ``__file__`` at import time must
    not ``NameError`` — the loader must expose the .enc origin as
    ``module.__file__`` before exec, mirroring CPython's FileLoader.

    Root cause this guards: ``find_spec`` builds ``ModuleSpec(...)``
    directly, which leaves ``has_location`` False, so CPython's
    ``_init_module_attrs`` never sets ``__file__``; ``exec_module`` didn't
    set it either.  The live hive-backend boot crash was exactly this —
    ``embodied_ai/models/qwen_auto_encoder.py:670`` does
    ``os.path.dirname(__file__)`` at module scope, raising
    ``NameError: name '__file__' is not defined`` and taking the whole
    hevolveai FastAPI app's lifespan startup down (only llama-server
    survived).  Witnessed 2026-06-07 in gui_app.log.

    Mocks ONLY the native-decrypt boundary; drives the real
    ``exec_module`` so it runs in any env (no Rust ext / cryptography
    needed) and asserts the observable effect.
    """
    import types

    armor_pkg = os.path.join(ROOT, 'hevolvearmor')
    if armor_pkg not in sys.path:
        sys.path.insert(0, armor_pkg)
    from hevolvearmor._loader import ArmoredFinder, ArmoredLoader

    enc_path = os.path.join(
        tempfile.gettempdir(), 'armored', 'pkg', 'usesfile.enc')
    code = compile(
        "import os\n"
        "HERE = os.path.dirname(__file__)\n"
        "BASENAME = os.path.basename(__file__)\n",
        enc_path, 'exec')

    # Bypass __init__ (needs a real modules_dir + key) — we only exercise
    # exec_module, mocking the decrypt boundary to return our code object.
    finder = ArmoredFinder.__new__(ArmoredFinder)
    finder._code_cache = {}
    finder._decrypt_and_unmarshal = lambda _p: code

    loader = ArmoredLoader(finder, enc_path, is_package=False)
    module = types.ModuleType('armored_usesfile')

    # Pre-fix this raises NameError: name '__file__' is not defined.
    loader.exec_module(module)

    assert module.__file__ == enc_path
    assert module.HERE == os.path.dirname(enc_path)
    assert module.BASENAME == 'usesfile.enc'
