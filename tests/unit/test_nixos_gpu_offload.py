"""Structural + behavioural guards for hybrid PRIME render-offload (hart-gpu-offload).

The OS arms the NVIDIA discrete GPU for PRIME render-offload ONLY when a boot-time
presence probe proves it physically present, otherwise it degrades to the Intel
iGPU and then the software floor — never force-loading a driver on a box without
the hardware (#132) and never blocking/failing boot. This touches the just-
stabilised real-hardware boot path, so the SAFETY MODEL is the thing under test:

  * The presence probe is FAIL-SAFE = software: a missing/empty render verdict, no
    NVIDIA PCI device, an unloaded driver, or absent device nodes must all yield a
    NON-armed verdict (`software` / `intel`); `armed` is written ONLY when ALL of
    present-hardware + loaded-driver + device-nodes hold.
  * CO-ARM: the probe reads hart-gpu-probe's /run/hart/gpu-render first; if the base
    GL path is not proven `hardware`, the offload verdict degrades to `software` in
    lockstep (it can never outrank the render verdict).
  * #132: the base config NEVER sets services.xserver.videoDrivers = ["nvidia"]
    (the force-load trigger). The driver is shipped AVAILABLE via hart.nvidia
    (udev-autoload when present). The upstream hardware.nvidia.prime.offload arm
    lives ONLY inside an OPT-IN boot specialisation (default OFF).
  * DEGRADE-NOT-DIE: the wrapper exports the NVIDIA PRIME render-offload env ONLY
    when `armed`, and passes the app through UNCHANGED otherwise — a missing/absent
    dGPU can never block an app launch.

The classification + wrapper halves are BEHAVIOURAL tests, not string-survival
greps: they extract the actual shell the module ships and run it through a real
shell against faked /sys PCI fixtures + verdict files, asserting the verdict and
the env. The boot-wiring / #132 / never-brick halves are proven END-TO-END by the
booted-VM nixosTest nixos/tests/gpu-offload.nix (which a Windows dev box cannot
run); the source-shape guards here keep that wiring from silently regressing.
"""

import os
import pathlib
import re
import shutil
import subprocess

import pytest

_NIXOS = pathlib.Path(__file__).resolve().parents[2] / "nixos"
_MODULES = _NIXOS / "modules"
_MODULE = _MODULES / "hart-gpu-offload.nix"
_VMTEST = _NIXOS / "tests" / "gpu-offload.nix"

