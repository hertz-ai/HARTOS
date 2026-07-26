{ config, lib, pkgs, ... }:

# ════════════════════════════════════════════════════════════════════════════
# HART OS — robot Model-Bus capability probe (the embodied twin of
#           hart-compat-smoketest: MEASURE a robot's reach, don't claim it)
# ════════════════════════════════════════════════════════════════════════════
#
# WHY:
#   The Model Bus (hart-model-bus.nix) makes "give me intelligence" a native OS
#   call, and the universal-AI-native-OS vision says a ROBOT's request for LLM /
#   vision / VLA is the SAME Model Bus call as a desktop app's. hart-compat-
#   smoketest already proves — per foreign-OS runtime — that the OS can actually
#   EXECUTE, writing an honest verdict to /run/hart/compat-status. This module is
#   the EMBODIED twin: after boot it actually REACHES the Model Bus for each core
#   intelligence a robot needs and writes an honest per-capability verdict to
#   /run/hart/robot-capability-status, so a robot (or the operator) can read
#   whether the brain is truly there BEFORE trusting it — a MEASUREMENT, not a
#   claim.
#
#   /run/hart/robot-capability-status holds one `key=value` line per capability:
#     model_bus=ok            ← GET :6790/health answered
#     llm=ok                  ← a real tiny :6790/v1/chat round-trip answered
#     llm=no-model            ← the bus answered but no LLM backend is loaded
#     vision=ready            ← a vision backend is registered + reachable
#     vla=ready               ← the embodied VLA/world-model surface is reachable
#     vla=no-backend          ← VLA metadata present, no live world model yet
#     intelligence_api=ok     ← the on-node robot /think fusion API answers
#     robots=0                ← robots currently registered on this node
#   Each line is also echoed to the journal ([hart-robot-probe] llm = ok), so
#   `journalctl -b -u hart-robot-probe` shows the verdicts on a real boot.
#
# HONEST SCOPE — a REAL reachability probe, not a full policy run:
#   `llm=ok` PROVES a robot got a served answer from the bus; `vision=ready`
#   proves a VLM backend is registered (we do NOT force a full GPU VLM inference
#   in a boot smoke-test, exactly like compat-smoketest reports android=ready for
#   an image-present-but-idle container). The measurement logic lives in the
#   portable, unit-tested integrations/robotics/model_bus_probe.py.
#
# NEVER-BLOCK-THE-DESKTOP + FAIL-SAFE (the hart-*.nix house rule):
#   * Runs IN PARALLEL with the desktop — wantedBy multi-user.target, NOT
#     `before greetd`. It must never delay first paint.
#   * oneshot + RemainAfterExit + the python probe NEVER raises and main() ALWAYS
#     returns 0, and a bounded TimeoutStartSec caps the whole run, so it can never
#     block or fail the boot. A dead backend records its honest fail-state and the
#     probe continues — one dead capability never aborts the others.

let
  cfg = config.hart;
  bus = config.hart.modelBus;
  probeCfg = config.hart.robotics.probe;
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.robotics.probe = {
    enable = lib.mkOption {
      type = lib.types.bool;
      # On wherever the Model Bus runs (that is where a robot has a brain to
      # reach). Adds nothing to a node with no Model Bus.
      default = bus.enable or false;
      defaultText = lib.literalExpression "config.hart.modelBus.enable";
      description = ''
        Run the post-boot robot Model-Bus capability probe (hart-robot-probe): a
        oneshot that actually REACHES the Model Bus for each core intelligence a
        robot needs (LLM, vision, embodied VLA / world model) plus the on-node
        robot /think fusion API, and writes an honest per-capability status
        (ok / no-model / ready / no-backend / down) to
        /run/hart/robot-capability-status (one key=value line per capability, also
        echoed to the journal).

        This MEASURES the embodied capability surface instead of letting the OS
        CLAIM a robot can reach it. It runs IN PARALLEL with the desktop (never
        `before greetd`), each probe is bounded + fail-safe, and the unit always
        succeeds so it can never block or fail the boot.
      '';
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Config  (gated on the hart master toggle + the Model Bus +
  # this probe toggle)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && bus.enable && probeCfg.enable) {
    # The status file lives under the shared /run/hart (tmpfs). Other consumers
    # declare the same rule (compat-smoketest / gpu-probe / model-bus); tmpfiles
    # de-dupes identical rules.
    systemd.tmpfiles.rules = [
      "d /run/hart 0750 hart hart -"
    ];

    # ── The robot capability probe oneshot — runs IN PARALLEL with the desktop ──
    # Ordered AFTER the Model Bus (so there is a bus to reach) + hart.target, and
    # network-online is WANTED (best-effort) not REQUIRED — a no-network boot must
    # still run the probe (it just reports the honest reachability it finds). It is
    # NOT `before greetd` — it must never delay first paint. It can never
    # block/fail the boot: oneshot + RemainAfterExit + the python always exits 0,
    # and a bounded TimeoutStartSec so even a wedged backend probe can't wedge boot
    # (the inner per-probe timeouts are the first belt).
    systemd.services.hart-robot-probe = {
      description = "HART OS — robot Model-Bus capability probe (writes honest per-capability status to /run/hart/robot-capability-status)";
      wantedBy = [ "multi-user.target" ];
      after = [ "hart-model-bus.service" "hart.target" "network-online.target" ];
      wants = [ "network-online.target" ];
      # A nixos-rebuild switch must not re-run the probe mid-session.
      restartIfChanged = false;

      environment = {
        HEVOLVE_DATA_DIR = cfg.dataDir;
        HART_MODEL_BUS_PORT = toString bus.ports.http;
        PYTHONDONTWRITEBYTECODE = "1";
        PYTHONUNBUFFERED = "1";
      };

      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        User = "hart";
        Group = "hart";
        # No `set -e`: a probe error must NEVER fail the unit. The python probe
        # writes the status file itself and always returns 0; the wrapper runs it
        # best-effort and exits 0 regardless (measurement, never a gate).
        ExecStart = pkgs.writeShellScript "hart-robot-probe" ''
          set -u
          echo "[hart-robot-probe] probing robot Model-Bus reach (port ${toString bus.ports.http})"
          ${cfg.package.python}/bin/python -c "
          import sys, os
          sys.path.insert(0, '${cfg.package}')
          os.environ.setdefault('HEVOLVE_DATA_DIR', '${cfg.dataDir}')
          from integrations.robotics.model_bus_probe import main
          raise SystemExit(main())
          " || true
          exit 0
        '';
        # 90s comfortably covers a cold-model LLM round-trip (the probe's inner
        # LLM timeout is 20s) + the cheap in-process VLA/API checks.
        TimeoutStartSec = "90";
      };
    };
  };
}
