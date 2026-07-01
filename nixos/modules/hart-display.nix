{ config, lib, pkgs, ... }:

# ════════════════════════════════════════════════════════════════════════════
# HART OS — DISPLAY MANAGEMENT (resolution / per-output scale / font scaling /
#           multi-monitor) — additive, boot-safe, degrade-not-die
# ════════════════════════════════════════════════════════════════════════════
#
# 64-BIT NATIVE (confirmed): HART OS is x86_64-linux native 64-bit (flake.nix
# builds x86_64-linux primary + aarch64/riscv64, all 64-bit; the only 32-bit
# surface anywhere is enable32Bit for Proton/DXVK app compat, never the OS
# userland). Nothing in display management is 32-bit; wlr-randr / kanshi /
# fontconfig are all native 64-bit store paths.
#
# WHAT THIS SHIPS (compositor-agnostic where it can be, honest where it cannot):
#   1. FONT SCALING — the one lever that works on ALL THREE tiers (hart-comp /
#      sway / cage) because it is env + fontconfig, NOT compositor IPC and NOT
#      shell JS: GDK_DPI_SCALE (scales GTK/WebKitGTK text DPI — i.e. the glass
#      shell's own text) + a fontconfig `dpi` edit (every fontconfig client).
#      Driven by hart.display.fontScale. DEFER-TO-ACCESSIBILITY: written with
#      lib.mkDefault on the SAME option path (environment.variables.GDK_DPI_SCALE)
#      that hart-accessibility.nix uses for its magnifier, so the a11y override
#      always wins when enabled — one namespace, deterministic precedence, never a
#      second parallel writer of the same env var.
#   2. MULTI-MONITOR — wlr-randr (live mode/scale/position on any wlr-output-
#      management compositor: Tier-2 sway today) + a NEVER-FAIL kanshi user daemon
#      that re-applies a SAVED arrangement on hotplug + across reboot. The settings
#      backend (shell_desktop_apis.py) writes the kanshi profile + a JSON mirror;
#      this module only ships the tools + the daemon + seeds an empty (safe-default)
#      config so kanshi has a file to read.
#   3. RESOLUTION / PER-OUTPUT SCALE — enumerated + applied live by the settings
#      backend via swaymsg/wlr-randr; persisted via the kanshi profile above.
#
# DEGRADE-NOT-DIE (the never-fail contract):
#   * The kanshi daemon is a USER service (systemd.user) — it can NEVER block or
#     fail the SYSTEM boot. On a compositor without wlr-output-management (Tier-1
#     hart-comp today, or Tier-3 cage) kanshi simply cannot bind the protocol and
#     exits; Restart=on-failure is CAPPED (StartLimitBurst) so it can never restart-
#     storm a core, then it gives up — the compositor keeps its own safe default
#     (every output enabled at its preferred mode). No saved profile == that same
#     safe default. A wrong/empty kanshi config is a no-op, never a brick.
#   * The font lever is identity at fontScale = 1.0 (GDK_DPI_SCALE unset, no
#     fontconfig edit) so the DEFAULT boot is byte-for-byte unchanged. It only ever
#     ADDS an env var + a fontconfig snippet; it removes nothing.
#   * #132 respected: this module NEVER touches GPU driver selection — no nvidia/
#     amdgpu force, no hardware.opengl change. The cage Tier-3 software floor and
#     every GL force elsewhere are untouched (the floor stays the floor).
#
# SCOPE LIMIT (honest): Tier-1 hart-comp advertises only wl_output + xdg-output, NOT
# zwlr_output_manager_v1, so wlr-randr / kanshi cannot drive multi-output on Tier-1
# yet (that is a compositor Rust change, tracked separately). On Tier-1 this module
# degrades to font scaling + the single preferred-mode output the compositor already
# drives. Multi-monitor arrange is live on Tier-2 sway today.

let
  cfg = config.hart;
  disp = config.hart.display;

  # Derived font DPI (fontconfig wants a number; 96 dpi is the 100% baseline).
  # builtins.ceil/floor are available in this Nix (hart-accessibility.nix already
  # uses builtins.ceil); a plain float here keeps the fontconfig <double> exact.
  fontDpi = 96.0 * disp.fontScale;

  # Seed an EMPTY kanshi config on first session start so the daemon has a file to
  # read and NO-OPS (compositor default = the safe all-outputs-at-preferred mode)
  # until the user saves an arrangement in Settings — which rewrites this file with
  # real per-output profiles and SIGHUPs the daemon. We never clobber an existing
  # config (the user's saved layout is the source of truth). `''${...}` escapes a
  # literal shell ${} inside the Nix '' string (the Nunba lesson: Nix collapses
  # un-escaped ${} as interpolation).
  kanshiSeed = pkgs.writeShellScript "hart-kanshi-seed" ''
    set -u
    cfgdir="''${XDG_CONFIG_HOME:-$HOME/.config}/kanshi"
    mkdir -p "$cfgdir" 2>/dev/null || true
    cfgfile="$cfgdir/config"
    if [ ! -e "$cfgfile" ]; then
      cat > "$cfgfile" <<'KANSHI_EOF'