_VERDICT_PATH = "/run/hart/gpu-offload"
_RENDER_PATH = "/run/hart/gpu-render"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _shell():
    for name in ("bash", "sh", "dash"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _denix(body: str) -> str:
    """Convert an extracted Nix `''` string body into runnable shell. Two things:
      1. Resolve the Nix interpolations (${...}) Nix would substitute at build time —
         the test exercises the SHELL logic, so the store-path PATH is irrelevant and
         the default-path interps are overridden by the HART_GPU_OFFLOAD_* seams.
      2. Unescape `''${` (the Nix escape for a literal `${`) into a shell `${`.
    Order matters: the Nix interps (no `''` prefix) are resolved first so the
    `''${`->`${` pass cannot re-introduce them."""
    body = body.replace("${binPath}", "")  # store-path PATH prefix — irrelevant to the test
    body = body.replace("${renderFile}", _RENDER_PATH)
    body = body.replace("${verdictFile}", _VERDICT_PATH)
    return body.replace("''${", "${")


# ─────────────────────────────────────────────────────────────────────────────
# 1. The module exists and defines the option surface.
# ─────────────────────────────────────────────────────────────────────────────

def test_module_exists():
    assert _MODULE.is_file(), "nixos/modules/hart-gpu-offload.nix is missing"


def test_vmtest_exists():
    assert _VMTEST.is_file(), (
        "nixos/tests/gpu-offload.nix (the booted-VM #132 + never-brick proof) is missing")


def test_defines_enable_option_default_false():
    src = _read(_MODULE)
    m = re.search(r"offload\s*=\s*\{.*?enable\s*=\s*lib\.mkOption\s*\{(.*?)\};", src, re.S)
    assert m, "hart.gpu.offload.enable must be a lib.mkOption"
    body = m.group(1)
    assert re.search(r"type\s*=\s*lib\.types\.bool", body), \
        "hart.gpu.offload.enable must be lib.types.bool"
    assert re.search(r"default\s*=\s*false", body), (
        "hart.gpu.offload.enable must default FALSE so server/edge/phone never pull "
        "the nvidia closure (desktop.nix turns it on in the Wire phase)")


def test_extends_shared_hart_gpu_submodule():
    """The option must hang off hart.gpu.offload so it MERGES with hart-gpu-probe's
    hart.gpu.accelerate (no leaf collision)."""
    src = _read(_MODULE)
    assert "options.hart.gpu.offload" in src, \
        "the option must be declared under options.hart.gpu.offload (merges with hart.gpu.accelerate)"


def test_driver_channel_enum_default_production():
    src = _read(_MODULE)
    m = re.search(r"driverChannel\s*=\s*lib\.mkOption\s*\{(.*?)\};", src, re.S)
    assert m, "hart.gpu.offload.driverChannel must be a lib.mkOption"
    body = m.group(1)
    assert 'lib.types.enum [ "production" "new-feature" "open" ]' in body, \
        "driverChannel must be the production/new-feature/open enum"
    assert re.search(r'default\s*=\s*"production"', body), (
        "driverChannel must default to production (the closed driver — Maxwell GM108 "
        "940MX can't use the Turing+ open module)")


def test_specialisation_default_off():
    src = _read(_MODULE)
    m = re.search(r"specialisation\s*=\s*\{.*?enable\s*=\s*lib\.mkOption\s*\{(.*?)\};", src, re.S)
    assert m, "hart.gpu.offload.specialisation.enable must be a lib.mkOption"
    assert re.search(r"default\s*=\s*false", m.group(1)), (
        "the NixOS-native prime.offload specialisation must default OFF (it force-loads "
        "nvidia; kept out of the default ISO closure for #132 + ISO size)")


# ─────────────────────────────────────────────────────────────────────────────
# 2. The presence probe unit runs early, is non-fatal, and fails safe.
# ─────────────────────────────────────────────────────────────────────────────

def test_probe_unit_runs_early_before_greetd_and_is_nonfatal():
    src = _read(_MODULE)
    m = re.search(
        r"systemd\.services\.hart-gpu-offload-probe\s*=\s*\{(.*?)\n      \};", src, re.S)
    assert m, "hart-gpu-offload.nix must define systemd.services.hart-gpu-offload-probe"
    svc = m.group(1)
    assert 'wantedBy = [ "multi-user.target" ]' in svc, \
        "the probe must be wantedBy multi-user.target (always runs on a graphical boot)"
    assert re.search(r'before\s*=\s*\[[^\]]*"greetd\.service"', svc), \
        "the probe must run BEFORE greetd.service so the verdict is ready before any session reads it"
    assert re.search(r'after\s*=\s*\[[^\]]*"systemd-udev-settle\.service"', svc), \
        "the probe must run after systemd-udev-settle.service so nvidia nodes exist if loaded"
    assert re.search(r'after\s*=\s*\[[^\]]*"hart-gpu-probe\.service"', svc), \
        "the probe must run after hart-gpu-probe so /run/hart/gpu-render is on disk to co-arm with"
    assert re.search(r'Type\s*=\s*"oneshot"', svc), "the probe must be a oneshot"
    assert re.search(r"RemainAfterExit\s*=\s*true", svc), "the probe must RemainAfterExit=true"
    assert re.search(r'TimeoutStartSec\s*=\s*"\d+s"', svc), \
        "the probe must set a bounded TimeoutStartSec so a wedged probe can't wedge boot"


def test_probe_script_fail_safe_software_and_paths():
    src = _read(_MODULE)
    assert re.search(r"^\s*RESULT=software\s*$", src, re.M), (
        "the probe script must default RESULT=software (the fail-safe floor) — armed is "
        "written ONLY when present-hardware + loaded-driver + nodes all hold")
    assert _VERDICT_PATH in src, f"the probe must write its verdict to {_VERDICT_PATH}"
    assert _RENDER_PATH in src, (
        f"the probe must co-arm by reading hart-gpu-probe's {_RENDER_PATH}")
    assert re.search(r"^\s*set -u\s*$", src, re.M), (
        "the probe script must use `set -u` (not `set -e`) so a failing probe falls back "
        "to software instead of aborting the unit")
    assert re.search(r"\bexit 0\b", src), "the probe must always exit 0 (never fail the unit)"


# ─────────────────────────────────────────────────────────────────────────────
# 3. #132 — the base config never force-loads nvidia; the driver is shipped
#    AVAILABLE via hart.nvidia; videoDrivers only appears inside the specialisation.
# ─────────────────────────────────────────────────────────────────────────────

def test_driver_armed_via_hart_nvidia_not_duplicated():
    src = _read(_MODULE)
    assert re.search(r"hart\.nvidia\s*=\s*\{", src), (
        "the offload arm must reuse hart-nvidia.nix (hart.nvidia.enable) for the driver "
        "lifecycle — DRY, no duplicated driver/CUDA/persistence config")
    assert re.search(r"enable\s*=\s*true", src), "hart.nvidia.enable must be true"
    # Laptop offload: persistence OFF (let the dGPU runtime-suspend), CUDA OFF (ISO size).
    assert re.search(r"persistenceMode\s*=\s*lib\.mkDefault\s*false", src), (
        "persistenceMode must default false for a laptop offload (runtime-suspend, not warm-pinned)")
    assert re.search(r"cuda\.enable\s*=\s*lib\.mkDefault\s*false", src), (
        "cuda.enable must default false to keep the offload arm off the at-ceiling ISO closure")


def test_base_never_sets_videodrivers_only_the_specialisation_does():
    """#132: services.xserver.videoDrivers = ["nvidia"] force-loads nvidia and would
    re-brick a non-NVIDIA box booting the portable image. The ONE real assignment
    must live INSIDE the opt-in boot.specialisation, never the base config."""
    src = _read(_MODULE)
    # Match only the REAL assignment (services.xserver.videoDrivers = lib.mkForce ...),
    # not the prose mentions in comments / option descriptions.
    matches = [m.start() for m in re.finditer(
        r'services\.xserver\.videoDrivers\s*=\s*lib\.mkForce\s*\[\s*"nvidia"\s*\]', src)]
    assert len(matches) == 1, (
        f"there must be exactly one videoDrivers=[\"nvidia\"] assignment (the "
        f"specialisation's mkForce), found {len(matches)}")
    # Top-level `specialisation`, NOT `boot.specialisation`: the latter is not
    # a NixOS option at all — writing it broke the entire Release/ISO eval and
    # c1bb2213 corrected the module. This test kept grepping the wrong name
    # and reported the FIX as "the specialisation block is missing".
    spec_idx = src.find('specialisation."nvidia-offload"')
    assert spec_idx != -1, "the opt-in nvidia-offload specialisation block is missing"
    assert matches[0] > spec_idx, (
        "services.xserver.videoDrivers must live INSIDE the boot.specialisation block — "
        "the base generation must never force-load nvidia (#132)")


def test_specialisation_wires_prime_offload_and_command():
    src = _read(_MODULE)
    assert re.search(r"offload\.enable\s*=\s*true", src), \
        "the specialisation must enable hardware.nvidia.prime.offload"
    assert re.search(r"offload\.enableOffloadCmd\s*=\s*true", src), \
        "the specialisation must provide the upstream nvidia-offload command (enableOffloadCmd)"
    assert "intelBusId = offload.intelBusId" in src and "nvidiaBusId = offload.nvidiaBusId" in src, \
        "the specialisation must pass the configured PRIME bus ids"


def test_config_gated_on_enable():
    src = _read(_MODULE)
    assert re.search(r"config\s*=\s*lib\.mkIf\s*\(cfg\.enable\s*&&\s*offload\.enable\)", src), (
        "the whole config must be gated on (cfg.enable && offload.enable) so it is a pure "
        "no-op for server/edge/phone and any node that does not opt in")


# ─────────────────────────────────────────────────────────────────────────────
# 4. BEHAVIOURAL: run the REAL presence-probe shell against faked /sys fixtures.
# ─────────────────────────────────────────────────────────────────────────────

def _extract_probe() -> str:
    src = _read(_MODULE)
    m = re.search(r'writeShellScript "hart-gpu-offload-probe" \'\'(.*?)\'\';', src, re.S)
    assert m, "could not locate the hart-gpu-offload-probe script body"
    return _denix(m.group(1))


def _make_pci_device(root: pathlib.Path, slot: str, vendor: str, klass: str):
    d = root / slot
    d.mkdir(parents=True, exist_ok=True)
    (d / "vendor").write_text(vendor + "\n")
    (d / "class").write_text(klass + "\n")


def _run_probe(tmp_path, *, render, pci_devices, module_loaded, node_present):
    """Materialise a fixture tree + run the extracted probe; return the verdict."""
    shell = _shell()
    if not shell:
        pytest.skip("no POSIX shell available to exercise the probe")

    pci = tmp_path / "pci"
    pci.mkdir()
    for slot, vendor, klass in pci_devices:
        _make_pci_device(pci, slot, vendor, klass)

    module_mark = tmp_path / "sys_module_nvidia"
    if module_loaded:
        module_mark.mkdir()

    dev = tmp_path / "dev"
    dev.mkdir()
    if node_present:
        (dev / "nvidia0").write_text("")

    render_file = tmp_path / "gpu-render"
    if render is not None:
        render_file.write_text(render + "\n")

    verdict_file = tmp_path / "gpu-offload"

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HART_GPU_OFFLOAD_RENDER_FILE": str(render_file),
        "HART_GPU_OFFLOAD_VERDICT_FILE": str(verdict_file),
        "HART_GPU_OFFLOAD_PCI_DIR": str(pci),
        "HART_GPU_OFFLOAD_MODULE_MARK": str(module_mark),
        "HART_GPU_OFFLOAD_DEV_DIR": str(dev),
    }
    subprocess.run([shell, "-c", _extract_probe()], env=env,
                   capture_output=True, text=True, timeout=30)
    return verdict_file.read_text().strip() if verdict_file.exists() else ""


