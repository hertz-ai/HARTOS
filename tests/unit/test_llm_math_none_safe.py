"""Chat must survive ``llm_math is None`` — and a failed init must be LOUD.

THE BUG (live, 2026-08-07, CI nightly install).  hart_intelligence_entry
builds the math chain at import time::

    try:
        llm_math = LLMMathChain(llm=get_llm(model_name="gpt-3.5-turbo"))
    except Exception:
        llm_math = None          # silent — nothing logged

In the nightly bundle the constructor raised (langchain drift family, see
memory/feedback_langchain_uplift_path.md), so ``llm_math`` was None for the
whole boot.  ``get_tools`` then evaluated ``func=llm_math.run`` UNGUARDED →
``AttributeError: 'NoneType' object has no attribute 'run'`` on EVERY /chat
— Tier-1 dead for 7 hours while ``--validate`` stayed green, because import
succeeds and the except leaves no trace.  First user chat of the boot
(08:55:44 "hi") produced the full traceback: chat → get_ans → get_tools
line 4765.

The author already guarded the sibling ``chain`` at every use site
(``if chain is not None:``) but not ``llm_math`` — three sites.

WHAT THIS PINS
  1. every ``llm_math.run`` access sits under an ``llm_math is not None``
     guard (the exact idiom already used for ``chain``);
  2. the init's except handler LOGS the failure — a boot-time silent-except
     that poisons the hot path is the vacuous-guard family
     (memory/feedback_vacuous_guards.md).

DISCRIMINATION: pre-fix, sites at ~4765/~5231/~5268 are unguarded and the
handler logs nothing — both tests FAIL.  Post-fix both pass.

AST-based, not import-based: importing hart_intelligence_entry starts the
Flask app + daemon threads (unsafe in a unit run).  Same precedent as
test_goal_config_shape.py's drift guard on _models_local.
"""
from __future__ import annotations

import ast
import os

SRC = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', 'hart_intelligence_entry.py'))


def _tree() -> ast.AST:
    with open(SRC, encoding='utf-8') as fh:
        tree = ast.parse(fh.read())
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._parent = parent  # type: ignore[attr-defined]
    return tree


def _under_none_guard(node: ast.AST) -> bool:
    """True iff an ancestor is ``if llm_math is not None: ...``."""
    cur = getattr(node, '_parent', None)
    while cur is not None:
        if isinstance(cur, ast.If):
            t = cur.test
            if (isinstance(t, ast.Compare)
                    and isinstance(t.left, ast.Name)
                    and t.left.id == 'llm_math'
                    and len(t.ops) == 1
                    and isinstance(t.ops[0], ast.IsNot)
                    and len(t.comparators) == 1
                    and isinstance(t.comparators[0], ast.Constant)
                    and t.comparators[0].value is None):
                return True
        cur = getattr(cur, '_parent', None)
    return False


def test_every_llm_math_run_site_is_none_guarded():
    """FAILS PRE-FIX: three ``llm_math.run`` sites have no None guard."""
    sites = [
        n for n in ast.walk(_tree())
        if isinstance(n, ast.Attribute) and n.attr == 'run'
        and isinstance(n.value, ast.Name) and n.value.id == 'llm_math'
    ]
    assert sites, (
        'no llm_math.run sites found — selector went stale, re-point it '
        'before trusting this test')
    unguarded = [n.lineno for n in sites if not _under_none_guard(n)]
    assert unguarded == [], (
        f'llm_math.run evaluated without an `llm_math is not None` guard at '
        f'lines {unguarded} — when the boot-time LLMMathChain init fails, '
        f'these lines turn EVERY chat turn into AttributeError (live '
        f'2026-08-07: Tier-1 dead for a full 7h boot)')


def test_llm_math_init_failure_is_logged():
    """FAILS PRE-FIX: the except handler swallows the constructor error."""
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.ExceptHandler):
            continue
        assigns_none = any(
            isinstance(stmt, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == 'llm_math'
                    for t in stmt.targets)
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is None
            for stmt in ast.walk(node))
        if not assigns_none:
            continue
        has_log = any(
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr in ('warning', 'error', 'exception')
            for c in ast.walk(node))
        assert has_log, (
            'llm_math init failure is swallowed silently — the None then '
            'kills every chat with zero evidence in any log (it took a live '
            'user traceback to find, 2026-08-07).  Log it.')
        return
    raise AssertionError(
        'no `except: llm_math = None` handler found — selector went stale, '
        're-point it before trusting this test')
