{
  description = "HART OS — AI-Native Agentic Operating System";

  inputs = {
    # Track unstable for latest GNOME (50+), kernel, and packages.
    # Safe because HART OTA pipeline (hart-ota.nix) does canary deploys
    # with automatic rollback on failure.
    # Pinned to known-good commit (June 2025). nixos-unstable March 2026
    # introduced breaking changes in ISO image builder (null coercion).
    # TODO: update once upstream fix lands.
    nixpkgs.url = "github:NixOS/nixpkgs/50ab793";

    # ── Newer-Rust toolchain source for the hart-comp Smithay crate ONLY ──
    # The main pin (50ab793) is the NixOS 24.11 release branch, whose newest Rust is
    # 1.83 (rust_1_82 / rust_1_83 are the only versioned attrs). The git-Smithay rev
    # the compositor pins (47843391, June-2026 main) has `edition = "2024"` +
    # `rust-version = "1.85"` in its Cargo.toml, so Cargo < 1.85 fails to even PARSE
    # its manifest ("feature `edition2024` is required"). 24.11 therefore CANNOT
    # build the moat crate — discovered by the first real `nix build .#hart-comp` in
    # CI (M9). This second input pins nixos-25.05 SOLELY to source `rust_1_88`
    # (rustc 1.88.0) for the hart-comp + hart-rust-precedent buildRustPackages —
    # 1.88 because claw's graph (time 0.3.47 / time-core / home 0.5.12) needs MSRV
    # 1.88, and it also satisfies Smithay's 1.85 floor. It is stock nixpkgs (NOT
    # rust-overlay/fenix — the precedent's "no new toolchain class" still holds), and
    # it touches NOTHING else: every ISO/image build keeps the 24.11 pin, and
    # hart-comp's C buildInputs (wayland/mesa/seatd/…) ALSO stay on 24.11 (24.11's
    # `mesa` still ships libgbm; 25.05 split it into a separate `libgbm` attr, so
    # mixing 25.05 libs would re-break the gbm link). Newer compiler, same libs.
    # Pinned to an exact rev (not the branch) for reproducibility.
    nixpkgs-rust.url = "github:NixOS/nixpkgs/ac62194c3917d5f474c1a844b6fd6da2db95077d";

    llama-cpp = {
      url = "github:ggml-org/llama.cpp";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    # Multi-format image generator (ISO, raw, SD, QCOW2, VMDK, VDI, Docker, AWS, GCE, Azure)
    nixos-generators = {
      url = "github:nix-community/nixos-generators";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    # Hardware-specific overlays (Raspberry Pi, etc.)
    nixos-hardware.url = "github:NixOS/nixos-hardware";

    # Mobile NixOS (PinePhone support)
    mobile-nixos = {
      url = "github:NixOS/mobile-nixos";
      flake = false;  # Not a flake yet — imported as path
    };
  };

  outputs = { self, nixpkgs, nixpkgs-rust, llama-cpp, nixos-generators, nixos-hardware, mobile-nixos }:
  let
    # Shared module list for all variants
    hartModules = [
      ./modules/hart-base.nix
      ./modules/hart-first-boot.nix
      ./modules/hart-backend.nix
      ./modules/hart-discovery.nix
      ./modules/hart-agent.nix
      ./modules/hart-llm.nix
      ./modules/hart-vision.nix
      ./modules/hart-conky.nix
      ./modules/hart-nunba.nix
      ./modules/hart-kernel.nix
      ./modules/hart-subsystems.nix
      ./modules/hart-ai-runtime.nix
      ./modules/hart-sandbox.nix
      # AI-Native Everything OS modules
      ./modules/hart-model-bus.nix
      ./modules/hart-compute-mesh.nix
      ./modules/hart-liquid-ui.nix
      ./modules/hart-app-bridge.nix
      # Never-blank-screen session tier-drop supervisor (Phase 1 / B4). Opt-in
      # (hart.sessionSupervisor.enable=false default) -> pure no-op for every
      # variant (gated config; lazy sway default never enters a disabled
      # closure); imported so the option exists + the nixosTest can enable it.
      ./modules/hart-session-supervisor.nix
      # sway-as-Tier-1: the proven-in-WSL OS-native windowing session (canonical
      # glass shell under sway) + the hart-swaymsg-shim the brain's HartWmClient
      # drives at Tier-2. Opt-in (hart.swayTier1.enable=false default) -> no-op
      # for every variant; NO test enables it, so it never pulls graphical-desktop
      # (no inotify-class tie). Makes "sway-Tier-1-now" a real greeter-selectable
      # session + gives the moat a Tier-2 shim target, instead of an orphan file.
      ./modules/hart-sway-tier1.nix
      # Phase-4 GTK4 layer-shell glass-shell host: the budgeted GTK3 -> GTK4
      # WebKitGTK host-window port that re-hosts the SAME served shell as a real
      # wlr-layer-shell BACKGROUND surface (exclusive zone 0, JS unchanged). Opt-in
      # (hart.layerShellHost.enable=false default) -> no-op for every variant;
      # the Phase-4 nixosTest enables it on a desktop node to prove the GTK4 host
      # paints on llvmpipe + a GTK4-crash drops to the GTK3 cage Tier-3 floor.
      # defaultSession STAYS cage (GTK3); this only ADDS a greeter-selectable L2
      # host-window session, instead of leaving the port an orphan file.
      ./modules/hart-layer-shell-host.nix
      # L4 Freedesktop portals + cross-process screen kill-switch + ext-session-
      # lock (Phase 7). Opt-in (hart.portal.enable=false default) -> pure no-op for
      # every variant; NO test enables it outside its own nixosTest, so it never
      # pulls a portal/lock closure into the default build. Imported so the option
      # exists + tests/portal-screencast.nix can enable it. Ships the cross-process
      # screen gate (the portal MUST consult core.ai_sensing fail-closed BEFORE any
      # native capture), wlr-screencopy routing, the theme->portal Settings bridge,
      # and the real PAM-backed ext-session-lock for Tier-1/2. cage Tier-3 (no
      # portal => no native capture) stays the safe floor.
      ./modules/hart-portal.nix
      # Remote Desktop peripherals + casting
      ./modules/hart-peripheral-bridge.nix
      ./modules/hart-dlna.nix
      # OS management
      ./modules/hart-ota.nix
      ./modules/hart-nvidia.nix
      ./modules/hart-luks.nix
      ./modules/hart-firewall.nix
      ./modules/hart-power.nix
      ./modules/hart-accessibility.nix
      # Desktop management
      ./modules/hart-cups.nix
      ./modules/hart-nightlight.nix
      ./modules/hart-ime.nix
      ./modules/hart-gaming.nix
      ./modules/hart-devtools.nix
      ./modules/hart-osk.nix
      # Onboarding ceremony (GTK4/libadwaita native)
      ./modules/hart-onboarding.nix
      # Runtime self-build (OS rebuilds itself live)
      ./modules/hart-self-build.nix
      # ── HART-comp: the AI-native Smithay/Rust Tier-1 compositor (THE MOAT) ──
      # The FIRST Rust-in-Nix build. hart-rust-precedent.nix proves the pinned
      # toolchain resolves a real crate graph (claw_native/rust) FIRST; hart-comp.nix
      # builds compositor/ (the DRM/KMS Wayland compositor) via buildRustPackage with
      # buildFeatures = [ "smithay" ]. BOTH are imported so their options exist + the
      # hart-comp assertion (`config.hart.rustPrecedent.enable`) resolves; BOTH default
      # OFF (hart.comp.enable / hart.rustPrecedent.enable = false), so they are a pure
      # no-op for every variant until the steward arms Tier-1. defaultSession STAYS
      # cage; hart-comp.nix only ADDS a greeter-selectable session + the supervisor's
      # Tier-1 rung. (M7: the DRM backend + run path now COMPILE; the package is
      # CI-gated via `nix build .#…config.hart.comp.package` + the flake eval here.)
      ./modules/hart-rust-precedent.nix
      ./modules/hart-comp.nix
      # Persistent boot-diagnostic log partition: when a FAT32 partition labelled
      # HARTLOG is present (the flasher creates it in the stick's free space),
      # HART OS writes the full current-boot journal + tier-supervisor state +
      # GTK4/GL diagnostics to it early in boot, on a periodic timer (so a HUNG
      # Tier-1 boot still leaves a record), and at shutdown — so a Windows host
      # reads the boot journal off the stick WITHOUT hand-copying from a TTY.
      # Opt-in (hart.bootLog.enable=false default) -> pure no-op for every
      # variant; ALSO a clean no-op at runtime when no HARTLOG partition exists.
      # Imported so the option exists + tests/boot-log.nix can enable it; the
      # live/desktop config turns it on (desktop.nix).
      ./modules/hart-boot-log.nix
      # Live-OS self-creation of the HARTLOG partition: on first boot from a
      # removable/USB stick with trailing free space + no existing HARTLOG, HART
      # OS carves a FAT32 HARTLOG partition into ONLY that free space (sgdisk +
      # mkfs.vfat), so hart-boot-log can land the journal on the stick. REPLACES
      # the Windows-flasher diskpart path (which hung on a wedged VDS + corrupted
      # a freshly-flashed stick's EFI/GPT). Ordered BEFORE hart-boot-log's
      # capture. Opt-in (hart.hartlogCreate.enable=false default) -> pure no-op
      # for every variant; ALSO a no-op at runtime when not USB-booted / no free
      # space / HARTLOG already exists. Imported so the option exists +
      # tests/hartlog-create.nix can enable it; desktop.nix turns it on.
      ./modules/hart-hartlog-create.nix
      # Boot continuity: when a restart is initiated FROM the Live OS, set a
      # ONE-SHOT efibootmgr BootNext to the USB's OWN EFI boot entry so the next
      # boot returns to HART OS WITHOUT the user mashing F12. It does NOT change
      # the permanent BootOrder, so Windows still boots normally when chosen. A
      # no-op if efibootmgr is missing, not UEFI-booted, or the entry can't be
      # matched. Opt-in (hart.bootContinuity.enable=false default) -> pure no-op
      # for every variant; desktop.nix turns it on. tests/boot-continuity.nix
      # gates the structural assertions.
      ./modules/hart-boot-continuity.nix
    ];

    # Single source of truth for nixpkgs config (allowUnfree etc.).  Kept OUT of
    # the shared modules (hart-base/desktop/server used to each set it — a DRY
    # spread) so vm-tests' runNixOSTest nodes, which receive read-only pkgs, do
    # NOT hit "nixpkgs.config defined multiple times" (#70).  Real builds get it
    # via mkSystem/mkImage below; the VM test gets it via its `pkgs` (checks).
    nixpkgsConfig = {
      allowUnfree = true;
      allowBroken = false;  # was set per-config in desktop.nix/server.nix
      permittedInsecurePackages = [ "electron-33.4.11" ];
    };

    # Common specialArgs passed to all modules
    mkSpecialArgs = variant: {
      inherit llama-cpp mobile-nixos nixos-hardware;
      hartVersion = "1.0.0";
      hartVariant = variant;
      hartSrc = ../.;  # repo root
      # The newer-Rust nixpkgs (25.05) — passed as the raw flake input so the Rust
      # modules can instantiate `rust_1_88` for THEIR system (the module knows its own
      # system via pkgs.stdenv). Only hart-comp.nix + hart-rust-precedent.nix consume
      # it; every other module ignores it.
      # See the `nixpkgs-rust` input comment for WHY (24.11 Rust < 1.85 can't parse the
      # edition2024 Smithay manifest).
      hartRustNixpkgs = nixpkgs-rust;
    };

    # Build a full NixOS system configuration
    mkSystem = { system, variant, extraModules ? [] }:
      nixpkgs.lib.nixosSystem {
        inherit system;
        specialArgs = mkSpecialArgs variant;
        modules = hartModules ++ [
          { nixpkgs.config = nixpkgsConfig; }  # single source — #70
          ./configurations/${variant}.nix
        ] ++ extraModules;
      };

    # Build an image via nixos-generators (for non-ISO formats)
    mkImage = { system, variant, format, extraModules ? [] }:
      nixos-generators.nixosGenerate {
        inherit system format;
        specialArgs = mkSpecialArgs variant;
        modules = hartModules ++ [
          { nixpkgs.config = nixpkgsConfig; }  # single source — #70
          ./configurations/${variant}.nix
        ] ++ extraModules;
      };

    # All supported systems
    forAllSystems = nixpkgs.lib.genAttrs [
      "x86_64-linux"
      "aarch64-linux"
      "riscv64-linux"
    ];

    # Go package builder helper
    mkGoPackage = { pkgs, name, src, subPackage ? "." }:
      pkgs.buildGoModule {
        pname = name;
        version = "1.0.0";
        inherit src;
        vendorHash = null;  # Zero external deps
        subPackages = [ subPackage ];
        meta = {
          description = "HART OS ${name}";
          license = pkgs.lib.licenses.mit;
        };
      };
  in
  {
    # ═════════════════════════════════════════════════════════════
    # NixOS Configurations (nixos-rebuild build --flake .#name)
    # ═════════════════════════════════════════════════════════════
    nixosConfigurations = {
      # ─── x86_64 (PC / Laptop / Server) ───
      hart-server  = mkSystem { system = "x86_64-linux"; variant = "server"; };
      hart-desktop = mkSystem { system = "x86_64-linux"; variant = "desktop"; };
      hart-edge    = mkSystem { system = "x86_64-linux"; variant = "edge"; };

      # ─── aarch64 (ARM: Raspberry Pi, edge, phones) ───
      hart-server-arm  = mkSystem { system = "aarch64-linux"; variant = "server"; };
      hart-desktop-arm = mkSystem { system = "aarch64-linux"; variant = "desktop"; };
      hart-edge-arm    = mkSystem { system = "aarch64-linux"; variant = "edge"; };

      # ─── riscv64 (RISC-V: StarFive, SiFive, edge) ───
      hart-server-riscv = mkSystem {
        system = "riscv64-linux";
        variant = "server";
        extraModules = [ ./hardware/riscv-generic.nix ];
      };
      hart-edge-riscv = mkSystem {
        system = "riscv64-linux";
        variant = "edge";
        extraModules = [ ./hardware/riscv-generic.nix ];
      };

      # ─── Phone (PinePhone / PinePhone Pro) ───
      hart-phone = mkSystem {
        system = "aarch64-linux";
        variant = "phone";
        extraModules = [ ./hardware/pinephone.nix ];
      };

      # ─── Raspberry Pi ───
      hart-server-rpi = mkSystem {
        system = "aarch64-linux";
        variant = "server";
        extraModules = [ ./hardware/raspberry-pi.nix ];
      };
      hart-desktop-rpi = mkSystem {
        system = "aarch64-linux";
        variant = "desktop";
        extraModules = [ ./hardware/raspberry-pi.nix ];
      };
    };

    # ═════════════════════════════════════════════════════════════
    # Packages: ISO images, multi-format images, Go binaries
    # ═════════════════════════════════════════════════════════════
    packages = forAllSystems (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        isX86 = system == "x86_64-linux";
        isArm = system == "aarch64-linux";
        # SHA256SUMS sidecar for an isoImage: a dir with the .iso symlinked + its
        # checksum, so an operator can `sha256sum -c` the build AND the flashed
        # USB (dd-device readback) BEFORE booting. The "corrupt squashfs on bare
        # metal while a VM boots fine" class is almost always a truncated or
        # bit-flipped DD-flash / cheap USB; this is the verify step that catches
        # it (and surfaces a non-reproducible build if two builders disagree).
        isoSha256 = name: isoDrv: pkgs.runCommand "${name}-iso-sha256" { } ''
          mkdir -p $out
          cd ${isoDrv}/iso
          ${pkgs.coreutils}/bin/sha256sum *.iso > $out/SHA256SUMS
          for f in *.iso; do ln -s ${isoDrv}/iso/"$f" $out/"$f"; done
          cat $out/SHA256SUMS
        '';
      in
      {
        # ─── ISO Images (bootable USB / optical) ───
        iso-server  = self.nixosConfigurations.hart-server.config.system.build.isoImage;
        iso-desktop = self.nixosConfigurations.hart-desktop.config.system.build.isoImage;
        iso-edge    = self.nixosConfigurations.hart-edge.config.system.build.isoImage;

        # ─── ISO + SHA256SUMS (flash-integrity verification for bare-metal) ───
        # `nix build .#iso-desktop-sha256` yields the .iso + its SHA256SUMS so the
        # USB is verifiable before AND after DD (sha256sum -c vs the dd'd device).
        iso-server-sha256  = isoSha256 "hart-os-server"  self.nixosConfigurations.hart-server.config.system.build.isoImage;
        iso-desktop-sha256 = isoSha256 "hart-os-desktop" self.nixosConfigurations.hart-desktop.config.system.build.isoImage;
        iso-edge-sha256    = isoSha256 "hart-os-edge"    self.nixosConfigurations.hart-edge.config.system.build.isoImage;

        # ─── Raw EFI disk images (dd to SSD/NVMe) ───
        raw-server  = mkImage { inherit system; variant = "server";  format = "raw-efi"; };
        raw-desktop = mkImage { inherit system; variant = "desktop"; format = "raw-efi"; };
        raw-edge    = mkImage { inherit system; variant = "edge";    format = "raw-efi"; };

        # ─── QCOW2 (QEMU / KVM / Proxmox) ───
        qcow2-server  = mkImage { inherit system; variant = "server";  format = "qcow"; };
        qcow2-desktop = mkImage { inherit system; variant = "desktop"; format = "qcow"; };

        # ─── VMware (VMDK) ───
        vmware-server  = mkImage { inherit system; variant = "server";  format = "vmware"; };
        vmware-desktop = mkImage { inherit system; variant = "desktop"; format = "vmware"; };

        # ─── VirtualBox (VDI) ───
        vbox-server  = mkImage { inherit system; variant = "server";  format = "virtualbox"; };
        vbox-desktop = mkImage { inherit system; variant = "desktop"; format = "virtualbox"; };

        # ─── Docker / OCI container image ───
        docker-server = mkImage { inherit system; variant = "server"; format = "docker"; };

        # ─── Cloud: Amazon AMI ───
        amazon-server = mkImage { inherit system; variant = "server"; format = "amazon"; };

        # ─── Cloud: Google Compute Engine ───
        gce-server = mkImage { inherit system; variant = "server"; format = "gce"; };

        # ─── Cloud: Azure VHD ───
        azure-server = mkImage { inherit system; variant = "server"; format = "azure"; };

        # ─── SD card images (Raspberry Pi, PinePhone) ───
        sd-server-arm = mkImage {
          system = "aarch64-linux";
          variant = "server";
          format = "sd-aarch64";
          extraModules = [ ./hardware/raspberry-pi.nix ];
        };
        sd-desktop-arm = mkImage {
          system = "aarch64-linux";
          variant = "desktop";
          format = "sd-aarch64";
          extraModules = [ ./hardware/raspberry-pi.nix ];
        };
        sd-phone = mkImage {
          system = "aarch64-linux";
          variant = "phone";
          format = "sd-aarch64";
          extraModules = [ ./hardware/pinephone.nix ];
        };

        # ─── Go binaries ───
        hart-cli-go = mkGoPackage {
          inherit pkgs;
          name = "hart-cli-go";
          src = ../deploy/linux/hart-cli-go;
        };
        hart-pxe-server-go = mkGoPackage {
          inherit pkgs;
          name = "hart-pxe-server-go";
          src = ../deploy/distro/pxe/hart-pxe-server-go;
        };
      }
      # ── Rust-in-Nix BUILD gates (compile the crates in CI, not just eval) ──
      # These re-expose the SAME read-only options the modules already promise in
      # their docstrings (hart-comp.nix:264-266 / hart-rust-precedent.nix:135 —
      # "CI gates on `nix build .#…config.hart.<x>.package`"). They are short
      # ALIASES, NOT second package definitions — the package expression lives once,
      # in the module, exposed via `lib.mkDefault` on EVERY config (outside the
      # `lib.mkIf <x>.enable` block), so building it does NOT arm the session or trip
      # the enable assertions (rustPrecedent.enable / liquidUI webkit). That is
      # exactly what the option's docstring intends: CI compiles the binary so the
      # node-integrity manifest can hash it WITHOUT flipping defaultSession.
      #
      # x86_64-only (guard like the ISO sha256 entries): the compositor is a
      # Linux/x86_64 DRM/Wayland crate; cross-eval on aarch64/riscv legacyPackages is
      # unnecessary and only the x86_64 CI runner compiles it.
      #
      # Precedent FIRST (claw-cli, registry-only lock) so a toolchain-resolution
      # failure is isolated BEFORE hart-comp depends on it (the whole reason
      # hart-rust-precedent.nix exists). Then hart-comp (the git-Smithay crate,
      # buildFeatures = [ "smithay" ], fetched via the resolved outputHashes entry).
      // pkgs.lib.optionalAttrs isX86 {
        hart-rust-precedent = self.nixosConfigurations.hart-desktop.config.hart.rustPrecedent.package;
        hart-comp           = self.nixosConfigurations.hart-desktop.config.hart.comp.package;
      }
      // {
        # Default: server ISO
        default = self.packages.${system}.iso-server;
      }
    );

    # ═════════════════════════════════════════════════════════════
    # Checks: NixOS VM integration tests (nix flake check)
    # ═════════════════════════════════════════════════════════════
    checks.x86_64-linux = let
      # Configured pkgs so runNixOSTest nodes (read-only) carry allowUnfree
      # WITHOUT any node module setting nixpkgs.config — the #70 fix.
      #
      # #70 FIX APPLIED (tests/vm-tests.nix): the vm-test nodes no longer import
      # the full ISO configs (../configurations/X.nix).  Those configs imported
      # the installer-CD profile, which set nixpkgs.overlays and collided with
      # runNixOSTest's read-only node.pkgs ("nodes.X.nixpkgs.overlays defined
      # multiple times") + dragged in isoImage.*, making the checks
      # un-EVALUABLE and blocking `nix flake check` (hence all ISO CI).  The
      # nodes are now built from the hart modules alone with the variant
      # enabled ({hart.enable; hart.variant} — modules are variant-gated), and
      # specialArgs(hartSrc) is passed via `node.specialArgs`.  `nix flake check
      # --no-build` only needs the nodes to EVALUATE; the testScript assertions
      # run in the build job.  Earlier landed #70 fixes: phosh +
      # services.modemManager removed, nixpkgs.config single-sourced (fd95368).
      pkgs = import nixpkgs { system = "x86_64-linux"; config = nixpkgsConfig; };
      vmTests = import ./tests/vm-tests.nix {
        inherit pkgs hartModules;
        specialArgs = mkSpecialArgs "server";
      };
      # Phase-0 floor-lock + Phase-1 session-supervisor nixosTests were authored
      # (tests/floor-lock.nix, tests/session-supervisor.nix) but never wired into
      # `checks` (= vm-tests.nix only), so `nix flake check` ran NEITHER — a test
      # that never runs guards nothing (CLAUDE.md Gate 5). Distinct attr names ->
      # clean //. specialArgs only carries hartSrc here (each node sets its own
      # hart.variant via mkNode), so the "desktop" tag is variant-neutral.
      desktopTestArgs = {
        inherit pkgs hartModules;
        specialArgs = mkSpecialArgs "desktop";
      };
      floorLock  = import ./tests/floor-lock.nix desktopTestArgs;
      supervisor = import ./tests/session-supervisor.nix desktopTestArgs;
      # GDM-based desktop-boot: the floor-lock's DM-driven twin. floor-lock runs a
      # #70-minimal node with NO display manager, so it DEFERS DM-driven
      # registration + the bit-for-bit software-GL launcher env + the first-frame
      # paint + the WebView-kill recovery (floor-lock.nix :89,:116 name this test).
      # This node adds a real GDM that materializes sessionPackages -> sessionData
      # and autologins the cage hart-shell session, making those four gates TESTED
      # on an llvmpipe VM. Distinct attr (hart-desktop-shell-boot) -> clean //.
      desktopShellBoot = import ./tests/desktop-boot.nix desktopTestArgs;
      # Phase-4 GTK4 layer-shell host: TWO nodes (distinct attrs -> clean //).
      #  (a) hart-layer-shell-host — STRUCTURAL (no DM): the GTK4 toolkit typelibs
      #      are in the closure + the served /shell/static fetch is 200 (dead-husk-
      #      aware) + the GTK3 cage Tier-3 floor is intact + Model-1 z-order in code.
      #  (b) hart-layer-shell-host-paint — the FRESH broken-GPU PAINT proof: a GDM
      #      node autologins the hart-glass-gtk4 session (sway hosting the GTK4 +
      #      WebKitGTK-6.0 + gtk4-layer-shell host), the GTK4 BACKGROUND surface
      #      anchors + paints on llvmpipe under NEVER-accel, the rendered brand is
      #      OCR'd off the framebuffer, and a GTK4-host kill lands on the cage floor
      #      (still software-GL + the served shell still serves). The GTK4 path's
      #      OWN paint floor, not an inherited GTK3 assumption (ROADMAP Phase 4).
      # desktop-variant nodes (mkNode sets the variant); specialArgs carries hartSrc.
      layerShellHost = import ./tests/layer-shell-host.nix desktopTestArgs;
      # Phase-7 portals + cross-process screen kill-switch: boots a desktop node
      # with hart.portal.enable; the LiquidUI shell host (the kill-switch's _state
      # holder) starts the cross-process authority, and the test asserts the
      # wlr-screencopy gate REFUSES (exit 77) when 'screen' is cut and ALLOWS
      # when on — a Flatpak/Wine-equivalent capture denied at the portal gate, not
      # just a flag. Plus: status() reports portal_screencast_blocked, the hart
      # .portal backend + dbus policy are in the closure, and the hart-lock PAM
      # service exists. Distinct attr names -> clean //; desktop-variant node.
      portalScreencast = import ./tests/portal-screencast.nix desktopTestArgs;
      # Central-controlled autonomous OTA: node auto-polls CENTRAL (not github),
      # stages via the existing pipeline, autoApply switches with auto-rollback.
      # server-variant node (OTA is variant-neutral; server is the lightest).
      otaCentral = import ./tests/ota-central.nix {
        inherit pkgs hartModules;
        specialArgs = mkSpecialArgs "server";
      };
      # Native subsystems (genuine app support): a desktop node turns the
      # Android (Waydroid) + web (browser-extension force-install) subsystems ON
      # and asserts the runtime is REAL — the stock waydroid container unit is in
      # the closure, the old `sleep infinity` Android stub is GONE, the first-boot
      # Waydroid init is never-fail/oneshot, the Chromium+Firefox extension
      # force-install policy surface exists, macOS-OFF is a pure no-op, and no
      # fake snapd was shipped (snap is honestly unsupported). Distinct attr names
      # -> clean //; desktop-variant node (mkNode), subsystems enabled in-test.
      nativeSubsystems = import ./tests/native-subsystems.nix desktopTestArgs;
      # Persistent boot-diagnostic log partition: a desktop node enables
      # hart.bootLog + attaches a spare disk the test formats FAT32/labels HARTLOG
      # (the stand-in for the stick's free-space partition the flasher creates).
      # It runs the REAL capture script and asserts the full bundle (boot journal
      # + supervisor tier state + shell-ready marker + GTK4/GL diagnostics) lands
      # on the partition with a stable hart-boot-latest.log, AND that the
      # no-HARTLOG path is a clean no-op (old stick / plain flash still boots).
      # Distinct attr name -> clean //; desktop-variant node (mkNode).
      bootLog = import ./tests/boot-log.nix desktopTestArgs;
      # Live-OS HARTLOG self-create: a desktop node enables hart.hartlogCreate +
      # attaches a spare disk standing in for the USB stick (GPT + a small "ISO"
      # part + trailing free space). It runs the REAL carve script and asserts a
      # NEW HARTLOG FAT32 partition appears in the free space, the pre-existing
      # part is UNTOUCHED, a second run is an idempotent no-op, and a full disk is
      # a clean no-op. This is the Linux-side replacement for the corrupting
      # Windows diskpart path. Distinct attr name -> clean //; desktop node.
      hartlogCreate = import ./tests/hartlog-create.nix desktopTestArgs;
      # Boot continuity (one-shot BootNext): a desktop node enables
      # hart.bootContinuity; the test asserts the unit + efibootmgr are in the
      # closure, the ExecStop reboot hook is wired + ordered before
      # systemd-reboot, the script NEVER writes BootOrder (the never-strand-
      # Windows invariant), and running it on a non-UEFI VM is a clean no-op
      # exit 0. The live BootNext write needs real UEFI HW. Distinct attr -> //.
      bootContinuity = import ./tests/boot-continuity.nix desktopTestArgs;
    in vmTests // floorLock // supervisor // desktopShellBoot // layerShellHost // portalScreencast // otaCentral // nativeSubsystems // bootLog // hartlogCreate // bootContinuity;

    # ═════════════════════════════════════════════════════════════
    # VM apps (fast dev/test cycle: nix run .#vm-server)
    # ═════════════════════════════════════════════════════════════
    apps = forAllSystems (system: {
      vm-server = {
        type = "app";
        program = "${self.nixosConfigurations.hart-server.config.system.build.vm}/bin/run-hart-server-vm";
      };
      vm-desktop = {
        type = "app";
        program = "${self.nixosConfigurations.hart-desktop.config.system.build.vm}/bin/run-hart-desktop-vm";
      };
      vm-edge = {
        type = "app";
        program = "${self.nixosConfigurations.hart-edge.config.system.build.vm}/bin/run-hart-edge-vm";
      };
    });
  };
}
