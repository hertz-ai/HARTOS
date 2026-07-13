"""G2 — agent-registered custom components render REAL UI on the client.

Audit gap G2: a component type an agent REGISTERED at runtime (register_component_type)
was accepted by the transport but rendered as a generic JSON dump on the client (the
stored `template` was never read). This wires it: agent_ui_update stamps the type's
render spec (ev._spec = {props, template}) onto the push, and renderAgentOverlay grows
one branch that renders from that spec — the template filled with the pushed props, else
the props as label/value rows — so a type an agent baked at runtime shows real UI.

The .mjs harness renders the shell with the project python, slices out _esc +
renderAgentOverlay, drives them on a tiny DOM shim, pushes a custom component, and
asserts the OBSERVABLE overlay HTML. This wrapper shells out so pytest/CI pick it up.
Skips cleanly when node is absent (the server-side stamp is covered locally by
tests/probes/test_os_pillars.py::test_p1_registered_custom_type_push_carries_its_render_spec).
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_shell_custom_render.mjs')


def test_custom_component_renders_from_its_spec_not_json_dump():
    """Drive the REAL renderAgentOverlay through Node + a DOM shim: a custom type with
    a template fills its {{props}}; a template-less custom type renders label/value
    rows; and a malicious prop value is escaped — never the generic JSON fallback."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=120)
    if 'RESULT: SKIP' in r.stdout:
        # node present but no python could render the shell (set HART_TEST_PYTHON to a
        # venv that can import LiquidUIService). Skip honestly — never a vacuous pass.
        pytest.skip('no python could render the shell for the JS harness:\n' + r.stdout)
    assert r.returncode == 0, 'custom-render harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout
