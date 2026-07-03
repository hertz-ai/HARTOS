{ config, lib, pkgs, ... }:

# ════════════════════════════════════════════════════════════════════════════
# HART OS — hybrid PRIME render-offload (Intel iGPU display + NVIDIA dGPU offload)
# ════════════════════════════════════════════════════════════════════════════
#
# GOAL (automatic GPU allocation, the macOS/Windows "use the fast GPU for heavy
# things" behaviour, but Optimus-laptop honest):
#   - The Intel iGPU ALWAYS drives the display (power-efficient, unchanged, the
#     proven #99-103 panel path). It is never replaced.
#   - The NVIDIA discrete GPU (the target laptop's GeForce 940MX) is armed for
#     PRIME RENDER-OFFLOAD: a heavy app launched through the offload wrapper runs
#     its GL/Vulkan on the dGPU and the result is composited back onto the Intel
#     display. The dGPU runtime-suspends when idle (laptop battery), so it costs
#     nothing until something asks for it.
#   - The dGPU is armed ONLY when a supported NVIDIA GPU is PROVEN PRESENT at boot
#     (PCI device + loaded kernel driver + device nodes). Absent/unsupported ->
#     degrade to pure Intel; no hardware GL at all -> degrade to the software
#     floor. It NEVER bricks boot and NEVER force-loads a driver on a box that
#     does not have the hardware (#132).
#
# WHY THIS SHAPE (the load-bearing #132 decision):
#   `services.xserver.videoDrivers = [ "nvidia" ]` is the canonical NixOS switch,
#   but it force-loads the proprietary nvidia module via boot.kernelModules /
#   systemd-modules-load. On the SAME portable image booted on a box WITHOUT the
#   dGPU, that force-load FAILS systemd-modules-load and degrades the boot — the
#   exact regression #132 fixed in hart-kernel.nix. So the DEFAULT arm here ships
#   the proprietary driver AVAILABLE but NOT force-loaded (via hart-nvidia.nix,
#   which configures hardware.nvidia WITHOUT videoDrivers — the established
#   #132-safe pattern in this tree) and lets udev modalias autoload it ONLY when
#   the NVIDIA PCI device is present. A boot-time presence probe then writes an
#   honest verdict to /run/hart/gpu-offload, and the offload wrapper consults it:
#     `armed`    -> the dGPU is present + usable; the wrapper exports the NVIDIA
#                   PRIME render-offload env so the wrapped app runs on the dGPU.
#     `intel`    -> Intel hardware GL is good but no usable dGPU; passthrough.
#     `software` -> no hardware GL at all (the iGPU smoke-test failed); passthrough
#                   onto the software floor.
#   Box WITH the 940MX -> udev matches pci:v000010DE...,c0300 -> loads nvidia ->
#   the device nodes appear -> the probe sees them -> `armed`. Box WITHOUT ->
#   never loads -> systemd-modules-load never fails (#132 preserved) -> `intel`/
#   `software` -> pure Intel. Zero reboot, single portable image, live-USB safe.
#
# CO-ARMED WITH THE iGPU SMOKE TEST (hart-gpu-probe) + THE COMPOSITOR GLES:
#   The probe READS hart-gpu-probe's /run/hart/gpu-render verdict first. If the
#   base GL path is not proven `hardware`, offloading is pointless, so the offload
#   verdict degrades to `software` in lockstep. The compositor's own GLES arm
#   (hart-comp.nix, _HART_ARMED, also gated on /run/hart/gpu-render) is unchanged:
#   the compositor stays on the Intel iGPU that drives the display — only HEAVY
#   APPS launched through the wrapper offload to the dGPU. So the two verdicts can
#   never disagree (a software compositor is never paired with an "armed" offload).
#
# DEGRADE-NOT-DIE / NEVER-FAIL (the same contract as hart-gpu-probe):
#   The probe is a oneshot ordered BEFORE greetd with a bounded TimeoutStartSec;
#   it ALWAYS exits 0 and the fail-safe default is the software floor. The wrapper
#   passes the app through UNCHANGED whenever the verdict is not `armed`, so a
#   missing/empty verdict can never block an app launch. Nothing here touches the
#   cage Tier-3 floor, the compositor's pixman fallback, or the supervisor paint
#   watchdog — the offload is a STRICTLY ADDITIVE accelerant.
#
# NixOS-NATIVE PRIME (opt-in, installed systems): hardware.nvidia.prime.offload
#   is the upstream offload primitive (it also provides the `nvidia-offload`
#   command). It requires videoDrivers=[ "nvidia" ] (the force-load above), so it
#   is shipped ONLY inside an OPT-IN boot.specialisation ("nvidia-offload"),
#   default OFF. The base generation stays byte-identical to today's proven
#   #132-safe pure-Intel desktop; the steward enters the specialisation on an
#   INSTALLED machine that is known to carry the dGPU (where a force-load arm is
#   acceptable and real-HW validates it). Keeping it off the default ISO closure
#   also keeps the desktop ISO under its size/build ceiling.

