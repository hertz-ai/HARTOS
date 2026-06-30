{ config, lib, pkgs, ... }:

# ════════════════════════════════════════════════════════════════════════════
# HART OS — GPU smoke-test gate (default-to-hardware-GL when the GPU is proven)
# ════════════════════════════════════════════════════════════════════════════
#
# WHY:
#   Today every tier forces software GL everywhere (LIBGL_ALWAYS_SOFTWARE=1) so
#   the shell paints on ANY GPU including broken/flaky drivers (the nouveau-GSP
#   crash class #99-103). That is correct for the FLOOR but pessimistic for the
#   common case: a machine with a known-good GPU is pinned to llvmpipe forever.
#
#   This module is a BOOT-TIME smoke test that runs BEFORE the display manager,
#   binds eglinfo to the INTEL iGPU render node (driver i915), checks that node
#   can create a GL context + reports an INTEL hardware renderer (not the dGPU,
#   not a software rasterizer), and writes a one-line verdict to /run/hart/gpu-render:
#     `hardware`  → the iGPU passed the smoke test; upper tiers may use GL.
#     `software`  → probe failed / errored / timed out / disabled / non-Intel /
#                   no i915 node → FORCE software.
#
#   WHY THE iGPU SPECIFICALLY (target Optimus laptop, Intel iGPU + GeForce dGPU):
#   the discrete GeForce (nouveau) FAULTS (MMIO PRIVRING) and is blacklisted +
#   KMS-off, so the iGPU is the one render path proven healthy. The probe binds to
#   the i915 render node AND requires an Intel renderer (iris/crocus/i965/Intel),
#   so a dGPU/NVIDIA/AMD hardware-renderer line can NEVER flip the verdict. The
#   compositor pins the SAME iGPU (it is the only DRM node once nouveau is out), so
#   the verdict provably reflects the GPU that will actually composite.
#   A safe consumer (Tier-2 sway, hart-layer-shell-host.nix) reads that file at
#   session launch and only forces software GL when the verdict is NOT `hardware`.
#
# HONEST SCOPE — this is a SMOKE TEST, not a render-readback proof:
#   `eglinfo` creates a real EGL/GL context and prints the GL renderer string; a
#   12s timeout catches a driver that HANGS on context creation (the real-HW
#   pointer-only failure mode). It does NOT render a frame and read pixels back,
#   so a GPU that creates a context but corrupts/hangs DURING scanout is NOT
#   caught here. That residual risk is acceptable because it is NOT the backstop:
#   the cage Tier-3 floor (hart-liquid-ui.nix) is 100% software and the session
#   supervisor's paint-watchdog drops any upper tier that fails to paint down to
#   that floor. So a probe miss degrades to the proven floor, never a blank screen.
#
# FAIL-SAFE = SOFTWARE (the never-fail contract):
#   ANY error, missing tool, empty output, ambiguous renderer, or timeout writes
#   `software`. The default is the floor; `hardware` is written ONLY on a positive
#   hardware-renderer match. The unit ALWAYS succeeds (oneshot, RemainAfterExit)
#   so it can never block or fail the boot transaction.
#
# NEVER-FAIL POSITION (ROADMAP §6 tiering — INVARIANT):
#   This module NEVER touches the cage Tier-3 floor, the GTK4 host's GSK cairo
#   renderer, the WebKit software forces, or hart-comp's pixman path. It only
#   PUBLISHES a verdict; a single safe consumer (Tier-2 sway) opts in to reading
#   it. The floor stays software no matter what this probe says.

