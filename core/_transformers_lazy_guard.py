"""Install transformers `_LazyModule.__getattr__` recursion guards.

This module exists solely for its **import side-effect** — it patches
`transformers` to prevent the ``_LazyModule.__getattr__`` re-entry
recursion that hangs splash / agent_daemon / Hypercorn workers.

Background (carried forward verbatim from the original install site at
`hart_intelligence_entry.py:80-209`, commit a6d2dca):

The guard was originally installed at the top of HIE because HIE was
the single canonical entry point into HARTOS — anyone touching the
package went through HIE first.  That assumption broke when modules
under ``core/`` and ``integrations/`` started getting imported via
non-HIE paths (e.g. Nunba's bg_import → ``models.catalog`` →
``integrations.service_tools.model_catalog`` → ``registry`` →
``core.labeled_tool``).  Those paths now reach into `langchain.agents`
→ `transformers` BEFORE HIE has loaded the guard.

Fix: extract the guard to a tiny standalone module imported by both
``core/__init__.py`` and ``integrations/__init__.py``.  Every HARTOS
entry path now triggers the guard before any transformers attribute
access.  Patch is idempotent (``_hartos_reentry_guarded`` sentinel) so
re-import during a single interpreter session is a no-op; HIE's
original install block still runs unchanged as a third defense.

Two patches:

  1. GPT2TokenizerFast direct-bind.  ``transformers.__dict__`` gets
     ``GPT2TokenizerFast`` resolved to its real class so the very first
     ``from transformers import GPT2TokenizerFast`` (langchain_core
     does this transitively) is a plain dict hit, not a lazy lookup.

  2. ``_LazyModule.__getattr__`` re-entry guard.  Wraps the method
     with ``threading.local()`` state.  When the same ``(module, name)``
     pair is requested again on the SAME thread while the original
     lookup is still in flight (which is what ``hasattr`` does inside
     ``__getattr__``), raise ``AttributeError`` immediately.  ``hasattr``
     swallows ``AttributeError`` → returns ``False`` → caller proceeds
     without recursion.  Cross-name recursion is preserved (only the
     pathological same-name re-entry is short-circuited).

See ``memory/feedback_transformers_lazy_module_patch.md`` for the
follow-up tracking the upstream HuggingFace fix.
"""
from __future__ import annotations


def _install_gpt2_direct_bind() -> None:
    """Resolve `transformers.GPT2TokenizerFast` eagerly so the very first
    consumer lookup hits a populated `__dict__` instead of the lazy graph."""
    try:
        import transformers as _tf
        from transformers.models.gpt2.tokenization_gpt2_fast import (
            GPT2TokenizerFast as _gpt2_fast,
        )
        if 'GPT2TokenizerFast' not in _tf.__dict__:
            _tf.__dict__['GPT2TokenizerFast'] = _gpt2_fast
    except Exception:
        # transformers not installed, version moved the submodule, or the
        # bundled hevolvearmor strip removed it.  Recursion guard below
        # still applies; this direct-bind is a one-symbol fast-path.
        pass


def _install_lazy_module_reentry_guard() -> None:
    """Wrap `_LazyModule.__getattr__` with a threading.local re-entry guard.

    Idempotent — the sentinel `_hartos_reentry_guarded` ensures repeated
    calls in the same interpreter are no-ops.
    """
    try:
        import threading
        from transformers.utils import import_utils as _tf_iu

        _LazyModule = getattr(_tf_iu, '_LazyModule', None)
        if _LazyModule is None:
            return
        if getattr(_LazyModule, '_hartos_reentry_guarded', False):
            return  # already wrapped — idempotent

        _orig_getattr = _LazyModule.__getattr__
        _resolving = threading.local()

        # Bind original + local via default args so the closure carries its
        # own references (caller's module body can be GC'd safely).
        def _hartos_guarded_getattr(
                self, name, _orig=_orig_getattr, _local=_resolving):
            in_progress = getattr(_local, 'set', None)
            if in_progress is None:
                in_progress = set()
                _local.set = in_progress
            key = (id(self), name)
            if key in in_progress:
                # Same (module, name) re-entry on this thread — `hasattr`
                # probe asking "is this bound yet?"; we're mid-resolution
                # so the answer is "not yet".  AttributeError lets
                # `hasattr` return False, which is the intended semantic.
                raise AttributeError(
                    f"module {self.__name__!r} has no attribute {name!r} "
                    f"(HARTOS re-entry guard: same-name __getattr__ "
                    f"recursion broken)"
                )
            in_progress.add(key)
            try:
                return _orig(self, name)
            finally:
                in_progress.discard(key)

        _LazyModule.__getattr__ = _hartos_guarded_getattr
        _LazyModule._hartos_reentry_guarded = True
    except Exception:
        # transformers absent, version moved _LazyModule, or guard
        # already-installed elsewhere — fall through.  Worker threads
        # will hit the lazy path and may pay the recursion once, but the
        # GPT2TokenizerFast direct-bind above still covers the common
        # entry symbol.
        pass


# Run on import — every entry path that triggers this module gets both
# guards before any transformers attribute access can fire.
_install_gpt2_direct_bind()
_install_lazy_module_reentry_guard()
