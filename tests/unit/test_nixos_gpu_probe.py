"""Structural + behavioural guards for the GPU smoke-test gate (hart-gpu-probe).

The OS now DEFAULTS to GPU rendering on the opt-in upper tiers WHEN a boot-time
smoke test (``hart-gpu-probe.nix``) proves the GPU can create a GL context and
report a HARDWARE renderer; otherwise it forces the software floor. This touches
the just-stabilised real-hardware boot path, so the SAFETY MODEL is the thing
under test:

  * The probe is FAIL-SAFE = software: disabled / empty / software-renderer /
    missing-tool output must all yield ``software`` (the floor); ``hardware`` is
    written ONLY on a positive hardware-renderer match.
  * The Tier-2 sway launcher (hart-layer-shell-host.nix) must gate
    ``LIBGL_ALWAYS_SOFTWARE`` on ``/run/hart/gpu-render == hardware`` — i.e. it
    only stops forcing software when the probe proved the GPU good.
  * The FLOOR must NOT depend on the probe: the cage Tier-3 floor
    (hart-liquid-ui.nix) keeps forcing software GL, the GTK4 host keeps
    ``GSK_RENDERER=cairo`` + the WebKit software forces, and hart-comp keeps its
    pixman renderer — none of these may be gated on ``/run/hart/gpu-render``.

The renderer-classification half is a BEHAVIOURAL test, not a string-survival
grep: it extracts the actual ``grep`` pipeline the module uses and runs it
through a real shell against hardware + software ``eglinfo`` fixtures, asserting
the verdict. The wiring/floor halves are source-shape guards (acceptable here —
a Nix module cannot be imported/executed on the Windows dev box; the behavioural
proof of the boot wiring is the session-supervisor + layer-shell-host nixosTests
in CI, which can't run on Windows).
"""

import pathlib
import re
import shutil
import subprocess

import pytest

_NIXOS = pathlib.Path(__file__).resolve().parents[2] / "nixos"
_MODULES = _NIXOS / "modules"
_PROBE = _MODULES / "hart-gpu-probe.nix"
_HOST = _MODULES / "hart-layer-shell-host.nix"
_CAGE = _MODULES / "hart-liquid-ui.nix"
_COMP = _MODULES / "hart-comp.nix"
_FLAKE = _NIXOS / "flake.nix"

_VERDICT_PATH = "/run/hart/gpu-render"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# 1. The module exists, is imported, and defines the option + early oneshot.
# ─────────────────────────────────────────────────────────────────────────────

def test_module_exists():
    assert _PROBE.is_file(), "nixos/modules/hart-gpu-probe.nix is missing"


def test_module_is_imported_in_flake():
    flake = _read(_FLAKE)
    assert "./modules/hart-gpu-probe.nix" in flake, (
        "hart-gpu-probe.nix must be added to the nixos/flake.nix hartModules[] "
        "import list (like its sibling hart-*.nix modules) — otherwise the "
        "hart.gpu option never exists and the probe unit never ships."
    )


def test_defines_accelerate_option_default_true():
    src = _read(_PROBE)
    # hart.gpu.accelerate must exist, be a bool, and default true (default-to-
    # hardware-when-proven; false is the operator force-software override).
    m = re.search(
        r"accelerate\s*=\s*lib\.mkOption\s*\{(.*?)\};", src, re.S)
    assert m, "hart.gpu.accelerate must be a lib.mkOption"
    body = m.group(1)
    assert re.search(r"type\s*=\s*lib\.types\.bool", body), (
        "hart.gpu.accelerate must be lib.types.bool")
    assert re.search(r"default\s*=\s*true", body), (
        "hart.gpu.accelerate must default to true (default-to-hardware-when-proven)")


def test_probe_runs_early_before_greetd_and_is_nonfatal():
    src = _read(_PROBE)
    m = re.search(
        r"systemd\.services\.hart-gpu-probe\s*=\s*\{(.*?)\n    \};", src, re.S)
    assert m, "hart-gpu-probe.nix must define systemd.services.hart-gpu-probe"
    svc = m.group(1)
    # EARLY: wanted by multi-user, ordered BEFORE greetd, AFTER udev settles so
    # the DRM render nodes exist when eglinfo runs.
    assert 'wantedBy = [ "multi-user.target" ]' in svc, (
        "the probe must be wantedBy multi-user.target (always runs on a graphical boot)")
    assert re.search(r'before\s*=\s*\[[^\]]*"greetd\.service"', svc), (
        "the probe must run BEFORE greetd.service so the verdict is on disk before "
        "any session launcher reads /run/hart/gpu-render")
    assert re.search(r'after\s*=\s*\[[^\]]*"systemd-udev-settle\.service"', svc), (
        "the probe must run after systemd-udev-settle.service so DRM nodes exist")
    # NON-FATAL: oneshot + RemainAfterExit + a bounded start timeout so it can
    # never block or fail the boot.
    assert re.search(r'Type\s*=\s*"oneshot"', svc), "the probe must be a oneshot"
    assert re.search(r"RemainAfterExit\s*=\s*true", svc), (
        "the probe must RemainAfterExit=true so it never re-runs / blocks")
    assert re.search(r'TimeoutStartSec\s*=\s*"\d+s"', svc), (
        "the probe must set a bounded TimeoutStartSec so a wedged probe can't wedge boot")