# Standard Optimus: Intel iGPU (VGA) + NVIDIA dGPU (3D controller, class 0x0302xx).
# The probe globs PCI_DIR/* and only reads each entry's vendor/class, so the slot
# DIR name is arbitrary — use colon-free names (Windows can't create '0000:01:00.0').
_INTEL = ("pci_intel_igpu", "0x8086", "0x030000")
_NVIDIA = ("pci_nvidia_dgpu", "0x10de", "0x030200")
# An NVIDIA device that is NOT a display controller (e.g. a USB-C controller) must
# never count as a render dGPU — vendor match alone is not enough.
_NVIDIA_NONDISPLAY = ("pci_nvidia_usb", "0x10de", "0x0c0330")


def test_probe_arms_when_present_loaded_and_noded(tmp_path):
    v = _run_probe(tmp_path, render="hardware", pci_devices=[_INTEL, _NVIDIA],
                   module_loaded=True, node_present=True)
    assert v == "armed", (
        "a present NVIDIA display GPU + loaded driver + device node, on a proven-hardware "
        f"render path, must arm offload, got {v!r}")


def test_probe_intel_when_no_nvidia_device(tmp_path):
    v = _run_probe(tmp_path, render="hardware", pci_devices=[_INTEL],
                   module_loaded=True, node_present=True)
    assert v == "intel", (
        f"no NVIDIA PCI device must keep the verdict on Intel (never armed), got {v!r}")


