"""Source-shape guards for the DRM-master ownership kernel params (hart-kernel.nix).

WHY (real-HW 2026-07-15, the slow software-rendered desktop RCA): on the Optimus
laptop the compositor hart-comp inits EGL+GLES2 on the Intel i915 node (card1),
arms hardware GLES, then logs "Unable to become drm master, assuming unprivileged
mode" and falls to the pixman software floor. Root cause: the AI-compute kernel
params shipped BOTH ``nvidia-drm.modeset=1`` AND ``nvidia-drm.fbdev=1``. fbdev=1
makes the nvidia DRM driver claim the framebuffer console (fbcon) — so on a hybrid
box where the Intel iGPU drives the panel and NVIDIA is offload-only, the nvidia
node contends to be the seat-primary DRM master and hart-comp (on the Intel card)
cannot become master → software scanout.

THE FIX (this guard locks it in): keep ``nvidia-drm.modeset=1`` (the nvidia RENDER
node for PRIME offload) but flip to ``nvidia-drm.fbdev=0`` so the Intel i915 owns
the boot/console framebuffer + seat-primary and hands DRM master cleanly to
hart-comp for GLES scanout. NVIDIA stays a pure render node, never the display.

NEVER-BRICK invariants this guard also protects (so a refactor can't regress them):
  * i915 stays force-loaded (the #99-103 Intel-panel path).
  * nvidia is NEVER added to boot.kernelModules (the #132 never-force-load rule) —
    the param is inert on a box whose nvidia module never loads.

Source-shape guard (NOT a behavioural test): a NixOS module cannot be evaluated or
booted on the Windows dev box, so the behavioural proof is the real-HW boot journal
(hart-comp taking DRM master on the iGPU) via the dev loop, canary + rollback
protecting the OTA push. This guard just prevents a silent regression of the exact
param values in the committed module.
"""

import pathlib
import re

_KERNEL = pathlib.Path(__file__).resolve().parents[2] / "nixos" / "modules" / "hart-kernel.nix"


def _read() -> str:
    return _KERNEL.read_text(encoding="utf-8")


def test_module_exists():
    assert _KERNEL.is_file(), "nixos/modules/hart-kernel.nix is missing"


def test_nvidia_drm_modeset_stays_on():
    """modeset=1 is REQUIRED for the nvidia render node (PRIME offload) — it must
    stay ON. It does not make nvidia the display; fbdev does."""
    src = _read()
    assert '"nvidia-drm.modeset=1"' in src, (
        "nvidia-drm.modeset=1 must stay ON — it enables KMS on the nvidia render "
        "node so PRIME render-offload works. Removing it breaks hart-gpu-offload.")


def test_nvidia_drm_fbdev_is_off_not_on():
    """THE FIX: fbdev=0, never fbdev=1. fbdev=1 makes nvidia claim the console
    framebuffer and contend for DRM master on a hybrid box, starving the Intel
    compositor of master → the software-rendered desktop."""
    src = _read()
    assert '"nvidia-drm.fbdev=0"' in src, (
        "nvidia-drm.fbdev must be 0 so the Intel i915 owns the boot/console "
        "framebuffer + seat-primary and hands DRM master cleanly to hart-comp — "
        "the fix for hart-comp 'Unable to become drm master' → pixman software floor.")
    assert '"nvidia-drm.fbdev=1"' not in src, (
        "nvidia-drm.fbdev=1 must NOT be present — it lets the nvidia DRM node claim "
        "the framebuffer console and contend for DRM master, the root cause of the "
        "compositor falling to the software floor on the Optimus laptop.")


def test_i915_stays_force_loaded_panel_path():
    """#99-103: the Intel iGPU drives the panel, so i915 must stay force-loaded in
    boot.kernelModules (deterministic KMS at boot)."""
    src = _read()
    # i915 must appear as a force-loaded kernel module (in a boot.kernelModules list).
    assert re.search(r'"i915"', src), (
        "i915 must stay force-loaded (boot.kernelModules) — it drives the panel on "
        "the Intel iGPU (the #99-103 panel path).")


def test_nvidia_never_force_loaded():
    """#132: nvidia is NEVER force-loaded via boot.kernelModules — it is udev-
    autoloaded only when the PCI device is present, so an image booted on a box
    without the dGPU never fails systemd-modules-load and the fbdev param is inert."""
    src = _read()
    # No boot.kernelModules entry may force-load the proprietary nvidia modules.
    # (They may appear only in comments / udev-permission rules / the DRM params.)
    for mod in ('"nvidia"', '"nvidia_drm"', '"nvidia_uvm"', '"nvidia_modeset"'):
        # A force-load would be a bare module name inside a boot.kernelModules list.
        assert not re.search(rf'boot\.kernelModules\s*=\s*\[[^\]]*{re.escape(mod)}', src, re.S), (
            f"{mod} must NOT be force-loaded via boot.kernelModules (#132) — it is "
            "udev-autoloaded only when the dGPU is present.")
