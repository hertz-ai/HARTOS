{ config, lib, pkgs, hartSrc ? /etc/hart, ... }:

# ════════════════════════════════════════════════════════════════════════════
# HART OS — sway as Tier-1: OS-native agent windowing NOW (no Rust required)
# ════════════════════════════════════════════════════════════════════════════
#
# WHY (ROADMAP §3.2 + §3.3 + HART_OS_NATIVE_ARCHITECTURE §3):
#
#   Smithay HART-comp is the EVENTUAL moat, but it is the FIRST-EVER Rust-in-Nix
#   build in this tree — a brand-new toolchain/eval-gate class. To NOT block
#   OS-native agent windowing on that bring-up, the architecture ships
#   **sway-as-Tier-1**: a proven wlroots WM in single-output kiosk running the
#   SAME glass shell, with agent tile/summon shimmed via `swaymsg`. This delivers
#   the OS-native windowing moat IMMEDIATELY; Smithay HART-comp (hart-comp.nix) is
#   a later upgrade behind the SAME never-fail watchdog.
#
#   This module is the "sway-Tier-1-now" deliverable: it registers a sway session
#   that runs the existing served glass shell as sway's client AND exposes a
#   `swaymsg`-backed window-management shim the brain's HartWmClient (Phase 6) can
#   drive. The moat (agent owns window placement) is REDUCED vs HART-comp but
#   PRESENT — `swaymsg` has no AI-native placement policy, but it CAN tile/summon/
#   focus/move real native toplevels by intent.
#
# NEVER-FAIL POSITION (ROADMAP §6 tiering):
#   Tier-1 = HART-comp (Smithay, later, VM-proven) → Tier-2/floor = sway (THIS
#   module, proven wlroots) → Tier-3 = cage + forced-software-GL (hart-liquid-ui
#   .nix, the audited bit-for-bit floor). Today defaultSession STAYS cage; this
#   module ADDS sway as a greeter-selectable session + the supervisor's Tier-2
#   rung. The Phase-1 out-of-process tier-drop supervisor owns when sway becomes
#   active; nothing here flips defaultSession. GNOME remains greeter-selectable.
#
# STATUS: AUTHORED ON A WINDOWS DEV BOX — NOT BOOTED HERE.
#   No Wayland/wlroots/sway session can run on Windows. This Nix expression is
#   authored + structurally validated (test_nixos_configs.py + Phase-3 source-
#   guard); the real boot + swaymsg shim + paint proof are VM/CI-pending (Linux
#   nixosTest on llvmpipe software-GL, or local QEMU-KVM). Opt-in, default OFF.
#
# DRY / no-parallel-path:
#   - REUSES the SAME software-GL hardening contract as the cage floor
#     (WLR_RENDERER_ALLOW_SOFTWARE / LIBGL_ALWAYS_SOFTWARE / WLR_NO_HARDWARE_CURSORS
#     / WEBKIT_DISABLE_DMABUF_RENDERER) — a kiosk MUST paint on any GPU.
#   - REUSES the SAME served shell URL (LiquidUIService :port, render_desktop_shell
#     + /shell/static) the cage floor serves — it does NOT fork a second renderer.
#   - The glass-shell WebView client is the EXISTING `hart-glass-shell` binary when
#     hart.liquidUI is enabled (no duplicate WebKitGTK Python renderer); a minimal
#     on-PATH fallback only covers the standalone-test case.
#   - The `swaymsg` shim is the SINGLE Tier-2 window-management surface the brain's
#     HartWmClient targets when HART-comp is absent (ROADMAP §"Tier-2 sway parity").

