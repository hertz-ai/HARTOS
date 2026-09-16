"""FAB-GUARD must say WHICH session it is talking about.

MEASURED LIVE 2026-09-09 20:47:49. The marker is emitted as

    [FAB-GUARD] action 5 names tool(s) ['execute_windows_or_android_command'];
    executed=['execute_windows_or_android_command']; unrun=[]

with no session anywhere on the line, while every agent AND every background
daemon on this box writes into the same server.log. So the single strongest
per-action evidence signal in the reuse pipeline is UNATTRIBUTABLE: you cannot
tell whose action 5 that was.

What that cost, measured the same evening. A serial REUSE walk scored
88764372848 as

    PARTIAL  reached=[1] tools-ran=[4] of 5

which is self-contradictory -- action 4's tool cannot run without action 4
being reached. `reached` comes from "Retrieved current_action_id: N for
session: <user>_<id>" and IS session-qualified; `tools-ran` came from
FAB-GUARD and was not, so it swept up a different agent's line. Both of that
run's per-agent verdicts had to be withdrawn.

This is the codebase's own documented lesson re-appearing at a new marker --
reuse_recipe.py:2655-2667 already session-qualifies its sibling markers, and
`Retrieved current_action_id: ... for session: ...` is the established idiom.
The fix is to follow it here, not to invent a new one.

    python -m pytest tests/unit/test_fab_guard_is_session_qualified.py --noconftest -q
"""
import os
import re

_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'hartos', 'reuse_recipe.py')


def _source():
    with open(_SRC, 'r', encoding='utf-8', errors='replace') as f:
        return f.read().splitlines()


def _emit_window(anchor, lines_after=6):
    """The source window for one FAB-GUARD emit.

    A window, not a contiguous search: these log messages are built from
    implicitly-concatenated f-string literals, so the assembled sentence
    NEVER appears contiguously in the .py file. Matching a window is the
    only thing that stays true when the literals are re-wrapped.
    """
    src = _source()
    # Anchor on the f-STRING, not the bare text: reuse_recipe.py quotes real
    # captured log lines inside its docstrings (e.g. :5482 "20:24:07
    # [FAB-GUARD] action 2 names [...]"), and a bare-text matcher picks those
    # prose copies up and can never go green. The f" prefix marks an emit.
    hits = [i for i, ln in enumerate(src) if anchor in ln]
    assert hits, (
        f"could not find a FAB-GUARD emit containing {anchor!r} in "
        f"reuse_recipe.py -- re-point this test rather than deleting it")
    return ['\n'.join(src[i:i + lines_after]) for i in hits]


class TestTheActionVerdictLine:
    """`[FAB-GUARD] action N names tool(s) ...` -- the line the walk reads."""

    def test_it_names_the_session(self):
        for w in _emit_window('f"[FAB-GUARD] action {'):
            assert 'user_prompt' in w, (
                "the FAB-GUARD action line does not interpolate user_prompt, "
                "so every agent's action-N lines are indistinguishable in a "
                "shared log. Live 20:47:49 this made tools-ran=[4] get "
                "attributed to an agent that had only reached action 1.")

    def test_it_uses_the_existing_for_session_idiom(self):
        """Do not invent new vocabulary -- 'for session:' already means this."""
        for w in _emit_window('f"[FAB-GUARD] action {'):
            assert 'for session:' in w, (
                "use the codebase's existing 'for session: {user_prompt}' "
                "suffix, the same one 'Retrieved current_action_id' uses, so "
                "one grep attributes every marker")


class TestTheWatermarkLine:
    """The sibling marker has the same defect and the same fix."""

    def test_watermark_names_the_session(self):
        for w in _emit_window('f"[FAB-GUARD] watermark for action '):
            assert 'for session:' in w, (
                "the watermark line is also global; it establishes the "
                "pre-existing-tool-call baseline an action is judged against")


class TestTheSignalStillCarriesItsPayload:
    """Attribution must be ADDED, not swapped in for the evidence."""

    def test_action_line_still_reports_executed_and_unrun(self):
        for w in _emit_window('f"[FAB-GUARD] action {'):
            assert 'executed=' in w and 'unrun=' in w, (
                "executed=/unrun= are what prove a tool ran; adding the "
                "session must not displace them")
