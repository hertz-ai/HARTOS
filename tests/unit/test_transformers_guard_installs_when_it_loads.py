"""The transformers guards are installed when transformers loads, not before.

MEASURED 2026-11-28 on the installed build (python-embed, fresh interpreter):
core/_transformers_lazy_guard.py imported transformers at import, and `core`
and `integrations` both import that module, so a bare ``import core`` cost
5.7 s and 1,153 MB of committed memory (415 MB working set, against 6 MB
before it) in EVERY process.  The speech-in worker (CTranslate2, no torch of
its own) and the MCP bridge each carried that, and every worker start waited
for it.

Each case runs in a fresh interpreter against a small real package on disk
named ``transformers``, so the import system itself is what is driven.  The
guards' own behaviour is in test_transformers_lazy_guard.py.

    python -m pytest tests/unit/test_transformers_guard_installs_when_it_loads.py -q --noconftest
"""
import os
import subprocess
import sys
import textwrap

_GUARD_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'core', '_transformers_lazy_guard.py')

_LOAD_GUARD = textwrap.dedent(f"""
    import importlib.util
    _spec = importlib.util.spec_from_file_location('guard_under_test', {_GUARD_FILE!r})
    guard = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(guard)
""")


def _fake_transformers_on_disk(root, init_body=''):
    """A package tree shaped like the parts of transformers the guards read."""
    pkg = root / 'transformers'
    (pkg / 'utils').mkdir(parents=True)
    (pkg / 'models' / 'gpt2').mkdir(parents=True)
    (pkg / '__init__.py').write_text(
        'LOADED = True\n' + textwrap.dedent(init_body), encoding='utf-8')
    (pkg / 'utils' / '__init__.py').write_text('', encoding='utf-8')
    (pkg / 'utils' / 'import_utils.py').write_text(textwrap.dedent("""
        class _LazyModule:
            __name__ = 'faketransformers'

            def __getattr__(self, name):
                return ('resolved', name, hasattr(self, name))
    """), encoding='utf-8')
    (pkg / 'models' / '__init__.py').write_text('', encoding='utf-8')
    (pkg / 'models' / 'gpt2' / '__init__.py').write_text('', encoding='utf-8')
    (pkg / 'models' / 'gpt2' / 'tokenization_gpt2_fast.py').write_text(
        'class GPT2TokenizerFast:\n    pass\n', encoding='utf-8')
    return root


def _fresh(tmp_path, script, with_package=True):
    head = 'import sys\n'
    if with_package:
        head += f'sys.path.insert(0, {str(tmp_path)!r})\n'
    # No site-packages at all for the "not installed" case: the developer's
    # own environment may hold a transformers.
    flags = ['-I'] if with_package else ['-I', '-S']
    done = subprocess.run(
        [sys.executable, *flags, '-c', head + textwrap.dedent(script)],
        capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout


def test_loading_the_guard_does_not_load_transformers(tmp_path):
    _fake_transformers_on_disk(tmp_path)
    out = _fresh(tmp_path, _LOAD_GUARD + textwrap.dedent("""
        print('transformers' in sys.modules, 'torch' in sys.modules,
              guard._waiting in sys.meta_path)
    """))
    assert out.split() == ['False', 'False', 'True']


def test_both_guards_are_in_place_when_the_import_returns(tmp_path):
    _fake_transformers_on_disk(tmp_path)
    out = _fresh(tmp_path, _LOAD_GUARD + textwrap.dedent("""
        import transformers
        from transformers.utils.import_utils import _LazyModule
        print(transformers.LOADED,
              getattr(_LazyModule, '_hartos_reentry_guarded', False),
              transformers.__dict__.get('GPT2TokenizerFast') is not None,
              _LazyModule().anything)
    """))
    assert out.strip() == "True True True ('resolved', 'anything', False)"


def test_the_first_from_import_already_finds_the_direct_bind(tmp_path):
    """The symbol langchain reads first: bound before the from-import's
    attribute lookup, as it was when the guard imported transformers."""
    _fake_transformers_on_disk(tmp_path, init_body="""
        def __getattr__(name):
            raise AssertionError('lazy lookup reached for ' + name)
    """)
    out = _fresh(tmp_path, _LOAD_GUARD + textwrap.dedent("""
        from transformers import GPT2TokenizerFast
        print(GPT2TokenizerFast.__module__)
    """))
    assert out.strip() == 'transformers.models.gpt2.tokenization_gpt2_fast'


def test_the_wrapper_leaves_nothing_behind(tmp_path):
    _fake_transformers_on_disk(tmp_path)
    out = _fresh(tmp_path, _LOAD_GUARD + textwrap.dedent("""
        import transformers
        print(guard._waiting in sys.meta_path,
              type(transformers.__spec__.loader).__name__,
              type(transformers.__loader__).__name__)
    """))
    assert out.split() == ['False', 'SourceFileLoader', 'SourceFileLoader']


def test_transformers_loaded_first_is_guarded_at_once(tmp_path):
    _fake_transformers_on_disk(tmp_path)
    out = _fresh(tmp_path, 'import transformers\n' + _LOAD_GUARD + textwrap.dedent("""
        from transformers.utils.import_utils import _LazyModule
        print(getattr(_LazyModule, '_hartos_reentry_guarded', False),
              guard._waiting in sys.meta_path)
    """))
    assert out.split() == ['True', 'False']


def test_asking_twice_waits_once(tmp_path):
    _fake_transformers_on_disk(tmp_path)
    out = _fresh(tmp_path, _LOAD_GUARD + textwrap.dedent("""
        guard._guard_when_transformers_loads()
        guard._guard_when_transformers_loads()
        print(sum(1 for f in sys.meta_path if f is guard._waiting))
    """))
    assert out.strip() == '1'


def test_a_transformers_that_fails_to_import_fails_as_itself(tmp_path):
    _fake_transformers_on_disk(tmp_path, init_body="""
        raise RuntimeError('broken install')
    """)
    out = _fresh(tmp_path, _LOAD_GUARD + textwrap.dedent("""
        try:
            import transformers
        except RuntimeError as e:
            print(e, guard._waiting in sys.meta_path,
                  'transformers' in sys.modules)
    """))
    assert out.strip() == 'broken install False False'


def test_no_transformers_installed_is_an_ordinary_import_error(tmp_path):
    out = _fresh(tmp_path, _LOAD_GUARD + textwrap.dedent("""
        try:
            import transformers
        except ModuleNotFoundError as e:
            print(e.name)
    """), with_package=False)
    assert out.strip() == 'transformers'


def test_other_imports_are_not_touched(tmp_path):
    _fake_transformers_on_disk(tmp_path)
    out = _fresh(tmp_path, _LOAD_GUARD + textwrap.dedent("""
        import json
        print(guard._waiting.find_spec('json') is None,
              guard._waiting.find_spec('transformers.utils') is None,
              'transformers' in sys.modules)
    """))
    assert out.split() == ['True', 'True', 'False']
