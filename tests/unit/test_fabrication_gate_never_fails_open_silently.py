"""The fabrication gate may fail open, but it may never do so SILENTLY.

THE DEFECT, measured live 2026-09-11 on the installed build (app PID 60744,
Tier-1 ACTIVE 19:38:43).  Two agents were walked to completion and their
per-action FAB-GUARD streams read in time order:

  agent 25214546249, 9 actions -> "[REUSE] All 9 actions completed" 20:13:26
    action 1..8  watermark -> [NOT RUN x1-2] -> re-steer -> RAN -> advance
    action 9     watermark 20:13:17 -> watermark for action 10 at 20:13:26
                 NO VERDICT LINE AT ALL
  agent 53803955119
    action 2     4x NOT RUN, no RAN verdict, advanced anyway
    action 5, 6  watermark -> watermark, 1-2s apart, NO VERDICT LINE

The retry machinery is healthy: 8 of 9 actions on the first agent ended with
`unrun=[]`, i.e. the named tool really ran before the pointer moved.  The
force/stuck-loop guard fired ZERO times in the window, so these are not
force-completions.  They are advances past a gate that never ran.

WHY IT IS INVISIBLE.  `_advance_reuse_action` wraps the whole gate in:

    try:
        _gc = get_registered_groupchat(user_prompt)
        if _gc is not None:
            ... _reuse_fabricated_tools(...) -> emits the verdict line ...
    except Exception as _fg_err:
        current_app.logger.debug(f"[FAB-GUARD] advance-gate skipped: {_fg_err}")

and then advances unconditionally.  That is TWO fail-open paths:

  1. `_gc is None` -- no else branch, NO log of any kind.  Undetectable.
  2. any exception -- logged at DEBUG.  Measured: gui_app.log contains ZERO
     lines at DEBUG level, so on the shipped configuration this message
     cannot appear.  "advance-gate skipped" appears 0 times in gui_app.log
     and server.log, which is consistent with BOTH "it never fired" and "it
     fired every time and was filtered out".  The log cannot distinguish
     them, which is exactly why the defect survived.

The verdict line itself is emitted inside `_reuse_fabricated_tools`
(reuse_recipe.py:5066, verdict at :5205), which is called from inside the
try -- so a missing verdict line proves the gate did not run, without saying
which of the two paths skipped it.

WHAT THIS GUARD REQUIRES, and deliberately what it does NOT.  It does not
require the gate to hold the advance -- failing open on a missing group chat
is a legitimate design choice (a permanent stall is worse).  It requires only
that BOTH fail-open paths announce themselves at a level the shipped app
actually logs, so an unverified advance is never mistaken for a verified one.
A safety gate that fails open in silence is the vacuous-guard shape recorded
in memory/feedback_vacuous_guards.md and the silent-swallow shape in
memory/feedback_second_error_masks_first.md.

RED BEFORE GREEN: against HEAD~ the except-branch calls `.debug(` and the
`_gc is None` case has no branch at all, so both tests below fail.

    python -m pytest tests/unit/test_fabrication_gate_never_fails_open_silently.py --noconftest -q
"""
import ast
import pathlib

import pytest

_SRC = (pathlib.Path(__file__).resolve().parents[2]
        / 'hartos' / 'reuse_recipe.py')
_FN = '_advance_reuse_action'
# Levels the shipped app actually emits.  gui_app.log carried 0 DEBUG lines
# across 12 rotated files on 2026-09-11, so `debug` is not observable.
_VISIBLE = ('info', 'warning', 'error', 'critical', 'exception')


@pytest.fixture(scope='module')
def fn():
    tree = ast.parse(_SRC.read_text(encoding='utf-8', errors='replace'))
    node = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == _FN), None)
    assert node is not None, '%s is gone from reuse_recipe.py' % _FN
    return node


def _log_level(call):
    """'warning' for logger.warning(...), else None."""
    f = call.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Attribute):
        if f.value.attr == 'logger':
            return f.attr
    return None


def test_the_exception_path_is_visible_in_the_shipped_log(fn):
    """An exception inside the gate must not be swallowed at DEBUG.

    Live 2026-09-11: zero DEBUG lines exist in gui_app.log, so this branch
    could fire on every advance and leave no trace at all.
    """
    offenders = []
    for handler in (h for n in ast.walk(fn)
                    if isinstance(n, ast.Try) for h in n.handlers):
        for inner in ast.walk(handler):
            if isinstance(inner, ast.Call):
                lvl = _log_level(inner)
                if lvl == 'debug':
                    for a in inner.args:
                        txt = getattr(a, 'value', '') if isinstance(a, ast.Constant) else ''
                        joined = ''.join(
                            getattr(v, 'value', '') for v in getattr(a, 'values', [])
                            if isinstance(v, ast.Constant))
                        if 'FAB-GUARD' in str(txt) + joined:
                            offenders.append(inner.lineno)
    assert not offenders, (
        'the fabrication gate swallows its exception at logger.debug (line(s) '
        '%s).  The shipped app logs nothing below INFO, so an advance whose '
        'evidence gate crashed is indistinguishable from one it passed. '
        'Use warning/error.' % offenders)


def test_the_missing_groupchat_path_announces_itself(fn):
    """`_gc is None` must not skip the gate in total silence.

    Measured: agent 25214546249 action 9 and 53803955119 actions 5/6
    advanced with no `[FAB-GUARD] action N names tool(s)` verdict line and
    no other trace, so nothing downstream can tell a tool-backed advance
    from an unverified one.
    """
    visible = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if _log_level(node) not in _VISIBLE:
            continue
        for a in node.args:
            joined = ''.join(
                getattr(v, 'value', '') for v in getattr(a, 'values', [])
                if isinstance(v, ast.Constant))
            txt = getattr(a, 'value', '') if isinstance(a, ast.Constant) else ''
            blob = str(txt) + joined
            if 'FAB-GUARD' in blob and ('no group chat' in blob.lower()
                                        or 'not registered' in blob.lower()
                                        or 'unverified' in blob.lower()):
                visible.append(node.lineno)
    assert visible, (
        '%s has no visible log for the `_gc is None` case.  The gate is '
        'skipped and the pointer advances with nothing recorded, which is '
        'how action 9 of agent 25214546249 reached "All 9 actions completed" '
        'with no verdict line. Emit a warning naming the action as '
        'UNVERIFIED.' % _FN)


def test_the_gate_still_fails_open(fn):
    """Guards the SHAPE of the fix: louder, not blocking.

    A permanent stall is worse than an unverified advance, and the existing
    loud-advance path (`_REUSE_FAB_STEER_MAX` spent) already encodes that
    trade.  This pins that neither fail-open branch was turned into an early
    `return None, False`, which would wedge every agent whose group chat is
    not registered.
    """
    for handler in (h for n in ast.walk(fn)
                    if isinstance(n, ast.Try) for h in n.handlers):
        for inner in ast.walk(handler):
            assert not isinstance(inner, ast.Return), (
                'the except branch now returns at line %d -- a gate that '
                'cannot run must not block the advance, only announce it '
                '(see the _REUSE_FAB_STEER_MAX loud-advance precedent).'
                % inner.lineno)
