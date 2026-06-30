"""Behavioural guards for the post-boot DISPLAY-HEALTH snapshot probe.

The never-black tier ladder DEGRADES-GRACEFULLY and is proven in CI nixosTests,
but three holes can only be OBSERVED on real hardware (a VM has no real DRM
scanout / seat): the unbuilt first-SCANOUT marker (#131, the "black-but-healthy"
Tier-1), the unwritten input-alive marker (#134, "pointer frozen at 0,0"), and
the DRM-master EBUSY handoff. ``nixos/display-health/hart-display-health.sh`` is
the real-HW probe that snapshots an HONEST per-dimension verdict to
``/run/hart/display-health`` so an operator / the UI can read what actually came
up after a real boot.

This is a BEHAVIOURAL test, not a string-survival grep: it runs the REAL probe
script (the SAME file the NixOS module ships, via env-overridable paths) under a
real shell against fixtures and asserts the verdict file. The contract under test:

  * FAIL-SAFE / degrade-not-die: every read is guarded — a missing latch falls
    back to the cage FLOOR, a missing/garbage gpu verdict reads ``unknown``, and
    the unit ALWAYS exits 0 (never blocks/fails the boot) even when every marker
    is absent.
  * HONEST `unknown` (never a faked positive): the input (#134) + scanout (#131)
    markers are unbuilt, so their ABSENCE must read ``unknown`` — NOT ``dead`` /
    ``black`` (absence is ambiguous; only the in-session watchdog may drop on it).
    ``screen`` is ``alive`` ONLY on a confirmed first paint, else ``unknown``
    (never a black claim from a post-boot snapshot).
  * The bounded first-paint wait can never block (HART_DISPLAY_HEALTH_WAIT=0 is a
    pure snapshot; a present marker breaks the wait early).

The NixOS wiring (oneshot, after greetd, never before it, bounded TimeoutStartSec)
is proven by the display-tiers-neverblack-gpu-failsafe nixosTest in CI (it can't
run on the Windows dev box); here we prove the SCRIPT's classification logic.
"""

import os
import pathlib
import shutil
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "nixos" / "display-health" / "hart-display-health.sh"
_MODULE = _ROOT / "nixos" / "modules" / "hart-display-health.nix"
_FLAKE = _ROOT / "nixos" / "flake.nix"