let
  cfg = config.hart;
  offload = config.hart.gpu.offload;

  verdictFile = "/run/hart/gpu-offload";
  renderFile = "/run/hart/gpu-render";

  # Tools by store path — the unit PATH is minimal (the iso_real_usb_boot lesson).
  binPath = lib.makeBinPath (with pkgs; [ coreutils gnugrep ]);

  # ── The boot-time NVIDIA-presence probe ──────────────────────────────────────
  # `set -u` only (NOT -e): a failing probe must FALL BACK to the software floor
  # and exit 0, never abort the unit. Every path the probe reads is overridable by
  # a clearly-labelled HART_GPU_OFFLOAD_* test seam (defaulting to the real path),
  # so the EXACT shell logic can be exercised behaviourally off a faked /sys tree
  # on the dev box (tests/unit/test_nixos_gpu_offload.py) — not just in a VM.
  probeScript = pkgs.writeShellScript "hart-gpu-offload-probe" ''
    set -u
    export PATH=${binPath}''${PATH:+:$PATH}

    # Roots (real paths in production; the test points them at a fixture tree).
    RENDER_FILE="''${HART_GPU_OFFLOAD_RENDER_FILE:-${renderFile}}"
    VERDICT_FILE="''${HART_GPU_OFFLOAD_VERDICT_FILE:-${verdictFile}}"
    PCI_DIR="''${HART_GPU_OFFLOAD_PCI_DIR:-/sys/bus/pci/devices}"
    MODULE_MARK="''${HART_GPU_OFFLOAD_MODULE_MARK:-/sys/module/nvidia}"
    DEV_DIR="''${HART_GPU_OFFLOAD_DEV_DIR:-/dev}"

    mkdir -p "$(dirname "$VERDICT_FILE")" 2>/dev/null || true

    # Fail-safe default: the software floor (degrade-not-die).
    RESULT=software

    # Co-arm with the iGPU smoke-test verdict (hart-gpu-probe). If the base GL path
    # is not proven `hardware`, there is no point offloading to a dGPU — stay on
    # the software floor so the offload verdict can never outrank the render one.
    RENDER_VERDICT="$(cat "$RENDER_FILE" 2>/dev/null || true)"

    NV_PRESENT=0
    NV_DRIVER=0
    NV_NODE=0

    if [ "$RENDER_VERDICT" = "hardware" ]; then
      # Intel hardware GL is proven good -> default to Intel (no offload yet).
      RESULT=intel

      # (1) Is a supported NVIDIA discrete GPU PHYSICALLY PRESENT? #132: we never
      #     assume it — we read the PCI bus and require BOTH the NVIDIA vendor id
      #     (0x10de) AND a display-controller class (0x03xxxx: VGA / 3D / display).
      #     On a box without the 940MX nothing matches -> stay `intel`.
      for _dev in "$PCI_DIR"/*; do
        [ -e "$_dev/vendor" ] || continue
        [ -e "$_dev/class" ] || continue
        _vendor="$(cat "$_dev/vendor" 2>/dev/null || true)"
        _class="$(cat "$_dev/class" 2>/dev/null || true)"
        case "$_vendor" in
          0x10de)
            case "$_class" in
              0x03*) NV_PRESENT=1; break ;;
            esac
            ;;
        esac
      done

      # (2) Is the proprietary kernel driver actually LOADED (udev-autoloaded when
      #     present)? /sys/module/nvidia exists exactly when the module is live.
      if [ -e "$MODULE_MARK" ]; then
        NV_DRIVER=1
      fi

      # (3) Do the device nodes exist? Without them the GLVND nvidia ICD cannot
      #     route an offloaded context, so arming would be a black hole.
      if [ -e "$DEV_DIR/nvidia0" ] || [ -e "$DEV_DIR/nvidiactl" ]; then
        NV_NODE=1
      fi

      # ARM only when ALL THREE hold — present hardware + loaded driver + nodes.
      if [ "$NV_PRESENT" = "1" ] && [ "$NV_DRIVER" = "1" ] && [ "$NV_NODE" = "1" ]; then
        RESULT=armed
      fi
    fi

    # Publish the verdict (single line) + announce the decision to the journal so a
    # real-HW boot shows exactly what was detected + chosen
    # (journalctl -b -u hart-gpu-offload-probe).
    printf '%s\n' "$RESULT" > "$VERDICT_FILE" 2>/dev/null || true
    echo "[hart-gpu-offload] offload verdict: $RESULT (render=''${RENDER_VERDICT:-none}; nvidia_present=$NV_PRESENT driver_loaded=$NV_DRIVER nodes=$NV_NODE) -> $VERDICT_FILE" >&2
    exit 0
  '';

  # ── The PRIME render-offload wrapper ─────────────────────────────────────────
  # `hart-gpu-offload <command> [args...]` runs <command> on the dGPU WHEN the
  # boot probe armed it, else runs it unchanged (pure-Intel / software passthrough)
  # — degrade-not-die at the app-launch boundary. `--status` prints the verdict.
  # The env it exports when armed is the canonical NVIDIA PRIME render-offload env;
  # GLVND only routes to the nvidia vendor library when these are set, so a box
  # that never armed (env never set) keeps every app on the Intel/Mesa path.
  offloadWrapper = pkgs.writeShellScriptBin "hart-gpu-offload" ''
    set -u
    VERDICT_FILE="''${HART_GPU_OFFLOAD_VERDICT_FILE:-${verdictFile}}"

    if [ "''${1:-}" = "--status" ]; then
      cat "$VERDICT_FILE" 2>/dev/null || echo software
      exit 0
    fi

    if [ "$#" -eq 0 ]; then
      echo "usage: hart-gpu-offload <command> [args...]   run <command> on the dGPU when armed, else unchanged" >&2
      echo "       hart-gpu-offload --status               print armed | intel | software" >&2
      exit 2
    fi

    VERDICT="$(cat "$VERDICT_FILE" 2>/dev/null || true)"
    if [ "$VERDICT" = "armed" ]; then
      export __NV_PRIME_RENDER_OFFLOAD=1
      export __NV_PRIME_RENDER_OFFLOAD_PROVIDER=NVIDIA-G0
      export __GLX_VENDOR_LIBRARY_NAME=nvidia
      export __VK_LAYER_NV_optimus=NVIDIA_only
    fi
    exec "$@"
  '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options  (extend the shared hart.gpu submodule — declarations merge with
  # hart-gpu-probe's hart.gpu.accelerate; no leaf collision)
  # ═══════════════════════════════════════════════════════════
  options.hart.gpu.offload = {
    enable = lib.mkOption {
      type = lib.types.bool;
      # FALSE in the shared module so server/edge/phone NEVER pull the nvidia
      # closure. desktop.nix turns it ON in the Wire phase.
      default = false;
      description = ''
        Arm hybrid PRIME render-offload: the Intel iGPU drives the display and a
        PRESENT, supported NVIDIA discrete GPU is armed for render-offload of heavy
        apps launched through the `hart-gpu-offload` wrapper. The dGPU is armed
        ONLY when a boot-time probe proves it present (PCI device + loaded driver +
        device nodes); absent -> pure Intel; no hardware GL -> the software floor.
        Strictly additive + boot-safe (#132): it never force-loads a driver on a
        box without the hardware and never blocks/fails boot.
      '';
    };

    driverChannel = lib.mkOption {
      type = lib.types.enum [ "production" "new-feature" "open" ];
      default = "production";
      description = ''
        The hart-nvidia driver channel for the offload dGPU. `production` (the
        stable closed driver) is the default — the open kernel module is Turing+
        only, so a Maxwell GM108 940MX needs the closed driver. Passed straight to
        hart.nvidia.driverChannel (DRY: hart-nvidia.nix owns the driver lifecycle).

        REAL-HW CAVEAT (build-verification item): a GM108/Maxwell dGPU is only
        supported by the legacy 470 driver branch; the latest `stable` package may
        not enumerate it. If the real-HW build shows the dGPU unsupported, grow
        hart-nvidia.nix a `legacy` channel rather than duplicating the driver here.
      '';
    };

    intelBusId = lib.mkOption {
      type = lib.types.str;
      default = "PCI:0:2:0";
      description = ''
        PCI bus id of the Intel iGPU, for the NixOS-native PRIME specialisation
        (`lspci | grep VGA` to confirm; the standard Optimus location is PCI:0:2:0).
        Only consulted when offload.specialisation.enable = true.
      '';
    };

    nvidiaBusId = lib.mkOption {
      type = lib.types.str;
      default = "PCI:1:0:0";
      description = ''
        PCI bus id of the NVIDIA dGPU, for the NixOS-native PRIME specialisation
        (the standard Optimus location is PCI:1:0:0). Only consulted when
        offload.specialisation.enable = true.
      '';
    };

    specialisation = {
      enable = lib.mkOption {
        type = lib.types.bool;
        # DEFAULT OFF: the NixOS-native prime.offload arm force-loads nvidia (it
        # needs videoDrivers=[ "nvidia" ]), so it is shipped only as an OPT-IN boot
        # specialisation and kept OUT of the default ISO closure (#132 + ISO size).
        default = false;
        description = ''
          Also ship a `nvidia-offload` boot specialisation that wires the upstream
          hardware.nvidia.prime.offload (and the `nvidia-offload` command). The
          base generation stays byte-identical pure-Intel (#132-safe); only this
          extra boot entry force-loads nvidia. Intended for an INSTALLED machine
          known to carry the dGPU — NOT the portable ISO. Default OFF.
        '';
      };
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Config  (gated on the hart master toggle AND the offload opt-in)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && offload.enable) (lib.mkMerge [
    {
      # The verdict lives under the shared /run/hart (tmpfs), 0750 hart hart — the
      # same rule every other hart probe declares (tmpfiles de-dupes identical rules).
      systemd.tmpfiles.rules = [
        "d /run/hart 0750 hart hart -"
      ];

      # ── Driver ARM via hart-nvidia (DRY: do NOT duplicate the driver lifecycle) ──
      # hart-nvidia.nix configures hardware.nvidia WITHOUT services.xserver.video-
      # Drivers, so the proprietary driver is AVAILABLE + udev-autoloaded when the
      # PCI device is present, but NEVER force-loaded (#132-safe — the same shape
      # hart-kernel.nix aiCompute already ships in the desktop closure).
      hart.nvidia = {
        enable = true;
        driverChannel = offload.driverChannel;
        # Laptop offload: let the dGPU RUNTIME-SUSPEND when idle (the whole point of
        # an Optimus offload is that the dGPU costs nothing until asked). Persistence
        # mode pins it warm — the opposite — and would also crash-loop the persistence
        # daemon on a box where the dGPU never appears, so disable it here.
        persistenceMode = lib.mkDefault false;
        powerManagement.enable = lib.mkDefault true;
        # Keep the offload arm LIGHT: CUDA is a multi-GB closure that would push the
        # already-at-ceiling desktop ISO over the size limit, and it is a separate
        # concern (the llama.cpp/hart-llm win). Steward opts into CUDA explicitly.
        cuda.enable = lib.mkDefault false;
      };

      # The offload wrapper (+ a familiar `prime-run` alias) on PATH, and the live
      # verdict surfaced as a session var so an app-launcher can decide to auto-wrap
      # heavy apps. The env is NEVER set system-wide (that would force EVERYTHING to
      # the dGPU); it is applied per-invocation by the wrapper only when `armed`.
      environment.systemPackages = [
        offloadWrapper
        (pkgs.runCommand "hart-prime-run-alias" { } ''
          mkdir -p $out/bin
          ln -s ${offloadWrapper}/bin/hart-gpu-offload $out/bin/prime-run
        '')
      ];

      # ── The presence probe — runs EARLY, BEFORE greetd ─────────────────────────
      # Ordered after udev settles (so the nvidia nodes exist if the driver loaded)
      # and after hart-gpu-probe (so /run/hart/gpu-render is on disk to co-arm with),
      # and BEFORE greetd (so the verdict is ready before any session reads it). It
      # must NEVER block/fail boot: oneshot + RemainAfterExit + always-exit-0 script
      # + a bounded TimeoutStartSec.
      systemd.services.hart-gpu-offload-probe = {
        description = "HART OS NVIDIA PRIME offload presence probe (writes armed/intel/software to ${verdictFile})";
        wantedBy = [ "multi-user.target" ];
        before = [ "greetd.service" ];
        after = [ "systemd-udev-settle.service" "local-fs.target" "hart-gpu-probe.service" ];
        # A nixos-rebuild switch must not re-run the probe mid-session.
        restartIfChanged = false;
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = "${probeScript}";
          TimeoutStartSec = "30s";
        };
      };
    }

    # ── OPT-IN NixOS-native PRIME offload specialisation (default OFF) ────────────
    # The upstream hardware.nvidia.prime.offload arm + the `nvidia-offload` command,
    # behind a SEPARATE boot entry. The base generation is untouched (pure Intel,
    # #132-safe); only this specialisation force-loads nvidia. For an installed
    # machine known to carry the dGPU — kept off the portable ISO by default.
    (lib.mkIf offload.specialisation.enable {
      specialisation."nvidia-offload".configuration = {
        system.nixos.tags = [ "nvidia-offload" ];
        # THE force-load arm — ONLY here, NEVER in the base generation.
        services.xserver.videoDrivers = lib.mkForce [ "nvidia" ];
        hardware.nvidia.prime = {
          intelBusId = offload.intelBusId;
          nvidiaBusId = offload.nvidiaBusId;
          offload.enable = true;
          # Provide the upstream `nvidia-offload` command alongside hart-gpu-offload.
          offload.enableOffloadCmd = true;
        };
      };
    })
  ]);
}
