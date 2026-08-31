"""Regression test: helper.py must NOT import langchain at module top.

The cx_Freeze --validate gate on Nunba's Windows build fails with
``ImportError: cannot import name 'LanguageModelOutput' from
'langchain_core.language_models'`` whenever helper.py (or any module
that transitively imports it) has a module-top
``from langchain_classic.* import X`` line.

Root cause documented in HARTOS commit 970006a:

    langchain_core v1.2.15's ``language_models/__init__.py`` uses
    ``__getattr__`` lazy attribute resolution; cx_Freeze's static
    tracer can't follow the lazy chain, so the bundled .pyc loses
    the LanguageModelOutput name and the frozen-binary import
    explodes at runtime.

The fix is to lazy-import langchain INSIDE the functions that use it.
This test parses helper.py at the AST level and flags any
module-level langchain import.  Any regression now fails the unit
suite locally + in CI before it can reach build-windows.

Why AST instead of regex: comments / docstrings can match the regex
spuriously.  ast.walk only sees real Import nodes.
"""
from __future__ import annotations

import ast
import os
import pytest


HARTOS_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _module_top_imports(filepath: str) -> list[str]:
    """Return the dotted module names imported at module top of `filepath`.

    Only module-level Import / ImportFrom nodes count.  Imports inside
    function bodies / class bodies / try blocks at module level are
    intentionally excluded — those are the lazy-pattern we want.
    """
    with open(filepath, 'r', encoding='utf-8') as f:
        tree = ast.parse(f.read(), filename=filepath)
    out = []
    for node in tree.body:  # top-level only
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.append(node.module)
    return out


@pytest.mark.parametrize('module_file', [
    'hartos/helper.py',
    'hartos/reuse_recipe.py',
    'hartos/gather_agentdetails.py',
    'hartos/create_recipe.py',
])
def test_no_module_top_langchain_import(module_file):
    """Each module the cx_Freeze validate gate checks must avoid
    importing langchain at module top.

    See the file docstring above for context.  The 4 modules below
    were the exact ones that failed validate on 4 consecutive
    build-windows runs (25855122044 / 26011572288 / 26012388043 /
    26013613058) until HARTOS commit 970006a lazy-fied
    helper.py's imports.
    """
    path = os.path.join(HARTOS_ROOT, module_file)
    if not os.path.isfile(path):
        pytest.skip(f'{module_file} not present (unexpected)')

    bad = [
        m for m in _module_top_imports(path)
        if m and m.startswith(('langchain', 'langchain_core',
                               'langchain_classic',
                               'langchain_community'))
    ]
    assert not bad, (
        f'{module_file} regressed: module-top langchain import(s) '
        f'detected: {bad}.  Move them inside the using function '
        f'body (lazy import pattern) — see helper.py docstring + '
        f'HARTOS commit 970006a for the why.'
    )
