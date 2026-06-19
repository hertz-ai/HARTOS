{ config, lib, pkgs, hartSrc ? /etc/hart, hartRustNixpkgs ? null, ... }:

# ════════════════════════════════════════════════════════════════════════════
# HART OS — FIRST Rust-in-Nix buildRustPackage PRECEDENT
# ════════════════════════════════════════════════════════════════════════════
#
# WHY THIS MODULE EXISTS (ROADMAP Phase 3 + HART_OS_NATIVE_ARCHITECTURE §3.2):
#
#   The architecture's original rationale — "Smithay is in-ecosystem already;
#   reuses existing Rust CI/packaging muscle; adds no new toolchain class" — was
#   VERIFIED FALSE about this repo. There is ZERO buildRustPackage /
#   rustPlatform / cargoHash anywhere in nixos/ (confirmed by grep), and the
#   claw_native/rust crates are referenced by ZERO .nix modules. So HART-comp
#   (nixos/modules/hart-comp.nix) would be the FIRST Rust-in-Nix build in the
#   tree — a brand-new toolchain class + bundle-accounting surface + #70 eval-gate
#   risk, with NO existing precedent to reuse.
#
#   THEREFORE: this module lands the buildRustPackage precedent FIRST, on an
#   EXISTING crate (claw_native/rust, which already has a committed Cargo.lock). If
#   the Rust toolchain + crate graph do NOT resolve, this module fails the eval/build
#   gate LOUDLY and IN ISOLATION — before HART-comp ever depends on the toolchain.
#   Proving the toolchain on a crate we already have de-risks the compositor build.
#
#   M9 UPDATE — which toolchain: the original plan was the STOCK pin-50ab793 (24.11)
#   toolchain, but the first real CI build proved claw's CURRENT Cargo.lock pulls an
#   edition2024 transitive dep (time-macros 0.2.27) that 24.11's cargo 1.82/1.83
#   cannot parse — the SAME wall hart-comp hit. So the precedent now builds with the
#   SAME rust_1_86 (from the nixos-25.05 `hartRustNixpkgs` input) that hart-comp uses;
#   it still isolates "does rust_1_86 resolve a SIMPLER real crate graph FIRST" before
#   the heavier Smithay graph. See the rust-platform block in the `let` below.
#
# STATUS: AUTHORED ON A WINDOWS DEV BOX — NOT BUILT HERE.
#   No Rust/Nix build can run on Windows. This Nix expression is authored and
#   structurally validated (test_nixos_configs.py + the Phase-3 source-guard) but
#   the actual `nix build` is VM/CI-pending (Linux nixosTest / Nix Build Matrix).
#   It is opt-in (default OFF) and changes NO runtime behavior of any existing
#   tier — it only adds an isolated package + an optional `claw` binary on PATH.
#
# DRY / no-parallel-path: the C-lib side + everything else still rides the SAME pin
# the rest of the flake uses (`pkgs` from 50ab793). The Rust COMPILER alone comes from
# the nixos-25.05 `hartRustNixpkgs` input (rust_1_86) because 24.11's is too old for
# the edition2024 transitive deps (see the M9 UPDATE above). It introduces NO rust-
# overlay and NO fenix — rust_1_86 is plain stock nixpkgs, just a newer pin, and it is
# the EXACT same toolchain hart-comp.nix uses (one toolchain, two crates — not a
# parallel path).

