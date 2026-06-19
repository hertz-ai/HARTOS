"""App Store (marketplace) polish — behavioural, via the REAL hartMarketplace.js.

LIVE-OS #22: after #20 made the store actually render, it "looked clumpy / not
top-notch". The polish (premium liquid-glass cards, a header, a sticky search,
category sections, vertical icon-over-name-over-Install cards) is structural, so
the .mjs harness drives the real hartRenderMarketplace on a DOM shim and asserts
the observable structure. This wrapper shells out so pytest/CI pick it up. Skips
cleanly when node is absent.

Paired with test_shell_marketplace.py, which guards the render-side wiring
(.hart-app-card / .hart-app-grid CSS + the App Store delegate + reused installer
routes) so neither side can drift.
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_shell_marketplace_polish.mjs')


def test_marketplace_premium_structure():
    """Drive the REAL hartMarketplace.js and assert the premium App Store layout
    (header + sticky search + category sections + vertical glass cards)."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, 'marketplace-polish harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout
