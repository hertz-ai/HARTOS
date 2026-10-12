"""Nunba is the shell's chat (2026-10-12): pytest wrapper.

The real assertions live in test_shell_nunba_chat.mjs beside this file, which
renders the REAL shell through python, slices the inline Nunba-chat block and
drives it on the shared DOM shim (tests/unit/shell_dom_shim.mjs), asserting
OBSERVABLE behaviour:

  * a named app launches via openPanel and never reaches Nunba,
  * free-form intent opens the assistant panel and is posted into Nunba's
    composer as nunba:compose once Nunba's page says nunba:ready,
  * nunba:turn start/done drives the orb lamp (window._hartThinking),
  * foreign origins and other windows are ignored,
  * toggleAssistantChat closes/reopens; a fresh frame must announce again,
  * a page that never announces does not hang the orb.

Skips cleanly if node is absent.
"""
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
MJS = os.path.join(HERE, 'test_shell_nunba_chat.mjs')


def test_shell_nunba_chat_js():
    """Drive the REAL inline Nunba-chat block (Node, headless)."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=180)
    print(r.stdout)
    if r.stderr:
        print(r.stderr)
    assert r.returncode == 0, 'node harness failed:\n' + r.stdout + '\n' + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout
