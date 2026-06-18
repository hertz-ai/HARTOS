{ config, lib, pkgs, hartSrc ? /etc/hart, ... }:

# ════════════════════════════════════════════════════════════════════════════
# HART OS — HART-comp: the AI-native Smithay/Rust compositor (Tier-1, OPT-IN)
# ════════════════════════════════════════════════════════════════════════════
#
# WHY (HART_OS_NATIVE_ARCHITECTURE §L1 + §3 + ROADMAP Phase 3):
#
#   HART-comp is the eventual Tier-1 compositor that owns DRM/KMS scanout,
#   libinput, the Wayland socket, and the live window tree — and exposes the
#   com.hart.Compositor IPC the brain drives so AGENTS own window-placement
#   POLICY (the moat GNOME/Mutter and a forked sway will not cede). It is built
#   from compositor/ (the Smithay Rust crate) via buildRustPackage.
#
#   This is the FIRST Rust-in-Nix build in the tree. Its toolchain resolution is
#   de-risked FIRST by hart-rust-precedent.nix (which packages the existing
#   claw_native/rust crate under the SAME pin) — if the stock pinned toolchain
#   (50ab793) cannot resolve a real crate graph, that precedent module fails the
#   gate in isolation BEFORE this module ships. This module asserts the precedent
#   is enabled so the dependency is explicit, not implicit.
#
# NEVER-FAIL POSITION (ROADMAP §6 tiering — INVARIANT):
#   Tier-1 = HART-comp (THIS module) → Tier-2 = sway (hart-sway-tier1.nix) →
#   Tier-3/floor = cage + forced-software-GL (hart-liquid-ui.nix, audited
#   bit-for-bit). **defaultSession STAYS cage.** HART-comp is OPT-IN until its
#   software-render path is VM-proven on an llvmpipe broken-GPU fixture and the
#   Phase-1 out-of-process tier-drop supervisor proves a crash-loop lands on cage
#   with the latch written. Nothing here flips defaultSession; this module only
#   ADDS a greeter-selectable session + the supervisor's Tier-1 rung.
#
# STATUS: AUTHORED ON A WINDOWS DEV BOX — NOT BUILT/BOOTED HERE.
#   No Rust/Wayland/KMS build can run on Windows. compositor/ is a COMPILE-PENDING
#   skeleton (see compositor/src/main.rs). This Nix expression is authored +
#   structurally validated (test_nixos_configs.py + the Phase-3 source-guard); the
#   real `nix build` + paint proof + GBM-fail-to-pixman + crash-loop-to-cage are
#   ALL VM/CI-pending (Linux nixosTest on llvmpipe, or local QEMU-KVM). Per the
#   ROADMAP "Honest hardware limit": no compositor source is authored ahead of CI
#   beyond this clearly-marked skeleton, and this module is NOT added to the shared
#   flake hartModules[] yet (that addition + the providedSessions registration is
#   the Phase-3 CI bring-up step, gated on the build resolving).
#
# DRY / no-parallel-path:
#   - REUSES the SAME software-GL hardening contract as cage Tier-3 + sway Tier-2
#     (the --force-software flag + WLR_RENDERER_ALLOW_SOFTWARE / LIBGL_ALWAYS_
#     SOFTWARE env) — the mandatory pixman path, one contract across all tiers.
#   - REUSES the canonical glass shell (hart-glass-shell) as the layer-shell
#     client, exactly as sway Tier-1 does — no third renderer.
#   - REUSES the rustPlatform.buildRustPackage idiom proven by hart-rust-precedent
#     .nix — no second Rust toolchain, no rust-overlay/fenix; the stock pinned
#     toolchain only.

