"""The home never fabricates the user's data — behavioural, via the REAL shell JS.

2026-07-24 real-HW: a just-installed node displayed "2,140 Spark - 3 agents -
41 tasks" plus a "Continue" row of invented half-finished work ("Trip to Goa 30%",
"Invoice chaser 80%", "Fix STT streaming 45%") and a "Top agents in the hive today"
row of fake network activity. None of it was the user's: the sample payload carried
those figures, fetchEarnings keeps the sample on 401/offline, fetchAgents returns
early when there are no agents (so the invented Continue cards were never
displaced), and the hive row has no fetch at all.

The .mjs harness slices the REAL samplePayload() out of the shipped hartHome.js,
evaluates it in a vm, and asserts the OBSERVABLE payload a fresh box paints —
zeros, empty rows — while pinning that the curated "Flagship agents" row (real
product agents with real prompts) survives. This wrapper shells out so pytest/CI
pick it up. Skips cleanly when node is absent.
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_hart_home_no_fabricated_data.mjs')


def test_home_sample_payload_is_honest_not_fabricated():
    """Drive the REAL samplePayload() and assert a fresh box shows 0 Spark /
    0 agents / 0 tasks and no invented work or hive activity."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, 'home-fabrication harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout
