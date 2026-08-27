{ config, lib, pkgs, hartSrc, ... }:

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

  # ── The unit bound, DERIVED from the work rather than guessed ──────────────
  # This unit shipped TimeoutStartSec=300, a constant with no relationship to
  # what the profiler does. Measured on the fleet box 2026-08-26: the run is 72
  # agent tasks + 3 surfaces = 75 probes, and the profiler's own per-probe
  # budgets (spectrum.json) are 25-45s each, so a complete pass needs ~2630s
  # (43.8 min). systemd was therefore killing it with SIGTERM after roughly
  # seven probes, EVERY time, and because the kill lands mid-run the node
  # recorded a failed unit and wrote no baseline at all -- losing the one
  # artifact the exercise exists to produce. It only started showing up once
  # llama actually answered on :808; before that the profiler self-deferred in
  # under a second and the timeout was never approached.
  #
  # A timeout for this job has to be a function of the number of actions. The
  # profiler now computes and enforces its own budget-derived deadline (see
  # plan_seconds / --plan), so THIS value is only the outer backstop; it is
  # sized from the same spectrum.json budgets times a probe ceiling, so it can
  # never end up tighter than the work it is bounding.
  # hartSrc (the repo root, supplied to every module via the flake's
  # specialArgs) rather than a ../../ escape: the flake lives in nixos/, so a
  # relative climb out of it is both a second way of reaching the repo and one
  # that need not resolve. No other module does it, and this reads the SAME
  # spectrum.json the profiler loads at runtime, so the bound and the work are
  # derived from one file.
  #
  # CONCATENATE the path, do NOT interpolate it. `hartSrc + "/..."` keeps a PATH
  # value; `"${hartSrc}/..."` coerces the repo root to a STRING, which forces the
  # whole source tree to be a realised store path at EVALUATION time. That is what
  # the interpolated form did here between 52a8a34 and this commit, and it broke
  # the flake evaluation gate with
  #     error: path '/nix/store/<hash>-<hash>-source' is not valid
  # on every run, naming a different path each time. Nothing built for a day, so
  # no image could carry any commit to any node. hart-comp.nix:59
  # (`compositorSrc = hartSrc + "/compositor"`) is the established form; match it.
  spectrum = builtins.fromJSON
    (builtins.readFile (hartSrc + "/scripts/agent_baseline/spectrum.json"));
  worstBudgetMs = lib.foldl'
    (acc: b: let ms = b.total_ms or 0; in if ms > acc then ms else acc)
    0
    (lib.filter lib.isAttrs (lib.attrValues spectrum.budgets));
  # Ceiling, not a count: nix cannot enumerate the Python task list, so this
  # bounds how far the list may grow before someone must revisit it. 128 is
  # comfortably above the measured 75.
  probeCeiling = 128;
  # + 300s of startup/teardown margin on top of the worst-case probe time.
  baselineTimeoutSec = (worstBudgetMs / 1000) * probeCeiling + 300;
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
        # Derived from spectrum.json's worst per-probe budget x the probe
        # ceiling (see baselineTimeoutSec above), NOT a constant. The profiler
        # bounds itself to its own exact plan and writes a partial baseline if
        # it runs long, so reaching this outer limit means something is truly
        # wedged rather than merely slow.
        TimeoutStartSec = toString baselineTimeoutSec;
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
