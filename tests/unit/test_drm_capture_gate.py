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
    """A definitive cut refuses, and is NAMED as a cut so nothing downstream
    can treat it as "we could not ask" and wave it through."""
    _authority(monkeypatch, 'cut')
    mod = _tool()
    ok, why = mod.screen_capture_allowed()
    assert ok is False
    assert why == mod.CUT_BY_HUMAN


def test_an_older_ai_sensing_still_refuses_but_does_not_claim_a_cut(monkeypatch):
    """BACKWARD COMPATIBILITY, and an honesty rule.

    An ai_sensing carrying only the boolean `query_authority` cannot tell a
    human's cut from a dead socket — that ambiguity is the whole defect. Against
    such a module the capture must still REFUSE (fail-closed is unchanged), but
    it must NOT report the refusal as a human's cut: doing so would block
    --allow-ungated on exactly the bare bring-up node that flag exists for.
    """
    fake = type(sys)("core.ai_sensing")
    fake.query_authority = lambda sensor: False      # no query_authority_state
    monkeypatch.setitem(sys.modules, "core.ai_sensing", fake)
    mod = _tool()
    ok, why = mod.screen_capture_allowed()
    assert ok is False, "fail-closed must survive an older ai_sensing"
    assert why != mod.CUT_BY_HUMAN, (
        "a boolean-only gate cannot know it was a cut; claiming so would "
        "wrongly block the no-authority bring-up path")


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


def _authority(monkeypatch, state):
    """Install a fake ai_sensing whose gate answers exactly `state`."""
    fake = type(sys)("core.ai_sensing")
    fake.SENSE_ALLOW = 'allow'
    fake.SENSE_CUT = 'cut'
    fake.SENSE_UNREACHABLE = 'unreachable'
    fake.query_authority_state = lambda sensor, *a, **kw: state
    fake.query_authority = lambda sensor, *a, **kw: state == 'allow'
    monkeypatch.setitem(sys.modules, "core.ai_sensing", fake)


def _record_capture(mod, monkeypatch, seen):
    """Stand in for the DRM read so main() can be driven off Linux, and so a
    capture that should NOT have happened is observable rather than inferred."""
    def fake_capture(path):
        seen.append(path)
        return (8, 8, mod.Scanout(1, '1', 'hart-comp', 'XR24', 'little', 0,
                                  'test'), 99, 0.5)
    monkeypatch.setattr(mod, "capture", fake_capture)


def test_a_reachable_cut_is_distinguishable_from_a_dead_authority(monkeypatch):
    """The root defect. `query_authority` returns False for BOTH, so the caller
    could not tell a human's NO from a socket that never answered -- and the
    escape hatch below therefore could not honour its own promise."""
    mod = _tool()
    _authority(monkeypatch, 'cut')
    ok, why = mod.screen_capture_allowed()
    assert ok is False and why == mod.CUT_BY_HUMAN

    _authority(monkeypatch, 'unreachable')
    ok, why = mod.screen_capture_allowed()
    assert ok is False and why != mod.CUT_BY_HUMAN, (
        "an unreachable authority must not be reported as a human's cut")


def test_allow_ungated_cannot_override_a_reachable_cut(monkeypatch, tmp_path):
    """THE BUG THIS FILE USED TO HIDE.

    The module docstring promises "--allow-ungated is NOT a way around a
    human's cut: a REACHABLE authority reporting 'screen' cut still refuses".
    The old test asserted that SENTENCE was present in the source file. The
    code did the opposite: `if not allow_ungated:` bypassed every refusal.

    This is the promise as behaviour -- no pixels read, no file written, flag
    or no flag. It FAILS against the code as it was written.
    """
    mod = _tool()
    _authority(monkeypatch, 'cut')
    seen = []
    _record_capture(mod, monkeypatch, seen)
    out = tmp_path / "shot.png"

    with pytest.raises(SystemExit) as exc:
        mod.main([str(out), "--allow-ungated"])

    assert "REFUSED" in str(exc.value)
    assert not seen, "--allow-ungated ran a capture the human had CUT"
    assert not out.exists()


def test_allow_ungated_still_works_on_a_node_with_no_authority(
        monkeypatch, tmp_path, capsys):
    """The case the flag exists for must keep working, or the fix above would
    have broken bring-up on a bare node. It must also SAY SO on stderr."""
    mod = _tool()
    _authority(monkeypatch, 'unreachable')
    seen = []
    _record_capture(mod, monkeypatch, seen)
    out = tmp_path / "shot.png"

    assert mod.main([str(out), "--allow-ungated"]) == 0
    assert seen == [str(out)], "the ungated capture did not run"
    assert "UNGATED" in capsys.readouterr().err, (
        "an ungated capture must announce itself, never proceed silently")


def test_no_flag_and_no_authority_still_refuses(monkeypatch, tmp_path):
    """Fail-closed stays the default: no flag, no answer, no capture."""
    mod = _tool()
    _authority(monkeypatch, 'unreachable')
    seen = []
    _record_capture(mod, monkeypatch, seen)

    with pytest.raises(SystemExit) as exc:
        mod.main([str(tmp_path / "shot.png")])

    assert "REFUSED" in str(exc.value)
    assert not seen


def test_an_allowed_sense_captures(monkeypatch, tmp_path):
    """The positive path, so the gate cannot be 'fixed' by refusing always."""
    mod = _tool()
    _authority(monkeypatch, 'allow')
    seen = []
    _record_capture(mod, monkeypatch, seen)
    assert mod.main([str(tmp_path / "shot.png")]) == 0
    assert seen == [str(tmp_path / "shot.png")]