def test_probe_intel_when_driver_not_loaded(tmp_path):
    v = _run_probe(tmp_path, render="hardware", pci_devices=[_INTEL, _NVIDIA],
                   module_loaded=False, node_present=True)
    assert v == "intel", (
        "#132: the dGPU is present but its driver is NOT loaded (never force-loaded) — "
        f"must NOT arm, got {v!r}")


def test_probe_intel_when_no_device_node(tmp_path):
    v = _run_probe(tmp_path, render="hardware", pci_devices=[_INTEL, _NVIDIA],
                   module_loaded=True, node_present=False)
    assert v == "intel", (
        f"present + loaded but no /dev/nvidia0 node must NOT arm (no offload target), got {v!r}")


def test_probe_intel_when_nvidia_is_not_a_display_controller(tmp_path):
    v = _run_probe(tmp_path, render="hardware", pci_devices=[_INTEL, _NVIDIA_NONDISPLAY],
                   module_loaded=True, node_present=True)
    assert v == "intel", (
        "an NVIDIA non-display PCI function (vendor match, wrong class) must NOT arm a "
        f"render dGPU, got {v!r}")


def test_probe_software_when_render_not_hardware(tmp_path):
    # Co-arm degrade: even with a fully present+loaded+noded dGPU, a software render
    # verdict must drag the offload verdict to software (it can never outrank render).
    v = _run_probe(tmp_path, render="software", pci_devices=[_INTEL, _NVIDIA],
                   module_loaded=True, node_present=True)
    assert v == "software", (
        f"a software render verdict must degrade offload to software (co-arm), got {v!r}")


