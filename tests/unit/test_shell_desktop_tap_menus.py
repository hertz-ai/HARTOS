"""Desktop TAP fix + right-click CONTEXT MENUS, behaviourally (no grep-tests).

The live bug: desktop icons launched only on a `dblclick`, which never fires on a
touchscreen tap, so a real device could not open anything. The fix wires a real
TAP (a quick, near-stationary pointerup) to launch on touch AND mouse, adds
right-click / long-press context menus for the icon, the desktop background, and
a window titlebar, and keeps everything draggable with a window raise on focus.

`test_desktop_tap_menus_behaviour` runs the REAL static modules
(hartContextMenu.js + hartDesktop.js) through their public surface on a tiny DOM
shim (test_shell_desktop_tap_menus.mjs) and asserts OBSERVABLE behaviour:

  * a quick press+release LAUNCHES (mouse and touch) -> openPanel(id);
  * a drag does NOT launch, it rearranges + persists (HartSession.set);
  * dblclick still launches (back-compat);
  * the icon / desktop / window menus offer the right actions and each routes to
    the existing helper (openPanel / closePanel / launch); the window menu raises
    the window first (multi-window focus);
  * HartCtxMenu activates on click + keyboard, closes on Escape / outside-click,
    and flips at the right/bottom screen edges.

Skips cleanly if node is absent (the harness needs no jsdom).

Local note: this box OOM-kills the full pytest import chain; the .mjs runs
standalone (`node tests/unit/test_shell_desktop_tap_menus.mjs`). Committed for CI
(node present there).
"""
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MJS = os.path.join(os.path.dirname(__file__), 'test_shell_desktop_tap_menus.mjs')
MJS_CUSTOMIZE = os.path.join(os.path.dirname(__file__), 'test_shell_icon_customize_preview.mjs')


def _run_harness(path):
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, path], capture_output=True, text=True, timeout=60)
    # Surface the harness' per-assertion log on failure so CI shows exactly which.
    assert r.returncode == 0, 'harness failed (' + os.path.basename(path) + '):\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout


def test_desktop_tap_menus_behaviour():
    """Drive the REAL tap + context-menu logic end-to-end (Node + DOM shim)."""
    _run_harness(MJS)


def test_icon_customize_preview_and_ctxmenu_fallthrough():
    """Regression: the customize-dialog preview must not null-deref .di-label (a
    dead, undismissable modal), and a right-click before the async-injected
    HartCtxMenu loads must fall through to the shell's own menu (not a no-op)."""
    _run_harness(MJS_CUSTOMIZE)


if __name__ == '__main__':
    test_desktop_tap_menus_behaviour()
    test_icon_customize_preview_and_ctxmenu_fallthrough()
    print('RESULT: ALL PASS')
