{ config, lib, pkgs, ... }:

# ════════════════════════════════════════════════════════════════════════════
# HART OS — Native desktop notifications (mako, the wlroots-native daemon)
# ════════════════════════════════════════════════════════════════════════════
#
# WHY (capability audit — "the brain pushes notifications only as in-shell SSE
#      toasts; there is NO native notification daemon"):
#
#   The glass shell renders the brain's own notifications as in-shell SSE toasts
#   (core.platform.events.broadcast_sse_safe('notification', …) → the WebKit
#   shell). But that path ONLY reaches surfaces the shell host draws — it is the
#   shell talking to itself. A FOREIGN app (a Wine/Android app, an AI-composed
#   `.hartapp`, the robot subsystem) has no way to surface a desktop notification,
#   because there is no `org.freedesktop.Notifications` D-Bus service on the bus
#   for it to call. On GNOME/KDE that service is the shell; on a wlroots session
#   (cage Tier-3 / sway Tier-2 / HART-comp Tier-1) nothing provides it unless we
#   ship a daemon. So the desktop silently drops every non-shell notification.
#
#   This module ships **mako** — the wlroots-native `org.freedesktop.Notifications`
#   daemon — as a graphical-session user service, styled to match the glass shell.
#   Once it owns the D-Bus name, ANY producer can fire a native toast through the
#   standard interface and the desktop shows it.
#
# THE BRAIN → NATIVE BRIDGE (how the brain's SSE notifications become native):
#
#   The brain keeps emitting `broadcast_sse_safe('notification', …)` for the
#   in-shell toast (unchanged — that is the per-user, in-glass surface). To ALSO
#   surface a notification natively (so it persists in the daemon's history, fans
#   out to foreign apps' expectations, and works even when the glass surface is
#   occluded), the brain bridges to the daemon by invoking the standard
#   freedesktop interface — the simplest path being:
#
#       notify-send "<summary>" "<body>"            # libnotify CLI, this module
#       notify-send -u critical -i dialog-warning "<summary>" "<body>"
#
#   `notify-send` is a thin client for the `org.freedesktop.Notifications.Notify`
#   D-Bus method; mako (running as this service) receives the call and draws the
#   toast with the glass styling below. A producer that already speaks D-Bus
#   (the robot bridge, a Qt/GTK app) can call `Notify` directly — same daemon,
#   same result. There is exactly ONE daemon owning the name, so SSE-toast and
#   native-toast are two surfaces over the same intent, not a parallel daemon.
#   (This module ships the daemon + the `notify-send` binary; the brain-side
#   emitter that shells out to it lives in the Python tree and is out of scope
#   here — the contract this module guarantees is "the name is owned and a
#   notify-send exists on PATH".)
#
# NEVER-FAIL (the hart-*.nix house rule — the daemon must NEVER break the session):
#   - graphical-session user service: after/partOf/wantedBy graphical-session.target
#     (the hart-nunba.nix / hart-conky.nix shape) — it is NOT ordered before greetd
#     and is NOT part of the boot/login critical path, so a mako crash can never
#     wedge the seat or block first-paint.
#   - Restart = "on-failure" + RestartSec: a crashed daemon is respawned; a
#     permanently-broken mako just leaves the desktop without native toasts (the
#     in-shell SSE toast still works on every tier), never a black/hung session.
#   - The mako config is referenced by an ABSOLUTE `--config` store path (an
#     `environment.etc` file), so the daemon starts identically regardless of the
#     user's `$XDG_CONFIG_HOME` — no dependence on a home-dir file that may not
#     exist on a fresh / read-only boot.

let
  cfg = config.hart;
  notifyCfg = config.hart.notifications;

  # ── Glass-shell mako config ──────────────────────────────────────────────
  # Matches the canonical glass palette (integrations/agent_engine/theme_service.py
  # ThemeService default: background 0F0E17, accent 00D4AA, glass_bg
  # rgba(15,14,23,0.65)). mako colours are #RRGGBB[AA]; we use the dark glass bg at
  # ~0.92 alpha (legible toast, still reads as the shell's dark glass), the teal
  # accent as the border, white-ish text, rounded corners + sane timeout/anchor so
  # native toasts look like they belong to the HART desktop.
  makoConfig = pkgs.writeText "hart-mako-config" ''
    # HART OS glass-shell notification styling (see hart-notify.nix header).
    # Dark glass background, teal HART accent border, rounded — matches the shell.
    sort=-time
    layer=overlay
    anchor=top-right
    max-visible=5

    # Glass surface: dark bg (#0F0E17) at high-but-not-opaque alpha, teal accent.
    background-color=#0F0E17EB
    text-color=#F5F5F7FF
    border-color=#00D4AAFF
    progress-color=over #00D4AA55

    border-size=2
    border-radius=14
    padding=14
    margin=12
    width=380
    height=160
    font=sans-serif 11

    # Sane lifetime: 6s default; never auto-dismiss a critical one.
    default-timeout=6000
    ignore-timeout=0
    icons=1
    max-icon-size=48

    [urgency=low]
    border-color=#3A3A4AFF
    default-timeout=4000

    [urgency=critical]
    border-color=#F44336FF
    default-timeout=0
  '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.notifications = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Ship a native `org.freedesktop.Notifications` daemon (mako) so the glass
        desktop has real desktop notifications. Foreign apps (Wine/Android),
        AI-composed `.hartapp`s, and the robot can surface a toast via the
        standard D-Bus interface (or `notify-send`); the brain bridges its
        in-shell SSE notifications to native ones the same way. Default ON; the
        daemon is a graphical-session user service that NEVER blocks the boot or
        login — a mako failure just leaves the desktop without native toasts (the
        in-shell SSE toast still works on every tier).
      '';
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Config  (pure no-op when disabled)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && notifyCfg.enable) {

    # mako (the daemon) + libnotify (provides `notify-send`, the client every app —
    # and the brain's bridge — calls). Both on PATH so any producer can fire a
    # native notification through the standard freedesktop interface.
    environment.systemPackages = [
      pkgs.mako        # the wlroots-native org.freedesktop.Notifications daemon
      pkgs.libnotify   # `notify-send` — the standard CLI client for the D-Bus Notify method
    ];

    # ── The notification daemon: a graphical-session user service ──
    # Same never-fail shape as hart-nunba.nix / hart-conky.nix: after + partOf +
    # wantedBy graphical-session.target (NOT before greetd, NOT a boot-critical
    # unit). When the daemon dies it is respawned; when it can't run at all the
    # desktop simply has no native toasts — the seat is never blocked.
    systemd.user.services.hart-notify = {
      description = "HART OS native notification daemon (mako)";
      after = [ "graphical-session.target" ];
      partOf = [ "graphical-session.target" ];
      wantedBy = [ "graphical-session.target" ];

      serviceConfig = {
        # Absolute `--config` store path so the daemon styling is identical
        # regardless of the user's XDG_CONFIG_HOME (works on a fresh / read-only
        # boot with no home-dir mako config present).
        ExecStart = "${pkgs.mako}/bin/mako --config ${makoConfig}";
        # Live-reload the config on SIGHUP (mako honours `makoctl reload`); cheap.
        ExecReload = "${pkgs.mako}/bin/makoctl reload";
        Restart = "on-failure";
        RestartSec = 3;
      };
    };
  };
}
