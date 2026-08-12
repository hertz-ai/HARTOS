{ config, lib, pkgs, ... }:

# ════════════════════════════════════════════════════════════════════════════
# HART OS — THERMAL-HEALTH truth-teller (a thermal stall must never look like a hang)
# ════════════════════════════════════════════════════════════════════════════
#
# WHY (real-hardware RCA, 2026-08-12, Samsung NP550P5C / i7-3630QM, HD 4000):
#   The desktop "froze" instantly and repeatedly. The mouse cursor kept moving, no
#   process was spinning, load was ~1.4, and the CPU/RAM widget showed nothing
#   unusual — so every software theory (compositor deadlock, WebKit block, waitress
#   thread-pool saturation, DRM-master denial) looked plausible and each one was
#   chased and DISPROVED. The measurement that ended it:
#
#     thermal_zone0/1 (acpitz) : 94C          <- agrees with
#     coretemp Package id 0    : 94-95C       <- the real CPU die sensor
#     cpu0 scaling_cur_freq    : 1200000      <- IDLE, lowest P-state
#     C7 (deepest C-state)     : dominant residency
#     Fan cooling_device       : cur=1/max=1  <- already MAXED
#     core_throttle_count      : 1.2M / 843K / 1.5M
#     dmesg                    : intel_powerclamp: Start idle injection (124x/boot)
#
#   i.e. the machine idles ~50C above where it should, so the KERNEL force-injects
#   idle to protect the silicon. That forced idle IS the freeze: it stalls the
#   compositor mid-frame while every userspace metric still reads "healthy".
#
# THE BUG THIS MODULE FIXES (and it is a real one, even though the heat is physical):
#   HART OS knew — the kernel had already counted a MILLION throttle events — and the
#   OS said NOTHING. The operator saw a frozen desktop and was left to conclude the
#   software had hung. Silently swallowing the single most important fact about the
#   machine's health is the failure. This module does NOT pretend to cool anything;
#   it makes the OS TELL THE TRUTH, loudly, the moment throttling starts.
#
# HONESTY RULES (mirrors hart-display-health's "never fake a positive"):
#   * Reports the MAX across every thermal zone, so a board whose acpitz reads low
#     can never under-report while coretemp cooks.
#   * Uses the kernel's OWN core_throttle_count delta as PROOF of throttling, not an
#     inference from temperature alone.
#   * Says explicitly, in the log, that a freeze happening right now is THERMAL — and
#     that if it occurs at idle no software change can fix blocked airflow.
#   * Writes /run/hart/thermal-health (`ok` / `throttling` + numbers) so the shell and
#     any operator tooling can surface it instead of the user guessing.
#
# COST DISCIPLINE (this probe runs on an already-thermally-limited box):
#   /sys reads + sleep ONLY. No subprocess, no journalctl, no dmesg. This is not
#   pedantry — during this very investigation a journalctl-based probe pegged a core
#   at 99% CPU and heated the machine it was measuring, which corrupted two rounds of
#   readings. A health probe that changes what it measures is a bug.
#
# NOT gated on hart.power.enable: that is a `mkEnableOption` (default false) which NO
# profile sets, so hart-power.nix is dormant on every shipped image. Thermal honesty
# must not inherit that dormancy — it is gated on hart.enable, so it runs wherever
# HART OS runs. (thermald / governors / profiles stay hart-power.nix's job; this
# module only OBSERVES and REPORTS. One implementation, no parallel thermal path.)

let
  cfg = config.hart;
  th = config.hart.thermalHealth;
in
{
  options.hart.thermalHealth = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Report thermal throttling honestly to the journal + /run/hart/thermal-health,
        so a kernel-forced idle stall is never mistaken for a software hang.
      '';
    };

    warnTemp = lib.mkOption {
      type = lib.types.int;
      default = 90;
      description = ''
        Celsius at or above which sustained operation is called out as thermal.
        Kept in step with hart.power.thermalThrottle.criticalTemp (default 90).
      '';
    };

    intervalSeconds = lib.mkOption {
      type = lib.types.int;
      default = 20;
      description = "Seconds between samples (cheap: /sys reads only, no subprocess).";
    };
  };

  config = lib.mkIf (cfg.enable && th.enable) {

    systemd.services.hart-thermal-health = {
      description = "HART OS — thermal truth-teller (a thermal stall must never masquerade as a software hang)";
      wantedBy = [ "multi-user.target" ];
      # Lowest priority everywhere: never compete with the desktop for the CPU we are
      # trying to prove is starved.
      serviceConfig = {
        Type = "simple";
        Restart = "always";
        RestartSec = 30;
        Nice = 19;
        # Nice only bites under contention; on an idle node this still takes
        # every core. A thermal HEALTH probe generating heat is self-defeating,
        # and this node spent 5.9 hours thermally throttled today. Bound it.
        CPUQuota = "20%";
        IOSchedulingClass = "idle";
        CPUSchedulingPolicy = "idle";
      };
      script = ''
        WARN=${toString th.warnTemp}
        IVAL=${toString th.intervalSeconds}
        STATE=/run/hart/thermal-health
        mkdir -p /run/hart

        warned=0
        last_throttle=-1

        while true; do
          # ── hottest zone wins (never under-report) ──
          t=0
          for z in /sys/class/thermal/thermal_zone*/temp; do
            [ -r "$z" ] || continue
            v=$(cat "$z" 2>/dev/null || echo 0)
            v=$((v / 1000))
            [ "$v" -gt "$t" ] && t=$v
          done

          # ── the kernel's own throttle counter: PROOF, not inference ──
          thr=0
          for c in /sys/devices/system/cpu/cpu*/thermal_throttle/core_throttle_count; do
            [ -r "$c" ] || continue
            v=$(cat "$c" 2>/dev/null || echo 0)
            thr=$((thr + v))
          done
          if [ "$last_throttle" -lt 0 ]; then
            delta=0            # first sample: no baseline yet, never report a phantom spike
          else
            delta=$((thr - last_throttle))
          fi
          last_throttle=$thr

          if [ "$t" -ge "$WARN" ] || [ "$delta" -gt 0 ]; then
            echo "throttling temp=''${t}C warn=''${WARN}C throttle_delta=''${delta} throttle_total=''${thr}" > "$STATE"
            if [ "$warned" -eq 0 ]; then
              echo "[hart-thermal] THERMAL LIMIT: ''${t}C (warn ''${WARN}C), +''${delta} kernel throttle events (total ''${thr})." >&2
              echo "[hart-thermal] The kernel is FORCE-IDLING the CPU to protect it. A desktop freeze right now is THERMAL, not a software hang." >&2
              echo "[hart-thermal] If this persists at IDLE, airflow is blocked — the heatsink/fan needs cleaning or repasting. No software change can fix that." >&2
              warned=1
            fi
          else
            echo "ok temp=''${t}C warn=''${WARN}C throttle_total=''${thr}" > "$STATE"
            # Re-arm only after a real recovery margin so we do not flap at the edge.
            if [ "$warned" -eq 1 ] && [ "$t" -lt $((WARN - 10)) ]; then
              echo "[hart-thermal] recovered: ''${t}C — throttling stopped" >&2
              warned=0
            fi
          fi

          sleep "$IVAL"
        done
      '';
    };
  };
}
