{ config, lib, pkgs, ... }:

# ════════════════════════════════════════════════════════════════════════════
# HART OS -- SHELL MEMORY / COMPOSITING WATCH
# ════════════════════════════════════════════════════════════════════════════
#
# WHY (real-HW 2026-07-20): the steward booted, the desktop was "fast snappy",
# then "after dragging the orb for a while it hung". Degradation OVER TIME is an
# ACCUMULATION signature, not a steady-state cost -- a merely expensive effect is
# slow from the FIRST frame, not fine for minutes and then dead.
#
# The shell's own JS/DOM layer was measured and EXONERATED on the dev box (a
# 40-drag / 1200-move synthetic run: DOM nodes plateau at +182 and stop, ripple
# elements self-clean back to 0, canvases +0, JS heap +2.4MB with GC clearly
# working). So the accumulation is BELOW JavaScript -- the candidates are
# WebKit compositing layers / GPU memory (the webkit-cairo rung re-arms 28 live
# backdrop-filters plus an animated full-screen hue-rotate) or hart-comp's own
# buffers. None of that is visible from a browser on a dev box; only the node
# can see it.
#
# So this samples the REAL processes on the REAL machine, periodically, into the
# journal. After the next sustained drag the journal shows WHICH process grew,
# turning "it hung" into a named leak instead of another hypothesis.
#
# NEVER-FAIL POSTURE: read-only sampling of /proc, every line best-effort, the
# unit is oneshot+timer (never long-running), always exits 0, and it cannot touch
# the render path. If every read fails it journals one line saying so.

let
  cfg = config.hart;

  memwatch = pkgs.writeShellScript "hart-shell-memwatch" ''
    set -u
    # RSS in kB for a process name pattern, summed across matches (WebKit forks a
    # WebProcess/NetworkProcess, so per-name summing is what shows real growth).
    rss_of() {
      pat="$1"
      total=0
      for p in /proc/[0-9]*; do
        [ -r "$p/comm" ] || continue
        c=$(cat "$p/comm" 2>/dev/null || echo "")
        case "$c" in
          *"$pat"*)
            r=$(awk '/^VmRSS:/{print $2}' "$p/status" 2>/dev/null || echo 0)
            [ -n "$r" ] && total=$((total + r))
            ;;
        esac
      done
      echo "$total"
    }

    web=$(rss_of WebKitWebProc)
    net=$(rss_of WebKitNetwor)
    comp=$(rss_of hart-comp)
    host=$(rss_of python)
    # GPU memory: i915 exposes per-client gem info on some kernels; best-effort.
    gpu="na"
    if [ -r /sys/class/drm/card1/device/mem_info_vram_used ]; then
      gpu=$(cat /sys/class/drm/card1/device/mem_info_vram_used 2>/dev/null || echo na)
    fi
    # Open FDs of the shell host: a compositing/buffer leak often shows here first.
    fds="na"
    for p in /proc/[0-9]*; do
      c=$(cat "$p/comm" 2>/dev/null || echo "")
      case "$c" in
        *WebKitWebProc*) n=$(ls "$p/fd" 2>/dev/null | wc -l); fds="$n"; break ;;
      esac
    done

    if [ "$web" = "0" ] && [ "$comp" = "0" ]; then
      echo "[hart-memwatch] no shell/compositor processes visible (session not up yet)"
      exit 0
    fi
    # ONE greppable line per sample. Diff two lines to get the growth rate; the
    # journal timestamps give the interval.
    echo "[hart-memwatch] webkit_rss_kb=$web netproc_rss_kb=$net hartcomp_rss_kb=$comp pyhost_rss_kb=$host webkit_fds=$fds gpu_vram_used=$gpu"
    exit 0
  '';
in
{
  options.hart.shellMemWatch = {
    enable = lib.mkEnableOption ''
      Periodic read-only sampling of the shell/compositor process RSS + FD count
      into the journal, so a desktop that degrades OVER TIME (the 2026-07-20
      drag hang) can be attributed to a named process instead of guessed at.
      Cheap: one short shell script per interval, no render-path involvement.
    '';
    intervalSec = lib.mkOption {
      type = lib.types.int;
      default = 20;
      description = "Seconds between samples. 20s resolves a multi-minute leak without spamming the journal.";
    };
  };

  config = lib.mkIf cfg.shellMemWatch.enable {
    systemd.services.hart-shell-memwatch = {
      description = "HART OS - sample shell/compositor memory into the journal (leak attribution)";
      # No wantedBy: the TIMER drives it. Never part of the boot-critical path.
      path = with pkgs; [ coreutils gawk ];
      serviceConfig = {
        Type = "oneshot";
        # Reads /proc for other users' processes, so run as root but with every
        # write capability dropped -- this unit only ever READS.
        User = "root";
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        NoNewPrivileges = true;
        ExecStart = "${memwatch}";
        TimeoutStartSec = "20";
      };
    };

    systemd.timers.hart-shell-memwatch = {
      description = "HART OS - shell memory sampling cadence";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "45s";
        OnUnitActiveSec = "${toString cfg.shellMemWatch.intervalSec}s";
        AccuracySec = "5s";
        Unit = "hart-shell-memwatch.service";
      };
    };
  };
}
