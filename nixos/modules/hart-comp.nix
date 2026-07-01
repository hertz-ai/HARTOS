{ config, lib, pkgs, hartSrc ? /etc/hart, hartRustNixpkgs ? null, hartCrane ? null, ... }:

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
# STATUS: the smithay/DRM compositor is REAL + Nix-built (the smithay build runs in
#   CI, not on this Windows dev box — no Wayland/KMS here).
#   compositor/ BUILDS in Nix/CI (M9 green; hart-comp.nix sets buildFeatures =
#   ["smithay"]) and its pixman software-DRM scanout is VM-PROVEN (a virgl-QEMU
#   scanout PNG exists). On the Windows dev box the smithay path cannot build, so it
#   is authored + structurally validated here (test_nixos_configs.py + the Phase-3
#   source-guard) and compiled in CI. What is still PENDING: real-HARDWARE paint on
#   the target GPU (GBM-fail-to-pixman + crash-loop-to-cage on a real panel), proven
#   via the boot-journal loop. The module IS wired into the flake (providedSessions
#   + the supervisor's Tier-1 rung); cage stays the never-fail floor until real-HW
#   paint is proven.
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

  # The compositor crate lives at <repo-root>/compositor (the Smithay compositor).
  compositorSrc = hartSrc + "/compositor";

  # ── Newer Rust (≥1.85) for the edition2024 Smithay manifest ──
  # The main pin (24.11, 50ab793) tops out at Rust 1.83, but the pinned git-Smithay
  # rev declares `edition = "2024"` + `rust-version = "1.85"`, so Cargo < 1.85 cannot
  # even PARSE its Cargo.toml ("feature `edition2024` is required") — proven by the
  # first real `nix build .#hart-comp` in CI (M9). We therefore build hart-comp with
  # `rust_1_88` (rustc 1.88.0) sourced from the nixos-25.05 input threaded in via
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
  # rust_1_88 = rustc 1.88.0 + matching cargo (≥1.85 → edition2024 OK). We use 25.05's
  # OWN makeRustPlatform (and hence its buildRustPackage + importCargoLock/cargo-vendor
  # machinery), because the vendoring step itself runs cargo to PARSE every dep
  # manifest — that is exactly what failed on 24.11 (cargo 1.82 choked on Smithay's
  # edition2024 Cargo.toml). 25.05's vendor machinery is built for cargo 1.88, so the
  # whole Rust build path is self-consistent on 25.05; only the C buildInputs below
  # stay on the 24.11 `pkgs` (libgbm-in-mesa). Off-flake (input absent), fall back to
  # the 24.11 rustPlatform so plain module eval never crashes.
  hartRustPlatform =
    if hartRustNixpkgs != null
    then rustNixpkgs.makeRustPlatform {
      cargo = rustNixpkgs.rust_1_88.packages.stable.cargo;
      rustc = rustNixpkgs.rust_1_88.packages.stable.rustc;
    }
    else pkgs.rustPlatform;

  # ── DRY: the pinned git-Smithay checkout, referenced by BOTH build paths ──
  # ONE source of truth for the rev-47843391 source NAR hash. Cross-checked (see the
  # buildRustPackage block below): `builtins.fetchGit {url;rev;}` + `nix hash path` ==
  # importCargoLock's fetchgit hash, and Smithay has no submodules/LFS so the bare-tree
  # NAR is fetcher-independent (crane's fetchgit sets fetchSubmodules/fetchLFS = true,
  # both no-ops here, so it yields the same NAR). The buildRustPackage fallback keys it
  # by Cargo.lock "name-version" ("smithay-0.7.0"); crane's vendorGitDeps keys
  # outputHashes by the full Cargo.lock `package.source` string (smithayGitSource).
  # Same value, two keyings.
  smithayGitHash = "sha256-44CNdBNGmGqBkCIVRVtJoQljZfn/JF682xAPX4m/2N8=";
  # The EXACT `source = "..."` line for smithay 0.7.0 in compositor/Cargo.lock. crane
  # indexes outputHashes by this string (NOT "smithay-0.7.0"). Split for readability;
  # the concatenation must equal the lock line verbatim or crane warns + falls back to
  # an eval-time builtins.fetchGit (non-fatal: it just loses offline eval).
  smithayGitSource =
    "git+https://github.com/Smithay/smithay"
    + "?rev=47843391c3cd34a32e5ed1721878ca2279269185"
    + "#47843391c3cd34a32e5ed1721878ca2279269185";

  # ── DRY: package meta, shared by the buildRustPackage fallback + the crane build ──
  compMeta = {
    description =
      "HART OS AI-native Wayland compositor (Smithay) - Tier-1, opt-in; "
      + "real-HW DRM/KMS scanout on the pixman software-render floor + "
      + "com.hart.Compositor IPC";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
    mainProgram = "hart-comp";
  };

  # ── Crane: incremental Rust build with a CACHED deps-only artifacts split ──
  # WHY (the iso-desktop 6h-limit fix): buildRustPackage compiles all ~245 crates in ONE
  # derivation keyed on the whole `src`, so ANY compositor/src edit recompiles every
  # crate (hours). crane's `buildDepsOnly` builds a DUMMY crate from the real
  # Cargo.toml/Cargo.lock (stubbed src), so its output (cargoArtifacts) is keyed on the
  # manifest + lock + toolchain + buildInputs + features, NOT on src/*.rs. A
  # compositor/src edit leaves cargoArtifacts UNCHANGED (substitutes from the store);
  # only the minutes-long app-crate `buildPackage` re-runs, keeping the in-ISO Rust
  # compile well under 6h.
  #
  # crane needs a SINGLE combined toolchain drv (nixpkgs ships cargo + rustc as separate
  # derivations), so symlinkJoin them. SAME rust_1_88 (rustc 1.88.0) stable cargo + rustc
  # the buildRustPackage path uses (>= 1.85 parses the edition2024 Smithay manifest;
  # 24.11's 1.83 cannot). rustc's wrapper bundles matching rust-std, so cargo + rustc
  # suffices (we never call cargoClippy/cargoFmt). Null off-flake (no specialArg) so the
  # buildRustPackage fallback is selected and plain module eval never crashes.
  rustToolchain =
    if hartCrane != null
    then rustNixpkgs.symlinkJoin {
      name = "hart-comp-rust-1_88";
      paths = with rustNixpkgs.rust_1_88.packages.stable; [ cargo rustc ];
    }
    else null;
  # mkLib uses rustNixpkgs (25.05) for crane's own helpers (jq, dummy-src tooling);
  # overrideToolchain points it at the rust_1_88 cargo + rustc above. The C buildInputs
  # in craneCommonArgs stay on the 24.11 `pkgs` (libgbm-in-mesa): newer compiler, same
  # libs, identical to the buildRustPackage cross-pin that is already M9-green.
  craneLib =
    if hartCrane != null
    then (hartCrane.mkLib rustNixpkgs).overrideToolchain rustToolchain
    else null;
  # cleanCargoSource keeps Cargo.toml/Cargo.lock + *.rs only (drops target/, the
  # m*_artifacts PNGs, *.md, *.sh) so the build's cache key is tight.
  compCleanSrc =
    if craneLib != null then craneLib.cleanCargoSource compositorSrc else null;
  # Vendor the 245-crate dep set ONCE (shared by the deps-only + app builds). The git
  # Smithay outputHash is provided EXPLICITLY so crane uses a fixed-output `fetchgit`
  # (offline eval, the SAME property the buildRustPackage cargoLock path has, so
  # `nix flake check --no-build` does NOT do an eval-time network fetch) keyed by the
  # Cargo.lock source string. A key mismatch is non-fatal (crane warns + falls back to
  # builtins.fetchGit); only a wrong VALUE fails, and this value is the cross-checked
  # rev NAR. All 245 crates are vendored regardless of features; `--features smithay`
  # only selects what COMPILES.
  cargoVendorDir =
    if craneLib != null
    then craneLib.vendorCargoDeps {
      src = compCleanSrc;
      outputHashes = { "${smithayGitSource}" = smithayGitHash; };
    }
    else null;

  # ── HART-comp package: the buildRustPackage FALLBACK (off-flake, no crane) ──
  # Selected only when hartCrane is NOT threaded (a consumer that imports this module
  # without the flake specialArgs); on the flake the crane path below is used. Kept so
  # plain module eval never crashes + so the importCargoLock outputHashes contract is
  # preserved for that path.
  #
  # NOTE: post the M1 forward-port the committed Cargo.lock is NO LONGER
  # registry-only — it now has 245 packages including the GIT Smithay dep pinned to
  # rev 47843391c3cd34a32e5ed1721878ca2279269185 (the `smithay`/`winit` cargo features
  # pull it). So a future reader must NOT "simplify" the outputHashes entry below
  # away — it is load-bearing for the smithay-feature resolution. This package uses
  # the reproducible cargoLock.lockFile model (the SAME idiom as
  # hart-rust-precedent.nix), PLUS an explicit `outputHashes` for the one git dep.
  hartCompPkgBuildRust = hartRustPlatform.buildRustPackage {
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
        "smithay-0.7.0" = smithayGitHash;
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
    # COMPILES the DRM modules (wayland.rs + udev.rs) AND runs the feature-independent
    # tests in main.rs (26) PLUS the smithay-gated `#[cfg(test)]` floors hoisted into the
    # shared modules — comp_core.rs (the chord map + cursor bake + fade-clock math, the
    # snap-zone `zone_rect` + tile `tile_rects` geometry, AND the screencopy region/time
    # floor: `clamp_region` no-out-of-bounds clamp + `transform_region` upright map +
    # `now_secs_nsecs` ready-timestamp split — these PURE helpers live in comp_core.rs,
    # gated `any(winit, smithay)`, NOT in screencopy.rs's winit-only `#![cfg]`, so this
    # smithay doCheck actually EXERCISES them), ipc.rs (the framed-JSON transport:
    # reassembly, the poison-frame guard, the request/response envelope, the arg
    # extractors, the 1↔0 workspace conversion + the event fan-out), and udev.rs (the
    # DRM-node override precedence + the color-format floor). So the build both
    # type-checks the DRM path AND asserts the backend-agnostic invariants. (Verified in
    # WSL: cargo test green at default; the smithay modules compile under --features
    # smithay.) The remaining winit-only screencopy floor is just the capture-FORMAT
    # invariants (CAPTURE_FOURCC + shm_supported), which touch winit-only items so they
    # run under the `--features winit` check; the live-Wayland SMOKE E2E
    # (compositor/tests/smoke_e2e.rs — boot→map→arrange→capture) is `#[ignore]`d and run
    # by the nested-Wayland CI job with `-- --ignored`.
    doCheck = true;

    meta = compMeta;
  };

  # ── Crane derivations: deps-only artifacts (cached) + the app crate ──
  # ONE shared args set used by BOTH cargoArtifacts and the app build. A deps crate that
  # links a C lib must see that lib at the deps stage too, so the SAME pkg-config +
  # wayland/libinput/.../xorg C-dep set goes on both (identical to the buildRustPackage
  # buildInputs above; `with pkgs` keeps them on the 24.11 pin -> libgbm-in-mesa).
  # `--features smithay` == the old buildFeatures = ["smithay"], in the SHARED args so the
  # deps build compiles the full smithay graph (incl. the git Smithay rev) into the
  # artifacts. The explicit cargoVendorDir (git Smithay hashed above) is shared so the
  # deps + app agree on the same vendored tree (a mismatch would defeat the cache reuse).
  craneCommonArgs = {
    src = compCleanSrc;
    inherit cargoVendorDir;
    strictDeps = true;
    cargoExtraArgs = "--features smithay";
    nativeBuildInputs = with pkgs; [ pkg-config ];
    buildInputs = lib.optionals pkgs.stdenv.isLinux (with pkgs; [
      wayland
      wayland-protocols
      libinput
      libxkbcommon
      pixman              # MANDATORY software-render path (never-fail floor)
      libdrm
      mesa                # GBM (libgbm ships in mesa) for the DRM scanout allocator
      seatd               # libseat C lib + seatd daemon (do NOT add pkgs.libseat: it is
                          # a throw-alias to seatd on this pin; listing both fails eval)
      udev                # device hotplug (backend_udev)
      xwayland            # link-time X11 client stack for smithay/xwayland
      xorg.libX11
      xorg.libxcb
      xorg.xcbutilwm
    ]);
  };

  # Deps-only (CACHED): a dummy crate from the real Cargo.toml/Cargo.lock. Keyed on the
  # manifest + lock + toolchain + buildInputs + features, NOT on src/*.rs, so a compositor
  # src edit reuses this from the store and only the app build below re-runs.
  cargoArtifacts =
    if craneLib != null then craneLib.buildDepsOnly craneCommonArgs else null;

  # The app crate (the only thing a compositor/src edit recompiles). doCheck = true keeps
  # the pure-logic test floor (main.rs/comp_core.rs/ipc.rs/udev.rs #[cfg(test)]) exactly
  # as the buildRustPackage path did; cargo forces panic=unwind for the test harness even
  # though [profile.release] panic="abort", so the tests stay green.
  hartCompPkgCrane = craneLib.buildPackage (craneCommonArgs // {
    pname = "hart-comp";
    version = "0.1.0";
    inherit cargoArtifacts;
    doCheck = true;
    meta = compMeta;
  });

  # Select crane (incremental, cached deps) when the flake threads it; fall back to the
  # buildRustPackage path off-flake so plain module eval (no specialArgs) never crashes.
  # Both install $out/bin/hart-comp, so config.hart.comp.package + the launcher's
  # ${hartCompPkg}/bin/hart-comp resolve identically either way.
  hartCompPkg = if hartCrane != null then hartCompPkgCrane else hartCompPkgBuildRust;

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
    # coreutils provides the launcher's own ls/seq/sleep/basename/id; xwayland puts
    # the `Xwayland` BINARY on PATH so Smithay's `XWayland::spawn` (which is hardcoded
    # to `Command::new("Xwayland")` and resolves it from $PATH — it only forwards PATH
    # + XDG_RUNTIME_DIR to the child) can launch it. xwayland in the package's
    # buildInputs is only the LINK-time C lib (smithay/xwayland); the runtime binary
    # must be on the session PATH or the X11/Wine path is dead ("xserver spawning
    # XWayland … No such file or directory" on real HW). Wayland-native clients are
    # unaffected — XWayland is best-effort — but the moat wants legacy/Wine windows too.
    PATH=${lib.makeBinPath (with pkgs; [ coreutils xwayland ])}:$PATH

    # ── GPU ARM DECISION — co-armed with the GSK shell renderer + the shell's
    #    effects via the SAME boot probe verdict (/run/hart/gpu-render) ──────────
    # preferHardwareGL = the operator override (force the hardware path). DEFAULT
    # false = AUTO: arm hardware ONLY when the boot probe (hart-gpu-probe, which
    # binds eglinfo to the Intel iGPU's i915 node and requires an Intel renderer)
    # wrote `hardware`. Re-probed every boot; fail-safe software (absent/garbled
    # verdict => software floor). The verdict file is written by hart-gpu-probe,
    # which runs BEFORE greetd, so it is on disk before this greetd session reads it.
    ${if (ui.preferHardwareGL or false) then ''
    _HART_ARMED=1   # operator override: hart.liquidUI.preferHardwareGL = true
    '' else ''
    _HART_GPU_VERDICT="$(cat /run/hart/gpu-render 2>/dev/null || echo software)"
    if [ "$_HART_GPU_VERDICT" = "hardware" ]; then _HART_ARMED=1; else _HART_ARMED=0; fi
    ''}

    # Software cursors are always safe (NOT a force-software signal) — unconditional.
    export WLR_NO_HARDWARE_CURSORS=1

    # ── THE FORCE-SOFTWARE GOTCHA FIX ──────────────────────────────────────────
    # main.rs::BootConfig::from_args treats WLR_RENDERER_ALLOW_SOFTWARE (AND
    # LIBGL_ALWAYS_SOFTWARE / HART_COMP_FORCE_SOFTWARE) as a FORCE-software signal,
    # so exporting WLR_RENDERER_ALLOW_SOFTWARE UNCONDITIONALLY pinned the compositor
    # to the pixman floor REGARDLESS of the probe verdict — making "drop
    # --force-software" a silent no-op so the GLES path could never build (the exact
    # gotcha this fix removes). Gate ALL of them — and the --force-software flag on
    # the launch below — on the SAME !armed condition, so an armed boot lets
    # select_render_path read the verdict and bring up GLES on the iGPU, while an
    # unarmed boot is byte-identical to the proven software floor. The pixman
    # renderer stays the renderer of record + the degrade-not-die fallback under
    # either path (udev.rs keeps it on ANY GLES fault), and a GLES/first-paint
    # failure still drops to cage via the session-supervisor paint watchdog — so a
    # half-finished hardware path can never brick the box (degrade chain + watchdog,
    # not an unconditional env pin).
    HART_COMP_FORCE_SW_FLAG=""
    if [ "$_HART_ARMED" != "1" ]; then
      export WLR_RENDERER_ALLOW_SOFTWARE=1
      export LIBGL_ALWAYS_SOFTWARE=1
      export HART_COMP_FORCE_SOFTWARE=1
      HART_COMP_FORCE_SW_FLAG="--force-software"
      echo "[hart-comp-session] render = SOFTWARE floor (pixman; not armed)" >&2
    else
      # ── THE OPERATOR-OVERRIDE SEAM (GLES-when-armed) ───────────────────────
      # Tell the compositor DIRECTLY that hardware is armed, so main.rs
      # select_render_path returns Hardware via its `prefer_hardware` check
      # WITHOUT independently re-reading /run/hart/gpu-render. This closes the
      # operator-override gap: when preferHardwareGL=true forces _HART_ARMED=1
      # but the boot probe fail-safed to `software`, dropping only the
      # --force-software pin was a silent no-op (select_render_path re-read the
      # verdict and stayed on pixman — the GLES path never came up). Exporting
      # HART_COMP_PREFER_HARDWARE=1 makes the LAUNCHER's arm decision the single
      # source of truth (no second, drift-prone read of the same decision). It
      # is also correct on the AUTO arm (verdict=hardware): prefer_hardware and
      # the verdict both resolve to Hardware. force_software is never set here,
      # so it can never win; and a GLES init/runtime fault still degrades to the
      # pixman renderer of record (udev.rs) + the paint watchdog drops a tier, so
      # this can raise the render path but never brick the box (#132 never-brick).
      export HART_COMP_PREFER_HARDWARE=1
      echo "[hart-comp-session] render = HARDWARE armed (GLES on the verified iGPU; pixman kept as the degrade-not-die fallback)" >&2
    fi

    # ── M7: Tier-1 is the REAL-HARDWARE DRM backend (`--backend drm`) — NOT the winit
    # dev backend (which needs a host Wayland socket that does not exist on a bare TTY).
    # hart-comp owns DRM/KMS scanout + the libinput seat; it creates its OWN wayland-N
    # socket. We run it in the BACKGROUND, wait for that socket to appear (it sets
    # WAYLAND_DISPLAY in its own env, but a sibling child needs the name explicitly), and
    # launch the SAME GTK4 layer-shell glass host as Tier-2 sway as its client — the
    # SINGLE glass-shell renderer, no parallel client.
    # HART_COMP_NO_TEST_CLIENT suppresses the dev auto-foot client.
    : "''${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
    export XDG_RUNTIME_DIR
    # The compositor binds its com.hart.Compositor IPC twin + its wayland-N socket
    # under $XDG_RUNTIME_DIR; both fail "No such file or directory" if the dir is
    # absent. Under greetd's logind session pam_systemd creates /run/user/$UID, but a
    # supervisor-less / non-logind launch (or a too-early bind) may race it — so
    # guarantee the dir exists (0700, owner-only, the systemd runtime-dir contract)
    # before the compositor starts. Idempotent: a no-op when pam_systemd already made it.
    mkdir -p "$XDG_RUNTIME_DIR" 2>/dev/null || true
    chmod 0700 "$XDG_RUNTIME_DIR" 2>/dev/null || true
    export HART_COMP_NO_TEST_CLIENT=1

    # Shell-paint readiness marker (the session-supervisor's HUNG-tier guard): the
    # glass host touches this once its WebView presents its first frame, telling the
    # paint-watchdog this Tier-1 surface is HEALTHY so it is NOT dropped as a hang.
    # The supervisor passes HART_SHELL_READY_FLAG into the session env; default to
    # the pinned /run/hart contract path so a bare (supervisor-less) launch is
    # harmless. We inherit + re-export it so the glass-host child sees it.
    export HART_SHELL_READY_FLAG="''${HART_SHELL_READY_FLAG:-/run/hart/session/shell-ready}"

    # ── ORPHAN-BRICK GUARD (FMEA #3, boot-hardening) ──────────────────────────
    # hart-comp and the glass shell run in the BACKGROUND (below) while this
    # wrapper blocks in `wait`. The session-supervisor launches this wrapper as
    # `sh -c "$CMD"` (a single bare path, so sh execs it -> this process IS its
    # sesspid) and, on a HUNG paint, sends it SIGTERM then SIGKILL. WITHOUT a
    # trap the wrapper dies but the backgrounded hart-comp is ORPHANED
    # (reparented to init), KEEPS the DRM master on card0, and EBUSY-blocks the
    # next tier (sway/cage) from becoming master -> a black screen instead of a
    # clean tier-drop. So forward the termination signal to the children and WAIT
    # for the compositor to actually exit (drmDropMaster on its way out) before we
    # leave, RELEASING the master so the lower tiers can take over. Registered
    # before the launch (PIDs default empty + guarded) so the whole window is
    # covered; the happy path (no signal) never runs the handler and is untouched.
    HART_COMP_PID=""
    HART_GLASS_PID=""
    _hart_comp_term() {
      trap - TERM INT          # one-shot: a second signal falls back to default
      [ -n "$HART_GLASS_PID" ] && kill -TERM "$HART_GLASS_PID" 2>/dev/null || true
      [ -n "$HART_COMP_PID" ]  && kill -TERM "$HART_COMP_PID"  2>/dev/null || true
      [ -n "$HART_COMP_PID" ]  && wait "$HART_COMP_PID"        2>/dev/null || true
      exit 0
    }
    trap _hart_comp_term TERM INT

    # --force-software is passed ONLY when NOT armed (HART_COMP_FORCE_SW_FLAG, set
    # above on the SAME !armed condition as the force-software env). Armed => the
    # flag is empty => select_render_path reads the verdict and brings up GLES on the
    # iGPU. Unquoted expansion is intentional (the flag is "" or "--force-software",
    # no spaces/globs) and safe under set -u (always assigned above).
    ${hartCompPkg}/bin/hart-comp --backend drm $HART_COMP_FORCE_SW_FLAG &
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
      # Prefer the GTK4 wlr-layer-shell glass host (hart.layerShellHost) — the
      # SAME host Tier-2 sway runs, anchored BACKGROUND (exclusive zone 0) so it
      # IS the desktop and native toplevels map above it. It touches the
      # HART_SHELL_READY_FLAG first-paint marker, so the supervisor's shell-paint
      # watchdog covers Tier-1 exactly as it covers Tier-2 (compositor up but no
      # first frame within the budget => HUNG => drop to sway then cage). Fall back
      # to the GTK3 cage `hart-glass-shell` (also touches the marker) if the GTK4
      # host is not in the closure — never a parallel renderer, just degrade.
      if command -v hart-glass-shell-gtk4 >/dev/null 2>&1; then
        hart-glass-shell-gtk4 &
        HART_GLASS_PID=$!
      elif command -v hart-glass-shell >/dev/null 2>&1; then
        hart-glass-shell &
        HART_GLASS_PID=$!
      else
        echo "hart-comp-session: no glass-shell host on PATH (enable hart.liquidUI / hart.layerShellHost)" >&2
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
      OPT-IN, default OFF; the compositor builds in Nix/CI + is VM-proven, real-HW
      paint proof required before it can become default. Does NOT flip defaultSession — cage
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
      # the IPC server lands; the feature-off build does not yet claim it (no server running).
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