# ─────────────────────────────────────────────────────────────────────────────
# 2. FAIL-SAFE: default is `software`; accelerate=false always writes `software`.
# ─────────────────────────────────────────────────────────────────────────────

def test_probe_defaults_to_software_floor():
    src = _read(_PROBE)
    # The script's default verdict (before any probe) must be the floor.
    assert re.search(r"^\s*RESULT=software\s*$", src, re.M), (
        "the probe script must default RESULT=software (the fail-safe floor) — "
        "hardware is written ONLY on a positive hardware-renderer match")
    # It must write the verdict to the canonical /run/hart/gpu-render path.
    assert _VERDICT_PATH in src, (
        f"the probe must write its verdict to {_VERDICT_PATH}")
    # set -u (NOT set -e): a probe failure must fall back to software, not abort.
    assert re.search(r"^\s*set -u\s*$", src, re.M), (
        "the probe script must use `set -u` (not `set -e`) so a failing probe "
        "falls back to software instead of aborting the unit")


def test_accelerate_false_forces_software():
    """When hart.gpu.accelerate = false the probe must ALWAYS write software
    (the operator force-the-floor override) — the eglinfo branch is gated on the
    ACCELERATE flag, so with it 0 the renderer is never even consulted."""
    src = _read(_PROBE)
    # The accelerate flag is materialised from the option, and the hardware
    # detection lives behind `if [ "$ACCELERATE" = "1" ]`.
    assert re.search(
        r'ACCELERATE="\$\{if gpu\.accelerate then "1" else "0"\}"', src), (
        "the probe must materialise hart.gpu.accelerate into an ACCELERATE flag")
    assert re.search(r'if \[ "\$ACCELERATE" = "1" \]', src), (
        "the eglinfo hardware probe must be gated on ACCELERATE=1 so "
        "hart.gpu.accelerate=false forces the software floor without probing")


# ─────────────────────────────────────────────────────────────────────────────
# 3. BEHAVIOURAL: the renderer-classification grep pipeline itself.
#    Extract the real pipeline from the module and run it against fixtures.
# ─────────────────────────────────────────────────────────────────────────────

# eglinfo-style renderer lines: the first three are HARDWARE, the rest SOFTWARE.
_HARDWARE_FIXTURES = [
    "OpenGL renderer string: Mesa Intel(R) UHD Graphics 620 (KBL GT2)",
    "OpenGL renderer string: AMD Radeon RX 6600 (radeonsi, navi23, LLVM 17)",
    "OpenGL renderer string: NVIDIA GeForce RTX 3060/PCIe/SSE2",
]
_SOFTWARE_FIXTURES = [
    "OpenGL renderer string: llvmpipe (LLVM 17.0.6, 256 bits)",
    "OpenGL renderer string: softpipe",
    "OpenGL renderer string: Software Rasterizer",
    "Device: swrast",
    "",  # empty output (eglinfo missing / failed / timed out)
    "/bin/sh: eglinfo: command not found",
]


def _extract_classifier() -> str:
    """Pull the exact `printf ... | grep ...` hardware-classification pipeline
    out of the module so the test exercises the REAL logic, not a copy."""
    src = _read(_PROBE)
    # The classifier is the `if printf '%s' "$OUT" | grep ... ; then RESULT=hardware`.
    m = re.search(
        r"if (printf '%s' \"\$OUT\" \| grep .*?); then\s*\n\s*RESULT=hardware",
        src, re.S)
    assert m, "could not locate the hardware-renderer classification pipeline"
    # Normalise the Nix-source line continuations (`\` + newline + indent) into a
    # single shell condition the test shell can evaluate.
    cond = m.group(1)
    cond = re.sub(r"\\\s*\n\s*", " ", cond)
    return cond


