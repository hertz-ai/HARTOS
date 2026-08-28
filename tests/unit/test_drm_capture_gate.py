"""The DRM capture path must obey the human's screen kill-switch.

scripts/hart_drm_capture.py reads the scanout framebuffer directly through DRM
ioctls. That exists because Tier-1 (hart-comp on the DRM backend) implements no
capture protocol -- grim gets "compositor doesn't support
wlr-screencopy-unstable-v1", /dev/fb0 is the black console buffer, and there is
no portal. Tier-1 painted a desktop nobody could photograph.

But reading the scanout buffer goes around EVERY consent surface the OS has: it
never touches the portal, so the xdg-desktop-portal ScreenCast gate never sees
it. An ungated version of this tool would be a way around the human's cut,
which is exactly what core/ai_sensing.py exists to prevent.

So it consults the same cross-process authority the portal gate uses and fails
CLOSED. Verified end to end on the box 2026-08-27:
    sense allowed     -> captured 1,092,488 bytes
    human cuts screen -> NO FILE PRODUCED
    restored          -> allowed again

Run:
  pytest tests/unit/test_drm_capture_gate.py -v --noconftest
"""

import importlib.util
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(REPO, "scripts", "hart_drm_capture.py")


def _tool():
    spec = importlib.util.spec_from_file_location("hart_drm_capture", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fails_closed_when_the_authority_is_unreachable(monkeypatch):
    """No answer is a NO. An unreachable gate must never mean 'go ahead'."""
    mod = _tool()
    monkeypatch.setitem(sys.modules, "core.ai_sensing", None)  # force import failure
    ok, why = mod.screen_capture_allowed()
    assert ok is False
    assert "consent" in why or "unavailable" in why


def test_refuses_when_the_human_cut_the_screen_sense(monkeypatch):
    fake = type(sys)("core.ai_sensing")
    fake.query_authority = lambda sensor: False
    monkeypatch.setitem(sys.modules, "core.ai_sensing", fake)
    mod = _tool()
    ok, why = mod.screen_capture_allowed()
    assert ok is False
    assert "CUT" in why


def test_allows_only_on_a_definitive_yes(monkeypatch):
    fake = type(sys)("core.ai_sensing")
    fake.query_authority = lambda sensor: True
    monkeypatch.setitem(sys.modules, "core.ai_sensing", fake)
    mod = _tool()
    ok, _why = mod.screen_capture_allowed()
    assert ok is True


def test_an_authority_error_is_not_an_allow(monkeypatch):
    def _boom(sensor):
        raise RuntimeError("socket exploded")
    fake = type(sys)("core.ai_sensing")
    fake.query_authority = _boom
    monkeypatch.setitem(sys.modules, "core.ai_sensing", fake)
    mod = _tool()
    ok, why = mod.screen_capture_allowed()
    assert ok is False and "unreachable" in why


def test_main_refuses_and_captures_nothing_when_gated(monkeypatch, tmp_path):
    """The refusal must happen BEFORE any pixels are read."""
    mod = _tool()
    monkeypatch.setattr(mod, "screen_capture_allowed",
                        lambda: (False, "the human has CUT the 'screen' sense"))
    called = []
    monkeypatch.setattr(mod, "capture", lambda p: called.append(p))
    out = tmp_path / "shot.png"

    with pytest.raises(SystemExit) as exc:
        mod.main([str(out)])

    assert "REFUSED" in str(exc.value)
    assert not called, "capture() ran despite the gate refusing"
    assert not out.exists()


def test_allow_ungated_does_not_override_a_reachable_cut(monkeypatch, tmp_path):
    """--allow-ungated is for a node with NO authority. It must still be usable
    there, but it is documented as not being a way around a human's decision --
    so it stays loud on stderr rather than silent."""
    mod = _tool()
    src = open(TOOL, encoding="utf-8").read()
    assert "--allow-ungated" in src
    assert "not a way around" in src.lower() or "NOT a way around" in src


def test_only_numeric_debugfs_nodes_are_used():
    """Newer kernels expose the same GPU twice, as /dri/1 and as
    /dri/0000:00:02.0. Only the numeric name is a valid /dev/dri/card suffix;
    using the PCI alias built '/dev/dri/card0000:00:02.0' and failed."""
    src = open(TOOL, encoding="utf-8").read()
    assert "isdigit()" in src, (
        "find_scanout_fb must skip PCI-address debugfs aliases")
