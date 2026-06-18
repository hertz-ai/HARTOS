{ config, lib, pkgs, hartSrc ? /etc/hart, ... }:

# ════════════════════════════════════════════════════════════════════════════
# HART OS — L4 Freedesktop portals + cross-process screen kill-switch + lock
# ════════════════════════════════════════════════════════════════════════════
#
# WHY (compositor/ROADMAP.md Phase 7 + HART_OS_NATIVE_ARCHITECTURE §L4 +
#      core/ai_sensing.py "Cross-process authority"):
#
#   Phases 5/6 add NATIVE app windows + screencast surfaces. The moment a
#   Flatpak/Wine/Qt app can ask xdg-desktop-portal for a ScreenCast stream, the
#   in-process `core.ai_sensing.allowed('screen')` flag is NOT enough — that flag
#   lives in the shell host's process memory and a separate portal process can't
#   read it. Shipping screencast without a CROSS-PROCESS gate would re-open the
#   screen the human cut: a NET-SECURITY-REGRESSION versus the cage floor (which
#   gives no-native-capture for free because it has no portal at all).
#
#   So Phase 7 promotes the 'screen' sense to a REAL cross-process authority the
#   portal MUST consult, fail-closed, BEFORE any capture — and only then ships the
#   `hart` portal backend. The supreme gate stays `core.ai_sensing`; the portal is
#   a CONSUMER of it with no write path back (no portal verb re-enables a sense).
#
# WHAT THIS MODULE SHIPS (opt-in, default OFF; cage floor unchanged):
#   1. The cross-process screen gate, wired on BOTH sides:
#        - state-holder side: the LiquidUI shell host (liquid_ui_service.py) serves
#          POST /api/shell/ai-sensing — the ONE writer of core.ai_sensing._state —
#          and starts start_authority_server() THERE, on the SHARED socket this
#          module pins via HART_AI_SENSING_SOCK=/run/hart/ai-sensing.sock (NOT the
#          :6777 backend, whose _state is a different process's copy).
#        - portal side: `hart-screencast-gate` (a 1-job binary) calls
#          query_authority('screen') and exits 0=allow / 77=REFUSE, FAIL-CLOSED.
#   2. `wlr-screencopy` routed through the SAME gate: `grim` / `wf-recorder` are
#      shadowed by gate wrappers on the session PATH, so the brain's OWN capture
#      tools (shell_os_apis screenshot/recording) AND any app that shells out to
#      them are refused when 'screen' is cut — real enforcement TODAY, not a flag.
#   3. xdg-desktop-portal `hart` backend registration (.portal) declaring
#      ScreenCast + Settings, with a dbus policy, its own systemd unit, and the
#      gate as the ScreenCast pre-check. (The full D-Bus method impl is the
#      compile-pending half — see HONEST LIMIT below — but the gate + screencopy
#      routing make the capture surface genuinely governed now.)
#   4. `org.freedesktop.portal.Settings` bridge: ThemeService `--hart-*` tokens →
#      `org.freedesktop.appearance` color-scheme + accent-color so native GTK/Qt
#      apps follow the HART theme. NEW capability (not "enhanced"); the in-shell
#      `theme.changed` apply still works on EVERY tier with no portal present.
#   5. REAL session lock for Tier-1/2: a PAM-backed `ext-session-lock-v1` client
#      (swaylock + a `hart-lock` PAM service) so `loginctl lock-session` GRABS
#      input + blanks surfaces + requires PAM re-auth — not a silent no-op. AND
#      the autologin-bypasses-PAM gap is closed by an explicit option.
#
# HONEST LIMIT (the no-phantom rule, ROADMAP §"Honest hardware limit"):
#   No Wayland/portal/PAM path can run on the Windows dev box. This expression is
#   AUTHORED + structurally validated (test_nixos_configs.py / source-guards) and
#   the Python gate is UNIT-tested (tests/unit/test_ai_sensing_authority.py +
#   test_portal_screencast_gate.py); the real portal-deny + PAM-lock are
#   VM/CI-pending (nixosTest tests/portal-screencast.nix). A full Rust/C portal
#   backend implementing the ScreenCast D-Bus methods end-to-end is NOT authored
#   ahead of CI — what IS real and enforcing today is the gate + the wlr-screencopy
#   routing (the tools the brain actually uses). Opt-in until VM-proven; cage
#   Tier-3 (no portal => no native capture) remains the safe floor.
#
# DRY / no-parallel-path:
#   - REUSES core.ai_sensing as the SINGLE supreme gate (start_authority_server /
#     query_authority) — this module starts NO second gate, mirrors NO flag.
#   - REUSES ThemeService.get_css_variables() `--hart-*` tokens as the SINGLE
#     token source for the Settings bridge — never re-hardcodes a colour.
#   - REUSES PAM (the true auth boundary) for the lock — the UX lock is never
#     over-claimed as security; PAM re-auth is the real unlock.
#   - EXTENDS hart-subsystems.nix's existing `xdg.portal` (gtk backend) — adds the
#     hart backend + screencast config; does not fork a second portal stack.

