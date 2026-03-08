{
  description = "HART OS — AI-Native Agentic Operating System";

  inputs = {
    # Track unstable for latest GNOME (50+), kernel, and packages.
    # Safe because HART OTA pipeline (hart-ota.nix) does canary deploys
    # with automatic rollback on failure.
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

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

  outputs = { self, nixpkgs, llama-cpp, nixos-generators, nixos-hardware, mobile-nixos }:
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
    ];

    # Common specialArgs passed to all modules
    mkSpecialArgs = variant: {
      inherit llama-cpp mobile-nixos nixos-hardware;
      hartVersion = "1.0.0";
      hartVariant = variant;
      hartSrc = ../.;  # repo root
    };

    # Build a full NixOS system configuration
    mkSystem = { system, variant, extraModules ? [] }:
      nixpkgs.lib.nixosSystem {
        inherit system;
        specialArgs = mkSpecialArgs variant;
        modules = hartModules ++ [
          ./configurations/${variant}.nix
        ] ++ extraModules;
      };

    # Build an image via nixos-generators (for non-ISO formats)
    mkImage = { system, variant, format, extraModules ? [] }:
      nixos-generators.nixosGenerate {
        inherit system format;
        specialArgs = mkSpecialArgs variant;
        modules = hartModules ++ [
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
      in
      {
        # ─── ISO Images (bootable USB / optical) ───
        iso-server  = self.nixosConfigurations.hart-server.config.system.build.isoImage;
        iso-desktop = self.nixosConfigurations.hart-desktop.config.system.build.isoImage;
        iso-edge    = self.nixosConfigurations.hart-edge.config.system.build.isoImage;

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

        # Default: server ISO
        default = self.packages.${system}.iso-server;
      }
    );

    # ═════════════════════════════════════════════════════════════
    # Checks: NixOS VM integration tests (nix flake check)
    # ═════════════════════════════════════════════════════════════
    checks.x86_64-linux = let
      pkgs = nixpkgs.legacyPackages.x86_64-linux;
      vmTests = import ./tests/vm-tests.nix {
        inherit pkgs hartModules;
        specialArgs = mkSpecialArgs "server";
      };
    in vmTests;

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
