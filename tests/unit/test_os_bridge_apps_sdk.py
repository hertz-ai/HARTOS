"""Pytest wrapper for the app-integration bridge SDK behavioural test (#117 / W3).

The real assertions live in test_os_bridge_apps_sdk.mjs, which drives the REAL static
module (integrations/agent_engine/static/hartOSBridge.js -> hartOS.apps) through its
public surface with fetch MOCKED, and asserts the OBSERVABLE side-effects:

  * apps.launch(appId, subsystem) POSTs the typed op {domain:'apps', op:'launch',
    params:{app_id, subsystem}} to the ONE /api/os/invoke dispatcher;
  * list/focus/close send the right typed op + params (window_id);
  * a failed launch REJECTS the promise with a real Error (the #133 client-side
    invariant — never a masked success).

Skips cleanly when node is absent (the harness runs on node 20 in CI).
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_os_bridge_apps_sdk.mjs')


def test_os_bridge_apps_sdk_js():
    """Drive the REAL hartOSBridge.js apps SDK (Node + fetch mock)."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, 'os_bridge apps SDK harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout


if __name__ == '__main__':
    test_os_bridge_apps_sdk_js()
    print('RESULT: ALL PASS')
