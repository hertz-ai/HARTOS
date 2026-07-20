"""Pytest wrapper for the unified navigation core behavioural test (#169).

The real assertions live in test_hart_nav.mjs, which drives the REAL static
module (integrations/agent_engine/static/hartNav.js) headlessly — it loads the
file in a realm with a ``window`` but NO ``document`` (so only the pure core
evaluates, the DOM wiring self-skips) and asserts the OBSERVABLE navigation
contract:

  * history push / back / forward with the canBack/canForward guards;
  * a new navigation from the middle truncates the forward (redo) tail;
  * re-navigating to the current id is a reuse no-op (no duplicate entry);
  * remove() (a closed panel) drops its entries and keeps the pointer valid;
  * decideOpen(): reuse an open panel, create a first open, and mint a NEW
    instance id (base#N) under {newInstance:true}.

Per CLAUDE.md Gate 5 (no-grep-tests): a string check could not catch a core that
fails to truncate the redo tail or that mints a colliding instance id — this runs
the real logic. Skips cleanly when node is absent (CI runs node 20).
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_hart_nav.mjs')


def test_hart_nav_core_js():
    """Drive the REAL hartNav.js pure core (Node, headless)."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, 'hartNav core harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout


if __name__ == '__main__':
    test_hart_nav_core_js()
    print('RESULT: ALL PASS')
