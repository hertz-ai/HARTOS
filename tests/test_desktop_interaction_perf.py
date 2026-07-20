"""Behavioural guards for shell-JS interaction latency (independent of the GPU).

These execute the REAL hartDesktop.js under a tiny DOM shim (node only, no ISO,
no jsdom) and assert the two contracts a browser would enforce but static
checks cannot:

  * the desktop right-click menu is built + shown SYNCHRONOUSLY on the
    'contextmenu' event (no fetch/await/setTimeout gates the menu), and
  * the marquee (rubber-band) select reads each icon's rect ONCE at pointerdown
    and never calls getBoundingClientRect again per pointermove (no forced
    reflow on the move hot path) while still toggling .selected correctly.

The harness self-verifies against a reverted copy in review; here it is the
regression gate that keeps the interaction instant on the software-render path.
"""
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
HARTOS_ROOT = os.path.dirname(HERE)
HARNESS = os.path.join(HERE, 'js', 'desktop_interaction_harness.js')
DESKTOP_JS = os.path.join(
    HARTOS_ROOT, 'integrations', 'agent_engine', 'static', 'hartDesktop.js')


def _node():
    n = shutil.which('node')
    if not n:
        pytest.skip('node not available in this environment')
    return n


def test_desktop_interactions_are_instant():
    """Right-click builds the menu synchronously; marquee never reflows per move."""
    node = _node()
    r = subprocess.run([node, HARNESS, DESKTOP_JS],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and 'PASS' in r.stdout, (r.stdout + '\n' + r.stderr)
