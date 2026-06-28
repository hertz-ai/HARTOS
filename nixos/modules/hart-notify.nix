{ config, lib, pkgs, ... }:

# ════════════════════════════════════════════════════════════════════════════
# HART OS - Native desktop notifications (mako, the wlroots-native daemon)
# ════════════════════════════════════════════════════════════════════════════
#
# WHY (capability audit - "the brain pushes notifications only as in-shell SSE
#      toasts; there is NO native notification daemon"):
#
#   The glass shell renders the brain's own notifications as in-shell SSE toasts
#   (core.platform.events.broadcast_sse_safe('notification', …) → the WebKit
#   shell). But that path ONLY reaches surfaces the shell host draws - it is the
#   shell talking to itself. A FOREIGN app (a Wine/Android app, an AI-composed
#   `.hartapp`, the robot subsystem) has no way to surface a desktop notification,
#   because there is no `org.freedesktop.Notifications` D-Bus service on the bus
#   for it to call. On GNOME/KDE that service is the shell; on a wlroots session
#   (cage Tier-3 / sway Tier-2 / HART-comp Tier-1) nothing provides it unless we
#   ship a daemon. So the desktop silently drops every non-shell notification.
#
#   This module ships **mako** - the wlroots-native `org.freedesktop.Notifications`
#   daemon - as a graphical-session user service, styled to match the glass shell.
#   Once it owns the D-Bus name, ANY producer can fire a native toast through the
#   standard interface and the desktop shows it.
#
# THE BRAIN → NATIVE BRIDGE (how the brain's SSE notifications become native):
#
#   The brain keeps emitting `broadcast_sse_safe('notification', …)` for the
#   in-shell toast (unchanged - that is the per-user, in-glass surface). To ALSO
#   surface a notification natively (so it persists in the daemon's history, fans
#   out to foreign apps' expectations, and works even when the glass surface is
#   occluded), the brain bridges to the daemon through the standard freedesktop
#   interface. TWO clients reach the daemon:
#
#     notify-send "<summary>" "<body>"     # libnotify CLI - FOREIGN apps, ungated
#     hart-notify-send "<summary>" "<body>" # the AI's emitter - gated (see below)
#
#   `notify-send` is the plain libnotify client every foreign app already uses; it
#   is NOT shadowed or gated (a Wine/Android/.hartapp app is not the AI and is not
#   subject to the AI's sense cut). `hart-notify-send` is the AI's privacy-
#   respecting emitter: it consults the SAME cross-process screen kill-switch the
#   screencast portal uses BEFORE painting, so a toast the brain composes honours
#   the human's "you may not use my screen" exactly like screen capture does. Both
#   end up calling the SAME mako daemon (one owner of the bus name) - SSE-toast and
#   native-toast are surfaces over one intent, not a parallel daemon.
#
# RESPECT THE SCREEN KILL-SWITCH / PRIVACY (the privacy-first contract):
#
#   1. `hart-notify-send` consults core.ai_sensing.query_authority('screen') over
#      the pinned /run/hart/ai-sensing.sock - the EXACT canonical gate the
#      screencast wrapper (hart-portal.nix) consults - and FAIL-CLOSES: if the
#      human cut 'screen', or the authority is unreachable/torn, the native toast
#      is SUPPRESSED (exit 77, paint nothing). Nothing is lost - the in-shell SSE
#      toast still delivers on every tier; only the AI's *native painting* on the
#      cut screen is withheld. There is ONE source of truth for the cut
#      (core.ai_sensing); this module REUSES it, it does not invent a second gate.
#   2. Do-Not-Disturb: the config defines a `do-not-disturb` mako mode
#      (`invisible=1`); `makoctl mode -t do-not-disturb` silences every toast. The
#      `doNotDisturb` option starts the daemon already in that mode.
#   3. Privacy mode: a `privacy` mako mode masks the body so private content is
#      never painted on screen - `makoctl mode -a privacy` shows only the app name.
#
# NEVER-FAIL (the hart-*.nix house rule - the daemon must NEVER break the session):
#   - graphical-session user service: after/partOf/wantedBy graphical-session.target
#     (the hart-nunba.nix / hart-conky.nix shape) - it is NOT ordered before greetd
#     and is NOT part of the boot/login critical path, so a mako crash can never
#     wedge the seat or block first-paint.
#   - Restart = "on-failure" + RestartSec: a crashed daemon is respawned; a
#     permanently-broken mako just leaves the desktop without native toasts (the
#     in-shell SSE toast still works on every tier), never a black/hung session.
#   - The mako config is referenced by an ABSOLUTE `--config` store path, so the
#     daemon starts identically regardless of the user's `$XDG_CONFIG_HOME` - no
#     dependence on a home-dir file that may not exist on a fresh / read-only boot.
#   - The AI emitter binds a 1-second socket timeout (core.ai_sensing default) and
#     fail-closes on ANY error, so it can never hang the brain or crash on a node
#     with no authority server.

