"""Tests for security.native_hive_loader — STUB-MODE / detection / import surface.

This is the loader for the closed-source HevolveAI native layer. HARTOS must run
correctly when that layer is ABSENT (the common case: no hevolveai on the box),
degrading to pure-Python stubs — that graceful-degradation path was largely
uncovered and is exactly what these tests pin.

SCOPE (deliberate, per the master-key AI-exclusion rules): only the safe surface
is exercised — binary hashing, stub-mode detection/status, the None-returning
stub inference wrappers, and the canonical import helper (with the compiled-load
path MOCKED, never invoked). The decryption path (_decrypt_binary_to_tmpfs, which
derives a key from a node private seed), _verify_binary_signature, and the real
load_native_lib/_try_install_hevolvearmor are NEVER called here — no private-key
or binary-decryption code is touched.
"""
from __future__ import annotations

import hashlib
import sys
import types

import security.native_hive_loader as nl


# ── _compute_binary_hash: chunked SHA-256 of a file ─────────────────────────
def test_compute_binary_hash_matches_sha256(tmp_path):
    data = b'HART native blob' * 5000  # spans multiple 64KiB chunks
    p = tmp_path / 'lib.so'
    p.write_bytes(data)
    assert nl._compute_binary_hash(str(p)) == hashlib.sha256(data).hexdigest()


# ── detection getters return the module state ───────────────────────────────
def test_detection_getters(monkeypatch):
    monkeypatch.setattr(nl, '_native_available', False)
    monkeypatch.setattr(nl, '_stub_mode', True)
    monkeypatch.setattr(nl, '_native_lib', None)
    monkeypatch.setattr(nl, '_cython_module', None)
    assert nl.is_native_available() is False
    assert nl.is_stub_mode() is True
    assert nl.get_native_lib() is None
    assert nl.get_hevolveai() is None


# ── stub-mode wrappers return None (no native/cython present) ────────────────
def test_stub_mode_inference_returns_none(monkeypatch):
    monkeypatch.setattr(nl, '_cython_module', None)
    monkeypatch.setattr(nl, '_native_available', False)
    monkeypatch.setattr(nl, '_native_lib', None)
    assert nl.native_infer('hello') is None
    assert nl.native_hebbian_update({'a': 1.0}) is None
    assert nl.native_version() is None


def test_native_version_uses_cython_dunder_when_present(monkeypatch):
    fake = types.ModuleType('hevolveai')
    fake.__version__ = '9.9.9'
    monkeypatch.setattr(nl, '_cython_module', fake)
    assert nl.native_version() == '9.9.9'


# ── shutdown is a safe no-op with nothing loaded, and resets state ──────────
def test_shutdown_native_resets_state(monkeypatch):
    monkeypatch.setattr(nl, '_native_lib', None)
    monkeypatch.setattr(nl, '_cython_module', object())
    monkeypatch.setattr(nl, '_native_available', True)
    nl.shutdown_native()  # must not raise
    assert nl.is_native_available() is False
    assert nl.get_hevolveai() is None


# ── get_status: diagnostic shape ────────────────────────────────────────────
def test_get_status_shape(monkeypatch):
    monkeypatch.setattr(nl, '_cython_module', None)
    monkeypatch.setattr(nl, '_native_available', False)
    monkeypatch.setattr(nl, '_native_lib', None)
    st = nl.get_status()
    assert set(st) >= {
        'native_available', 'stub_mode', 'load_method', 'cython_package',
        'version', 'platform_lib', 'search_paths'}
    assert st['native_available'] is False
    assert st['cython_package'] is False


# ── try_import_hevolveai: validation + cython-present paths (load MOCKED) ────
def test_try_import_rejects_non_hevolveai_path():
    # Pure validation branch — never reaches any loader.
    assert nl.try_import_hevolveai('os') is None
    assert nl.try_import_hevolveai('') is None


def test_try_import_unavailable_returns_none(monkeypatch):
    # _cython_module None -> would call the compiled loader; MOCK it to report
    # unavailable so no real decryption/armor install happens.
    monkeypatch.setattr(nl, '_cython_module', None)
    monkeypatch.setattr(nl, '_try_load_cython_package',
                        lambda: (False, 'stubbed-unavailable'))
    assert nl.try_import_hevolveai('hevolveai.embodied_ai') is None


def test_try_import_toplevel_returns_cython_module(monkeypatch):
    fake = types.ModuleType('hevolveai')
    monkeypatch.setattr(nl, '_cython_module', fake)
    assert nl.try_import_hevolveai('hevolveai') is fake


def test_try_import_submodule_via_importlib(monkeypatch):
    fake_top = types.ModuleType('hevolveai')
    monkeypatch.setattr(nl, '_cython_module', fake_top)
    sub = types.ModuleType('hevolveai.sub')
    sub.marker = 42
    monkeypatch.setitem(sys.modules, 'hevolveai.sub', sub)
    got = nl.try_import_hevolveai('hevolveai.sub')
    assert got is sub and got.marker == 42


# ── try_import_hevolveai_names: attribute extraction + failure modes ─────────
def test_try_import_names_returns_tuple(monkeypatch):
    fake_top = types.ModuleType('hevolveai')
    monkeypatch.setattr(nl, '_cython_module', fake_top)
    sub = types.ModuleType('hevolveai.util')
    sub.a, sub.b = 1, 2
    monkeypatch.setitem(sys.modules, 'hevolveai.util', sub)
    assert nl.try_import_hevolveai_names('hevolveai.util', ('a', 'b')) == (1, 2)


def test_try_import_names_none_when_module_missing(monkeypatch):
    monkeypatch.setattr(nl, '_cython_module', None)
    monkeypatch.setattr(nl, '_try_load_cython_package',
                        lambda: (False, 'unavailable'))
    assert nl.try_import_hevolveai_names('hevolveai.x', ('a',)) is None


def test_try_import_names_none_when_attr_missing(monkeypatch):
    fake_top = types.ModuleType('hevolveai')
    monkeypatch.setattr(nl, '_cython_module', fake_top)
    sub = types.ModuleType('hevolveai.util2')
    sub.a = 1  # 'b' intentionally absent
    monkeypatch.setitem(sys.modules, 'hevolveai.util2', sub)
    assert nl.try_import_hevolveai_names('hevolveai.util2', ('a', 'b')) is None
