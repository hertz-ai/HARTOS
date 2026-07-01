{ config, lib, pkgs, ... }:

# ════════════════════════════════════════════════════════════════════════════
# HART OS - boot-time audio rescue (unmute + sane default volume on the sink)
# ════════════════════════════════════════════════════════════════════════════
#
# WHY (a real-HW failure the steward hit):
#   A sound SINK existed but was MUTED or pinned at volume 0 on boot, so the
#   desktop had "no audio out" with no obvious cause. WirePlumber persists the
#   per-user mute/volume state across reboots, so a once-muted default sink stays
#   silent forever. A fresh OS that boots SILENT feels broken.
#
# WHAT this module ships:
#   A graphical-session USER oneshot (`hart-audio-unmute`) that, once
#   PipeWire/WirePlumber are up, UNMUTES the default sink and, ON FIRST BOOT,
#   sets it to the full default (100%) so a fresh OS is audible out of the box;
#   on every LATER boot it only rescues a SILENT (level-0) sink. It also does a
#   hotplug-safe default-sink RESELECTION (promote a sink to default when none is
#   assigned). It runs in the user session (where wpctl/pactl can reach the
#   per-user PipeWire socket), NOT as a root system unit (a root unit cannot see
#   the user's PipeWire instance). The decision logic lives in
#   ../modules/hart-audio-unmute.sh so a portable behavioural test can exercise
#   the REAL script against stub wpctl/pactl.
#
# DEGRADE-NOT-DIE (the never-brick contract):
#   The script is `set -u` (NOT -e), every probe is guarded, and it ALWAYS exits
#   0. No default sink -> clean no-op. Neither wpctl nor pactl present -> clean
#   no-op. A wpctl/pactl that errors -> clean no-op. The unit is a oneshot with a
#   bounded TimeoutStartSec, so it can never wedge, crash, or fail the session.
#   It is also socket-aware: it does NOTHING destructive (it only RAISES a silent
#   sink), so a headless / no-audio machine is simply unaffected.
#
# NEVER CLOBBER A DELIBERATE LEVEL:
#   The full-volume set is a FIRST-BOOT-only, once-per-user action (gated on a
#   per-user stamp). On every later boot it only rescues a SILENT default (muted,
#   or level 0). A user who set 30% on purpose keeps 30% across logins - the
#   unmute is unconditional (a muted sink IS the bug), but the level set is
#   conditional on first boot OR the sink reading 0.
#
# SCOPE / no-op rule: gated on `cfg.enable` AND `services.pipewire.enable`, so it
#   is a PURE no-op on the server/edge variants (no PipeWire) and active only on
#   the desktop/phone variants that actually configure audio. Privacy-first: this
#   is a LOCAL capability, ON by default where audio exists (no opt-in friction),
#   nothing leaves the device.

let
  cfg = config.hart;
  audio = config.hart.audio;

  # The rescue logic as a standalone script (readFile so it is ONE source the
  # unit test also runs). The configured floor volume is passed as $1.
  rescueScript = pkgs.writeShellScript "hart-audio-unmute"
    (builtins.readFile ./hart-audio-unmute.sh);

  # The unit PATH is minimal; reference every tool the script calls. wpctl ships
  # with WirePlumber, pactl with the pulseaudio package (also present on a
  # PipeWire-pulse system). coreutils gives sleep/printf/head/tr; gnugrep/gnused
  # the parse helpers. Attr-guarded so a nixpkgs rev lacking wireplumber cannot
  # break evaluation (the drm_info attr-guard pattern).
  audioBins = (with pkgs; [ coreutils gnugrep gnused ])
    ++ lib.optional (pkgs ? wireplumber) pkgs.wireplumber
    ++ lib.optional (pkgs ? pulseaudio)  pkgs.pulseaudio;
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.audio.bootUnmute = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Run a graphical-session oneshot that UNMUTES the default audio sink, sets
        it to bootVolumePercent on FIRST boot (so a fresh OS is audible), and on
        later boots rescues the level only when it reads 0 - so the desktop never
        boots silent because of a persisted mute / volume-0 state. Also does a
        hotplug-safe default-sink reselection. A pure no-op when there is no
        default sink or no wpctl/pactl. Active only where services.pipewire.enable
        is true (desktop/phone); a no-op on server/edge.
      '';
    };

    bootVolumePercent = lib.mkOption {
      type = lib.types.ints.between 0 150;
      default = 100;
      description = ''
        The default level (percent) the rescue sets on the default sink. On FIRST
        boot it is applied unconditionally so a brand-new install is audible out
        of the box (steward: default sink at 100%). On later boots it is applied
        only WHEN the sink reads 0 (a silent-default rescue); a deliberate
        non-zero user level is then left untouched - it never clobbers a chosen
        volume after the first boot.
      '';
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Config  (no-op unless hart is enabled AND PipeWire is configured)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && config.services.pipewire.enable
                     && audio.bootUnmute.enable) {

    # A USER oneshot in the graphical session. Ordered AFTER PipeWire +
    # WirePlumber so a default sink can exist; wantedBy default.target so it runs
    # on every session start. It NEVER blocks the system boot (a user unit) and
    # the script self-bounds, but cap it anyway so even a pathological wpctl hang
    # cannot hold the user session's startup transaction.
    systemd.user.services.hart-audio-unmute = {
      description = "HART OS - unmute + sane default volume on the default audio sink";
      after = [ "pipewire.service" "wireplumber.service" "pipewire-pulse.service" ];
      wants = [ "pipewire.service" "wireplumber.service" ];
      wantedBy = [ "default.target" ];
      # A nixos-rebuild switch must not re-stomp the user's live volume mid-session.
      restartIfChanged = false;
      path = audioBins;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${rescueScript} ${toString audio.bootUnmute.bootVolumePercent}";
        TimeoutStartSec = "30s";
      };
    };

    # Make the rescue runnable by hand from a recovery context too:
    #   hart-audio-unmute 60
    environment.systemPackages = [
      (pkgs.runCommand "hart-audio-unmute-cli" { } ''
        mkdir -p $out/bin
        ln -s ${rescueScript} $out/bin/hart-audio-unmute
      '')
    ];
  };
}