let
  cfg = config.hart;
  ui = config.hart.liquidUI;
  comp = config.hart.comp;

  # The compositor crate lives at <repo-root>/compositor (the Smithay skeleton).
  compositorSrc = hartSrc + "/compositor";

  # ── The HART-comp package (buildRustPackage of compositor/) ──
  #
  # NOTE: compositor/ ships a COMMITTED Cargo.lock (committed in 60d04a7;
  # registry-only deps — tracing/tracing-subscriber). So this package uses the
  # reproducible cargoLock.lockFile model — the SAME idiom as
  # hart-rust-precedent.nix (claw_native/rust). Both Rust-in-Nix crates now use
  # lockFile, the one correct/DRY path; no hand-maintained cargoHash that drifts.
  hartCompPkg = pkgs.rustPlatform.buildRustPackage {
    pname = "hart-comp";
    version = "0.1.0";

    src = compositorSrc;

    # compositor/ now ships a COMMITTED Cargo.lock (registry-only deps), so use
    # the reproducible cargoLock.lockFile path — the SAME idiom as
    # hart-rust-precedent.nix. This replaces the old all-A cargoHash placeholder
    # that would have FAILED the first `nix build` (it was written when the crate
    # shipped no lock; the lock landed in 60d04a7 without updating this module).
    cargoLock = {
      lockFile = compositorSrc + "/Cargo.lock";
      # Forward-safety for any future git-sourced dep; current lock is
      # registry-only (mirrors the precedent module).
      allowBuiltinFetchGit = true;
    };

    # Smithay's build needs the Wayland/DRM/input/render C libraries on Linux.
    # Attr-guarded so a nixpkgs rev that renames one cannot break EVAL; CI's Nix
    # Build Matrix validates the actual link. pixman is the MANDATORY software
    # renderer's C dep (the broken-GPU floor); libGL/mesa for the optional hw path.
    nativeBuildInputs = with pkgs; [ pkg-config ];
    buildInputs = lib.optionals pkgs.stdenv.isLinux (with pkgs; [
      wayland
      wayland-protocols
      libinput
      libxkbcommon
      pixman              # MANDATORY software-render path (never-fail floor)
      libdrm
      mesa                # GBM + the optional hardware GL path
      seatd               # libseat session/seat management
      udev                # device hotplug
      # ── Phase 5 (native toplevels) — added when the Smithay `xwayland` feature
      # is uncommented in compositor/Cargo.toml at CI bring-up (today the feature
      # is a commented manifest + the handler bodies are todo!()/unwired, so these
      # are NOT yet needed to build the pure-logic skeleton). xwayland + the X11
      # client libs back the XWayland path that surfaces Wine/legacy-X11 toplevels;
      # xdg-shell / xdg-decoration / wlr-layer-shell / wlr-foreign-toplevel-
      # management need no extra C dep (they ride wayland-protocols above):
      #   xwayland xorg.libX11 xorg.libxcb xorg.xcbutilwm
    ]);

    # ── Phase-5 native-toplevel feature (src/wayland.rs) ──
    # The real Smithay handler bodies (xdg-shell / XWayland / xdg-decoration /
    # wlr-foreign-toplevel-management trait impls + the live summon orchestration)
    # live in compositor/src/wayland.rs behind `#![cfg(feature = "smithay")]`. They
    # compile ONLY when the `smithay` cargo feature is on — which is ALSO when the
    # git-Smithay dep + the xwayland C deps below get uncommented (one CI step). Until
    # that bring-up, buildFeatures stays EMPTY so the default build is the pure-logic
    # skeleton (no git-Smithay fetch, no Wayland link) and the dev box / this eval
    # path never need Smithay. At Phase-5 CI bring-up, set:
    #   buildFeatures = [ "smithay" ];
    # together with uncommenting the smithay/calloop deps in Cargo.toml and the
    # xwayland C libs below. We do NOT set it here (the build must stay resolvable on
    # the pinned toolchain WITHOUT git-Smithay until the rev is pinned + vendored).
    buildFeatures = [ ];

    # The skeleton's pure-logic unit tests (render-path selection / splash alpha /
    # the no-phantom-window WindowRegistry + SummonResolver invariants) run in the
    # build; the real paint/scanout/toplevel-map proof is the nixosTest VM. doCheck
    # stays ON so the never-fail-floor + no-phantom-window invariant tests gate every
    # build. (Feature-OFF, so cargo test compiles only main.rs + its #[cfg(test)];
    # wayland.rs is excluded until the smithay feature is on in CI.)
    doCheck = true;

    meta = {
      description =
        "HART OS AI-native Wayland compositor (Smithay) — Tier-1, opt-in, "
        + "compile-pending skeleton; software-render floor + com.hart.Compositor IPC";
      license = lib.licenses.mit;
      platforms = lib.platforms.linux;
      mainProgram = "hart-comp";
    };
  };

  # ── HART-comp session launcher ──
  # Forces the mandatory software path (paint on any GPU) and runs the glass shell
  # as the compositor's layer-shell client. The Phase-1 supervisor selects this as
  # Tier-1 ONLY after VM-proof; today it is greeter-selectable + opt-in.
  #
  # full_boot_verification gating (architecture §L1, ROADMAP Phase 3 never-break
  # gate): HART-comp must boot ONLY AFTER the guardrail kernel + master-key + origin
  # attestation pass. That gate is enforced by the boot ordering the supervisor owns
  # (Phase 1) + the signed-manifest integrity extension (Phase 3 node-integrity work,
  # owned by the security task) — NOT re-implemented here. This launcher is the thin
  # exec wrapper; it inherits the constitution gate from the session boot path.
  compSessionLauncher = pkgs.writeShellScriptBin "hart-comp-session" ''
    # Mandatory software-render floor — same contract as cage Tier-3 + sway Tier-2.
    # HART-comp's pixman path is type-checked (not an env prayer), but we ALSO pass
    # --force-software + set the shared env so the decision is unambiguous and a
    # half-finished hardware path can never brick the box.
    export WLR_RENDERER_ALLOW_SOFTWARE=1
    export WLR_NO_HARDWARE_CURSORS=1
    ${lib.optionalString (!(ui.preferHardwareGL or false)) "export LIBGL_ALWAYS_SOFTWARE=1"}
    export HART_COMP_FORCE_SOFTWARE=${if (ui.preferHardwareGL or false) then "0" else "1"}
    exec ${hartCompPkg}/bin/hart-comp ${lib.optionalString (!(ui.preferHardwareGL or false)) "--force-software"}
  '';

  compSessionDesktop = pkgs.writeText "hart-comp.desktop" ''
    [Desktop Entry]
    Name=HART OS (HART-comp Tier-1)
    Comment=AI-native Smithay compositor — agents own window placement (THE MOAT)
    Exec=${compSessionLauncher}/bin/hart-comp-session
    Type=Application
    DesktopNames=HART-OS-comp
  '';
  # passthru.providedSessions REQUIRED by services.displayManager.sessionPackages
  # (session id must match the wayland-sessions/*.desktop basename) — the same
  # lesson hart-liquid-ui.nix's kioskSession + hart-sway-tier1.nix's swaySession
  # encode. Without it, nix flake-check fails "did not specify any session names".
  compSession = pkgs.runCommand "hart-comp-wayland-session"
    { passthru.providedSessions = [ "hart-comp" ]; } ''
      install -Dm644 ${compSessionDesktop} $out/share/wayland-sessions/hart-comp.desktop
    '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.comp = {
    enable = lib.mkEnableOption ''
      HART OS HART-comp: the AI-native Smithay/Rust Tier-1 compositor that owns the
      window tree + exposes com.hart.Compositor so agents own window placement.
      OPT-IN, default OFF; compile-pending skeleton (compositor/), VM-proof
      required before it can become default. Does NOT flip defaultSession — cage
      stays the floor; this only registers a greeter-selectable session + the
      supervisor's Tier-1 rung.
    '';

    package = lib.mkOption {
      type = lib.types.package;
      readOnly = true;
      description = ''
        The built HART-comp package (read-only). CI gates on
        `nix build .#nixosConfigurations.<cfg>.config.hart.comp.package`; the
        Phase-3 node-integrity work hashes this binary into the signed manifest.
      '';
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Config
  # ═══════════════════════════════════════════════════════════
  config = lib.mkMerge [
    {
      # Always expose the package so CI + the node-integrity manifest can reach it
      # without enabling the session. mkDefault so a downstream override is allowed.
      hart.comp.package = lib.mkDefault hartCompPkg;
    }

    (lib.mkIf comp.enable {
      # Explicit dependency: HART-comp is the FIRST Rust-in-Nix build; its toolchain
      # resolution is de-risked by the precedent module. Assert it is enabled so the
      # dependency is structural, and assert the glass-shell renderer exists (HART-
      # comp reuses it as the layer-shell client — no third renderer).
      assertions = [
        {
          assertion = config.hart.rustPrecedent.enable;
          message =
            "hart.comp.enable requires hart.rustPrecedent.enable = true — HART-comp "
            + "is the FIRST Rust-in-Nix build; the precedent module must prove the "
            + "pinned toolchain (50ab793) resolves a real crate graph FIRST.";
        }
        {
          assertion = ui.enable && (ui.renderer == "webkit");
          message =
            "hart.comp.enable requires hart.liquidUI.enable = true with "
            + "renderer = \"webkit\" — HART-comp reuses the canonical hart-glass-shell "
            + "as its layer-shell client (no parallel renderer).";
        }
      ];

      environment.systemPackages = [ hartCompPkg compSessionLauncher ];

      # Register the opt-in HART-comp session. desktop.nix keeps the default
      # session on cage ("hart-shell"); this is ADDITIVE — a selectable session +
      # the Tier-1 ladder rung the Phase-1 supervisor consumes. GNOME + sway + cage
      # all stay selectable (the full never-fail ladder).
      services.displayManager.sessionPackages = [ compSession ];

      # ── Integration contract with the Phase-1 tier-drop supervisor ──
      # hart-session-supervisor.nix's `compCommand` option (Tier-1 launcher,
      # default null = "slot reserved, falls through to sway/cage") should be
      # pointed at THIS module's `hart-comp-session` ONLY after the compositor's
      # software-render path is VM-proven on llvmpipe. Until then it stays null so
      # the supervisor falls straight through to a tier that paints. The launcher
      # is on PATH when hart.comp.enable is set. We do NOT edit the supervisor here
      # (separate module/owner); the operator/config wires
      # `hart.sessionSupervisor.compCommand = "hart-comp-session"` post-VM-proof.

      # com.hart.Compositor D-Bus policy (the IPC the brain's HartWmClient drives in
      # Phase 6). Declared so the bus name is reserved + the policy is in place when
      # the IPC server lands; the SKELETON does not yet claim it (no server running).
      # The fail-closed guardrail/circuit-breaker/audit/rate-cap gate lives
      # BRAIN-SIDE (agent_ui_update, Phase 2/6), NOT in this policy file.
      services.dbus.packages = [
        (pkgs.writeTextDir "share/dbus-1/system.d/com.hart.Compositor.conf" ''
          <?xml version="1.0" encoding="UTF-8"?>
          <!DOCTYPE busconfig PUBLIC
           "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
           "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
          <busconfig>
            <!-- HART OS HART-comp: AI-native compositor IPC (Phase 6).
                 The brain drives it within the constitution; no verb re-enables a
                 cut AI sense or weakens a guardrail (gate is brain-side). -->
            <policy user="hart">
              <allow own="com.hart.Compositor"/>
              <allow send_destination="com.hart.Compositor"/>
            </policy>
            <policy context="default">
              <deny own="com.hart.Compositor"/>
              <deny send_destination="com.hart.Compositor"/>
            </policy>
          </busconfig>
        '')
      ];
    })
  ];
}