def test_probe_software_when_render_missing(tmp_path):
    v = _run_probe(tmp_path, render=None, pci_devices=[_INTEL, _NVIDIA],
                   module_loaded=True, node_present=True)
    assert v == "software", (
        f"a missing render verdict must fail safe to the software floor, got {v!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 5. BEHAVIOURAL: run the REAL offload wrapper against a faked verdict file.
# ─────────────────────────────────────────────────────────────────────────────

def _extract_wrapper() -> str:
    src = _read(_MODULE)
    m = re.search(r'writeShellScriptBin "hart-gpu-offload" \'\'(.*?)\'\';', src, re.S)
    assert m, "could not locate the hart-gpu-offload wrapper body"
    return _denix(m.group(1))


def _run_wrapper(tmp_path, verdict, args):
    shell = _shell()
    if not shell:
        pytest.skip("no POSIX shell available to exercise the wrapper")
    verdict_file = tmp_path / "gpu-offload"
    if verdict is not None:
        verdict_file.write_text(verdict + "\n")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HART_GPU_OFFLOAD_VERDICT_FILE": str(verdict_file),
    }
    out = subprocess.run(
        [shell, "-c", _extract_wrapper(), "hart-gpu-offload", *args],
        env=env, capture_output=True, text=True, timeout=30)
    return out


_ENV_PROBE = ['sh', '-c', 'echo "${__GLX_VENDOR_LIBRARY_NAME:-}|${__NV_PRIME_RENDER_OFFLOAD:-}"']


def test_wrapper_applies_offload_env_when_armed(tmp_path):
    out = _run_wrapper(tmp_path, "armed", _ENV_PROBE)
    assert out.stdout.strip() == "nvidia|1", (
        "an `armed` verdict must export the NVIDIA PRIME render-offload env so the wrapped "
        f"app runs on the dGPU, got {out.stdout.strip()!r}")


@pytest.mark.parametrize("verdict", ["intel", "software", None, ""])
def test_wrapper_passes_through_when_not_armed(tmp_path, verdict):
    out = _run_wrapper(tmp_path, verdict, _ENV_PROBE)
    assert out.stdout.strip() == "|", (
        "a not-armed / missing verdict must pass the app through UNCHANGED (no offload env) "
        f"— degrade-not-die at the launch boundary; verdict={verdict!r}, got {out.stdout.strip()!r}")


def test_wrapper_status_reports_verdict(tmp_path):
    out = _run_wrapper(tmp_path, "armed", ["--status"])
    assert out.stdout.strip() == "armed", \
        f"--status must print the verdict, got {out.stdout.strip()!r}"


def test_wrapper_status_defaults_software_when_no_verdict(tmp_path):
    out = _run_wrapper(tmp_path, None, ["--status"])
    assert out.stdout.strip() == "software", \
        f"--status with no verdict file must default to the software floor, got {out.stdout.strip()!r}"
