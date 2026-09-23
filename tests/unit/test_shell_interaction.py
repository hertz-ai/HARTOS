"""Pytest driver for tests/shell/test_interaction.mjs (desktop interaction stream).

The harness drives the REAL static modules
integrations/agent_engine/static/hartContextMenu.js and hartDesktop.js through
their public surface on a dependency-free DOM shim and asserts OBSERVABLE
behaviour, never a source string:

  * activation per surface (f2, steward #1466/#1467): a TOUCH single tap OPENS,
    a MOUSE single click only SELECTS, dblclick OPENS;
  * a context menu builds the right items per target (icon / desktop / window)
    and dismisses on Escape, outside-click and offline;
  * edge-flip / clamp keeps the menu fully on-screen at every viewport edge.

The .mjs stays under tests/shell/ (its documented home); this driver is what
lets it ride the flake-checks.yml shards, which discover only `test_*.py`.
Until now it ran in no CI job, which is how it went stale unnoticed: since
hartDesktop.js began rendering icon tiles through the shared
window.HartBrandArt (hartBrandArt.js), a realm that does not load
hartBrandArt.js first dies in renderGlyphTile with "Cannot read properties of
undefined (reading 'glyphTint')". The other hartDesktop harnesses
(test_shell_icon_customize.mjs line 126) already load it first. That one-line
fix belongs to the harness's current editor (S6); this driver reports the
truth until it lands. Skips cleanly when node is absent (CI installs node 20).
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), '..', 'shell', 'test_interaction.mjs')


def test_desktop_interaction_js():
    """Drive the REAL hartDesktop.js + hartContextMenu.js interaction flow (Node)."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, 'desktop interaction harness failed:\n' + r.stdout + r.stderr
    # The harness prints "RESULT: ALL <n> PASSED" with its assertion count.
    assert 'RESULT: ALL ' in r.stdout and ' PASSED' in r.stdout and 'FAILED' not in r.stdout, r.stdout


if __name__ == '__main__':
    test_desktop_interaction_js()
    print('RESULT: ALL PASS')
