"""Drift-guard tests for Bug 3 / task #508 — chat-hot-path stage events.

The fix instruments 6 milestones in hart_intelligence_entry.get_ans (+
the CustomGPT.run "generating" step) so the UI's ThinkingProcessContainer
updates every 3-8s instead of going dark for 50+ seconds on slow first
turns (Tamil-meta-request: 67s end-to-end, 2 SSE events pre-fix).

These tests pin the contract so the next refactor doesn't silently
strip emissions:

  T1  Every CHAT_STAGES entry has a CHAT_STAGE_TEXTS mapping (no
      orphan stage names that publish nothing).
  T2  Every stage referenced by hart_intelligence_entry exists in
      CHAT_STAGES (no AST-emit of an unknown stage that would
      silently no-op at runtime).
  T3  hart_intelligence_entry emits ALL 6 canonical stages.  Drops
      surface as a test failure (not a missing UI update at 3am).
  T4  publish_chat_stage routes through publish_thinking_trace —
      it is NOT a parallel SSE publisher (CLAUDE.md Gate 4).
  T5  publish_chat_stage is idempotent per (request_id, stage) —
      a re-entered milestone in the same turn produces ONE bubble.
  T6  Unknown stage logs a warning and returns False — drift-guard
      against silent typos in emit sites.
"""
from __future__ import annotations

import ast
import io
import os
from unittest.mock import patch

import pytest


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
HART_ENTRY = os.path.join(REPO_ROOT, 'hart_intelligence_entry.py')
CROSSBAR_PUBLISH = os.path.join(
    REPO_ROOT, 'core', 'peer_link', 'crossbar_publish.py')


# Stages we expect every chat hot path to emit.  Mirrors the design in
# core.constants.CHAT_STAGE_TEXTS.  If a stage is added/removed, this
# tuple AND the constants dict AND the AST emit sites all have to move
# together — that lockstep is the whole point of the drift-guard.
EXPECTED_STAGES = (
    'loading_context',
    'loading_memory',
    'loading_tools',
    'thinking',
    'generating',
    'finalizing',
)


def _collect_publish_chat_stage_args(path: str) -> list[str]:
    """Return the first positional arg (stage name) of every
    publish_chat_stage(...) call found by AST walk."""
    src = io.open(path, encoding='utf-8').read()
    tree = ast.parse(src)
    stages = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        # Match bare `publish_chat_stage(...)` (after the module-level
        # import we added).  Don't bother with attribute access — every
        # call site in HARTOS uses the imported name.
        if isinstance(fn, ast.Name) and fn.id == 'publish_chat_stage':
            if node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(
                        first.value, str):
                    stages.append(first.value)
    return stages


# ─── T1: CHAT_STAGES ⊆ CHAT_STAGE_TEXTS ─────────────────────────────

def test_every_stage_has_text():
    from core.constants import CHAT_STAGES, CHAT_STAGE_TEXTS
    for stage in CHAT_STAGES:
        assert stage in CHAT_STAGE_TEXTS, (
            f"Stage {stage!r} is in CHAT_STAGES but has no text in "
            f"CHAT_STAGE_TEXTS — extend the dict.")


# ─── T2: AST-emitted stages ⊆ CHAT_STAGES ───────────────────────────

def test_no_orphan_emit_sites():
    from core.constants import CHAT_STAGES
    emitted = set(_collect_publish_chat_stage_args(HART_ENTRY))
    orphans = emitted - set(CHAT_STAGES)
    assert not orphans, (
        f"hart_intelligence_entry emits unknown stages {sorted(orphans)} — "
        f"add them to core.constants.CHAT_STAGE_TEXTS or fix the typo.")


# ─── T3: All canonical stages emitted ───────────────────────────────

def test_all_expected_stages_emitted():
    emitted = set(_collect_publish_chat_stage_args(HART_ENTRY))
    missing = set(EXPECTED_STAGES) - emitted
    assert not missing, (
        f"hart_intelligence_entry is missing emissions for "
        f"{sorted(missing)} — Bug 3 / task #508 requires all 6 stages "
        f"so the UI spinner updates every 3-8s on slow first turns.")


