"""Marketplace OFFLINE app logos (#143) — behavioural, via the REAL hartMarketplace.js.

Offline-art: a known Flathub app renders its BUNDLED same-origin logo <img> in
the .hac-ic tile (no network), with the Material glyph as the onerror fallback; a
non-Flathub (undotted) id skips straight to the glyph. The .mjs harness drives the
real hartRenderMarketplace on a DOM shim; this wrapper shells out so pytest/CI
pick it up. Skips cleanly when node is absent.
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_shell_marketplace_offline_art.mjs')


def test_marketplace_bundled_logo_with_glyph_fallback():
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, 'offline-art harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout
