"""Pytest wrapper for the connectivity-cluster in-flight-guard behavioural test.

The real assertions live in test_shell_connectivity_inflight.mjs, which drives the
REAL static module (integrations/agent_engine/static/hartConnectivity.js) through
its public surface on a tiny DOM shim and asserts the OBSERVABLE side-effect — how
many fetches are actually issued — for both guards:

  * refresh()      coalesces a second summary probe onto a pending one, and
                   releases the guard once the pending probe settles;
  * loadNetworks() does the same for the wifi scan.

This is the no-grep-test contract (CLAUDE.md Gate 5): a string check could not catch
a guard that mutates the flag in the wrong order or never releases. Skips cleanly
when node is absent (the harness runs on node 20 in CI).
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_shell_connectivity_inflight.mjs')


def test_connectivity_inflight_guards_js():
    """Drive the REAL hartConnectivity.js in-flight guards (Node + DOM shim)."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=60)
    # Surface the per-assertion log on failure so CI shows exactly which guard broke.
    assert r.returncode == 0, 'connectivity in-flight guard harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout


if __name__ == '__main__':
    test_connectivity_inflight_guards_js()
    print('RESULT: ALL PASS')
