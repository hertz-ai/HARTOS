"""LLM completions must not inherit the 15s generic HTTP read timeout.

LIVE EVIDENCE (bundled desktop, 2026-08-03). Every chat turn logged this pair,
exactly 30s apart (two 15s attempts):

    09:14:44  casual conv first call — routing to draft 0.8B
    09:15:14  In except the exception is HTTPConnectionPool(
              host='127.0.0.1', port=8080): Read timed out. (read timeout=15)

`_pooled_post_with_refusal_check` called `pooled_post` with NO timeout, so it
inherited http_pool.DEFAULT_TIMEOUT = (3, 15). A local 4B generation takes
longer than 15s (measured 14.4s for an 8-token reply, and that is the floor),
so that call could never succeed. It timed out, the caller's bare
`except Exception` swallowed it at INFO level, and the whole request was
re-issued — a one-word reply cost ~110s wall across 3 LLM round-trips
(py-spy caught all three: dispatch_draft_first, _call:5772, _call:5827).

Two things made it invisible: the failure was logged at INFO, and the fallback
produced a correct answer, so only the latency showed.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

http_pool = pytest.importorskip("core.http_pool")

ENTRY = REPO / "hart_intelligence_entry.py"


def _func_source(name: str) -> str:
    """Source of a top-level function, via AST (importing this module is heavy)."""
    tree = ast.parse(ENTRY.read_text(encoding="utf-8", errors="replace"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(
                ENTRY.read_text(encoding="utf-8", errors="replace"), node
            ) or ""
    raise AssertionError(f"{name} not found in hart_intelligence_entry.py")


def test_llm_timeout_constant_exists():
    assert hasattr(http_pool, "LLM_COMPLETION_TIMEOUT"), (
        "core.http_pool must own the LLM completion budget — it is the "
        "canonical home for HTTP timeouts"
    )


def test_llm_read_budget_beats_a_real_generation():
    connect, read = http_pool.LLM_COMPLETION_TIMEOUT
    assert read >= 60, (
        f"read timeout {read}s is too tight for a local generation; 15s was "
        "the bug and even 30s would fail under load"
    )
    assert connect <= 10, f"connect timeout {connect}s is too slack for loopback"


def test_llm_budget_is_strictly_larger_than_the_generic_default():
    assert (
        http_pool.LLM_COMPLETION_TIMEOUT[1] > http_pool.DEFAULT_TIMEOUT[1]
    ), (
        "the whole point is that completions need MORE read budget than "
        "ordinary API calls; if these converge the bug is back"
    )


def test_generic_default_stays_tight():
    """Do not 'fix' this by loosening the global default.

    DEFAULT_TIMEOUT guards health checks and ordinary JSON APIs. Raising it
    would make every dead endpoint hang for minutes.
    """
    assert http_pool.DEFAULT_TIMEOUT[1] <= 30, (
        "DEFAULT_TIMEOUT was widened — completions should carry their own "
        "budget instead"
    )


def test_refusal_check_helper_sets_the_llm_timeout():
    src = _func_source("_pooled_post_with_refusal_check")
    code = "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "LLM_COMPLETION_TIMEOUT" in code, (
        "_pooled_post_with_refusal_check posts a COMPLETION but does not set "
        "the LLM timeout — it inherits DEFAULT_TIMEOUT (3, 15) and cannot "
        "succeed against a local 4B"
    )
    assert "setdefault" in code, (
        "use kwargs.setdefault so an explicit caller-supplied timeout wins"
    )


def test_both_posts_in_the_helper_are_covered():
    """The helper posts twice: the initial call and the refusal retry.

    Both go through **kwargs, so one setdefault before the first covers both —
    but only if the setdefault precedes them.
    """
    src = _func_source("_pooled_post_with_refusal_check")
    assert src.count("pooled_post(") >= 2, (
        "expected the initial POST and the refusal-retry POST"
    )
    first_post = src.index("pooled_post(")
    setdefault_at = src.find("setdefault('timeout'")
    if setdefault_at == -1:
        setdefault_at = src.find('setdefault("timeout"')
    assert 0 <= setdefault_at < first_post, (
        "the timeout setdefault must come BEFORE the first pooled_post, "
        "otherwise the initial completion still inherits (3, 15)"
    )


def test_import_is_wired():
    head = ENTRY.read_text(encoding="utf-8", errors="replace")[:40000]
    assert "LLM_COMPLETION_TIMEOUT" in head.split("def ", 1)[0] or (
        "from core.http_pool import" in head and "LLM_COMPLETION_TIMEOUT" in head
    ), "LLM_COMPLETION_TIMEOUT referenced but never imported -> NameError at runtime"
