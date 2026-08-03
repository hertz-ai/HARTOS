"""Drift-guard: HARTOS must not steal a host application's root log handlers.

Task #489.  ``hart_intelligence_entry`` configures logging at IMPORT time.  It
used to run a blanket ``logging.getLogger().handlers.clear()``, which silently
detached whatever the *host* process had already attached to root.

Measured on a live frozen Nunba build (2026-08-03): Nunba attaches
``gui_app.log`` and ``server.log`` to the root logger at startup and imports
hart_intelligence_entry lazily ~140s in.  Both files stopped dead at boot+140s
and stayed silent for the next 134 minutes of uptime, while named-logger files
(``agent_system.log``) kept writing.  The two files CLAUDE.md documents as THE
diagnostic logs were dark for ~98% of every session.

The fix tags HARTOS's own root handlers and removes only those, preserving the
de-duplication intent for re-imports without touching foreign handlers.

The AST test is static (importing hart_intelligence_entry boots the whole
runtime).  The behavioural test exercises the tag/remove contract directly on a
throwaway logger, so it verifies semantics rather than just source text.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

ENTRY = Path(__file__).resolve().parents[2] / "hart_intelligence_entry.py"
TAG = "_hartos_root_handler"


def _module_level_source() -> str:
    return ENTRY.read_text(encoding="utf-8")


def test_no_blanket_handlers_clear_on_root():
    """`<root>.handlers.clear()` is the exact bug — it takes the host's down."""
    tree = ast.parse(_module_level_source(), filename=str(ENTRY))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        # matches  X.handlers.clear()
        if (
            isinstance(fn, ast.Attribute)
            and fn.attr == "clear"
            and isinstance(fn.value, ast.Attribute)
            and fn.value.attr == "handlers"
        ):
            offenders.append(node.lineno)
    assert not offenders, (
        "hart_intelligence_entry.py calls <logger>.handlers.clear() at line(s) "
        f"{offenders}. This module is imported by host apps (Nunba) that have "
        "already configured root logging; a blanket clear() silently detaches "
        "their handlers and their log files go dark (task #489). Remove only "
        f"handlers tagged {TAG!r}."
    )


def test_no_blanket_handlers_reassignment_on_root():
    """`<root>.handlers = []` is the same defect spelled differently."""
    tree = ast.parse(_module_level_source(), filename=str(ENTRY))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute) and target.attr == "handlers"
    ]
    assert not offenders, (
        "hart_intelligence_entry.py reassigns <logger>.handlers at line(s) "
        f"{offenders} — same host-handler theft as clear() (task #489)."
    )


def test_entry_module_tags_the_handlers_it_owns():
    """Positive guard: without the tag there is nothing to selectively remove,
    so re-import would either duplicate handlers or tempt a clear() again."""
    assert TAG in _module_level_source(), (
        f"hart_intelligence_entry.py no longer references {TAG!r}; HARTOS can "
        "no longer tell its own root handlers apart from the host's "
        "(task #489)."
    )


class _Marker(logging.Handler):
    """Inert handler — we only care about identity/attributes."""

    def emit(self, record):  # pragma: no cover - never emits in these tests
        pass


@pytest.fixture()
def scratch_logger():
    lg = logging.getLogger("hartos_test_root_hijack")
    lg.handlers = []
    yield lg
    lg.handlers = []


def test_selective_removal_preserves_foreign_handlers(scratch_logger):
    """The contract: a HARTOS re-import drops HARTOS handlers and ONLY those.

    This is the behaviour Nunba depends on — gui_app.log and server.log must
    survive an import of hart_intelligence_entry.
    """
    host_a, host_b = _Marker(), _Marker()          # Nunba: gui_app + server
    hartos_old = _Marker()
    setattr(hartos_old, TAG, True)

    scratch_logger.addHandler(host_a)
    scratch_logger.addHandler(host_b)
    scratch_logger.addHandler(hartos_old)

    # Replicates the entry-module block.
    for existing in list(scratch_logger.handlers):
        if getattr(existing, TAG, False):
            scratch_logger.removeHandler(existing)

    assert host_a in scratch_logger.handlers, "host handler was stolen (#489)"
    assert host_b in scratch_logger.handlers, "host handler was stolen (#489)"
    assert hartos_old not in scratch_logger.handlers, (
        "HARTOS's own previous handler should be dropped so a re-import does "
        "not duplicate output"
    )


def test_blanket_clear_would_have_lost_them(scratch_logger):
    """Pins WHY the selective removal exists: the old code loses host handlers.

    Without this, a future reader could 'simplify' back to clear() and the
    test above would still pass for the wrong reason.
    """
    host_a = _Marker()
    scratch_logger.addHandler(host_a)

    scratch_logger.handlers.clear()  # the pre-fix behaviour

    assert host_a not in scratch_logger.handlers, (
        "sanity: clear() is supposed to drop everything — if this ever fails "
        "the premise of task #489 needs re-checking"
    )
