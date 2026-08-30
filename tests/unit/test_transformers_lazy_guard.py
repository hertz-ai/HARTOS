"""Behavioural tests for core._transformers_lazy_guard.

The guard patches transformers' `_LazyModule.__getattr__` so that a SAME-(module,
name) re-entry on one thread (which is exactly what `hasattr` does inside
`__getattr__`) raises AttributeError instead of recursing forever — the recursion
that otherwise hangs splash / agent_daemon / Hypercorn workers. Cross-name
recursion and normal lookups must still work, and the patch must be idempotent.

transformers is not installed in this env (the very reason the guard is
defensive), so these tests inject a FAKE `transformers.utils.import_utils`
carrying a minimal `_LazyModule` whose original `__getattr__` re-enters via
`hasattr` — proving the guard turns an infinite recursion into a clean return.
0% covered before this file.

    python -m pytest tests/unit/test_transformers_lazy_guard.py -q --noconftest
"""
from __future__ import annotations

import sys
import types

import pytest

from core import _transformers_lazy_guard as guard


def _make_fake_lazy_module_class():
    """A stand-in _LazyModule whose __getattr__ probes hasattr(self, name) — the
    same-name re-entry the guard must break. Without the guard this recurses
    until RecursionError; with it, the re-entry returns cleanly."""
    class FakeLazyModule:
        __name__ = "faketransformers"

        def __getattr__(self, name):
            # The real _LazyModule does a hasattr(self, name)-style probe here,
            # which re-enters __getattr__ with the SAME name.
            probed_present = hasattr(self, name)
            return ("resolved", name, probed_present)

    return FakeLazyModule


def _inject_fake_transformers(monkeypatch, lazy_cls=None, gpt2_cls=None):
    """Wire a fake transformers package tree into sys.modules so the guard's
    inline imports resolve to our stand-ins."""
    tf = types.ModuleType("transformers")
    monkeypatch.setitem(sys.modules, "transformers", tf)

    if lazy_cls is not None:
        iu = types.ModuleType("transformers.utils.import_utils")
        iu._LazyModule = lazy_cls
        utils = types.ModuleType("transformers.utils")
        utils.import_utils = iu
        tf.utils = utils
        monkeypatch.setitem(sys.modules, "transformers.utils", utils)
        monkeypatch.setitem(sys.modules, "transformers.utils.import_utils", iu)

    if gpt2_cls is not None:
        tok = types.ModuleType("transformers.models.gpt2.tokenization_gpt2_fast")
        tok.GPT2TokenizerFast = gpt2_cls
        monkeypatch.setitem(
            sys.modules, "transformers.models.gpt2.tokenization_gpt2_fast", tok)
    return tf


# ── re-entry guard ──────────────────────────────────────────────────────────
class TestReentryGuard:
    def test_same_name_reentry_no_longer_recurses(self, monkeypatch):
        Lazy = _make_fake_lazy_module_class()
        _inject_fake_transformers(monkeypatch, lazy_cls=Lazy)

        guard._install_lazy_module_reentry_guard()
        inst = Lazy()
        # Without the guard this is infinite recursion (RecursionError). With it,
        # the inner hasattr re-entry raises AttributeError -> hasattr False ->
        # the original resolves and returns.
        result = inst.some_attr
        assert result == ("resolved", "some_attr", False)

    def test_patch_marks_the_class_guarded(self, monkeypatch):
        Lazy = _make_fake_lazy_module_class()
        _inject_fake_transformers(monkeypatch, lazy_cls=Lazy)
        guard._install_lazy_module_reentry_guard()
        assert getattr(Lazy, "_hartos_reentry_guarded", False) is True

    def test_install_is_idempotent(self, monkeypatch):
        Lazy = _make_fake_lazy_module_class()
        _inject_fake_transformers(monkeypatch, lazy_cls=Lazy)
        guard._install_lazy_module_reentry_guard()
        wrapped_once = Lazy.__getattr__
        guard._install_lazy_module_reentry_guard()  # second call must no-op
        assert Lazy.__getattr__ is wrapped_once, "guard double-wrapped the method"

    def test_distinct_names_are_independent(self, monkeypatch):
        Lazy = _make_fake_lazy_module_class()
        _inject_fake_transformers(monkeypatch, lazy_cls=Lazy)
        guard._install_lazy_module_reentry_guard()
        inst = Lazy()
        # Two different names both resolve — the guard keys on (id(self), name),
        # so it never blocks a DIFFERENT attribute.
        assert inst.alpha[1] == "alpha"
        assert inst.beta[1] == "beta"

    def test_missing_lazymodule_symbol_is_a_noop(self, monkeypatch):
        # transformers present but the version moved/removed _LazyModule.
        iu = types.ModuleType("transformers.utils.import_utils")
        utils = types.ModuleType("transformers.utils")
        utils.import_utils = iu
        tf = types.ModuleType("transformers")
        tf.utils = utils
        monkeypatch.setitem(sys.modules, "transformers", tf)
        monkeypatch.setitem(sys.modules, "transformers.utils", utils)
        monkeypatch.setitem(sys.modules, "transformers.utils.import_utils", iu)
        guard._install_lazy_module_reentry_guard()  # must not raise

    def test_transformers_absent_is_a_noop(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "transformers", None)
        # None in sys.modules makes `import transformers` raise ImportError,
        # which the guard swallows. Must not propagate.
        guard._install_lazy_module_reentry_guard()


# ── GPT2 direct-bind ────────────────────────────────────────────────────────
class TestGpt2DirectBind:
    def test_binds_gpt2_into_transformers_dict(self, monkeypatch):
        class FakeGPT2TokenizerFast:  # noqa: N801
            pass

        tf = _inject_fake_transformers(monkeypatch, gpt2_cls=FakeGPT2TokenizerFast)
        assert "GPT2TokenizerFast" not in tf.__dict__
        guard._install_gpt2_direct_bind()
        assert tf.__dict__.get("GPT2TokenizerFast") is FakeGPT2TokenizerFast

    def test_does_not_clobber_existing_binding(self, monkeypatch):
        class Real:  # noqa: N801
            pass

        class Other:  # noqa: N801
            pass

        tf = _inject_fake_transformers(monkeypatch, gpt2_cls=Other)
        tf.__dict__["GPT2TokenizerFast"] = Real  # already resolved
        guard._install_gpt2_direct_bind()
        assert tf.__dict__["GPT2TokenizerFast"] is Real, "must not overwrite"

    def test_absent_gpt2_submodule_is_a_noop(self, monkeypatch):
        # transformers present, but the gpt2 tokenizer submodule is gone.
        _inject_fake_transformers(monkeypatch)  # no gpt2 module injected
        guard._install_gpt2_direct_bind()  # must not raise