let
  cfg = config.hart;
  ui = config.hart.liquidUI;
  portal = config.hart.portal;

  hartApp = config.hart.package;

  # ── The cross-process screen gate socket (the Phase-7 contract) ──
  # ONE path both the LiquidUI shell host (which serves /api/shell/ai-sensing and
  # therefore holds the canonical core.ai_sensing._state + runs the authority
  # server) and the portal query client pin to, via HART_AI_SENSING_SOCK. /run/hart
  # is the canonical hart runtime socket dir (model-bus/app-bridge already live
  # there, 0750 hart hart). The portal is its own systemd unit with no shared
  # XDG_RUNTIME_DIR, so the env-pinned path is load-bearing.
  senseSock = "/run/hart/ai-sensing.sock";

  # ── hart-screencast-gate: the 1-job fail-closed gate binary ──
  # Exit 0 = capture allowed; exit 77 = REFUSED (the human cut 'screen'). 77 is
  # the conventional "permission denied"-ish sysexits EX_NOPERM-adjacent code; any
  # NON-zero means refuse. It consults query_authority('screen') which FAIL-CLOSES
  # (returns denied) if the authority is unreachable — so a down LiquidUI host, a
  # torn socket, or a path mismatch all DENY capture, never grant it.
  screencastGate = pkgs.writeShellScriptBin "hart-screencast-gate" ''
    set -u
    export HART_AI_SENSING_SOCK="${senseSock}"
    # Single supreme gate: core.ai_sensing.query_authority('screen'), fail-closed.
    if ${hartApp.python}/bin/python - <<'PY'
import sys
try:
    from core.ai_sensing import query_authority
    ok = query_authority('screen')
except Exception:
    ok = False          # fail-closed on ANY error — never capture on doubt