def _shell():
    for name in ("bash", "sh", "dash"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _run(tmp_path, *, latch=None, gpu=None, ready=False, inp=False,
         scanout=False, wait="0", extra_env=None):
    """Run the REAL probe script against fixtures; return (rc, parsed_verdict).

    Each kwarg materialises (or omits) one marker/file in tmp_path; the probe's
    env-overridable paths point at them, so the shipped script is exercised
    verbatim. Returns the process rc and the parsed key=value verdict dict.
    """
    shell = _shell()
    if not shell:
        pytest.skip("no POSIX shell available to exercise the probe script")

    status = tmp_path / "display-health"
    latch_f = tmp_path / "session-tier"
    gpu_f = tmp_path / "gpu-render"
    ready_f = tmp_path / "shell-ready"
    input_f = tmp_path / "input-alive"
    scanout_f = tmp_path / "first-scanout"

    if latch is not None:
        latch_f.write_text(latch, encoding="utf-8")
    if gpu is not None:
        gpu_f.write_text(gpu, encoding="utf-8")
    if ready:
        ready_f.write_text("", encoding="utf-8")
    if inp:
        input_f.write_text("", encoding="utf-8")
    if scanout:
        scanout_f.write_text("", encoding="utf-8")

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HART_DISPLAY_HEALTH_FILE": str(status),
        "HART_LATCH_FILE": str(latch_f),
        "HART_GPU_RENDER_FILE": str(gpu_f),
        "HART_SHELL_READY_FLAG": str(ready_f),
        "HART_INPUT_ALIVE_FLAG": str(input_f),
        "HART_FIRST_SCANOUT_FLAG": str(scanout_f),
        "HART_DISPLAY_HEALTH_WAIT": wait,
    }
    if extra_env:
        env.update(extra_env)

    proc = subprocess.run(
        [shell, str(_SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    parsed = {}
    if status.exists():
        for line in status.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                parsed[k.strip()] = v.strip()
    return proc.returncode, parsed


# ─────────────────────────────────────────────────────────────────────────────
# 0. The script + its wiring exist.
# ─────────────────────────────────────────────────────────────────────────────

def test_script_and_module_exist():
    assert _SCRIPT.is_file(), "nixos/display-health/hart-display-health.sh is missing"
    assert _MODULE.is_file(), "nixos/modules/hart-display-health.nix is missing"


def test_module_is_imported_in_flake():
    flake = _FLAKE.read_text(encoding="utf-8")
    assert "./modules/hart-display-health.nix" in flake, (
        "hart-display-health.nix must be imported in nixos/flake.nix hartModules[] "
        "so the probe ships + the option exists.")


# ─────────────────────────────────────────────────────────────────────────────
# 1. FAIL-SAFE / degrade-not-die: everything absent -> floor verdict, exit 0.
# ─────────────────────────────────────────────────────────────────────────────

def test_all_markers_absent_is_failsafe_and_never_fails(tmp_path):
    rc, v = _run(tmp_path)  # no latch, no gpu, no markers
    assert rc == 0, "the probe must ALWAYS exit 0 (never block/fail the boot)"
    # Missing latch -> the cage FLOOR (never an unproven higher tier).
    assert v.get("tier") == "cage", f"absent latch must fail safe to cage: {v!r}"
    assert v.get("gpu") == "unknown", f"absent gpu verdict must be unknown: {v!r}"
    assert v.get("painted") == "no", f"absent paint marker -> painted=no: {v!r}"
    # #134 / #131: absence is UNKNOWN, never a false alarm.
    assert v.get("input") == "unknown", f"absent input marker -> unknown (not dead): {v!r}"
    assert v.get("scanout") == "unknown", f"absent scanout marker -> unknown (not black): {v!r}"
    assert v.get("screen") == "unknown", f"no confirmed paint -> screen unknown (never black): {v!r}"


def test_every_dimension_is_recorded(tmp_path):
    _, v = _run(tmp_path)
    for key in ("tier", "gpu", "painted", "input", "scanout", "screen"):
        assert key in v, f"the verdict must record the {key!r} dimension: {v!r}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. HEALTHY: all markers present -> the positive verdict (alive/live/confirmed).
# ─────────────────────────────────────────────────────────────────────────────

def test_fully_healthy_tier1(tmp_path):
    rc, v = _run(tmp_path, latch="hart-comp", gpu="hardware",
                 ready=True, inp=True, scanout=True)
    assert rc == 0
    assert v["tier"] == "hart-comp"
    assert v["gpu"] == "hardware"
    assert v["painted"] == "yes"
    assert v["input"] == "live"        # the marker is present -> proven live
    assert v["scanout"] == "confirmed"  # a real frame hit the screen
    assert v["screen"] == "alive"      # confirmed paint -> alive


# ─────────────────────────────────────────────────────────────────────────────
# 3. HONEST UNKNOWN: painted on the software floor but #131/#134 markers unbuilt.
#    This is the real-HW production state TODAY — the probe must NOT fake them.
# ─────────────────────────────────────────────────────────────────────────────

def test_painted_software_but_markers_unbuilt_reads_unknown(tmp_path):
    rc, v = _run(tmp_path, latch="sway", gpu="software", ready=True,
                 inp=False, scanout=False)
    assert rc == 0
    assert v["tier"] == "sway"
    assert v["gpu"] == "software"
    assert v["painted"] == "yes"
    # The compositor does not write these yet (#131/#134) -> honest unknown, NOT a
    # faked positive AND not a false "dead"/"black".
    assert v["input"] == "unknown", "an unbuilt input marker must read unknown, never a positive"
    assert v["scanout"] == "unknown", "an unbuilt scanout marker must read unknown, never a positive"
    # The screen DID paint (the shell-ready marker is present) -> alive, even though
    # scanout-confirmation is not yet wired (paint is the best signal we have today).
    assert v["screen"] == "alive"


# ─────────────────────────────────────────────────────────────────────────────
# 4. GARBAGE INPUTS fail safe (a torn latch / weird gpu verdict never crashes).
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("latch", ["garbage", "tier-9", "", "hart-comp\n\n", "  sway  "])
def test_latch_garbage_or_padded_falls_back_or_normalises(tmp_path, latch):
    _, v = _run(tmp_path, latch=latch)
    # Only the three valid tokens survive; anything else fails safe to cage. Padded
    # valid tokens normalise to the bare token (whitespace stripped).
    stripped = latch.strip()
    expected = stripped if stripped in ("hart-comp", "sway", "cage") else "cage"
    assert v["tier"] == expected, \
        f"latch {latch!r} must classify as {expected!r}, got {v.get('tier')!r}"


@pytest.mark.parametrize("gpu", ["weird", "", "HARDWARE-ish", "llvmpipe"])
def test_gpu_garbage_falls_back_to_unknown(tmp_path, gpu):
    _, v = _run(tmp_path, gpu=gpu)
    # Only the two valid verdicts survive; anything else is unknown (never a guess).
    expected = gpu.strip() if gpu.strip() in ("hardware", "software") else "unknown"
    assert v["gpu"] == expected, \
        f"gpu verdict {gpu!r} must classify as {expected!r}, got {v.get('gpu')!r}"


# ─────────────────────────────────────────────────────────────────────────────
# 5. The bounded first-paint wait NEVER blocks (the never-block-boot guarantee).
# ─────────────────────────────────────────────────────────────────────────────

def test_wait_zero_is_a_pure_snapshot(tmp_path):
    # WAIT=0 must not sleep at all — a pure snapshot. (Determinism for the unit.)
    import time
    t0 = time.monotonic()
    rc, v = _run(tmp_path, ready=False, wait="0")
    assert rc == 0
    assert (time.monotonic() - t0) < 5, "WAIT=0 must be a pure snapshot (no sleep)"
    assert v["painted"] == "no"


def test_wait_breaks_early_when_already_painted(tmp_path):
    # With the paint marker already present, even a positive WAIT must return
    # immediately (the loop breaks on the marker) — it never burns the full budget.
    import time
    t0 = time.monotonic()
    rc, v = _run(tmp_path, ready=True, wait="20")
    assert rc == 0
    assert (time.monotonic() - t0) < 10, \
        "a present paint marker must break the wait early (never burn the full budget)"
    assert v["painted"] == "yes"
    assert v["screen"] == "alive"