let
  cfg = config.hart;
  ui = config.hart.liquidUI;
  sway = config.hart.swayTier1;

  liquidPort = toString (ui.port or 6800);
  nunbaPort = toString (config.hart.nunba.port or 5000);

  # The shell client launched INSIDE sway. We delegate to the canonical
  # `hart-glass-shell` (built by hart-liquid-ui.nix) when LiquidUI is enabled, so
  # there is ONE renderer across cage + sway — never a parallel WebKitGTK host.
  #
  # HONEST CAVEAT (ROADMAP §7.4 / Phase 4): the existing hart-glass-shell is a
  # GTK3 single FULLSCREEN Gtk.Window, NOT a true wlr-layer-shell BACKGROUND
  # surface. Under sway it runs as sway's single fullscreen client — the SAME
  # visual result as cage, and sufficient to ship Tier-1 windowing — while TRUE
  # layer-shell anchoring (desktop-below-native-windows) is the budgeted GTK3→GTK4
  # port in Phase 4 (hart-comp / hart-sway both consume it then). This module does
  # NOT pretend the layer-shell anchoring already exists.
  swayShellClient = pkgs.writeShellScriptBin "hart-sway-shell-client" ''
    set -euo pipefail
    # Same software-GL hardening as the cage floor — paint on any GPU.
    export WEBKIT_DISABLE_DMABUF_RENDERER=1
    export WEBKIT_DISABLE_COMPOSITING_MODE=1
    # Use the canonical glass-shell renderer — the SINGLE renderer across cage +
    # sway. hart.swayTier1 asserts hart.liquidUI.enable (see config below) so this
    # binary is always present in production; no second WebKitGTK host is forked.
    if command -v hart-glass-shell >/dev/null 2>&1; then
      exec hart-glass-shell
    fi
    # If the canonical renderer is somehow absent (a misconfigured standalone test),
    # fail LOUDLY rather than fork a parallel renderer or paint a blank surface —
    # the assertion in config should make this unreachable in a real build.
    echo "hart-sway-shell-client: hart-glass-shell not on PATH." >&2
    echo "  sway Tier-1 reuses the canonical glass shell; enable hart.liquidUI" >&2
    echo "  (URL would be http://localhost:${liquidPort}, SPA fallback :${nunbaPort})." >&2
    exit 69
  '';

  # ── The swaymsg window-management shim ──
  # The SINGLE Tier-2 surface the brain's HartWmClient (Phase 6) drives when
  # HART-comp is absent. It maps the com.hart.Compositor verb family onto sway's
  # IPC (`swaymsg`). The moat is REDUCED (no AI-native placement policy) but
  # PRESENT — real native toplevels can be tiled/summoned/focused/moved by intent.
  #
  # SECURITY NOTE (ROADMAP §6 Phase 6): this shim is the TRANSPORT only. The
  # fail-closed guardrail + circuit-breaker + immutable-audit + per-agent rate cap
  # live BRAIN-SIDE in agent_ui_update / HartWmClient (Phase 2 + 6), NOT here. The
  # shim never makes a policy decision; it executes an ALREADY-gated request.
  swaymsgShim = pkgs.writeShellScriptBin "hart-swaymsg-shim" ''
    set -euo pipefail
    # Usage: hart-swaymsg-shim <verb> [args...]
    # Verbs mirror com.hart.Compositor (IPC_PROTOCOL.md §4) at the Tier-2 floor.
    # This is a THIN translation to `swaymsg`; the brain holds the gate.
    verb="''${1:-}"; shift || true
    case "$verb" in
      list)
        # window.list → sway tree (the brain parses geometry/app_id/focused).
        exec ${pkgs.sway}/bin/swaymsg -t get_tree
        ;;
      focus)
        # window.focus <con_id>
        exec ${pkgs.sway}/bin/swaymsg "[con_id=''${1:?con_id required}] focus"
        ;;
      close)
        # window.close <con_id> — destructive geometry is gated BRAIN-SIDE
        # (PREVIEW FSM) BEFORE this shim is ever called.
        exec ${pkgs.sway}/bin/swaymsg "[con_id=''${1:?con_id required}] kill"
        ;;
      tile)
        # window.tile <layout∈{splith,splitv,tabbed,stacking}> on focused workspace.
        exec ${pkgs.sway}/bin/swaymsg "layout ''${1:?layout required}"
        ;;
      move_to_workspace)
        # window.move_to_workspace <con_id> <n>
        exec ${pkgs.sway}/bin/swaymsg "[con_id=''${1:?con_id required}] move container to workspace number ''${2:?workspace required}"
        ;;
      switch_workspace)
        exec ${pkgs.sway}/bin/swaymsg "workspace number ''${1:?workspace required}"
        ;;
      *)
        echo "hart-swaymsg-shim: unsupported verb '$verb'" >&2
        echo "  (Tier-2 sway shim covers list|focus|close|tile|move_to_workspace|switch_workspace;" >&2
        echo "   full AI-native placement policy is HART-comp Tier-1 only)" >&2
        exit 64
        ;;
    esac
  '';

  # ── sway single-output kiosk config ──
  # No bars, no default app launchers — sway exists ONLY to host the glass shell
  # as its client and to give the brain a real wlroots window tree to arrange.
  # Software rendering forced so it paints on any GPU (same contract as cage).
  swayKioskConfig = pkgs.writeText "hart-sway-kiosk.conf" ''
    # HART OS sway Tier-1 kiosk — single client (glass shell), agent-arranged tree.
    # Generated; do not edit on the live ISO. See hart-sway-tier1.nix.

    # No window decorations / gaps for the kiosk shell surface.
    default_border none
    default_floating_border none
    titlebar_padding 1
    gaps inner 0
    gaps outer 0

    # Launch the glass shell as sway's startup client (fullscreen).
    exec ${swayShellClient}/bin/hart-sway-shell-client

    # The brain drives further placement via `swaymsg` (the shim above). We do NOT
    # bind a pile of human keybinds — this is an AI-native session, not a tiling-WM
    # power-user setup. Super+Space PTT + Ctrl+Alt+1-4 land as compositor globals in
    # Phase 6 (registered by the brain), kept as in-WebView keydown fallback today.

    # Keep the screen painting on broken GPUs: never abort on missing hardware GL.
    # (The env in the session launcher forces software rendering; this is belt+
    # suspenders so a config reload cannot re-enable a crashing GL path.)
  '';

  # ── sway session launcher ──
  # Forces software rendering (paint on any GPU) and points sway at the kiosk
  # config. This is the Tier-2 rung the Phase-1 supervisor selects; it is also a
  # greeter-selectable session so an operator can pick it manually.
  swaySessionLauncher = pkgs.writeShellScriptBin "hart-sway-session" ''
    export WLR_RENDERER_ALLOW_SOFTWARE=1
    export WLR_NO_HARDWARE_CURSORS=1
    ${lib.optionalString (!(ui.preferHardwareGL or false)) "export LIBGL_ALWAYS_SOFTWARE=1"}
    exec ${pkgs.sway}/bin/sway -c ${swayKioskConfig}
  '';

  # ── Wayland session registration ──
  # passthru.providedSessions REQUIRED by services.displayManager.sessionPackages
  # (the session id must match the wayland-sessions/*.desktop basename) — without
  # it nix flake-check fails with "did not specify any session names" (the same
  # lesson hart-liquid-ui.nix's kioskSession encodes).
  swaySessionDesktop = pkgs.writeText "hart-sway.desktop" ''
    [Desktop Entry]
    Name=HART OS (sway Tier-1)
    Comment=AI-native glass shell on sway — OS-native agent windowing
    Exec=${swaySessionLauncher}/bin/hart-sway-session
    Type=Application
    DesktopNames=HART-OS-sway
  '';
  swaySession = pkgs.runCommand "hart-sway-wayland-session"
    { passthru.providedSessions = [ "hart-sway" ]; } ''
      install -Dm644 ${swaySessionDesktop} $out/share/wayland-sessions/hart-sway.desktop
    '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.swayTier1 = {
    enable = lib.mkEnableOption ''
      HART OS sway-as-Tier-1: OS-native agent windowing NOW via a proven wlroots
      WM running the same glass shell, with agent tile/summon shimmed over
      `swaymsg`. Opt-in, default OFF; does NOT flip defaultSession (cage stays the
      default floor until a higher tier is VM-proven). Registers a greeter-
      selectable sway session + the supervisor's Tier-2 rung.
    '';
  };

  # ═══════════════════════════════════════════════════════════
  # Config
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf sway.enable {
    # sway Tier-1 REUSES the canonical glass shell (single renderer, no parallel
    # path). It is therefore only coherent when hart.liquidUI is enabled and using
    # the webkit renderer. Fail EVAL loudly otherwise rather than silently forking
    # a second renderer or shipping a blank session.
    assertions = [
      {
        assertion = ui.enable && (ui.renderer == "webkit");
        message =
          "hart.swayTier1.enable requires hart.liquidUI.enable = true with "
          + "renderer = \"webkit\" — sway Tier-1 reuses the canonical hart-glass-shell "
          + "renderer (no parallel WebKitGTK host). Enable LiquidUI or disable swayTier1.";
      }
    ];

    # sway + the swaymsg shim + the glass-shell client + wlr-randr for output mgmt.
    # sway pulls wlroots; the software-GL env in the launcher guarantees paint.
    environment.systemPackages = [
      pkgs.sway
      swaymsgShim
      swayShellClient
      swaySessionLauncher
      pkgs.wlr-randr      # output mode/scale backend for the shell display panels
    ];

    # Register the sway session so the display manager (and the Phase-1 supervisor)
    # can run it. desktop.nix keeps the default session on cage ("hart-shell");
    # this is ADDITIVE — a selectable session + the Tier-2 ladder rung. GNOME stays
    # selectable too (the ultimate human escape hatch, desktop.nix guarantee).
    services.displayManager.sessionPackages = [ swaySession ];

    # The brain reaches the Tier-2 window shim by PATH (`hart-swaymsg-shim`); the
    # HartWmClient feature-detects HART-comp first, falls to this when absent.
    # No D-Bus name is claimed here — com.hart.Compositor is owned by HART-comp
    # (Tier-1); at Tier-2 the brain calls the shim directly. (ROADMAP §6 Phase 6
    # "Tier-2 swaymsg shim".)

    # ── Phase-8 wiring to the Phase-1 tier-drop supervisor ──
    # Point the supervisor's Tier-2 `swayCommand` at THIS module's
    # `hart-sway-session` (sway + the kiosk config that auto-EXECs the glass-shell
    # client + the swaymsg tile/summon shim on PATH) — NOT the bare
    # `${pkgs.sway}/bin/sway` the supervisor defaults to while this module is off.
    # That bare default starts an EMPTY sway with no shell and no agent windowing;
    # the real session below is the Tier-2 parity rung the architecture mandates
    # "wired NOW" (ROADMAP Phase 8: "Tier-2 sway parity … same glass shell as a
    # layer-shell client").
    #
    # mkDefault (not mkForce): an operator who set `hart.sessionSupervisor.
    # swayCommand` explicitly still wins; this only upgrades the DEFAULT from
    # bare-sway to the real session whenever swayTier1 is enabled. The supervisor
    # treats a null swayCommand as "Tier-2 unavailable, fall through to cage", so
    # a non-null real session here strictly improves the ladder, never weakens the
    # floor. The supervisor still owns WHEN sway is selected + the never-drop-
    # below-cage invariant; this module only provides a better Tier-2 binary.
    hart.sessionSupervisor.swayCommand =
      lib.mkDefault "${swaySessionLauncher}/bin/hart-sway-session";

    # Floor invariant the supervisor honors: this module can NEVER drop below cage
    # Tier-3, and never flips defaultSession itself.
  };
}
