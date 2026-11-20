"""The GPT2TokenizerFast pre-bind: one implementation, right on transformers 4 and 5.

core/_transformers_lazy_guard binds GPT2TokenizerFast into
transformers.__dict__ before anything asks for it, so the lazy-module
__getattr__ recursion it documents never runs.  It runs first on every HARTOS
import (core/__init__.py, integrations/__init__.py) and
hart_intelligence_entry calls the same function.

Two things went wrong with the earlier copies (review of #142):

* transformers 5 dropped the *_fast modules, so the import they relied on
  failed and every boot logged, with a traceback:

      hart_intelligence_entry - WARNING - transformers GPT2 tokenizer defang
      skipped (symbol absent/moved)
      ModuleNotFoundError: No module named
          'transformers.models.gpt2.tokenization_gpt2_fast'

  (installed Nunba, transformers 5.1.0, 2026-10-22).  On 5.1.0
  `from transformers import GPT2TokenizerFast` returns
  transformers.models.gpt2.tokenization_gpt2.GPT2Tokenizer, so that is the
  class to bind there.  The copy that runs first never had the 5.x branch.
* the 5.x branch fell back on ANY ImportError.  On transformers 4.x the _fast
  module imports `tokenizers` at the top, so a missing or wrong-ABI
  `tokenizers` silently bound the SLOW tokenizer as GPT2TokenizerFast and
  logged nothing.  The fallback is for "the _fast module does not exist",
  not "importing it failed".

These run the REAL function against a fake transformers package laid out like
each major version.

Run:
  pytest tests/unit/test_gpt2_defang_transformers5.py -v --noconftest
"""
import ast
import importlib.util
import logging
import os
import sys
import types

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_ENTRY = os.path.join(_ROOT, 'hart_intelligence_entry.py')
_GUARD = os.path.join(_ROOT, 'core', '_transformers_lazy_guard.py')
_FAST = 'transformers.models.gpt2.tokenization_gpt2_fast'
_SLOW = 'transformers.models.gpt2.tokenization_gpt2'


def _package(name):
    mod = types.ModuleType(name)
    mod.__path__ = []
    return mod


def _fake_transformers(monkeypatch, *, has_fast_module, fast_import_error=None):
    """A transformers laid out like 4.x (has_fast_module) or 5.x.

    fast_import_error: an exception the 4.x _fast module raises when imported
    (what a missing / wrong-ABI `tokenizers` does at its top).
    """
    class GPT2Tokenizer:
        pass

    class GPT2TokenizerFast:
        pass

    tf = _package('transformers')
    slow = types.ModuleType(_SLOW)
    slow.GPT2Tokenizer = GPT2Tokenizer
    mods = {'transformers': tf,
            'transformers.models': _package('transformers.models'),
            'transformers.models.gpt2': _package('transformers.models.gpt2'),
            _SLOW: slow}
    monkeypatch.delitem(sys.modules, _FAST, raising=False)
    if has_fast_module:
        fast = types.ModuleType(_FAST)
        fast.GPT2TokenizerFast = GPT2TokenizerFast
        mods[_FAST] = fast
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    if fast_import_error is not None:
        class _Boom:
            def find_spec(self, name, path=None, target=None):
                if name == _FAST:
                    raise fast_import_error
        monkeypatch.setattr(sys, 'meta_path', [_Boom()] + list(sys.meta_path))
    return tf, GPT2Tokenizer, GPT2TokenizerFast


