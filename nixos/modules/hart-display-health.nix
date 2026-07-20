{ config, lib, pkgs, ... }:

# ════════════════════════════════════════════════════════════════════════════
# HART OS — DISPLAY-HEALTH snapshot probe (the never-black real-HW observability)
# ════════════════════════════════════════════════════════════════════════════
#
# WHY:
#   The display tier ladder (hart-comp -> sway -> cage) + the paint/input
#   watchdogs DEGRADE-GRACEFULLY and are proven in CI nixosTests. Three never-black
#   holes can only be OBSERVED on real hardware (a VM has no real DRM scanout/seat):
#     * #131 first-SCANOUT marker unbuilt -> a "black-but-healthy" Tier-1 is
#       indistinguishable from a painting one in a VM,
#     * #134 input-alive marker not written by the real compositor -> the
#       "pointer frozen at 0,0" failure is invisible in a VM,
#     * the DRM-master EBUSY handoff grace is real-HW only.
#   This module ships a post-boot snapshot (nixos/display-health/hart-display-
#   health.sh) that records an HONEST per-dimension verdict (tier / gpu / painted /
#   input / scanout / screen) to /run/hart/display-health and the journal, with
#   `unknown` for the markers that are not built yet (it NEVER fakes a positive).
#   It is the real-HW twin of the gpu-probe verdict file + the compat-smoketest
#   status file: a measurement an operator / the UI reads after a real boot.
#
# NEVER-BLOCK-THE-DESKTOP + FAIL-SAFE (the never-fail contract):
#   * Runs in PARALLEL with the desktop — wantedBy multi-user.target, ordered AFTER
#     greetd (NOT `before greetd`): it can never delay first paint. (Contrast
#     hart-gpu-probe, which MUST run before greetd because a session reads its
#     verdict; nothing consumes THIS file at boot, so it must never gate the seat.)
#   * The script is `set -u` (not -e), every read `|| true`-guarded, and the unit
#     is oneshot + RemainAfterExit + a bounded TimeoutStartSec and always `exit 0`s,
#     so a missing marker / wedged read can never block or fail the boot.
#
# SCOPED to the tier-ladder topology: the config is gated on
# hart.sessionSupervisor.enable, so it ships ONLY where the never-black ladder
# exists (desktop with the supervisor) and adds nothing to server/edge closures.
# The probe SCRIPT is a single source of truth shared verbatim with
# tests/unit/test_hart_display_health.py (env-overridable paths), so its
# classification logic is behaviourally unit-tested on the dev box and shipped
# unchanged here.

let
  cfg = config.hart;
  dh = config.hart.displayHealth;
  sup = config.hart.sessionSupervisor;

  # The honest per-dimension verdict file. One key=value line per dimension. In
  # /run (tmpfs) so it is re-derived every boot — a display verdict must never
  # outlive the boot it measured (mirrors gpu-render + compat-status).
  statusFile = "/run/hart/display-health";

  # Ship the standalone script verbatim (one source of truth with the unit test).
  # runCommand (not writeShellScript) preserves the file's own `#!/bin/sh` shebang
  # and lets us POSIX-lint it at build time, exactly like hartctl is shipped.
  probeScript = pkgs.runCommand "hart-display-health"
    { nativeBuildInputs = [ pkgs.coreutils ]; }
    ''
      mkdir -p $out/bin
      cp ${../display-health/hart-display-health.sh} $out/bin/hart-display-health
      chmod +x $out/bin/hart-display-health
      # Smoke-check the script parses under POSIX sh at build time (no bashisms).
      ${pkgs.dash}/bin/dash -n $out/bin/hart-display-health
    '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.displayHealth = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Run the post-boot display-health snapshot (hart-display-health): a oneshot
        that records an honest per-dimension verdict (tier / gpu / painted / input /
        scanout / screen) to ${statusFile} (one key=value line each, also echoed to
        the journal) AFTER greetd is up. It is the real-HW observability for the
        never-black tier ladder — it surfaces a "black-but-healthy" Tier-1 (#131)
        and an input-starved seat (#134) once the compositor writes those markers,
        reporting `unknown` honestly until then (never a faked positive).

        It runs in PARALLEL with the desktop (never `before greetd`), is fail-safe
        (a missing marker records its fail-safe value, the unit always succeeds), so
        it can never block or fail the boot. Only meaningful where the tier ladder
        exists, so the unit ships only when hart.sessionSupervisor.enable is set.

        Set to FALSE to skip the snapshot (the verdict file is simply not written;
        nothing else changes).
      '';
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Config  (gated on the hart master toggle + the supervisor topology + this
  # toggle — ships only where the never-black ladder actually exists)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && sup.enable && dh.enable) {
    # The verdict lives under the shared /run/hart (tmpfs). Other consumers rely on
    # this dir at 0750 hart hart (gpu-probe / session-supervisor / compat-smoketest
    # all declare the same rule — tmpfiles de-dupes identical rules).
    systemd.tmpfiles.rules = [
      "d /run/hart 0750 hart hart -"
    ];

    # ── The snapshot oneshot — runs IN PARALLEL with the desktop, AFTER greetd ───
    # wantedBy multi-user.target so it always runs on a graphical boot. Ordered
    # AFTER greetd (the markers it reads only appear once a session is launching) —
    # NOT `before greetd`, so it can NEVER delay first paint. It can never
    # block/fail the boot: oneshot + RemainAfterExit + the script always exits 0 +
    # a bounded TimeoutStartSec (the inner first-paint wait is also bounded).
    systemd.services.hart-display-health = {
      description = "HART OS — post-boot display-health snapshot (writes the honest never-black verdict to ${statusFile})";
      wantedBy = [ "multi-user.target" ];
      after = [ "greetd.service" ];
      # A nixos-rebuild switch must not re-run the snapshot mid-session.
      restartIfChanged = false;
      # Add coreutils to the unit PATH (cat / tr / mkdir / printf / sleep). `path`
      # APPENDS to the unit's default PATH (list-merge) rather than overriding
      # environment.PATH (which collides with NixOS's default at equal priority) —
      # the script does NOT hardcode the store path so the SAME file is dev-box
      # unit-testable.
      path = with pkgs; [ coreutils ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        User = "hart";
        ExecStart = "${probeScript}/bin/hart-display-health";
        # The script's first-paint wait is bounded (HART_DISPLAY_HEALTH_WAIT,
        # default 20s); this outer belt caps the whole run so even a pathological
        # read can't wedge boot. 60s comfortably covers the bounded wait + reads.
        TimeoutStartSec = "60";
      };
    };
  };
}
