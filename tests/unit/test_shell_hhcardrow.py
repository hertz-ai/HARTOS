"""Netflix image-card ROW helper hhCardRow() behaviourally (d4, no grep-tests).

R1 added `function hhCardRow(title, items, opts)` to the rendered desktop shell
(integrations/agent_engine/liquid_ui_service.py) as the SHARED row renderer the
panel surfaces (Installed-Apps registry, This-PC drives) use so content listings
paint as the SAME cinematic `.hh-card` rows the home does — one design language,
not a second card system.

`test_hhcardrow_behaviour` renders the REAL shell, slices out the inline
hhCardRow region, runs it on a bare vm context with the one collaborator
(window.HartBrandArt) stubbed (tests/unit/test_shell_hhcardrow.mjs), then calls
the real function and asserts the OBSERVABLE HTML a panel would inject:

  * a registry surface renders exactly one .hh-card per item, wearing the shared
    brand-art gradient tile + scrim vocabulary, keeping the Uninstall action
    (with stopPropagation);
  * a drive card carries an onclick -> openFilesAt(this.dataset.mount) button and
    a real usage progress bar, with the mount path on a data-attr (never inline);
  * an empty list degrades to a single empty-state card;
  * untrusted item text is HTML-escaped (no tag injection).

Skips cleanly if node is absent (this box has no node; CI does).
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_shell_hhcardrow.mjs')


def test_hhcardrow_behaviour():
    """Drive the REAL hhCardRow row renderer end-to-end (Node + vm)."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, 'harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout
