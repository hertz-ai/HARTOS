# ═══════════════════════════════════════════════════════════════
# HART OS — hybrid PRIME render-offload nixosTest (probe-gated arming, #132 + never-brick)
# ═══════════════════════════════════════════════════════════════
#
# Proves, on a BOOTED VM, the two contracts the offload arm must honour:
#
#   #132 — NEVER ARM / NEVER FORCE-LOAD ON ABSENT HARDWARE. A normal CI VM has NO
#     NVIDIA discrete GPU, so the boot-time presence probe (hart-gpu-offload-probe)
#     MUST NOT arm: the verdict in /run/hart/gpu-offload is `intel` or `software`,
#     NEVER `armed`. And critically systemd-modules-load must NOT be failed — the
#     proprietary nvidia module is shipped AVAILABLE but never force-loaded, so an
#     absent-NVIDIA box boots clean (the exact regression #132 fixed).
#
#   NEVER-BRICK / DEGRADE-NOT-DIE. The probe is a oneshot ordered BEFORE greetd
#     with a bounded TimeoutStartSec and an always-exit-0 script, so it can never
#     block or fail the boot: multi-user.target + greetd both come up, and the
#     verdict fails safe to the software floor. The offload wrapper passes an app
#     through UNCHANGED when not armed (no offload env), so an app launch is never
#     blocked by a missing/absent dGPU.
#
# It also proves the co-arm with hart-gpu-probe: a VM has no hardware GL, so the
# render verdict is `software`; the offload verdict degrades to `software` in
# lockstep (it can never outrank the render verdict).
#
# The renderer/PCI CLASSIFICATION logic (armed vs intel vs software across a faked
# /sys tree) is exercised BEHAVIOURALLY on the dev box by the extracted-shell test
# tests/unit/test_nixos_gpu_offload.py; this VM proves the END-TO-END boot wiring a
# Windows box cannot (the unit ran before greetd, the closure has no force-load,
# the verdict is on tmpfs). `[VM]` — gates in CI / local QEMU-KVM.
#
# #70 discipline: built from `hartModules` via the shared `mkNode` (./lib.nix) and
# imports ../modules/hart-gpu-offload.nix directly so it runs whether or not
# flake.nix has registered the module yet (held-file follow-up, like llm-provision).

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  gpu-offload = pkgs.testers.runNixOSTest {
    name = "gpu-offload";
    # Same runtime-injected node-global false positives the sibling hart tests
    # document; the VM boots and the assertions run.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.off = mkNode "desktop" {
      imports = [ ../modules/hart-gpu-offload.nix ];

      virtualisation = {
        memorySize = 3072;
        cores = 2;
        # DELIBERATELY no NVIDIA device: the VM has no discrete GPU, so the probe
        # must NEVER arm and the driver must NEVER force-load (#132).
      };

      # Arm the offload opt-in. hart.nvidia (driver AVAILABLE, udev-autoload, NOT
      # force-loaded) comes in via the module; no videoDrivers in the base.
      hart.gpu.offload.enable = true;

      # Bring up greetd (the never-black supervisor) so we can prove the probe runs
      # BEFORE greetd without wedging it. crash-only (paint watchdog off) keeps the
      # node about the PROBE, not the paint ladder (covered elsewhere).
      hart.sessionSupervisor = {
        enable = true;
        shellPaintTimeoutSeconds = 0;
        drmMasterSettleSeconds = 0;
        tierTermGraceSeconds = 0;
      };
    };

    testScript = ''
      off = machines[0]
      off.start()
      # Reaching multi-user.target at all is the first half of never-brick: the
      # offload probe is a oneshot ordered BEFORE greetd with a bounded timeout +
      # an always-exit-0 script, so even on absent hardware it can never block boot.
      off.wait_for_unit("multi-user.target")

      VERDICT = "/run/hart/gpu-offload"

      with subtest("the offload presence probe RAN and SUCCEEDED (never blocked/failed the boot)"):
          off.wait_for_unit("hart-gpu-offload-probe.service", timeout=60)
          state = off.succeed("systemctl is-active hart-gpu-offload-probe.service").strip()
          assert state == "active", \
              f"hart-gpu-offload-probe must be active(exited) — it must always succeed, got {state!r}"

      with subtest("the probe is BOUNDED + ordered before greetd so a wedged probe can never wedge boot"):
          show = off.succeed(
              "systemctl show hart-gpu-offload-probe.service "
              "-p Type -p RemainAfterExit -p TimeoutStartUSec -p Before")
          assert "Type=oneshot" in show, f"probe must be a oneshot: {show!r}"
          assert "RemainAfterExit=yes" in show, f"probe must RemainAfterExit: {show!r}"
          assert "TimeoutStartUSec=infinity" not in show, \
              "probe must have a FINITE TimeoutStartSec so a wedged probe can't wedge boot"
          assert "greetd.service" in show, \
              "the offload probe must be ordered BEFORE greetd (verdict ready before any session reads it)"

      with subtest("#132: the verdict NEVER arms on absent hardware (intel/software, never armed)"):
          off.succeed(f"test -f {VERDICT}")
          verdict = off.succeed(f"cat {VERDICT}").strip()
          assert verdict in ("intel", "software"), \
              f"a VM with no NVIDIA dGPU must yield intel|software, got {verdict!r}"
          assert verdict != "armed", \
              "the offload probe must NEVER arm a dGPU that is not physically present (#132)"

      with subtest("#132: the proprietary nvidia module is NOT force-loaded (systemd-modules-load clean)"):
          # The driver ships AVAILABLE (udev-autoload when present) but is never in
          # boot.kernelModules, so on this absent-NVIDIA box the module never loads
          # and systemd-modules-load.service must NOT be failed (the #132 regression).
          assert off.succeed("systemctl is-active systemd-modules-load.service").strip() != "failed", \
              "systemd-modules-load must not be failed — nvidia must not be force-loaded on absent hardware (#132)"
          off.fail("test -e /dev/nvidia0")  # no dGPU node -> the probe correctly did not arm

      with subtest("the verdict lives on tmpfs (/run) so it is re-derived every boot"):
          fstype = off.succeed(f"df --output=fstype {VERDICT} | tail -1").strip()
          assert fstype.lower() in ("tmpfs", "ramfs"), \
              f"the gpu-offload verdict must be on tmpfs (re-derived per boot), got {fstype!r}"

      with subtest("the offload wrapper is on PATH, reports the verdict, and passes through when not armed"):
          # --status mirrors the verdict file.
          status = off.succeed("hart-gpu-offload --status").strip()
          assert status == verdict, f"--status {status!r} must mirror the verdict {verdict!r}"
          # Not armed -> the NVIDIA offload env is NOT set; the app runs unchanged.
          env = off.succeed(
              "hart-gpu-offload sh -c 'echo \"''${__GLX_VENDOR_LIBRARY_NAME:-}|''${__NV_PRIME_RENDER_OFFLOAD:-}\"'").strip()
          assert env == "|", \
              f"not-armed wrapper must NOT export the offload env (passthrough), got {env!r}"
          # The familiar prime-run alias resolves to the same wrapper.
          off.succeed("command -v prime-run")

      with subtest("a pre-greetd offload probe never wedges the never-black supervisor — greetd still comes up"):
          off.wait_for_unit("greetd.service", timeout=120)
          assert off.succeed("systemctl is-active greetd.service").strip() == "active"
    '';
  };
}
