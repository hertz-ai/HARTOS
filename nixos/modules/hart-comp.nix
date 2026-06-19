{ config, lib, pkgs, hartSrc ? /etc/hart, hartRustNixpkgs ? null, ... }:

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

  # ── Newer Rust (≥1.85) for the edition2024 Smithay manifest ──
  # The main pin (24.11, 50ab793) tops out at Rust 1.83, but the pinned git-Smithay
  # rev declares `edition = "2024"` + `rust-version = "1.85"`, so Cargo < 1.85 cannot
  # even PARSE its Cargo.toml ("feature `edition2024` is required") — proven by the
  # first real `nix build .#hart-comp` in CI (M9). We therefore build hart-comp with
  # `rust_1_86` (rustc 1.86.0) sourced from the nixos-25.05 input threaded in via
  # specialArgs (`hartRustNixpkgs`), while keeping EVERY C buildInput below on the
  # 24.11 `pkgs` (24.11's `mesa` still bundles libgbm; 25.05 split it into a separate
  # `libgbm` attr, so mixing 25.05 libs would re-break the gbm link). Newer compiler,
  # same libs — the standard nixpkgs "build this crate with a specific Rust" idiom via
  # makeRustPlatform. This is stock nixpkgs, NOT rust-overlay/fenix (the precedent's
  # "no new toolchain class" holds: the toolchain is still plain nixpkgs).
  #
  # Fallback to `pkgs.rustPlatform` if the input is somehow absent (e.g. a consumer
  # that imports this module without the flake specialArgs) so eval never crashes;
  # that path only matters off the flake, where the package is not actually built.
  rustNixpkgs =
    if hartRustNixpkgs != null
    then import hartRustNixpkgs {
      inherit (pkgs.stdenv.hostPlatform) system;
      config = pkgs.config;
    }
    else pkgs;
  # rust_1_86 = rustc 1.86.0 + matching cargo (≥1.85 → edition2024 OK). We use 25.05's
  # OWN makeRustPlatform (and hence its buildRustPackage + importCargoLock/cargo-vendor
  # machinery), because the vendoring step itself runs cargo to PARSE every dep
  # manifest — that is exactly what failed on 24.11 (cargo 1.82 choked on Smithay's
  # edition2024 Cargo.toml). 25.05's vendor machinery is built for cargo 1.86, so the
  # whole Rust build path is self-consistent on 25.05; only the C buildInputs below
  # stay on the 24.11 `pkgs` (libgbm-in-mesa). Off-flake (input absent), fall back to
  # the 24.11 rustPlatform so plain module eval never crashes.
  hartRustPlatform =
    if hartRustNixpkgs != null
    then rustNixpkgs.makeRustPlatform {
      cargo = rustNixpkgs.rust_1_86.packages.stable.cargo;
      rustc = rustNixpkgs.rust_1_86.packages.stable.rustc;
    }
    else pkgs.rustPlatform;

  # ── The HART-comp package (buildRustPackage of compositor/) ──
  #
  # NOTE: post the M1 forward-port the committed Cargo.lock is NO LONGER
  # registry-only — it now has 245 packages including the GIT Smithay dep pinned to
  # rev 47843391c3cd34a32e5ed1721878ca2279269185 (the `smithay`/`winit` cargo features
  # pull it). So a future reader must NOT "simplify" the outputHashes entry below
  # away — it is load-bearing for the smithay-feature resolution. This package uses
  # the reproducible cargoLock.lockFile model (the SAME idiom as
  # hart-rust-precedent.nix), PLUS an explicit `outputHashes` for the one git dep.
  hartCompPkg = hartRustPlatform.buildRustPackage {
    pname = "hart-comp";
    version = "0.1.0";

    src = compositorSrc;

    # compositor/ ships a COMMITTED Cargo.lock (now 245 packages incl. the git
    # Smithay rev), so use the reproducible cargoLock.lockFile path — the SAME idiom
    # as hart-rust-precedent.nix.
    cargoLock = {
      lockFile = compositorSrc + "/Cargo.lock";
      # The ONE git-sourced dep is Smithay (rev 4784339…), pulled by the `smithay`
      # cargo feature (buildFeatures below). Nix's fixed-output fetch of a git crate
      # needs its content hash. This is the REAL sha256 of the pinned-rev checkout,
      # resolved off /mnt/c (the local nix-build hang is only on the SOURCE tree, not
      # on a git fetch into the store): realising the importCargoLock fetchgit
      # derivation for rev 47843391c3cd34a32e5ed1721878ca2279269185 reported
      #   got: sha256-44CNdBNGmGqBkCIVRVtJoQljZfn/JF682xAPX4m/2N8=
      # (cross-checked: `builtins.fetchGit {url;rev;}` + `nix hash path` on the same
      # rev yields the identical NAR hash — Smithay has no submodules, so the
      # fetchgit-vs-fetchGit default difference is moot). With this REAL hash the
      # git crate is reproducibly fetchable, so `hart.comp.enable = true` (or a CI
      # `nix build .#…config.hart.comp.package`) no longer fails the Smithay-hash
      # gate. Keyed by `${pname}-${version}` from Cargo.lock.
      outputHashes = {
        "smithay-0.7.0" = "sha256-44CNdBNGmGqBkCIVRVtJoQljZfn/JF682xAPX4m/2N8=";
      };
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
      mesa                # GBM (libgbm ships in mesa) for the DRM scanout allocator
      seatd               # provides BOTH the libseat C lib (libseat.pc) + the seatd
                          # daemon — `backend_session_libseat` links libseat from here.
                          # NOTE: do NOT also add `pkgs.libseat` — on this nixpkgs pin
                          # (50ab793) `libseat` is a THROW alias renamed to `seatd`, so
                          # listing both makes `nix build` fail at buildInputs eval
                          # ("'libseat' has been renamed to/replaced by 'seatd'"). seatd
                          # alone is the canonical provider (M9: removed the dup libseat).
      udev                # device hotplug (backend_udev)
      # ── M7: native-toplevel XWayland C deps. The `smithay` cargo feature enables
      # `smithay/xwayland`, which links against an X11 client stack, so these MUST be
      # present or the link fails. xdg-shell / xdg-decoration / wlr-layer-shell /
      # wlr-foreign-toplevel-management need no extra C dep (they ride
      # wayland-protocols above). Uncommented at the M7 bring-up together with
      # buildFeatures = [ "smithay" ] below.
      xwayland
      xorg.libX11
      xorg.libxcb
      xorg.xcbutilwm
    ]);

    # ── M7: the real-hardware DRM backend feature (src/wayland.rs + src/udev.rs) ──
    # The real Smithay handler bodies (compositor/shm/output/layer/xdg-shell/XWayland/
    # decoration/foreign-toplevel + the DRM/KMS scanout run loop on the PixmanRenderer
    # software floor) live behind `#[cfg(feature = "smithay")]`. They compile ONLY when
    # the `smithay` cargo feature is on — which pulls the git-Smithay dep (resolved via
    # the cargoLock.outputHashes entry above) + needs the xwayland C deps above. M7
    # turns it ON: the DRM path now COMPILES green (verified in WSL: `cargo build
    # --features smithay` exits 0; it cannot RUN there — no DRM device — that is the
    # flash's job). `doCheck` below runs the pure-logic floor (main.rs #[cfg(test)],
    # feature-independent), so the smithay-only modules are type-checked by the build
    # but the unit tests stay the backend-agnostic invariants.
    buildFeatures = [ "smithay" ];

    # The pure-logic unit tests (render-path selection / splash alpha / the
    # no-phantom-window WindowRegistry + SummonResolver invariants) run in the build;
    # the real paint/scanout/toplevel-map proof is the flash onto hardware. doCheck
    # stays ON so the never-fail-floor + no-phantom-window invariant tests gate every
    # build. With buildFeatures = [ "smithay" ] now ON, `cargo test --features smithay`
    # COMPILES the DRM modules (wayland.rs + udev.rs) AND runs the 19 feature-
    # independent tests in main.rs — so the build both type-checks the DRM path and
    # asserts the backend-agnostic invariants. (Verified in WSL: cargo test green at
    # default; the smithay modules compile under --features smithay.)
    doCheck = true;

    meta = {
      description =
        "HART OS AI-native Wayland compositor (Smithay) — Tier-1, opt-in; "
        + "real-HW DRM/KMS scanout on the pixman software-render floor + "
        + "com.hart.Compositor IPC";
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
    set -u
    PATH=${lib.makeBinPath (with pkgs; [ coreutils ])}:$PATH

    # Mandatory software-render floor — same contract as cage Tier-3 + sway Tier-2.
    # HART-comp's pixman path is type-checked (not an env prayer), but we ALSO pass
    # --force-software + set the shared env so the decision is unambiguous and a
    # half-finished hardware path can never brick the box.
    export WLR_RENDERER_ALLOW_SOFTWARE=1
    export WLR_NO_HARDWARE_CURSORS=1
    ${lib.optionalString (!(ui.preferHardwareGL or false)) "export LIBGL_ALWAYS_SOFTWARE=1"}
    export HART_COMP_FORCE_SOFTWARE=${if (ui.preferHardwareGL or false) then "0" else "1"}

    # ── M7: Tier-1 is the REAL-HARDWARE DRM backend (`--backend drm`) — NOT the winit
    # dev backend (which needs a host Wayland socket that does not exist on a bare TTY).
    # hart-comp owns DRM/KMS scanout + the libinput seat; it creates its OWN wayland-N
    # socket. We run it in the BACKGROUND, wait for that socket to appear (it sets
    # WAYLAND_DISPLAY in its own env, but a sibling child needs the name explicitly), and
    # launch the canonical glass shell as its layer-shell client — exactly how sway
    # Tier-2 launches `hart-glass-shell` as ITS client (the SINGLE glass-shell renderer,
    # no parallel client). HART_COMP_NO_TEST_CLIENT suppresses the dev auto-foot client.
    : "''${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
    export XDG_RUNTIME_DIR
    export HART_COMP_NO_TEST_CLIENT=1

    ${hartCompPkg}/bin/hart-comp --backend drm ${lib.optionalString (!(ui.preferHardwareGL or false)) "--force-software"} &
    HART_COMP_PID=$!

    # Wait (bounded) for hart-comp to create its wayland socket, then point the glass
    # shell at it. The socket is the first wayland-N in XDG_RUNTIME_DIR that hart-comp
    # created (it logs the name; we discover it by polling the runtime dir).
    SHELL_SOCK=""
    for _ in $(seq 1 50); do
      # Newest wayland-N socket in the runtime dir (hart-comp's auto socket).
      SHELL_SOCK=$(ls -t "$XDG_RUNTIME_DIR"/wayland-* 2>/dev/null | grep -v '\.lock$' | head -1 || true)
      [ -n "$SHELL_SOCK" ] && [ -S "$SHELL_SOCK" ] && break
      # Bail early if the compositor died (the supervisor counts this as a crash).
      kill -0 "$HART_COMP_PID" 2>/dev/null || break
      sleep 0.2
    done

    if [ -n "$SHELL_SOCK" ] && [ -S "$SHELL_SOCK" ]; then
      export WAYLAND_DISPLAY="$(basename "$SHELL_SOCK")"
      if command -v hart-glass-shell >/dev/null 2>&1; then
        hart-glass-shell &
      else
        echo "hart-comp-session: hart-glass-shell not on PATH (enable hart.liquidUI)" >&2
      fi
    else
      echo "hart-comp-session: hart-comp did not create a wayland socket — exiting so the supervisor drops a tier" >&2
    fi

    # Foreground the compositor: when it EXITS, control returns to the selector wrapper,
    # which counts a fast exit as a crash and drops a tier (never a blank screen).
    wait "$HART_COMP_PID"
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

      # ── M7: wire HART-comp as the Tier-1 rung of the B4 tier-drop supervisor ──
      # hart-session-supervisor.nix's ladder is [ "hart-comp" "sway" "cage" ]; its
      # `compCommand` defaults to null = "slot reserved, falls straight through to
      # sway/cage". We point it at THIS module's `hart-comp-session` launcher (the
      # --backend drm session above) via mkDefault, so when the steward arms Tier-1
      # (hart.comp.enable = true) the supervisor's `tier_available "hart-comp"`
      # returns true and HART-comp becomes the real Tier-1 — and a crash-loop drops
      # hart-comp → sway → cage, NEVER a blank screen (the supervisor's invariant).
      # mkDefault so an explicit operator override still wins; the launcher is on PATH
      # via environment.systemPackages above. This is gated under hart.comp.enable
      # (default false), so shipped images still default to cage until armed —
      # defaultSession is untouched here (the supervisor owns session selection, and
      # it only PROMOTES hart-comp when its launch SUCCEEDS; a DRM-less box falls
      # through to sway/cage on the launcher's early exit).
      hart.sessionSupervisor.compCommand =
        lib.mkDefault "${compSessionLauncher}/bin/hart-comp-session";

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