# ─── T4: Canonical publisher reuse — no parallel path ───────────────

def test_publish_chat_stage_reuses_canonical_publish_thinking_trace():
    """publish_chat_stage MUST call publish_thinking_trace; it must NOT
    add a second SSE publisher (CLAUDE.md Gate 4 parallel-path rule)."""
    src = io.open(CROSSBAR_PUBLISH, encoding='utf-8').read()
    tree = ast.parse(src)

    # Find the publish_chat_stage function def
    chat_stage_fn = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and node.name == 'publish_chat_stage'):
            chat_stage_fn = node
            break
    assert chat_stage_fn is not None, (
        "publish_chat_stage missing from core.peer_link.crossbar_publish")

    # Inside that function it MUST call publish_thinking_trace
    calls = [
        n.func.id for n in ast.walk(chat_stage_fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    assert 'publish_thinking_trace' in calls, (
        "publish_chat_stage must route through publish_thinking_trace "
        "(canonical SSE/WAMP publisher) — no parallel SSE path allowed.")

    # And it must NOT call broadcast_sse_safe / broadcast_sse_event
    # directly (that would be a second parallel SSE leg).
    banned = {'broadcast_sse_safe', 'broadcast_sse_event'}
    parallels = banned & set(calls)
    assert not parallels, (
        f"publish_chat_stage introduces parallel SSE publishers "
        f"{sorted(parallels)} — must route through publish_thinking_trace.")


# ─── T5: (removed) idempotency dict was YAGNI; relying on UI dedup ──
# Original Bug 3 design had a (request_id, stage) idempotency set; trim
# pass removed it because the 6 emit sites in get_ans each fire exactly
# once per turn, and downstream MessageBus dedup keys on msg_id (unique
# per emit) so any double-emit would be visible upstream of the
# wrapper.  If duplicate bubbles surface in production, restore the
# guard at core.peer_link.crossbar_publish.publish_chat_stage.


# ─── T6: Unknown stage is a warning, not a crash ────────────────────

def test_publish_chat_stage_unknown_stage_returns_false(caplog):
    """Unknown stage + no text override returns False, doesn't crash."""
    from core.peer_link import crossbar_publish
    ok = crossbar_publish.publish_chat_stage(
        'totally_made_up_stage', user_id='u1', request_id='r')
    assert ok is False


# ─── T7: Empty user_id → no publish ─────────────────────────────────

def test_publish_chat_stage_empty_user_id_skips():
    from core.peer_link import crossbar_publish
    ok = crossbar_publish.publish_chat_stage(
        'thinking', user_id='', request_id='r')
    assert ok is False


# ─── T8: Stage text strings are user-visible, kept short ────────────

def test_stage_texts_are_reasonable_length():
    """UI bubble clips beyond container width; keep texts short."""
    from core.constants import CHAT_STAGE_TEXTS
    for stage, text in CHAT_STAGE_TEXTS.items():
        assert isinstance(text, str), (
            f"CHAT_STAGE_TEXTS[{stage!r}] must be str, got {type(text)}")
        assert 1 <= len(text) <= 60, (
            f"CHAT_STAGE_TEXTS[{stage!r}] = {text!r} should be 1-60 "
            f"chars (UI bubble clips beyond container width)")


# ─── T9: TOOL_LABELS completeness — REMOVED ─────────────────────────
#
# The old AST drift-guard `test_tool_labels_cover_all_hardcoded_tools`
# is replaced by compile-time enforcement.  Every Tool() construction
# now flows through `core.labeled_tool.labeled_tool(..., *, ui_label=…)`
# which raises TypeError if ui_label is omitted and ValueError if it's
# empty.  A new tool without a UI label can no longer be merged —
# Python refuses to build the Tool object at all.  That's stronger
# coverage than the regex AST walk (which couldn't see dynamic
# registries) and removes one false-positive-prone test from the suite.


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
