"""The shell's approval answer carries the signed-in Bearer (real shell JS, via
the node harness).  Skips when node is absent."""
import os
import shutil
import subprocess
import sys

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_shell_approval_sends_bearer.mjs')


def test_shell_approval_sends_the_bearer():
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    env = dict(os.environ, HART_TEST_PYTHON=sys.executable)
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=180, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout
