"""A curated home row survives refresh() because something READS `flagship`.

The home prompt offers the curator `"emphasis": <flagship|ranked|normal>`.
_home_curate maps flagship onto the row and _sanitize_home_payload validates and
forwards it, so the flag travelled the whole way to the client and then meant
nothing: `ranked` restyles every card in its row, `normal` is the absence of both,
and `flagship` was read by no one. test_home_producer.py pins that the flag is
SET; this pins that it DOES something.

What kept the "Flagship agents" row alive was accidental. _replaceRow matches on a
display title, so the row survived only by never colliding with 'Continue' or
'Recipes'. Retitling it, or a curated row of its own titled 'Continue', silently
handed it to the live dashboard.

The .mjs harness slices the REAL _replaceRow out of the shipped hartHome.js,
evaluates it in a vm, and asserts the observable rows after a replace, including
that a protected row is never ALSO shadowed by an appended duplicate. This wrapper
shells out so pytest/CI pick it up. Skips cleanly when node is absent.
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_home_row_protection.mjs')


def test_a_curated_row_is_protected_by_its_flag_not_by_its_title():
    """Drive the REAL _replaceRow and assert a flagship row keeps its own cards
    while an ordinary row is still replaced by the live fetch."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, 'row-protection harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout
