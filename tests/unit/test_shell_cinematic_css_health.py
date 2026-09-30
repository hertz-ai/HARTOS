"""Pytest driver for tests/shell/test_cinematic_css.mjs (stylesheet artifact health).

The harness loads the REAL bytes of
integrations/agent_engine/static/hartResponsive.css through a comment- and
string-aware CSS reader and asserts on that reader's OBSERVABLE output:

  * braces balance, no unterminated comment or string (a packaging slip must
    surface as a verdict, not a blank shell);
  * the v3 cinematic layer is present (--hv-* tokens, the multi-radial bloom);
  * the brand SPECTRUM is woven, never a single teal wash;
  * a prefers-reduced-motion / html.a11y-rmotion hatch exists and stills motion;
  * no U+2014 em dash leaked into the product CSS.

Every check is re-exercised against a crafted boundary input with the opposite
verdict, so the assertions discriminate rather than pass vacuously.

The sibling tests/unit/test_stream_cinematic_css.py covers cascade OUTCOMES;
this covers the artifact. The .mjs stays under tests/shell/ (its documented
home, `node tests/shell/test_cinematic_css.mjs`); this driver is what lets it
ride the flake-checks.yml shards, which discover only `test_*.py`. Until now
it ran in no CI job. Skips cleanly when node is absent (CI installs node 20).
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), '..', 'shell', 'test_cinematic_css.mjs')


def test_cinematic_css_artifact_health_js():
    """Run the REAL hartResponsive.css through the validator harness (Node)."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, 'cinematic CSS health harness failed:\n' + r.stdout + r.stderr
    # The harness prints "RESULT: ALL <n> PASS" with its assertion count.
    assert 'RESULT: ALL ' in r.stdout and ' PASS' in r.stdout and 'FAILED' not in r.stdout, r.stdout


if __name__ == '__main__':
    test_cinematic_css_artifact_health_js()
    print('RESULT: ALL PASS')