def _load_guard(name='hartos_lazy_guard_under_test'):
    """The guard module, loaded by path: `import core` pulls in all of HARTOS."""
    spec = importlib.util.spec_from_file_location(name, _GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bind(tf, guard):
    tf.__dict__.pop('GPT2TokenizerFast', None)
    guard.bind_gpt2_tokenizer_fast()


def test_transformers5_binds_the_class_5_itself_answers_with(monkeypatch, caplog):
    tf, slow_cls, _ = _fake_transformers(monkeypatch, has_fast_module=False)
    guard = _load_guard()
    with caplog.at_level(logging.WARNING):
        _bind(tf, guard)
    assert tf.__dict__.get('GPT2TokenizerFast') is slow_cls
    assert 'defang skipped' not in caplog.text


def test_transformers4_still_binds_the_fast_class(monkeypatch, caplog):
    tf, _, fast_cls = _fake_transformers(monkeypatch, has_fast_module=True)
    guard = _load_guard()
    with caplog.at_level(logging.WARNING):
        _bind(tf, guard)
    assert tf.__dict__.get('GPT2TokenizerFast') is fast_cls
    assert 'defang skipped' not in caplog.text


def test_the_guard_binds_on_import_the_path_every_hartos_import_takes(monkeypatch):
    """core/__init__ imports this module for its side effect, so importing it
    must bind on 5.x too (the copy that runs first used to do nothing there)."""
    tf, slow_cls, _ = _fake_transformers(monkeypatch, has_fast_module=False)
    _load_guard()
    assert tf.__dict__.get('GPT2TokenizerFast') is slow_cls


def test_missing_tokenizers_on_4x_is_not_papered_over_with_the_slow_class(
        monkeypatch, caplog):
    """The _fast module IS there; importing it failed on `tokenizers`."""
    tf, _, _ = _fake_transformers(
        monkeypatch, has_fast_module=False,
        fast_import_error=ModuleNotFoundError(
            "No module named 'tokenizers'", name='tokenizers'))
    guard = _load_guard()
    with caplog.at_level(logging.WARNING):
        _bind(tf, guard)
    assert 'GPT2TokenizerFast' not in tf.__dict__
    assert 'defang skipped' in caplog.text
    assert 'tokenizers' in caplog.text


def test_wrong_abi_tokenizers_on_4x_is_logged_not_swallowed(monkeypatch, caplog):
    tf, _, _ = _fake_transformers(
        monkeypatch, has_fast_module=False,
        fast_import_error=ImportError('DLL load failed while importing tokenizers'))
    guard = _load_guard()
    with caplog.at_level(logging.WARNING):
        _bind(tf, guard)
    assert 'GPT2TokenizerFast' not in tf.__dict__
    assert 'defang skipped' in caplog.text


def test_an_existing_binding_is_left_alone(monkeypatch):
    tf, _, _ = _fake_transformers(monkeypatch, has_fast_module=True)
    guard = _load_guard()
    sentinel = object()
    tf.__dict__['GPT2TokenizerFast'] = sentinel
    guard.bind_gpt2_tokenizer_fast()
    assert tf.__dict__['GPT2TokenizerFast'] is sentinel


def test_no_transformers_is_not_a_fault(monkeypatch, caplog):
    """Nothing to bind is not "skipped, symbol moved": HARTOS imports on
    hosts without transformers, so it must not warn on every import."""
    monkeypatch.setitem(sys.modules, 'transformers', None)
    with caplog.at_level(logging.WARNING):
        guard = _load_guard()
        guard.bind_gpt2_tokenizer_fast()
    assert 'defang skipped' not in caplog.text


@pytest.mark.parametrize('error', [
    KeyError('Name'),                  # corrupt dist-info (observed 2026-04-26)
    RuntimeError('transformers blew up while importing'),
    ImportError('cannot import name x from y'),
])
def test_a_transformers_that_raises_on_import_never_escapes_import_core(
        monkeypatch, caplog, error):
    """The guard runs at import of `core` and `integrations`, before
    hart_intelligence_entry's packages_distributions workaround.  A corrupt
    dist-info makes `import transformers` raise KeyError('Name'); the original
    code swallowed everything, and narrowing the import's catch to ImportError
    let it escape `import core` and fail the backend's start (review of
    #142).  It must be logged, never raised, at import and on a direct call."""
    monkeypatch.delitem(sys.modules, 'transformers', raising=False)

    class _Boom:
        def find_spec(self, name, path=None, target=None):
            if name == 'transformers':
                raise error

    monkeypatch.setattr(sys, 'meta_path', [_Boom()] + list(sys.meta_path))
    with caplog.at_level(logging.WARNING):
        guard = _load_guard()            # the import-time call
        guard.bind_gpt2_tokenizer_fast()  # and a direct one
    assert 'defang skipped' in caplog.text


def test_the_entry_module_has_no_second_copy():
    """One implementation: hart_intelligence_entry calls the guard's."""
    with open(_ENTRY, encoding='utf-8') as fh:
        src = fh.read()
    tree = ast.parse(src)
    inline = [n for n in tree.body if isinstance(n, ast.Try)
              and "'GPT2TokenizerFast'" in (ast.get_source_segment(src, n) or '')]
    assert inline == [], 'the entry module re-implements the GPT2 pre-bind'
    assert 'bind_gpt2_tokenizer_fast()' in src
    assert 'from core._transformers_lazy_guard import bind_gpt2_tokenizer_fast' in src


def test_the_real_transformers_answers_with_what_we_bind():
    """Pins the measured fact the v5 branch relies on, where 5.x is installed."""
    transformers = pytest.importorskip('transformers')
    if int(transformers.__version__.split('.')[0]) < 5:
        pytest.skip('transformers 4.x still ships tokenization_gpt2_fast')
    from transformers import GPT2TokenizerFast
    from transformers.models.gpt2.tokenization_gpt2 import GPT2Tokenizer
    assert GPT2TokenizerFast is GPT2Tokenizer
