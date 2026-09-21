"""An env pin must never grant the copilot the human revoked.

`copilot_enabled()` is the SINGLE gate that the `claude -p` spawn
(invoke_claude), `claude_code_available`, the MCP bridge and the agent daemon all
consult. The `hartos-copilot.off` marker is how a revoked `copilot_access`
consent reaches that gate (revoke_consent -> announce_revocation ->
ConsentService._copilot_switch_from_consent -> set_copilot_enabled(False)).

Before this, `HARTOS_COPILOT_ENABLED=1` was checked FIRST and returned early, so
it outranked the marker: one env var silently re-enabled all four readers against
the human's answer. An override may only ever be more restrictive.
"""
import os

import pytest

try:
    from integrations.coding_agent import claude_code_backend as ccb
except Exception:  # minimal env
    ccb = None

pytestmark = pytest.mark.skipif(ccb is None, reason="coding_agent not importable")


@pytest.fixture
def marker(tmp_path, monkeypatch):
    """Point the switch marker at a tmp path and yield a controller for it."""
    p = tmp_path / 'hartos-copilot.off'
    monkeypatch.setattr(ccb, '_copilot_switch_path', lambda: str(p))
    monkeypatch.delenv('HARTOS_COPILOT_ENABLED', raising=False)

    class M:
        def revoke(self):   # what set_copilot_enabled(False) does
            p.write_text('off', encoding='utf-8')

        def grant(self):
            if p.exists():
                p.unlink()
    return M()


def test_env_on_cannot_re_enable_a_revoked_copilot(marker, monkeypatch):
    """THE BUG: a revoked consent plus an ON pin used to return True."""
    marker.revoke()
    monkeypatch.setenv('HARTOS_COPILOT_ENABLED', '1')
    assert ccb.copilot_enabled() is False, (
        "an env pin re-enabled a copilot the human revoked")


@pytest.mark.parametrize('val', ['1', 'true', 'yes', 'on', 'TRUE', ' On '])
def test_no_on_value_can_override_the_marker(marker, monkeypatch, val):
    marker.revoke()
    monkeypatch.setenv('HARTOS_COPILOT_ENABLED', val)
    assert ccb.copilot_enabled() is False, f"{val!r} overrode the revocation"


@pytest.mark.parametrize('val', ['0', 'false', 'no', 'off', 'garbage'])
def test_a_non_on_pin_still_disables(marker, monkeypatch, val):
    """Unchanged behaviour: the restrictive direction still works."""
    marker.grant()
    monkeypatch.setenv('HARTOS_COPILOT_ENABLED', val)
    assert ccb.copilot_enabled() is False


def test_env_on_is_a_no_op_when_there_is_no_marker(marker, monkeypatch):
    """Why nothing is lost for headless installs: already enabled without it."""
    marker.grant()
    assert ccb.copilot_enabled() is True          # no env at all
    monkeypatch.setenv('HARTOS_COPILOT_ENABLED', '1')
    assert ccb.copilot_enabled() is True          # identical with the pin


def test_marker_alone_still_decides(marker):
    marker.grant()
    assert ccb.copilot_enabled() is True
    marker.revoke()
    assert ccb.copilot_enabled() is False


def test_the_env_is_not_consulted_before_the_marker_anymore():
    """Divergence guard: an early `return env in (...)` is what caused the hole.

    Pins the SHAPE, because the regression is re-introduced by reordering, not by
    changing the values.
    """
    import inspect
    src = inspect.getsource(ccb.copilot_enabled)
    assert 'return env in' not in src, (
        "copilot_enabled returns on the env before reading the marker again — "
        "that is exactly how an env pin outranked a revocation")
    assert '_copilot_switch_path' in src, "the marker must still be the decider"