sys.exit(0 if ok else 77)
PY
    then
      exit 0
    else
      echo "hart-screencast-gate: screen capture REFUSED — the human cut the AI's 'screen' sense." >&2
      exit 77
    fi
  '';

  # ── wlr-screencopy routed through the gate ──
  # grim (screenshot) + wf-recorder (recording) are the wlr-screencopy consumers.
  # We shadow them on the session PATH with wrappers that consult the gate FIRST,
  # then exec the real tool only if allowed. This governs BOTH the brain's own
  # capture (shell_os_apis tries `grim` / `wf-recorder` by name) AND any native
  # app that shells out to them — the same fail-closed cut, cross-process.
  #
  # Each wrapper is a writeShellScriptBin (the codebase's proven idiom — NO
  # nested heredoc) so the shebang is byte-0 correct. The real binary is referenced
  # by absolute store path so the wrapper never recurses into itself. The combined
  # dir is placed AHEAD of pkgs.grim on PATH (systemPackages order) so a bare `grim`
  # hits the wrapper. "$@" is single-quoted in the Nix string ('' ⇒ '' $@) so it is
  # the wrapper's runtime args, not a build-time antiquotation.
  gatedGrim = pkgs.writeShellScriptBin "grim" ''
    # HART OS wlr-screencopy gate wrapper (Phase 7) — consult the cross-process
    # screen kill-switch BEFORE capturing; refuse fail-closed when 'screen' is cut.
    ${screencastGate}/bin/hart-screencast-gate || exit 77
    exec ${pkgs.grim}/bin/grim "$@"
  '';
  gatedWfRecorder = pkgs.writeShellScriptBin "wf-recorder" ''
    # HART OS wlr-screencopy gate wrapper (Phase 7) — same cross-process screen
    # gate for screen RECORDING; refuse fail-closed when 'screen' is cut.
    ${screencastGate}/bin/hart-screencast-gate || exit 77
    exec ${pkgs.wf-recorder}/bin/wf-recorder "$@"
  '';
  gatedScreencopy = pkgs.symlinkJoin {
    name = "hart-gated-screencopy";
    paths = [ gatedGrim gatedWfRecorder ];
  };

  # ── Settings bridge generator: ThemeService --hart-* tokens → portal Settings ──
  # Reads the SINGLE token source (ThemeService.get_active_theme) and writes the
  # org.freedesktop.appearance values native GTK/Qt apps read via the portal
  # Settings interface: color-scheme (0=default/no-pref, 1=dark, 2=light) + the
  # accent-color (sRGB 0..1 triple from --hart-accent). Also pushes the GTK
  # gsettings color-scheme (ThemeService already owns this path) so apps that read
  # gsettings directly stay consistent. This is the ONLY place tokens cross into
  # native-app land; it never invents a colour.
  settingsBridge = pkgs.writeShellScriptBin "hart-portal-settings-bridge" ''
    set -u
    export HEVOLVE_DATA_DIR="${cfg.dataDir}"
    exec ${hartApp.python}/bin/python - "$@" <<'PY'
import json, os, sys
try:
    from integrations.agent_engine.theme_service import ThemeService
    theme = ThemeService.get_active_theme()
except Exception:
    theme = {}
colors = theme.get('colors', {}) if isinstance(theme, dict) else {}
dark = bool(theme.get('gtk_prefer_dark', True)) if isinstance(theme, dict) else True
# org.freedesktop.appearance color-scheme: 1=prefer-dark, 2=prefer-light.
color_scheme = 1 if dark else 2
accent = str(colors.get('accent', '00D4AA') or '00D4AA').lstrip('#')
try:
    r = int(accent[0:2], 16) / 255.0
    g = int(accent[2:4], 16) / 255.0
    b = int(accent[4:6], 16) / 255.0
except (ValueError, IndexError):
    r, g, b = 0.0, 0.831, 0.667
out = {
    'org.freedesktop.appearance': {
        'color-scheme': color_scheme,
        'accent-color': [round(r, 4), round(g, 4), round(b, 4)],
    },
}
# Emit the value map the portal backend serves; written atomically to the
# runtime dir so the (compile-pending) D-Bus backend reads ONE source.
target = os.environ.get('HART_PORTAL_SETTINGS_PATH',
                        '/run/hart/portal/appearance.json')
try:
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(out, f)
    os.replace(tmp, target)
except OSError:
    pass