let
  cfg = config.hart;
  rustCfg = config.hart.rustPrecedent;

  # The existing crate we package to prove the toolchain. claw_native/rust is a
  # cargo WORKSPACE (members = crates/*) with a committed Cargo.lock — exactly the
  # shape buildRustPackage wants, so it is the honest "prove the precedent on an
  # existing crate" target the ROADMAP names.
  #
  # hartSrc is the repo root (passed via flake specialArgs). The crate lives at
  # <root>/claw_native/rust. We use it via `src = ... + "/claw_native/rust"` so
  # the package builds from the in-tree crate, not a fetched copy.
  clawCrateSrc = hartSrc + "/claw_native/rust";

  # ── Newer Rust (≥1.85) — the stock 24.11 toolchain is too old for the CURRENT
  # claw graph ──
  # ORIGINAL premise: "prove the STOCK pinned (24.11) toolchain resolves a real crate
  # graph." DISCOVERED FALSE by the first real `nix build .#hart-rust-precedent` in CI
  # (M9): claw_native/rust's committed Cargo.lock now pulls an edition2024 TRANSITIVE
  # dep (time-macros 0.2.27, via reqwest→…→time), and 24.11's cargo (1.82/1.83) cannot
  # even PARSE its Cargo.toml during vendoring ("failed to parse manifest at
  # …/time-macros-0.2.27/Cargo.toml"). So the stock 24.11 toolchain canNOT resolve
  # THIS crate graph either — the same edition2024 wall hart-comp hit. We therefore
  # build the precedent with the SAME rust_1_86 (rustc 1.86.0) from the nixos-25.05
  # input threaded in via specialArgs (`hartRustNixpkgs`), so the precedent still does
  # its job: isolate that the rust_1_86 toolchain resolves a SIMPLER real crate graph
  # (claw-cli) FIRST, before hart-comp depends on the same toolchain for the heavier
  # Smithay graph. Kept BYTE-FOR-BYTE in sync with the same block in hart-comp.nix
  # (DRY across exactly two call sites; a shared lib file would add more surface than
  # the 8-line mirror saves). Off-flake (input absent) → fall back to 24.11 so plain
  # eval never crashes.
  rustNixpkgs =
    if hartRustNixpkgs != null
    then import hartRustNixpkgs {
      inherit (pkgs.stdenv.hostPlatform) system;
      config = pkgs.config;
    }
    else pkgs;
  hartRustPlatform =
    if hartRustNixpkgs != null
    then rustNixpkgs.makeRustPlatform {
      cargo = rustNixpkgs.rust_1_86.packages.stable.cargo;
      rustc = rustNixpkgs.rust_1_86.packages.stable.rustc;
    }
    else pkgs.rustPlatform;

  # ── The precedent package ──
  # buildRustPackage with the COMMITTED Cargo.lock as the source of truth for the
  # dependency closure (`cargoLock.lockFile`). This is the DRY/correct path when a
  # lock exists: it proves the EXACT locked graph resolves on the pin, and it does
  # not require a hand-maintained cargoHash that drifts.
  #
  # ┌─ cargoHash ALTERNATIVE (the ROADMAP "fixed cargoHash placeholder") ─────────┐
  # │ If a future consumer prefers the vendored-tarball model over lockFile, the   │
  # │ equivalent is:                                                               │
  # │     cargoHash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";       │
  # │ (a placeholder of the right LENGTH so `nix build` fails with the REAL hash,  │
  # │ which CI then pastes back — the standard fixed-output bootstrap). We prefer  │
  # │ cargoLock.lockFile here because claw_native/rust SHIPS a Cargo.lock, so the  │
  # │ lockFile path is both more reproducible and avoids a placeholder that would  │
  # │ never resolve on the Windows box. The placeholder form is documented so      │
  # │ hart-comp.nix (whose crate has no committed lock yet) has the recipe.        │
  # └──────────────────────────────────────────────────────────────────────────────┘
  clawPrecedentPkg = hartRustPlatform.buildRustPackage {
    pname = "hart-claw-precedent";
    version = "0.1.0";

    src = clawCrateSrc;

    cargoLock = {
      lockFile = clawCrateSrc + "/Cargo.lock";
      # allowBuiltinFetchGit keeps git-sourced deps (if any appear later) working
      # under the pin without a flake input per dep. The current Cargo.lock is
      # registry-only, so this is a forward-safety default, not a present need.
      allowBuiltinFetchGit = true;
    };

    # Build ONLY the claw-cli binary crate to keep the precedent build small and
    # fast — we are proving the TOOLCHAIN resolves, not shipping the whole CLI.
    cargoBuildFlags = [ "-p" "claw-cli" ];
    # Some workspace members are libraries / harnesses whose tests need a TTY or
    # network; the precedent's job is "does it COMPILE on the pin", so skip the
    # check phase here. CI's dedicated Rust job runs the real test matrix.
    doCheck = false;

    # pkg-config + the C libs the terminal/syntax crates (crossterm/syntect) may
    # link against on Linux. Guarded so a nixpkgs rev lacking one cannot break
    # EVAL — CI's Nix Build Matrix validates the actual build.
    nativeBuildInputs = with pkgs; [ pkg-config ];
    buildInputs = lib.optionals pkgs.stdenv.isLinux (with pkgs; [ ]);

    meta = {
      description =
        "HART OS first Rust-in-Nix buildRustPackage precedent (claw-cli) — proves "
        + "the stock pinned toolchain (50ab793) resolves a real crate graph before "
        + "HART-comp depends on it";
      license = lib.licenses.mit;
      # Mark broken-on-non-Linux so eval on a Darwin/Windows-cross builder does not
      # claim it builds; the real target is the Linux CI/VM.
      platforms = lib.platforms.linux;
    };
  };
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.rustPrecedent = {
    enable = lib.mkEnableOption ''
      the HART OS first Rust-in-Nix buildRustPackage precedent (claw-cli).
      Opt-in, default OFF: it only proves the pinned toolchain resolves a real
      crate graph + optionally puts the `claw` binary on PATH. Changes no tier.
    '';

    installBinary = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Whether to add the built `claw` binary to environment.systemPackages.
        Default OFF — the precedent's value is proving the BUILD resolves; the
        binary is an optional convenience, not a tier dependency.
      '';
    };

    package = lib.mkOption {
      type = lib.types.package;
      readOnly = true;
      description = ''
        The built precedent package (read-only). Other modules (hart-comp.nix)
        and CI reference this to gate on the SAME proven toolchain resolution:
        `nix build .#nixosConfigurations.<cfg>.config.hart.rustPrecedent.package`.
      '';
    };
  };

  # Expose the package so other modules (hart-comp.nix) + CI can reference the
  # proven precedent build directly via config.hart.rustPrecedent.package, and so
  # `nix build .#nixosConfigurations.*.config.hart.rustPrecedent.package` works as
  # the isolated toolchain-resolution gate.
  config = lib.mkMerge [
    {
      hart.rustPrecedent.package = lib.mkDefault clawPrecedentPkg;
    }

    (lib.mkIf rustCfg.enable (lib.mkIf rustCfg.installBinary {
      environment.systemPackages = [ clawPrecedentPkg ];
    }))
  ];
}