# HART OS multi-monitor profiles (managed by the display settings backend).
# Empty by default: kanshi no-ops and the compositor keeps its safe default mode
# (every output enabled at its preferred mode). Saving an arrangement in Settings
# rewrites this file with real per-output profiles, re-applied on hotplug.
KANSHI_EOF
    fi
    exit 0
  '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.display = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Ship HART OS display management: the resolution/scale tools (wlr-randr),
        the compositor-agnostic font-scaling lever (GDK_DPI_SCALE + fontconfig dpi
        from hart.display.fontScale), and the never-fail multi-monitor kanshi
        daemon. ON by default (a local OS capability, privacy-first — nothing here
        leaves the device). The font lever is identity at fontScale = 1.0, so the
        default boot is unchanged. Set FALSE to drop all of it (no tools, no daemon,
        no env) — the compositor still drives its own single preferred-mode output.
      '';
    };

    fontScale = lib.mkOption {
      type = lib.types.float;
      default = 1.0;
      description = ''
        Global TEXT scale factor (1.0 = 100%, 1.25 = 125%, 1.5 = 150%). Applied
        compositor-agnostically via GDK_DPI_SCALE (GTK/WebKitGTK — the glass shell's
        own text) and a fontconfig `dpi` edit (= 96 * fontScale), NOT via shell JS or
        compositor IPC, so it works on every tier. Written with lib.mkDefault so the
        accessibility magnifier (hart.accessibility.fontScale) overrides it when
        enabled. At 1.0 nothing is emitted (the default boot is byte-for-byte
        unchanged).
      '';
    };

    multiMonitor = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Ship the multi-monitor stack: wlr-randr (live per-output mode/scale/position)
        + a NEVER-FAIL kanshi user daemon that re-applies the SAVED arrangement on
        hotplug and across reboot. The arrangement is written by the settings backend
        (POST /api/shell/displays/profile). On a compositor without wlr-output-
        management (Tier-1 hart-comp, Tier-3 cage) kanshi is a capped, harmless no-op
        and the compositor keeps its safe default. Set FALSE to ship only single-
        display font scaling (no kanshi, no wlr-randr).
      '';
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Config  (gated on the hart master toggle + this module's toggle)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && disp.enable) (lib.mkMerge [

    # ── 1. Tools + font scaling (always, when the module is on) ───────────────
    {
      # wlr-randr: the wlroots output backend the settings panels + manual use call.
      # (hart-sway-tier1 also ships it, but only when swayTier1 is on; shipping it
      # here makes the display panels work on any wlr-output-management session. The
      # closure de-dupes identical store paths.)
      environment.systemPackages = [ pkgs.wlr-randr ];

      # FONT SCALING — env lever (GTK/WebKitGTK text DPI). Same option path
      # (environment.variables.GDK_DPI_SCALE) hart-accessibility.nix uses, written
      # with mkDefault so the a11y magnifier wins when enabled. Only emitted when
      # the scale actually differs from 100% (identity boot unchanged at 1.0).
      environment.variables = lib.mkIf (disp.fontScale != 1.0) {
        GDK_DPI_SCALE = lib.mkDefault (toString disp.fontScale);
      };

      # FONT SCALING — fontconfig lever (every fontconfig client). A `dpi` edit
      # appended to /etc/fonts; only when scale != 1.0 so the default boot adds no
      # fontconfig snippet. Does NOT force fontconfig.enable (desktop already has it;
      # on a headless server this is an inert string and never pulls fontconfig in).
      fonts.fontconfig.localConf = lib.optionalString (disp.fontScale != 1.0) ''
        <?xml version="1.0"?>
        <!DOCTYPE fontconfig SYSTEM "fonts.dtd">
        <fontconfig>
          <!-- HART OS font scaling: hart.display.fontScale = ${toString disp.fontScale} -->
          <match target="pattern">
            <edit name="dpi" mode="assign">
              <double>${toString fontDpi}</double>
            </edit>
          </match>
        </fontconfig>
      '';
    }

    # ── 2. Multi-monitor: wlr-randr + the never-fail kanshi user daemon ───────
    (lib.mkIf disp.multiMonitor {
      environment.systemPackages = [ pkgs.kanshi ];

      # kanshi as a USER service — it lives in the user's graphical session and can
      # NEVER block or fail the SYSTEM boot. It re-applies the saved arrangement on
      # output hotplug + at session start. NEVER-FAIL: Restart=on-failure is CAPPED
      # (StartLimitBurst) so on a compositor without wlr-output-management (Tier-1
      # hart-comp / Tier-3 cage) it exits, retries a few times, then gives up — no
      # restart storm, no impact on the desktop. The compositor keeps its own safe
      # default the whole time.
      systemd.user.services.hart-kanshi = {
        description = "HART OS multi-monitor profile daemon (kanshi) — never-fail, degrade to compositor default";
        # graphical-session.target is the correct semantic anchor (kanshi needs a
        # Wayland session). On a session that does not activate that target kanshi
        # simply never starts — a safe no-op; the live wlr-randr/swaymsg path in the
        # settings backend still works regardless.
        wantedBy = [ "graphical-session.target" ];
        partOf = [ "graphical-session.target" ];
        after = [ "graphical-session.target" ];
        serviceConfig = {
          Type = "simple";
          # Seed an empty (safe-default) config first so kanshi never errors on a
          # missing file; never clobbers a saved layout.
          ExecStartPre = "${kanshiSeed}";
          ExecStart = "${pkgs.kanshi}/bin/kanshi";
          Restart = "on-failure";
          RestartSec = "10";
        };
        # Cap restart attempts so a protocol-less compositor can't restart-storm a
        # core: at most 3 starts per 120s, then systemd stops trying (never-fail).
        startLimitIntervalSec = 120;
        startLimitBurst = 3;
      };
    })
  ]);
}