let
  cfg = config.hart;
  notifyCfg = config.hart.notifications;

  # The HART application package exposes `.python`; the gated AI emitter runs the
  # canonical core.ai_sensing consult through it (same as hart-portal.nix's gate).
  # Lazy - only forced inside the `mkIf` config block, which is reached only on a
  # graphical variant that sets `hart.package` (every real config + the test node).
  hartApp = cfg.package;

  # The ONE cross-process screen kill-switch socket (the Phase-7 contract). The
  # LiquidUI shell host binds it (HART_AI_SENSING_SOCK); the portal screencast gate
  # AND this notification emitter pin the same path. No new path, no new gate.
  senseSock = "/run/hart/ai-sensing.sock";

  # ── hart-notify-send: the AI's privacy-respecting native emitter ──────────────
  # Consults core.ai_sensing.query_authority('screen') (fail-closed) BEFORE
  # painting. Exit 77 == REFUSED (the human cut 'screen', or authority unreachable)
  # - the same convention hart-screencast-gate uses. On allow, exec the plain
  # libnotify client. This binary is what the brain shells out to; foreign apps
  # keep using the ungated `notify-send`. Mirrors hart-portal.nix's proven gate
  # idiom (single-quoted heredoc => no Nix antiquotation inside the python).
  # The python heredoc body + its `PY` terminator are kept FLUSH-LEFT (column 0),
  # exactly like hart-portal.nix's gate, so the Nix `''` indentation-strip can never
  # shift the heredoc terminator off column 0. Single-quoted `<<'PY'` => the shell
  # does not expand inside; there is no `${` in the python so Nix leaves it verbatim.
  aiNotifySend = pkgs.writeShellScriptBin "hart-notify-send" ''
    set -u
    export HART_AI_SENSING_SOCK="${senseSock}"
    # Put the HART app root (carries core/ via `cp -r . $out`) on PYTHONPATH so the
    # `from core.ai_sensing import query_authority` resolves no matter the caller's
    # cwd - the hart-onboarding.nix / hart-vision.nix idiom.
    export PYTHONPATH="${hartApp}:''${PYTHONPATH:-}"
    # Single supreme gate: core.ai_sensing.query_authority('screen'), fail-closed
    # on ANY error (down/torn authority, missing python dep) - never paint on doubt.
    if ${hartApp}/bin/python - <<'PY'
import sys
try:
    from core.ai_sensing import query_authority
    ok = query_authority('screen')
except Exception:
    ok = False
