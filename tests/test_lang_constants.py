"""Invariants for the canonical language collections in core.constants.

Failing tests = DRY violation: someone inlined a duplicate frozenset
for "non-Latin script" or "Indic" instead of importing from the single
source of truth.  Fix by importing from core.constants.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Repo paths are DERIVED, never hardcoded. These guards read source files, and
# an absolute dev-box path (`C:/Users/sathi/...`) makes the test pass on
# exactly one machine and fail everywhere else -- which is what it did in CI.
# Same relative idiom the rest of tests/ already uses.
HARTOS_ROOT = Path(__file__).resolve().parents[1]
# Nunba is a SIBLING repo, so it is genuinely absent on a CI runner that only
# checked out HARTOS. Skip there rather than fail: a missing sibling is not a
# DRY regression, and reporting it as one trains everyone to ignore this file.
NUNBA_ROOT = HARTOS_ROOT.parent / 'Nunba-HART-Companion'
TTS_ENGINE = NUNBA_ROOT / 'tts' / 'tts_engine.py'
_needs_nunba = pytest.mark.skipif(
    not TTS_ENGINE.is_file(),
    reason='sibling repo Nunba-HART-Companion not checked out beside HARTOS',
)


def test_non_latin_script_langs_exists():
    from core.constants import NON_LATIN_SCRIPT_LANGS, SUPPORTED_LANG_DICT
    assert isinstance(NON_LATIN_SCRIPT_LANGS, frozenset)
    # Every code in the set must be a valid ISO 639-1 from the canonical
    # language-name dict.  If this fails, a new lang was added to the
    # set without registering its display name.
    unknown = NON_LATIN_SCRIPT_LANGS - set(SUPPORTED_LANG_DICT)
    assert not unknown, f"Unknown codes in NON_LATIN_SCRIPT_LANGS: {unknown}"


def test_indic_langs_exists_and_subset():
    from core.constants import INDIC_LANGS, NON_LATIN_SCRIPT_LANGS
    assert isinstance(INDIC_LANGS, frozenset)
    assert INDIC_LANGS <= NON_LATIN_SCRIPT_LANGS, (
        "INDIC_LANGS must be a subset of NON_LATIN_SCRIPT_LANGS"
    )


def test_critical_scripts_in_non_latin():
    from core.constants import NON_LATIN_SCRIPT_LANGS
    for code in ('ta', 'hi', 'bn', 'te', 'mr', 'zh', 'ja', 'ko',
                 'ar', 'he', 'th'):
        assert code in NON_LATIN_SCRIPT_LANGS, (
            f"{code!r} must be in NON_LATIN_SCRIPT_LANGS — the 0.8B draft "
            f"cannot produce its native script."
        )


def test_english_not_in_non_latin():
    from core.constants import NON_LATIN_SCRIPT_LANGS
    assert 'en' not in NON_LATIN_SCRIPT_LANGS


def test_dispatcher_imports_canonical_set():
    """speculative_dispatcher.py MUST NOT define its own frozenset.
    If this fails, revert the inline `_skip_draft_langs = frozenset({...})`
    and `from core.constants import NON_LATIN_SCRIPT_LANGS`."""
    src = (HARTOS_ROOT / 'integrations' / 'agent_engine'
           / 'speculative_dispatcher.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in (
                    '_skip_draft_langs', '_indic_or_script', '_non_latin',
                ):
                    pytest.fail(
                        f"Inline frozenset `{t.id}` at line {node.lineno} — "
                        f"import from core.constants.NON_LATIN_SCRIPT_LANGS instead",
                    )


def test_hart_intelligence_entry_imports_canonical_set():
    src = (HARTOS_ROOT / 'hart_intelligence_entry.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in (
                    '_skip_draft_langs', '_indic_or_script', '_non_latin',
                ):
                    pytest.fail(
                        f"Inline frozenset `{t.id}` at line {node.lineno} — "
                        f"import from core.constants.NON_LATIN_SCRIPT_LANGS",
                    )


@_needs_nunba
def test_tts_engine_imports_indic_langs_from_canonical():
    src = TTS_ENGINE.read_text(encoding='utf-8')
    # Must have the import line; must NOT still have `_INDIC_LANGS = {...}`
    assert 'from core.constants import' in src, (
        "tts_engine.py must import INDIC_LANGS from core.constants"
    )
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == '_INDIC_LANGS':
                    # Only acceptable if it's a re-export/alias (RHS is Name
                    # pointing to the imported canonical).  A set literal
                    # means DRY violation.
                    if not isinstance(node.value, ast.Name):
                        pytest.fail(
                            f"_INDIC_LANGS inline at line {node.lineno} — "
                            f"import from core.constants.INDIC_LANGS instead"
                        )


_CJK = {'zh', 'ja', 'ko'}
_RTL = {'ar', 'he', 'fa', 'ur'}
# Latin-script codes are the DISCRIMINATOR, not part of the signature:
# NON_LATIN_SCRIPT_LANGS by definition contains none of them, while a
# backend's supported-LANGUAGES capability set (tts_engine's per-engine
# `'languages': {...}` blocks, which legitimately mix 'zh'+'ar' with
# 'en'/'es'/...) always does. A literal with CJK+RTL AND Latin codes is a
# coverage list, not a duplicate of the canonical concept.
_LATIN = {'en', 'es', 'fr', 'de', 'it', 'pt', 'nl', 'pl'}


def _is_non_latin_concept(elems):
    s = set(elems)
    return bool(_CJK & s) and bool(_RTL & s) and not (_LATIN & s)


def _set_literal_strings(node: ast.AST):
    """String elements of a set/frozenset LITERAL node, else None."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id == 'frozenset' and node.args:
        node = node.args[0]
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        elems = [e.value for e in node.elts
                 if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        return elems
    return None


def test_no_cjk_rtl_duplicates_across_files():
    """No source file may define its own set literal of the NON-LATIN-SCRIPT
    concept.  There's only one; it lives in core.constants.

    The signature is a SINGLE set/frozenset literal whose elements include
    both a CJK code and an RTL code — only the canonical concept mixes those
    scripts in one collection.  The old heuristic ("the file mentions 'zh'
    somewhere AND 'ar' somewhere") false-positived on tts_engine.py, whose
    language→backend capability map legitimately contains both codes as DICT
    KEYS with frozensets of BACKEND NAMES as values.  A guard that is red on
    a legal file trains everyone to ignore it.
    """
    offenders = []
    for path in (
        HARTOS_ROOT / 'integrations' / 'agent_engine' / 'speculative_dispatcher.py',
        HARTOS_ROOT / 'hart_intelligence_entry.py',
        TTS_ENGINE,          # sibling repo — skipped below when absent
    ):
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            elems = _set_literal_strings(node)
            if elems and _is_non_latin_concept(elems):
                offenders.append((path, sorted(set(elems))[:6]))
                break
    assert not offenders, (
        f"Inline CJK+RTL set literals (DRY violation — import "
        f"core.constants.NON_LATIN_SCRIPT_LANGS instead): {offenders}"
    )


def test_cjk_rtl_signature_detector_boundary():
    """The detector itself, proven on synthetic inputs against BOTH legal
    shapes that previously false-positived in tts_engine.py."""
    def hits(src):
        return [n for n in ast.walk(ast.parse(src))
                if (e := _set_literal_strings(n)) and _is_non_latin_concept(e)]

    # The canonical-concept duplicate: CJK + RTL, no Latin codes.
    assert hits("x = frozenset({'zh', 'ar', 'hi'})"), \
        'the canonical-concept signature must be detected'
    # Legal shape 1: capability map — backend-name sets keyed by language.
    assert not hits("caps = {'zh': frozenset({'pocket', 'lux'}),"
                    " 'ar': frozenset({'lux'})}"), \
        'backend-name sets keyed by language must NOT be flagged'
    # Legal shape 2: a backend's supported-languages coverage list — mixes
    # CJK + RTL with Latin codes, which the canonical concept never contains.
    assert not hits("langs = {'en', 'es', 'cs', 'ar', 'zh', 'ja'}"), \
        'a coverage list containing Latin codes must NOT be flagged'