def _classify(shell: str, egl_output: str) -> str:
    """Run the extracted classifier under a real shell; return hardware|software."""
    cond = _extract_classifier()
    script = (
        "RESULT=software\n"
        'OUT="$EGL"\n'
        f"if {cond}; then RESULT=hardware; fi\n"
        'printf "%s" "$RESULT"\n'
    )
    out = subprocess.run(
        [shell, "-c", script],
        env={"EGL": egl_output, "PATH": _os_path()},
        capture_output=True, text=True, timeout=30,
    )
    return out.stdout.strip()


def _os_path() -> str:
    import os
    return os.environ.get("PATH", "")


def _shell():
    for name in ("bash", "sh", "dash"):
        p = shutil.which(name)
        if p:
            return p
    return None


@pytest.mark.parametrize("egl", _HARDWARE_FIXTURES)
def test_classifier_accepts_hardware_renderers(egl):
    shell = _shell()
    if not shell:
        pytest.skip("no POSIX shell available to exercise the classifier")
    assert _classify(shell, egl) == "hardware", (
        f"a hardware renderer line must classify as hardware: {egl!r}")


@pytest.mark.parametrize("egl", _SOFTWARE_FIXTURES)
def test_classifier_rejects_software_and_empty(egl):
    shell = _shell()
    if not shell:
        pytest.skip("no POSIX shell available to exercise the classifier")
    assert _classify(shell, egl) == "software", (
        f"a software / empty / missing-tool eglinfo output must classify as "
        f"software (the fail-safe floor): {egl!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 4. CONSUMER: the Tier-2 sway launcher gates LIBGL_ALWAYS_SOFTWARE on the probe.
# ─────────────────────────────────────────────────────────────────────────────

def test_sway_launcher_gates_software_gl_on_probe_verdict():
    host = _read(_HOST)
    # Isolate the GTK4-layer-shell SESSION launcher (the sway one), not the GTK4
    # HOST env block (which keeps its own unconditional GSK/WebKit forces).
    m = re.search(
        r'sessionLauncher = pkgs\.writeShellScriptBin '
        r'"hart-glass-shell-gtk4-session" \'\'(.*?)\'\';',
        host, re.S)
    assert m, "could not locate the hart-glass-shell-gtk4-session launcher"
    launcher = m.group(1)
    # It must read the probe verdict and force software ONLY when != hardware.
    assert re.search(
        r'cat /run/hart/gpu-render.*!=\s*"hardware".*\n\s*export LIBGL_ALWAYS_SOFTWARE=1',
        launcher, re.S), (
        "the Tier-2 sway launcher must gate `export LIBGL_ALWAYS_SOFTWARE=1` on "
        '`[ "$(cat /run/hart/gpu-render ...)" != "hardware" ]` — it must keep '
        "forcing software GL UNLESS the boot probe proved the GPU good.")
    # WLR_RENDERER_ALLOW_SOFTWARE + WLR_NO_HARDWARE_CURSORS stay UNCONDITIONAL
    # (they only PERMIT the pixman fallback / fix cursors — never gated).
    assert re.search(r"^\s*export WLR_RENDERER_ALLOW_SOFTWARE=1", launcher, re.M), (
        "WLR_RENDERER_ALLOW_SOFTWARE=1 must stay exported unconditionally "
        "(it only permits the software fallback; the GPU that lies still drops to cage)")
    assert re.search(r"^\s*export WLR_NO_HARDWARE_CURSORS=1", launcher, re.M), (
        "WLR_NO_HARDWARE_CURSORS=1 must stay exported unconditionally")
    # And the LIBGL force must NOT be unconditional any more (it was a build-time
    # optionalString before; it must now be inside the runtime `if`).
    assert not re.search(
        r'optionalString\s*\(!ui\.preferHardwareGL\)\s*"export LIBGL_ALWAYS_SOFTWARE=1"',
        launcher), (
        "the sway launcher must no longer force LIBGL_ALWAYS_SOFTWARE via a "
        "build-time optionalString — it must gate on the runtime probe verdict.")


def test_sway_launcher_preserves_preferHardwareGL_optin():
    """If the operator set hart.liquidUI.preferHardwareGL = true that is an
    explicit hardware opt-in — the launcher must NEVER force software in that
    branch (the probe gate is only the !preferHardwareGL else-branch)."""
    host = _read(_HOST)
    m = re.search(
        r'sessionLauncher = pkgs\.writeShellScriptBin '
        r'"hart-glass-shell-gtk4-session" \'\'(.*?)\'\';',
        host, re.S)
    assert m
    launcher = m.group(1)
    # The launcher branches on preferHardwareGL; the probe-gated LIBGL force lives
    # only in the else (software-by-default) branch.
    assert re.search(r"if ui\.preferHardwareGL then", launcher), (
        "the launcher must branch on ui.preferHardwareGL so the explicit "
        "hardware opt-in never enters the software-forcing path")


# ─────────────────────────────────────────────────────────────────────────────
# 5. FLOOR INVARIANT: cage + GTK4 GSK cairo + hart-comp pixman are NOT gated.
# ─────────────────────────────────────────────────────────────────────────────

def test_cage_floor_still_forces_software_unconditionally():
    cage = _read(_CAGE)
    # The cage kioskLauncher must keep forcing software GL via the build-time
    # optionalString (NOT a runtime probe gate) — it is the never-fail floor and
    # must NEVER depend on the GPU smoke test.
    m = re.search(
        r'kioskLauncher = pkgs\.writeShellScriptBin "hart-shell-session" \'\'(.*?)\'\';',
        cage, re.S)
    assert m, "could not locate the cage kioskLauncher"
    launcher = m.group(1)
    assert re.search(
        r'optionalString\s*\(!ui\.preferHardwareGL\)\s*"export LIBGL_ALWAYS_SOFTWARE=1"',
        launcher), (
        "the cage Tier-3 floor must KEEP forcing LIBGL_ALWAYS_SOFTWARE via the "
        "build-time optionalString — the floor is the never-fail backstop and "
        "must NOT be gated on /run/hart/gpu-render.")
    # And it must NOT have been wired to the probe verdict.
    assert "/run/hart/gpu-render" not in launcher, (
        "the cage floor launcher must NOT read /run/hart/gpu-render — the floor "
        "is software unconditionally so a GPU that lies still has a backstop.")


def test_cage_module_not_gated_on_probe_anywhere():
    cage = _read(_CAGE)
    assert "/run/hart/gpu-render" not in cage, (
        "hart-liquid-ui.nix (the cage floor) must not reference the GPU probe "
        "verdict anywhere — the floor stays 100% software.")


def test_gtk4_host_keeps_gsk_cairo_and_webkit_software_forces_ungated():
    host = _read(_HOST)
    # The GTK4 HOST env (layerShellHost, distinct from the sessionLauncher) keeps
    # GSK_RENDERER=cairo + GDK_GL=disable + the WEBKIT_DISABLE_* forces gated only
    # on !preferHardwareGL — NOT on the probe verdict (the documented GSK GL hang
    # fix; a separate task addresses that hang).
    assert re.search(
        r'optionalString \(!ui\.preferHardwareGL\) "export GSK_RENDERER=cairo',
        host), (
        "the GTK4 host must keep GSK_RENDERER=cairo forced on !preferHardwareGL "
        "(the GSK GL layer-shell hang workaround) — it must NOT be gated on the probe.")
    assert re.search(
        r'optionalString \(!ui\.preferHardwareGL\) "export WEBKIT_DISABLE_DMABUF_RENDERER=1',
        host), (
        "the GTK4 host must keep the WEBKIT_DISABLE_* forces on !preferHardwareGL "
        "— they must NOT be gated on the probe verdict.")
    # The GSK/WebKit host forces must NOT have been re-gated on the probe file.
    # (Find the layerShellHost block specifically and assert the probe path is
    # absent from its GSK/WebKit lines.)
    lhm = re.search(
        r'layerShellHost = pkgs\.writeShellScriptBin "hart-glass-shell-gtk4" \'\'(.*?)\'\';',
        host, re.S)
    assert lhm, "could not locate the GTK4 layerShellHost env block"
    layer_host = lhm.group(1)
    # The host env block sets GSK_RENDERER but must not gate it on the probe file.
    assert "GSK_RENDERER=cairo" in layer_host
    assert "/run/hart/gpu-render" not in layer_host, (
        "the GTK4 host env (GSK_RENDERER=cairo / WEBKIT_DISABLE_* / GDK_GL) must "
        "NOT be gated on /run/hart/gpu-render — only the Tier-2 sway LAUNCHER's "
        "LIBGL force is. The GSK cairo + WebKit forces are the documented hang "
        "workaround and stay exactly as-is.")


def test_hart_comp_pixman_renderer_not_gated_on_probe():
    comp = _read(_COMP)
    assert "/run/hart/gpu-render" not in comp, (
        "hart-comp.nix must NOT be gated on the GPU probe verdict — hart-comp "
        "uses the mandatory pixman software renderer (a GLES path is a separate "
        "task); the probe must not touch it.")
