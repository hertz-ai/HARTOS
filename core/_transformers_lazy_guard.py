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
re-import during a single interpreter session is a no-op.  HIE calls this
module's ``bind_gpt2_tokenizer_fast`` and ``install_lazy_module_reentry_guard``
rather than carrying its own copies (three copies of the GPT2 pre-bind drifted:
the one that ran first had no transformers-5 branch).

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

import logging

_GPT2_FAST_MODULE = 'transformers.models.gpt2.tokenization_gpt2_fast'


def _gpt2_tokenizer_fast_class():
    """The class `from transformers import GPT2TokenizerFast` answers with.

    transformers 4.x: the fast tokenizer has its own module.  transformers 5.x
    dropped the ``*_fast`` modules, and its lazy lookup answers
    ``GPT2TokenizerFast`` with ``GPT2Tokenizer`` (measured on 5.1.0), so that
    is the class to bind there.

    The slow class is the answer ONLY when the ``_fast`` module itself does
    not exist.  On 4.x that module imports ``tokenizers`` at its top, so a
    missing or wrong-ABI ``tokenizers`` fails the same ``import`` statement
    with a different ``ModuleNotFoundError.name`` (or a plain ImportError):
    that is a broken install, and papering over it with the slow tokenizer
    bound under the fast name would hide it.  It propagates to the caller.
    """
    try:
        from transformers.models.gpt2.tokenization_gpt2_fast import (
            GPT2TokenizerFast,
        )
        return GPT2TokenizerFast
    except ModuleNotFoundError as exc:
        if exc.name != _GPT2_FAST_MODULE:
            raise
    from transformers.models.gpt2.tokenization_gpt2 import GPT2Tokenizer
    return GPT2Tokenizer


def bind_gpt2_tokenizer_fast() -> None:
    """Resolve `transformers.GPT2TokenizerFast` eagerly so the very first
    consumer lookup hits a populated `__dict__` instead of the lazy graph.

    THE one implementation of this pre-bind: this module runs first on every
    HARTOS import, and ``hart_intelligence_entry`` calls the same function.
    Idempotent, and an existing binding is left alone.  transformers not being
    installed is not a fault (nothing to bind); any other failure is logged
    with its traceback and never raised.

    Never raised, because this runs at import of ``core`` and
    ``integrations``, before hart_intelligence_entry's packages_distributions
    workaround: a corrupt dist-info makes ``import transformers`` raise
    ``KeyError('Name')`` (observed 2026-04-26), and an exception here would
    escape ``import core`` and stop the backend from starting.
    """
    try:
        import transformers as _tf
    except Exception as exc:
        if isinstance(exc, ImportError) and exc.name == 'transformers':
            return
        logging.getLogger(__name__).warning(
            "transformers GPT2 tokenizer defang skipped (transformers failed "
            "to import)", exc_info=True)
        return
    try:
        if 'GPT2TokenizerFast' not in _tf.__dict__:
            _tf.__dict__['GPT2TokenizerFast'] = _gpt2_tokenizer_fast_class()
    except Exception:
        # The recursion guard below still applies, and worker threads pay the
        # lazy path once; but say so, with the cause (a broken `tokenizers`
        # install looks exactly like this).
        logging.getLogger(__name__).warning(
            "transformers GPT2 tokenizer defang skipped (symbol absent/moved)",
            exc_info=True)


def install_lazy_module_reentry_guard() -> None:
    """Wrap `_LazyModule.__getattr__` with a threading.local re-entry guard.

    THE one implementation: this module runs it on import, and
    ``hart_intelligence_entry`` calls it again after its
    packages_distributions workaround (a first attempt that a corrupt
    dist-info stopped is retried there).  It used to carry its own copy.

    Idempotent — the sentinel `_hartos_reentry_guarded` ensures repeated
    calls in the same interpreter are no-ops.  transformers not being
    installed is not a fault; any other failure is logged and never raised.
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
    except Exception as exc:
        if isinstance(exc, ImportError) and (exc.name or '').startswith('transformers'):
            return  # transformers not installed: nothing to guard
        # Worker threads will hit the lazy path and may pay the recursion
        # once, but the GPT2TokenizerFast direct-bind above still covers the
        # common entry symbol.  Say so, with the cause.
        logging.getLogger(__name__).warning(
            "transformers _LazyModule re-entry defang skipped", exc_info=True)


# Run on import — every entry path that triggers this module gets both
# guards before any transformers attribute access can fire.
bind_gpt2_tokenizer_fast()
install_lazy_module_reentry_guard()
