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
  * The GTK4 GSK renderer + the hart-comp compositor CO-ARM on the SAME verdict:
    GSK is ``vulkan`` (else ``cairo``) and hart-comp drops its force-software
    signals (so ``select_render_path`` reaches the GLES path) only when the probe
    proved the Intel iGPU good — both reading ``/run/hart/gpu-render``, so a
    hardware-Vulkan client is never paired with a software compositor.
  * The FLOOR must NOT depend on the probe: the cage Tier-3 floor
    (hart-liquid-ui.nix) keeps forcing software GL unconditionally, and the GTK4
    host's WebKit software forces stay on the build-time ``!preferHardwareGL`` gate
    (WebKit GPU is a separate, more conservative step). The pixman/cairo software
    paths remain the never-fail fallback under the armed tiers (degrade chain +
    the supervisor paint watchdog), so an armed boot that fails still lands on cage.

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

# eglinfo-style renderer lines. ONLY a verified INTEL iGPU renderer (Mesa Intel
# driver family: iris / crocus / i965 / "Intel") classifies as HARDWARE — the
# probe binds to the i915 render node and requires the Intel signature so a
# faulting/foreign GPU can never flip the verdict.
_HARDWARE_FIXTURES = [
    "OpenGL renderer string: Mesa Intel(R) UHD Graphics 620 (KBL GT2)",
    "OpenGL renderer string: Mesa Intel(R) Arc(tm) Graphics (MTL)",
    "OpenGL renderer string: Mesa Intel(R) Iris(R) Xe Graphics (iris)",
    "OpenGL renderer string: Mesa DRI Intel(R) HD Graphics 530 (crocus)",
]
# Everything else stays on the fail-safe SOFTWARE floor: software rasterizers
# (llvmpipe/softpipe/swrast), empty/missing eglinfo output, AND — by design — a
# NON-Intel hardware renderer (AMD/NVIDIA). The target Optimus laptop's dGPU is the
# GeForce that FAULTS (nouveau MMIO PRIVRING) and is blacklisted, so a non-Intel
# renderer line must NEVER arm hardware; a pure-AMD/NVIDIA box stays conservatively
# on the proven software floor until that vendor's path is itself proven.
_SOFTWARE_FIXTURES = [
    "OpenGL renderer string: llvmpipe (LLVM 17.0.6, 256 bits)",
    "OpenGL renderer string: softpipe",
    "OpenGL renderer string: Software Rasterizer",
    "Device: swrast",
    "OpenGL renderer string: AMD Radeon RX 6600 (radeonsi, navi23, LLVM 17)",
    "OpenGL renderer string: NVIDIA GeForce RTX 3060/PCIe/SSE2",
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
def test_classifier_accepts_intel_igpu_renderers(egl):
    shell = _shell()
    if not shell:
        pytest.skip("no POSIX shell available to exercise the classifier")
    assert _classify(shell, egl) == "hardware", (
        f"a verified Intel iGPU renderer line must classify as hardware: {egl!r}")


@pytest.mark.parametrize("egl", _SOFTWARE_FIXTURES)
def test_classifier_rejects_software_empty_and_non_intel(egl):
    shell = _shell()
    if not shell:
        pytest.skip("no POSIX shell available to exercise the classifier")
    assert _classify(shell, egl) == "software", (
        f"a software / empty / missing-tool / NON-Intel eglinfo output must "
        f"classify as software (the fail-safe floor — only the verified Intel "
        f"iGPU arms hardware): {egl!r}")


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
# 5. FLOOR INVARIANT: the cage Tier-3 floor stays software UNCONDITIONALLY (never
#    gated on the probe). hart-comp + the GTK4 GSK renderer now CO-ARM on the SAME
#    verdict (the GLES/vulkan path is THIS task), but the pixman/cairo software
#    paths remain the never-fail fallback under both (degrade chain + watchdog).
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


def test_gtk4_host_gsk_gates_vulkan_but_never_uses_gl():
    host = _read(_HOST)
    # Part 2 (the GTK4 GSK-GL layer-shell hang): the host now GATES GSK_RENDERER on the
    # boot probe — VULKAN when the GPU is proven, CAIRO otherwise — but it must NEVER use
    # the GL renderer (the documented hang). The safety: GDK_GL=disable is UNCONDITIONAL,
    # so even if Vulkan init fails GSK falls back to cairo, never to the hanging GL; a
    # GSK-vulkan hang on the layer-shell surface still drops to the cage GTK3 floor.
    lhm = re.search(
        r'layerShellHost = pkgs\.writeShellScriptBin "hart-glass-shell-gtk4" \'\'(.*?)\'\';',
        host, re.S)
    assert lhm, "could not locate the GTK4 layerShellHost env block"
    layer_host = lhm.group(1)
    # GDK_GL=disable must be UNCONDITIONAL (its own line, outside any if/optionalString) —
    # GL is the hang path and must never be reachable as a Vulkan-failure fallback.
    assert re.search(r'^\s*export GDK_GL=disable\s*$', layer_host, re.M), (
        "the GTK4 host must export GDK_GL=disable UNCONDITIONALLY — GL is the hang path "
        "and must never be reachable, so a Vulkan-init failure falls back to cairo, not GL.")
    # The only GSK renderers it may ever select are vulkan (probed-good GPU) + cairo
    # (floor / Vulkan fallback) — NEVER gl/opengl.
    assert "GSK_RENDERER=vulkan" in layer_host and "GSK_RENDERER=cairo" in layer_host, (
        "the GTK4 host must select GSK vulkan (probed GPU) or cairo (floor).")
    assert "GSK_RENDERER=gl" not in layer_host and "GSK_RENDERER=opengl" not in layer_host, (
        "the GTK4 host must NEVER select the GL/opengl GSK renderer (the layer-shell hang).")
    # The vulkan-vs-cairo choice is gated on the boot probe verdict.
    assert "/run/hart/gpu-render" in layer_host, (
        "the GTK4 host's GSK vulkan-vs-cairo choice must be gated on the boot probe "
        "(/run/hart/gpu-render).")
    # WebKit forces stay gated on !preferHardwareGL (NOT the probe) — WebKit GPU is a
    # separate later step, not part of the GSK fix.
    assert re.search(
        r'optionalString \(!ui\.preferHardwareGL\) "export WEBKIT_DISABLE_DMABUF_RENDERER=1',
        host), (
        "the GTK4 host must keep the WEBKIT_DISABLE_* forces on !preferHardwareGL "
        "(WebKit GPU is a separate later step, not part of the GSK fix).")


def test_hart_comp_arms_gles_on_probe_verdict_with_gated_force_software():
    """hart-comp now ARMS its GLES path from the probe verdict (the GLES path is
    THIS task, no longer 'separate'), co-armed with the GSK shell on the SAME
    /run/hart/gpu-render. THE FORCE-SOFTWARE GOTCHA FIX is the load-bearing part:
    main.rs::BootConfig::from_args treats WLR_RENDERER_ALLOW_SOFTWARE /
    LIBGL_ALWAYS_SOFTWARE / HART_COMP_FORCE_SOFTWARE as FORCE-software signals, so
    they MUST be gated on the runtime !armed decision (else exporting them
    unconditionally re-pins the compositor to pixman regardless of the verdict and
    the GLES path can never build). pixman stays the never-fail fallback (udev.rs
    keeps it on any GLES fault; the supervisor watchdog drops to cage on a
    first-paint failure)."""
    comp = _read(_COMP)
    # The AUTO arm reads the SAME verdict file the GSK shell + the shell effects read.
    assert "/run/hart/gpu-render" in comp, (
        "hart-comp.nix must read /run/hart/gpu-render to AUTO-arm the GLES path "
        "(co-armed with the GSK shell renderer on the same verdict).")
    assert "_HART_ARMED" in comp, (
        "hart-comp.nix must compute a runtime arm decision (_HART_ARMED).")
    # preferHardwareGL stays the operator override (the launcher branches on it).
    assert re.search(r"if \(ui\.preferHardwareGL or false\) then", comp), (
        "preferHardwareGL must remain the operator-override branch of the arm decision.")
    # THE GOTCHA FIX: every force-software signal is INSIDE the !armed gate, never
    # exported unconditionally before it.
    gate = comp.find('if [ "$_HART_ARMED" != "1" ]; then')
    assert gate != -1, (
        "hart-comp.nix must gate the force-software signals on `if "
        '[ "$_HART_ARMED" != "1" ]` (the not-armed branch).')
    for sig in ("export WLR_RENDERER_ALLOW_SOFTWARE=1",
                "export LIBGL_ALWAYS_SOFTWARE=1",
                "export HART_COMP_FORCE_SOFTWARE=1",
                'HART_COMP_FORCE_SW_FLAG="--force-software"'):
        idx = comp.find(sig)
        assert idx > gate, (
            f"{sig!r} must be INSIDE the not-armed gate — main.rs treats it as a "
            "force-software signal, so exporting it unconditionally re-pins the "
            "compositor to pixman regardless of the probe verdict (the gotcha).")
    # WLR_NO_HARDWARE_CURSORS stays unconditional (software cursors are always safe;
    # it is NOT a force-software signal).
    assert "export WLR_NO_HARDWARE_CURSORS=1" in comp
    # And the launch passes the GATED flag (empty when armed), not an unconditional
    # --force-software.
    assert "--backend drm $HART_COMP_FORCE_SW_FLAG" in comp, (
        "the hart-comp launch must pass the gated $HART_COMP_FORCE_SW_FLAG "
        "(empty when armed) instead of an unconditional --force-software.")
