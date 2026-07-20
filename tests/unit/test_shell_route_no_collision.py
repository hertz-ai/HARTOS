"""Regression: the shell OS + system route modules must coexist on one Flask app.

Both register_shell_os_routes and register_shell_system_routes used to define a
view named ``shell_trash_list`` on ``/api/shell/trash``.  register_shell_os_routes
runs first (liquid_ui_service.py), so when register_shell_system_routes ran second
Flask raised "View function mapping is overwriting an existing endpoint function:
shell_trash_list" — aborting shell_system registration partway AND (because the
call sits in the same try-block) skipping register_app_install_routes entirely.
Net effect: the app installer + the later shell_system routes silently vanished.

The dup was removed from shell_os_apis (trash is shell_system's job).  These
assert the collision is gone and the installer survives.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _flask_app():
    try:
        from flask import Flask
    except Exception:
        pytest.skip("flask not available")
    return Flask(__name__)


def test_shell_os_and_system_coexist_without_trash_collision():
    try:
        from integrations.agent_engine.shell_os_apis import register_shell_os_routes
        from integrations.agent_engine.shell_system_apis import register_shell_system_routes
    except Exception as e:
        pytest.skip(f"shell api modules not importable: {e}")

    app = _flask_app()
    register_shell_os_routes(app)
    # This is the call that used to raise AssertionError on the duplicate
    # shell_trash_list endpoint — it must now complete cleanly.
    register_shell_system_routes(app)

    rules = {r.rule for r in app.url_map.iter_rules()}
    assert '/api/shell/trash' in rules, "canonical trash route missing"
    # /move is defined AFTER the trash GET in shell_system; its presence proves
    # register_shell_system_routes did NOT abort at the (former) collision point.
    assert '/api/shell/trash/move' in rules, "shell_system aborted before /move"

    # Exactly one endpoint owns shell_trash_list (no duplicate registration).
    trash_eps = [r.endpoint for r in app.url_map.iter_rules()
                 if r.endpoint.split('.')[-1] == 'shell_trash_list']
    assert len(trash_eps) == 1, f"expected one shell_trash_list, got {trash_eps}"


def test_app_installer_registers_after_shell_routes():
    """The real consequence: with the collision gone, the app-installer routes are
    contributed (not silently dropped by a swallowed exception) AND the Phase-8
    consolidation holds — register_shell_os_routes now DELEGATES the app surface to
    register_app_install_routes, so the canonical /api/shell/apps/* routes appear,
    and the second direct register_app_install_routes call (as liquid_ui still
    makes) is idempotent rather than a duplicate-endpoint collision."""
    try:
        from integrations.agent_engine.shell_os_apis import register_shell_os_routes
        from integrations.agent_engine.shell_system_apis import register_shell_system_routes
        from integrations.agent_engine.app_installer import register_app_install_routes
    except Exception as e:
        pytest.skip(f"modules not importable: {e}")

    app = _flask_app()
    register_shell_os_routes(app)        # now also delegates the app surface
    register_shell_system_routes(app)

    rules = {r.rule for r in app.url_map.iter_rules()}
    # Canonical app surface present via the delegation (the installer routes
    # survived — the original regression's real victim).
    assert '/api/shell/apps/install' in rules, "canonical app install route missing"
    assert '/api/shell/apps/search' in rules, "canonical app search route missing"
    # Legacy alias preserved so the marketplace frontend keeps working.
    assert '/api/apps/install' in rules, "legacy app install alias missing"

    # liquid_ui_service calls register_app_install_routes a SECOND time directly;
    # the idempotency latch must make that a no-op, never a collision (the bug the
    # original test guarded against — a swallowed exception aborting registration).
    before = len(list(app.url_map.iter_rules()))
    register_app_install_routes(app)     # must not raise (idempotent)
    after = len(list(app.url_map.iter_rules()))
    assert after == before, "second register_app_install_routes must be idempotent"
