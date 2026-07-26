"""Pytest wrapper for the typed Shell<->OS bridge SDK behavioural test (#133 / W3).

The real assertions live in test_os_bridge_sdk.mjs, which drives the REAL static
module (integrations/agent_engine/static/hartOSBridge.js) through its public surface
on a tiny sandbox with fetch MOCKED, and asserts the OBSERVABLE side-effects:

  * power.reboot() POSTs the typed op {domain:'power', op:'reboot'} to the ONE
    /api/os/invoke dispatcher and resolves only on the server's ok:true payload;
  * a DENIAL / failure REJECTS the promise with a real Error (the #133 client-side
    invariant — never a masked success), whether the HTTP status is 500 or a 200
    body carries ok:false;
  * capabilities() reads the caps endpoint and DEGRADES to {} on network failure
    (never throws), and contract() introspects the self-describing manifest.

This is the no-grep-test contract (CLAUDE.md Gate 5): a string check could not catch
an SDK that resolves on a denial instead of rejecting. Skips cleanly when node is
absent (the harness runs on node 20 in CI).
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_os_bridge_sdk.mjs')


def test_os_bridge_sdk_js():
    """Drive the REAL hartOSBridge.js SDK (Node + fetch mock)."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=60)
    # Surface the per-assertion log on failure so CI shows exactly which case broke.
    assert r.returncode == 0, 'os_bridge SDK harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout


if __name__ == '__main__':
    test_os_bridge_sdk_js()
    print('RESULT: ALL PASS')