json.dump(out, sys.stdout)
PY
  '';

  # ── ext-session-lock-v1: a REAL, PAM-backed lock for Tier-1/2 ──
  # swaylock implements ext-session-lock-v1 (input grab + blank surfaces) AND
  # authenticates the unlock against PAM. We give it a dedicated `hart-lock` PAM
  # service (pam_unix) so the unlock is a REAL credential check, not a UX dialog.
  # On sway Tier-2 + HART-comp Tier-1 this makes `loginctl lock-session` /
  # `hart-lock` genuinely lock the seat. (Tier-3 cage has no ext-session-lock
  # protocol object — documented honest gap; closed by disabling autologin OR the
  # PAM-backed kiosk lock option below.)
  lockLauncher = pkgs.writeShellScriptBin "hart-lock" ''
    set -u
    # swaylock grabs input + blanks every output as an ext-session-lock surface,
    # and re-auth goes through the hart-lock PAM service (pam_unix => real creds).
    exec ${pkgs.swaylock}/bin/swaylock \
      --daemonize \
      --ignore-empty-password \
      --color 0F0E17 \
      --indicator-caps-lock \
      "$@"
  '';

  # ── The hart portal backend registration (.portal + config) ──
  # Declares the `hart` backend implements ScreenCast + Settings. xdg-desktop-
  # portal routes those interfaces to this backend for the HART-OS desktop env.
  # NOTE: the executable the .portal DBusName points at is the (compile-pending)
  # backend; the ENFORCING surface that ships today is the gate + screencopy
  # routing above. The .portal + dbus policy reserve the name and wire the routing
  # so the backend drops in without a second design.
  #
  # Built with writeText (clean files, NO indented heredoc) + a runCommand install
  # — the SAME idiom as hart-comp.nix's compSession. A heredoc inside a Nix ''
  # string carries the Nix indentation into the file + indents the closing token,
  # which both corrupts the .portal and never terminates the heredoc; writeText
  # avoids the whole class.
  hartPortalFile = pkgs.writeText "hart.portal" ''
    [portal]
    DBusName=org.freedesktop.impl.portal.desktop.hart
    Interfaces=org.freedesktop.impl.portal.ScreenCast;org.freedesktop.impl.portal.Settings;
    UseIn=HART-OS;HART-OS-comp;HART-OS-sway
  '';
  # ScreenCast => hart (gated). Settings => hart (theme bridge). FileChooser /
  # notifications fall back to the gtk backend hart-subsystems.nix already ships.
  hartPortalsConf = pkgs.writeText "hart-os-portals.conf" ''
    [preferred]
    default=gtk
    org.freedesktop.impl.portal.ScreenCast=hart
    org.freedesktop.impl.portal.Settings=hart
  '';
  hartPortalDir = pkgs.runCommand "hart-portal-backend" { } ''
    install -Dm644 ${hartPortalFile} \
      $out/share/xdg-desktop-portal/portals/hart.portal
    install -Dm644 ${hartPortalsConf} \
      $out/share/xdg-desktop-portal/hart-os-portals.conf
  '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.portal = {

    enable = lib.mkEnableOption ''
      HART OS L4 portals: the xdg-desktop-portal `hart` backend (ScreenCast gated
      on the cross-process AI-senses screen kill-switch, Settings bridging the HART
      theme), wlr-screencopy routed through the SAME gate, and a real PAM-backed
      ext-session-lock for Tier-1/2. OPT-IN, default OFF — cage Tier-3 (no portal =
      no native capture) stays the safe floor; the full D-Bus backend impl is
      compile/VM-pending. Enabling ships the gate + screencopy routing + lock,
      which ARE enforcing today.
    '';

    closeAutologinPamGap = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Close the autologin-bypasses-PAM gap HONESTLY. desktop.nix autologins
        `hart-admin` (no PAM at boot), so at Tier-3 cage `loginctl lock-session`
        is a silent no-op (no ext-session-lock object) AND the boot never saw a
        credential. With this TRUE, the lock is made real at every tier: a
        PAM-backed lock binary (`hart-lock`) is on PATH and `loginctl
        lock-session` triggers it, so a locked seat genuinely requires PAM
        re-auth. DEFAULT FALSE so the current frictionless-kiosk boot is
        byte-identical until an operator opts in (a product/identity call —
        a kiosk appliance may WANT no lock; a personal desktop wants one).
        Tier-1/2 already get the real ext-session-lock via swaylock regardless;
        this option is specifically about the Tier-3 cage honest gap + wiring
        loginctl to the lock binary.
      '';
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Config  (opt-in; pure no-op when disabled)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && portal.enable) (lib.mkMerge [
    {
      # The portal backend reuses the canonical glass shell / theme stack, so it
      # is only coherent when LiquidUI (the shell + ThemeService host) is enabled.
      # Fail EVAL loudly rather than ship a Settings bridge with no token source.
      assertions = [
        {
          assertion = ui.enable;
          message =
            "hart.portal.enable requires hart.liquidUI.enable = true — the portal "
            + "Settings bridge reads ThemeService (--hart-* tokens) from the LiquidUI "
            + "host, and the screencast gate consults the brain's core.ai_sensing "
            + "authority. Enable LiquidUI or disable hart.portal.";
        }
      ];

      # Runtime socket + portal-settings dirs (same /run/hart contract as the
      # model-bus/app-bridge sockets; 0750 hart hart). The LiquidUI shell process
      # binds the authority socket here; the Settings bridge writes appearance.json
      # here; the portal (as the hart user) traverses /run/hart to consult it.
      systemd.tmpfiles.rules = [
        "d /run/hart        0750 hart hart -"
        "d /run/hart/portal 0750 hart hart -"
      ];

      # ── Pin the AI-senses authority socket on the CANONICAL state holder ──
      # liquid_ui_service.py serves POST /api/shell/ai-sensing (the ONE _state
      # writer) and starts core.ai_sensing.start_authority_server() there. Pin its
      # socket to the shared /run/hart path BOTH the LiquidUI host and the portal
      # gate read via HART_AI_SENSING_SOCK, and grant the hart-liquid-ui sandbox
      # write access to /run/hart (its base ReadWritePaths only covers
      # /run/hart/liquid-ui) so the AF_UNIX bind succeeds. The gate binary exports
      # the same env (screencastGate above), so both sides resolve ONE socket.
      systemd.services.hart-liquid-ui = {
        environment.HART_AI_SENSING_SOCK = senseSock;
        serviceConfig.ReadWritePaths = [ "/run/hart" ];
      };

      # The gate + screencopy wrappers + settings bridge + lock on PATH. The
      # gated screencopy shadow MUST come BEFORE pkgs.grim/pkgs.wf-recorder so the
      # wrappers win on PATH (an app that runs bare `grim` hits the gate). We do
      # NOT also add the raw pkgs.grim to systemPackages — the wrapper bundles the
      # real binary by absolute store path, so it's reachable without being on PATH.
      environment.systemPackages = [
        gatedScreencopy        # grim/wf-recorder gate wrappers (PATH-shadow)
        screencastGate
        settingsBridge
        lockLauncher
        hartPortalDir
        pkgs.xdg-desktop-portal
        pkgs.swaylock
      ];

      # Register the hart portal backend + its preference config with xdg-desktop-
      # portal. EXTENDS hart-subsystems.nix's xdg.portal (gtk backend stays for
      # file-chooser/notifications); adds the hart ScreenCast+Settings backend and
      # the screencast config the wlroots screencopy path reads.
      xdg.portal = {
        extraPortals = [ hartPortalDir ];
        # config.* lands as /etc/xdg-desktop-portal/*; route ScreenCast+Settings
        # to the hart backend for the HART-OS desktop environments, gtk for the
        # rest. (Mirrors the hart-os-portals.conf above; this is the system path
        # xdg-desktop-portal actually consults.)
        config."HART-OS" = {
          default = [ "gtk" ];
          "org.freedesktop.impl.portal.ScreenCast" = [ "hart" ];
          "org.freedesktop.impl.portal.Settings" = [ "hart" ];
        };
      };

      # ── dbus policy: only the session user may own/call the hart portal name ──
      # The ScreenCast impl is OS-level sensitive (it can hand an app a live
      # screen stream); lock it to the hart user, deny default. Same posture as
      # com.hart.Compositor's policy in hart-comp.nix.
      services.dbus.packages = [
        (pkgs.writeTextDir "share/dbus-1/system.d/org.freedesktop.impl.portal.desktop.hart.conf" ''
          <?xml version="1.0" encoding="UTF-8"?>
          <!DOCTYPE busconfig PUBLIC
           "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
           "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
          <busconfig>
            <!-- HART OS xdg-desktop-portal `hart` backend (Phase 7).
                 ScreenCast is gated fail-closed on core.ai_sensing('screen');
                 no portal verb can re-enable a cut AI sense. -->
            <policy user="hart">
              <allow own="org.freedesktop.impl.portal.desktop.hart"/>
              <allow send_destination="org.freedesktop.impl.portal.desktop.hart"/>
            </policy>
            <policy context="default">
              <deny own="org.freedesktop.impl.portal.desktop.hart"/>
              <deny send_destination="org.freedesktop.impl.portal.desktop.hart"/>
            </policy>
          </busconfig>
        '')
      ];

      # ── PAM service for the lock: real credential re-auth (not a UX dialog) ──
      # swaylock authenticates the unlock against the `hart-lock` PAM service; a
      # bare pam_unix stack means a genuine password check. This is what makes the
      # ext-session-lock a SECURITY boundary, not just a blanked surface.
      security.pam.services.hart-lock = {};

      # The Settings bridge runs once at session start to publish appearance.json
      # from the active theme, and on `theme.changed` the brain re-runs it. A
      # user-service oneshot keeps it on the SINGLE token source. partOf the
      # graphical session so it tracks login; the in-shell apply still works with
      # no portal (theme.changed drives the shell directly on every tier).
      systemd.user.services.hart-portal-settings = {
        description = "HART OS portal Settings — publish theme tokens to native apps";
        after = [ "graphical-session.target" ];
        partOf = [ "graphical-session.target" ];
        wantedBy = [ "graphical-session.target" ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = "${settingsBridge}/bin/hart-portal-settings-bridge";
          Environment = [ "HEVOLVE_DATA_DIR=${cfg.dataDir}" ];
        };
      };
    }

    # ── Close the autologin-bypasses-PAM gap (opt-in, honest) ──
    (lib.mkIf portal.closeAutologinPamGap {
      # Wire `loginctl lock-session` to the real PAM-backed lock at EVERY tier so
      # the Tier-3 cage no-op is closed. systemd's session lock signal is consumed
      # by a user service that runs hart-lock — so lock-session is no longer a
      # silent no-op even on cage. (The lock surface on cage is swaylock as a
      # plain overlay client; the PAM re-auth is the real boundary.)
      systemd.user.services.hart-session-lock-handler = {
        description = "HART OS — honor loginctl lock-session with a PAM-backed lock";
        after = [ "graphical-session.target" ];
        partOf = [ "graphical-session.target" ];
        wantedBy = [ "graphical-session.target" ];
        serviceConfig = {
          Type = "simple";
          # Listen for the systemd-logind Lock signal on the session and run the
          # PAM-backed lock. dbus-monitor is the minimal, dependency-free listener;
          # absolute store paths (the user service PATH is minimal).
          ExecStart = pkgs.writeShellScript "hart-session-lock-handler" ''
            set -u
            ${pkgs.dbus}/bin/dbus-monitor --system \
              "type='signal',interface='org.freedesktop.login1.Session',member='Lock'" |
            while read -r line; do
              case "$line" in
                *member=Lock*) ${lockLauncher}/bin/hart-lock || true ;;
              esac
            done
          '';
          Restart = "on-failure";
          RestartSec = 5;
        };
      };
    })
  ]);
}
