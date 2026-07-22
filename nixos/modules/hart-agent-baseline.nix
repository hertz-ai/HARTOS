{ config, lib, pkgs, ... }:

# ════════════════════════════════════════════════════════════════════════════
# HART OS -- LOCAL-2B AGENT BASELINE runner (potato-machine baseline capture)
# ════════════════════════════════════════════════════════════════════════════
#
# WHY (steward 2026-07-22): "agents which are not profiled with the llama.cpp 2B
# running locally, so we have a baseline in this potato machine." The profiler
# (scripts/agent_baseline/profile_local_2b.py) enumerates the real agent tasks +
# surfaces and runs one short turn each against the SHARED llama-server, recording
# first-token/total latency + pass/fail. This unit RUNS it on the node once the
# model is up, so the baseline is actually captured on the target hardware, not
# only runnable in theory.
#
# NEVER-FAIL: oneshot + timer, always exits 0 on a modelless boot (the profiler
# self-skips), read-mostly (writes only the baseline JSON under the agent data
# dir + a journal line), and nowhere near the render/boot-critical path. Opt-in,
# default OFF.

let
  cfg = config.hart;
  app = cfg.package;
in
{
  options.hart.agentBaseline = {
    enable = lib.mkEnableOption ''
      Capture the local-2B agent/surface baseline on the node: run the real
      profiler against the shared llama-server after it is up, journal the
      PASS/FAIL summary, and store the baseline JSON. Opt-in, default OFF; a
      modelless boot is a clean no-op (the profiler defers).
    '';
    intervalSec = lib.mkOption {
      type = lib.types.int;
      default = 3600;
      description = "Seconds between baseline captures. Hourly is plenty; the model does not change between boots.";
    };
  };

  config = lib.mkIf cfg.agentBaseline.enable {
    systemd.services.hart-agent-baseline = {
      description = "HART OS - capture the local-2B agent baseline (potato-machine profile)";
      after = [ "hart-backend.service" ];
      # No wantedBy: the timer drives it, off the boot-critical path.
      path = with pkgs; [ coreutils ];
      serviceConfig = {
        Type = "oneshot";
        User = "hart";
        Group = "hart";
        WorkingDirectory = app;
        # The profiler resolves the llm port from port_registry and writes the
        # baseline under the agent data dir; give it HART_OS_MODE so port math
        # matches the running shell.
        Environment = [ "HART_OS_MODE=1" ];
        ExecStart = "${app.python}/bin/python scripts/agent_baseline/profile_local_2b.py";
        # A FAIL exit (model reachable but under budget) is informative, not a unit
        # failure: SuccessExitStatus keeps the unit green so the timer keeps
        # sampling and the journal is the record of the verdict.
        SuccessExitStatus = "0 1";
        TimeoutStartSec = "300";
      };
    };

    systemd.timers.hart-agent-baseline = {
      description = "HART OS - local-2B agent baseline cadence";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        # First run 2 min after boot so llama-server has a chance to come up.
        OnBootSec = "120s";
        OnUnitActiveSec = "${toString cfg.agentBaseline.intervalSec}s";
        AccuracySec = "30s";
        Unit = "hart-agent-baseline.service";
      };
    };
  };
}
