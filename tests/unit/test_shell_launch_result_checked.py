"""Parallel-path fix: ``/api/shell/launch`` did a fire-and-forget
``subprocess.Popen(['gtk-launch', app_id])`` that returned
``{'status': 'launched'}`` even when the launch failed (missing app, denied,
gtk-launch absent), bypassing the canonical RESULT-CHECKED
``app_bridge.launch_app``. It was also unauthenticated. Now it delegates to
``get_app_bridge().launch_app`` (real 500 on failure) and is gated with
``@_require_shell_auth``.
"""
import re
from pathlib import Path

_SRC = (Path(__file__).resolve().parents[2]
        / 'integrations' / 'agent_engine' / 'liquid_ui_service.py').read_text(encoding='utf-8')


def _shell_launch_block():
    m = re.search(
        r"@app\.route\('/api/shell/launch'.*?def shell_launch\(\):.*?(?=\n\s*@app\.route|\n\s*# ── Shell)",
        _SRC, re.DOTALL)
    assert m, "shell_launch route block not found"
    return m.group(0)


def test_launch_delegates_to_result_checked_bridge():
    src = _shell_launch_block()
    assert 'get_app_bridge().launch_app' in src, "must delegate to the result-checked launcher"
    assert 'gtk-launch' not in src, "the raw fire-and-forget gtk-launch Popen must be gone"
    assert "res.get('ok')" in src, "must check the launch result and 500 on failure"


def test_launch_route_is_authenticated():
    m = re.search(
        r"@app\.route\('/api/shell/launch'.*?\n\s*(@[\w_]+)\s*\n\s*def shell_launch",
        _SRC, re.DOTALL)
    assert m and '_require_shell_auth' in m.group(1), \
        "shell_launch must be gated by _require_shell_auth"
