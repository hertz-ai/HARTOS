"""The GPT2TokenizerFast pre-bind works on transformers 5 as well as 4.

hart_intelligence_entry binds GPT2TokenizerFast into transformers.__dict__
before anything asks for it, so the lazy-module __getattr__ recursion it
documents never runs.  transformers 5 dropped the *_fast modules, so the
import it relied on failed and every boot logged, with a traceback:

    hart_intelligence_entry - WARNING - transformers GPT2 tokenizer defang
    skipped (symbol absent/moved)
    ModuleNotFoundError: No module named
        'transformers.models.gpt2.tokenization_gpt2_fast'

(installed Nunba, transformers 5.1.0, 2026-10-22 -- on every boot).  On 5.1.0
`from transformers import GPT2TokenizerFast` returns
transformers.models.gpt2.tokenization_gpt2.GPT2Tokenizer, so that is the
class to bind there.

These run the REAL try-block from hart_intelligence_entry.py against a fake
transformers package laid out like each major version.

Run:
  pytest tests/unit/test_gpt2_defang_transformers5.py -v --noconftest
"""
import ast
import logging
import os
import sys
import types

import pytest

_ENTRY = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'hart_intelligence_entry.py')


def _defang_block():
    with open(_ENTRY, encoding='utf-8') as fh:
        src = fh.read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Try) and \
                'GPT2TokenizerFast' in ast.get_source_segment(src, node):
            return compile(ast.Module(body=[node], type_ignores=[]),
                           _ENTRY, 'exec')
    raise AssertionError('hart_intelligence_entry.py has no GPT2 defang block')


def _package(name):
    mod = types.ModuleType(name)
    mod.__path__ = []
    return mod


def _fake_transformers(monkeypatch, *, has_fast_module):
    class GPT2Tokenizer:
        pass

    class GPT2TokenizerFast:
        pass

    tf = _package('transformers')
    models = _package('transformers.models')
    gpt2 = _package('transformers.models.gpt2')
    slow = types.ModuleType('transformers.models.gpt2.tokenization_gpt2')
    slow.GPT2Tokenizer = GPT2Tokenizer
    mods = {'transformers': tf, 'transformers.models': models,
            'transformers.models.gpt2': gpt2,
            'transformers.models.gpt2.tokenization_gpt2': slow}
    if has_fast_module:
        fast = types.ModuleType('transformers.models.gpt2.tokenization_gpt2_fast')
        fast.GPT2TokenizerFast = GPT2TokenizerFast
        mods['transformers.models.gpt2.tokenization_gpt2_fast'] = fast
    else:
        monkeypatch.delitem(
            sys.modules, 'transformers.models.gpt2.tokenization_gpt2_fast',
            raising=False)
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return tf, GPT2Tokenizer, GPT2TokenizerFast


def test_transformers5_binds_the_class_5_itself_answers_with(monkeypatch, caplog):
    tf, slow_cls, _ = _fake_transformers(monkeypatch, has_fast_module=False)
    with caplog.at_level(logging.WARNING):
        exec(_defang_block(), {'__name__': 'hart_intelligence_entry'})  # noqa: S102 - runs the real block
    assert tf.__dict__.get('GPT2TokenizerFast') is slow_cls
    assert 'defang skipped' not in caplog.text


def test_transformers4_still_binds_the_fast_class(monkeypatch, caplog):
    tf, _, fast_cls = _fake_transformers(monkeypatch, has_fast_module=True)
    with caplog.at_level(logging.WARNING):
        exec(_defang_block(), {'__name__': 'hart_intelligence_entry'})  # noqa: S102 - runs the real block
    assert tf.__dict__.get('GPT2TokenizerFast') is fast_cls
    assert 'defang skipped' not in caplog.text


def test_no_transformers_is_still_only_a_warning(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, 'transformers', None)
    with caplog.at_level(logging.WARNING):
        exec(_defang_block(), {'__name__': 'hart_intelligence_entry'})  # noqa: S102 - runs the real block
    assert 'defang skipped' in caplog.text


def test_the_real_transformers_answers_with_what_we_bind():
    """Pins the measured fact the v5 branch relies on, where 5.x is installed."""
    transformers = pytest.importorskip('transformers')
    if int(transformers.__version__.split('.')[0]) < 5:
        pytest.skip('transformers 4.x still ships tokenization_gpt2_fast')
    from transformers import GPT2TokenizerFast
    from transformers.models.gpt2.tokenization_gpt2 import GPT2Tokenizer
    assert GPT2TokenizerFast is GPT2Tokenizer