sys.exit(0 if ok else 77)
PY
    then
      exec ${pkgs.libnotify}/bin/notify-send "$@"
    else
      echo "hart-notify-send: native toast SUPPRESSED: the human cut the AI's 'screen' sense (the in-shell SSE toast still delivers)." >&2
      exit 77
    fi
  '';

  # ── Glass-shell mako config ──────────────────────────────────────────────
  # Matches the canonical glass palette (integrations/agent_engine/theme_service.py
  # ThemeService default: background 0F0E17, accent 00D4AA, glass_bg
  # rgba(15,14,23,0.65)). mako colours are #RRGGBB[AA]; we use the dark glass bg at
  # ~0.92 alpha (legible toast, still reads as the shell's dark glass), the teal
  # accent as the border, white-ish text, rounded corners + sane timeout/anchor so
  # native toasts look like they belong to the HART desktop. anchor + default
  # timeout come from the module options.
  makoConfig = pkgs.writeText "hart-mako-config" ''
    # HART OS glass-shell notification styling (see hart-notify.nix header).
    # Dark glass background, teal HART accent border, rounded to match the shell.
    sort=-time
    layer=overlay
    anchor=${notifyCfg.position}
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

    # Sane lifetime: configurable default; never auto-dismiss a critical one.
    default-timeout=${toString notifyCfg.defaultTimeout}
    ignore-timeout=0
    icons=1
    max-icon-size=48

    [urgency=low]
    border-color=#3A3A4AFF
    default-timeout=4000

    [urgency=critical]
    border-color=#F44336FF
    default-timeout=0

    # ── Do-Not-Disturb ──  `makoctl mode -t do-not-disturb` silences every toast.
    # `hart.notifications.doNotDisturb = true` starts the daemon already in it.
    [mode=do-not-disturb]
    invisible=1

    # ── Privacy ──  `makoctl mode -a privacy` masks the body so private content is
    # never painted on screen (only the app name shows), so a glance, an
    # over-the-shoulder, or a capture cannot read the message body. Toggle it
    # alongside the screen kill-switch.
    [mode=privacy]
    format=<b>%a</b>
  '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.notifications = {
    enable = lib.mkOption {
      type = lib.types.bool;
      # Privacy-first default: ON where there is a screen to paint on (desktop /
      # phone), OFF on the headless variants (server / edge) so their ISOs do not
      # carry mako needlessly. The enable gate is still honoured everywhere - a
      # headless node can opt in, a graphical node can opt out.
      default = lib.elem cfg.variant [ "desktop" "phone" ];
      defaultText = lib.literalExpression ''lib.elem config.hart.variant [ "desktop" "phone" ]'';
      description = ''
        Ship a native `org.freedesktop.Notifications` daemon (mako) so the glass
        desktop has real desktop notifications. Foreign apps (Wine/Android),
        AI-composed `.hartapp`s, and the robot can surface a toast via the
        standard D-Bus interface (or `notify-send`); the brain bridges its
        in-shell SSE notifications to native ones through the privacy-respecting
        `hart-notify-send`. The daemon is a graphical-session user service that
        NEVER blocks boot or login - a mako failure just leaves the desktop
        without native toasts (the in-shell SSE toast still works on every tier).
      '';
    };

    position = lib.mkOption {
      type = lib.types.enum [
        "top-right" "top-left" "top-center"
        "bottom-right" "bottom-left" "bottom-center"
        "center"
      ];
      default = "top-right";
      description = "Screen anchor for native toasts (maps to mako `anchor`).";
    };

    defaultTimeout = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 6000;
      description = ''
        Default toast lifetime in milliseconds (maps to mako `default-timeout`).
        0 means never auto-dismiss. Critical-urgency toasts always stay until
        dismissed regardless of this value.
      '';
    };

    doNotDisturb = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Start the daemon already in Do-Not-Disturb mode (every toast silenced
        until `makoctl mode -t do-not-disturb` toggles it off). The DnD mode is
        always defined in the config; this only sets the initial state.
      '';
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Config  (pure no-op when disabled)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && notifyCfg.enable) {

    # mako (the daemon) + libnotify (the foreign-app `notify-send` client) + the
    # AI's gated `hart-notify-send` emitter. mako also ships `makoctl` (mode/DnD
    # control). Any producer can fire a native notification through the standard
    # freedesktop interface; the AI's path is screen-kill-switch gated.
    environment.systemPackages = [
      pkgs.mako        # the wlroots-native org.freedesktop.Notifications daemon (+ makoctl)
      pkgs.libnotify   # `notify-send` - the standard CLI client for foreign apps
      aiNotifySend     # `hart-notify-send` - the AI's privacy-gated emitter
    ];

    # ── The notification daemon: a graphical-session user service ──
    # Same never-fail shape as hart-nunba.nix / hart-conky.nix: after + partOf +
    # wantedBy graphical-session.target (NOT before greetd, NOT a boot-critical
    # unit). When the daemon dies it is respawned; when it can't run at all the
    # desktop simply has no native toasts - the seat is never blocked.
    systemd.user.services.hart-notify = {
      description = "HART OS native notification daemon (mako)";
      after = [ "graphical-session.target" ];
      partOf = [ "graphical-session.target" ];
      wantedBy = [ "graphical-session.target" ];

      serviceConfig = {
        # mako owns `org.freedesktop.Notifications` on the session bus, so use the
        # UPSTREAM-canonical mako.service shape: Type=dbus marks the unit started
        # only once mako has ACTUALLY acquired the name (its Wayland + IPC are up).
        # That lets dependent units order after a real daemon AND makes the
        # doNotDisturb ExecStartPost below run AFTER mako's IPC socket exists,
        # instead of racing a Type=simple immediate fork (which would makoctl-miss
        # and silently never enter DnD). Reuses the proven daemon readiness
        # contract rather than reinventing it.
        Type = "dbus";
        BusName = "org.freedesktop.Notifications";
        # Absolute `--config` store path so the daemon styling is identical
        # regardless of the user's XDG_CONFIG_HOME (works on a fresh / read-only
        # boot with no home-dir mako config present).
        ExecStart = "${pkgs.mako}/bin/mako --config ${makoConfig}";
        # Live-reload the config (mako honours `makoctl reload`); cheap.
        ExecReload = "${pkgs.mako}/bin/makoctl reload";
        Restart = "on-failure";
        RestartSec = 3;
      } // lib.optionalAttrs notifyCfg.doNotDisturb {
        # Enter DnD once the daemon owns the bus name (Type=dbus means mako's IPC
        # is up by now, so makoctl reaches it). The leading `-` still makes systemd
        # ignore a failure, so a transient miss can never wedge the unit (never-fail).
        ExecStartPost = "-${pkgs.mako}/bin/makoctl mode -a do-not-disturb";
      };
    };
  };
}