let
  cfg = config.hart;
  gpu = config.hart.gpu;

  # The verdict file the safe consumers read. One line: `hardware` or `software`.
  # In /run (tmpfs) so it is re-derived every boot — a probe verdict must never
  # outlive the hardware/driver state it measured.
  verdictFile = "/run/hart/gpu-render";

  # Tools referenced by absolute store path — the unit PATH is minimal (the
  # iso_real_usb_boot lesson: coreutils/grep/eglinfo are not on the bare unit
  # PATH). `timeout` + `cat`/`mkdir`/`echo`/`readlink`/`basename` come from
  # coreutils; gnused for the renderer-line trim (sed is NOT in coreutils, so the
  # journal RENDERER display was silently empty without it).
  binPath = lib.makeBinPath (with pkgs; [ coreutils gnugrep gnused ]);

  # ── The GPU smoke-test probe ───────────────────────────────────────────────
  # `set -u` only (NOT -e): a probe failing must NEVER abort — it must FALL BACK
  # to `software` and exit 0. The unit must always succeed. eglinfo is run under
  # `timeout` and `|| true` so a HANG or non-zero exit cannot fail the unit; the
  # RENDERER decision is made purely from the captured text.
  probeScript = pkgs.writeShellScript "hart-gpu-probe" ''
    set -u
    export PATH=${binPath}''${PATH:+:$PATH}

    VERDICT="${verdictFile}"
    mkdir -p /run/hart 2>/dev/null || true

    # Fail-safe default: the FLOOR. Only a positive INTEL iGPU match flips this.
    RESULT=software

    # Operator override: hart.gpu.accelerate = false forces the software floor
    # regardless of what the GPU can do (an explicit "pin me to llvmpipe" knob).
    ACCELERATE="${if gpu.accelerate then "1" else "0"}"

    # Journal context (filled below): which render node + which renderer drove the
    # verdict, so a real-HW boot shows exactly what was detected + chosen.
    IGPU_NODE=""
    RENDERER=""

    if [ "$ACCELERATE" = "1" ]; then
      # ── Resolve the Intel iGPU render node (driver == i915) ──────────────────
      # Scan /sys/class/drm/renderD*/device/driver and pick the node whose driver
      # symlink basename is `i915` — the Intel integrated GPU that drives the panel.
      # The DISCRETE GPU is deliberately NOT matched: on the target Optimus laptop
      # the GeForce dGPU (nouveau) FAULTS (MMIO PRIVRING) and is blacklisted +
      # KMS-off, so binding the probe to the iGPU node — together with the Intel
      # allow-list below — means a dGPU / NVIDIA / AMD renderer line can NEVER flip
      # the verdict to hardware. No i915 node => stay `software`: a non-Intel box
      # keeps the PROVEN software floor (conservative by design; the arm is gated on
      # the one render path proven healthy on the target hardware). readlink/basename
      # come from coreutils (on PATH above).
      for _rd in /sys/class/drm/renderD*; do
        [ -e "$_rd/device/driver" ] || continue
        _drv="$(basename "$(readlink -f "$_rd/device/driver" 2>/dev/null)" 2>/dev/null || true)"
        if [ "$_drv" = "i915" ]; then
          IGPU_NODE="/dev/dri/$(basename "$_rd")"
          break
        fi
      done

      if [ -n "$IGPU_NODE" ] && [ -e "$IGPU_NODE" ]; then
        # eglinfo creates a real EGL/GL context and prints the GL renderer string.
        # Force the SURFACELESS platform: the probe runs BEFORE greetd with NO
        # X/Wayland display, and the surfaceless platform reaches the DRM render
        # node headlessly (without it eglinfo may find no display and report
        # nothing => software, never detecting the good iGPU). `timeout 12` catches
        # a driver that HANGS on context creation (the real-HW pointer-only
        # failure). Capture stdout+stderr; never let it fail the unit (|| true).
        OUT="$(EGL_PLATFORM=surfaceless timeout 12 ${pkgs.mesa-demos}/bin/eglinfo 2>&1 || true)"

        # The renderer line eglinfo reported — captured for the journal so a real-HW
        # boot shows WHICH renderer (Intel iGPU or which software rasterizer) drove
        # the verdict.
        RENDERER="$(printf '%s' "$OUT" | grep -iE 'renderer' | head -1 | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' || true)"

        # A HARDWARE verdict requires BOTH: (1) the renderer is an INTEL iGPU
        # renderer (iris / crocus / i965 / "Intel" — the Mesa Intel driver family),
        # which is the POSITIVE proof it is the iGPU (defense-in-depth atop the
        # i915-node bind so a non-Intel hardware renderer can never flip it), AND
        # (2) it is NOT a software rasterizer (llvmpipe / softpipe / swrast /
        # "software"). Either condition failing keeps the fail-safe `software` floor.
        if printf '%s' "$OUT" | grep -iqE 'renderer' \
           && printf '%s' "$OUT" | grep -iE 'renderer' | grep -iqE 'iris|crocus|i965|intel' \
           && printf '%s' "$OUT" | grep -iE 'renderer' \
              | grep -ivqE 'llvmpipe|softpipe|swrast|software rasterizer|software'; then
          RESULT=hardware
        fi
      else
        echo "[hart-gpu-probe] no Intel i915 render node found - staying on the software floor" >&2
      fi
    fi

    # Publish the verdict (single line) + announce the decision to the journal so a
    # real-HW boot shows exactly what was detected + chosen (journalctl -b -u hart-gpu-probe).
    printf '%s\n' "$RESULT" > "$VERDICT" 2>/dev/null || true
    echo "[hart-gpu-probe] GPU render verdict: $RESULT (accelerate=$ACCELERATE; igpu_node: ''${IGPU_NODE:-none}; renderer: ''${RENDERER:-none reported}) -> $VERDICT" >&2
    exit 0
  '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.gpu = {
    accelerate = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Let the GPU smoke-test gate DEFAULT to hardware GL when the boot-time
        probe (hart-gpu-probe) proves the GPU can create a GL context and reports
        a hardware renderer. The verdict is written to ${verdictFile} and a safe
        consumer (Tier-2 sway) only forces software GL when the verdict is NOT
        `hardware`. The cage Tier-3 floor + the GTK4 GSK cairo renderer + hart-comp
        pixman stay forced-software regardless — this only governs the OPT-IN
        upper-tier path.

        Set to FALSE to force the software floor everywhere (the probe always
        writes `software`) — an operator override for a machine whose GPU passes
        the smoke test but misbehaves during real scanout.
      '';
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Config  (gated on cfg.enable like every hart module)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf cfg.enable {
    # The verdict lives under the shared /run/hart (tmpfs). Other consumers rely
    # on this dir at 0750 hart hart (model-bus / session-supervisor / portal all
    # declare the same rule — tmpfiles de-dupes identical rules).
    systemd.tmpfiles.rules = [
      "d /run/hart 0750 hart hart -"
    ];

    # ── The smoke-test oneshot — runs EARLY, BEFORE greetd ─────────────────────
    # Ordered after udev settles (so DRM render nodes exist under /dev/dri) and
    # BEFORE greetd starts (so the verdict is on disk before any session reads it).
    # wantedBy multi-user.target so it always runs on a graphical boot. It must
    # NEVER block/fail the boot: oneshot + RemainAfterExit + the script always
    # exits 0, and a bounded TimeoutStartSec so even a wedged probe can't wedge boot.
    systemd.services.hart-gpu-probe = {
      description = "HART OS — GPU smoke-test gate (writes hardware/software to ${verdictFile})";
      wantedBy = [ "multi-user.target" ];
      before = [ "greetd.service" ];
      after = [ "systemd-udev-settle.service" "local-fs.target" ];
      # A nixos-rebuild switch must not re-run the probe mid-session.
      restartIfChanged = false;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${probeScript}";
        # The script bounds eglinfo at 12s itself; this is the outer belt so a
        # pathological hang outside eglinfo still can't wedge the boot.
        TimeoutStartSec = "30s";
      };
    };
  };
}
