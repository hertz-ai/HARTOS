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

    # ── Crane: incremental Rust-in-Nix builder for hart-comp (deps-only cache) ──
    # WHY: rustPlatform.buildRustPackage compiles ALL ~245 compositor crates in ONE
    # derivation keyed on the whole `src`, so ANY compositor/src edit recompiles every
    # crate from scratch (hours). Baked into the iso-desktop closure, that pushed the
    # desktop ISO Release job past the GitHub Actions 6h job limit. crane splits the
    # build into a CACHED deps-only `cargoArtifacts` derivation plus a thin app-crate
    # `buildPackage`, so a compositor/src edit recompiles ONLY the app crate (minutes)
    # and the 245 deps substitute from the store. hart-comp.nix is the ONLY consumer
    # (threaded via mkSpecialArgs as hartCrane, the same pattern as nixpkgs-rust).
    #
    # NO `inputs.nixpkgs.follows` here, on purpose: crane is a pure LIBRARY with ZERO
    # flake inputs (`nix flake metadata github:ipetkov/crane` -> root inputs []), so a
    # `follows` line is a hard eval error ("input 'crane' has no input named
    # 'nixpkgs'"). crane.mkLib takes whatever pkgs we hand it (hart-comp.nix hands it
    # the 25.05 rust_1_88 toolchain), so it pulls NO second nixpkgs into the closure
    # and there is nothing to dedupe. Pinned to an exact rev (verified live via
    # `nix flake metadata github:ipetkov/crane`) for reproducibility, same as the
    # nixpkgs-rust pin above; run `nix flake lock` on a nix-capable host to also
    # commit the resolved narHash into flake.lock.
    crane.url = "github:ipetkov/crane/469fd08d0bcf6926321fa973c6777fbc87785dd7";

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

  outputs = { self, nixpkgs, nixpkgs-rust, crane, llama-cpp, nixos-generators, nixos-hardware, mobile-nixos }:
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
      # Cross-OS runtime smoke-test: a post-boot oneshot (IN PARALLEL with the
      # desktop, NOT before greetd) that actually EXECUTES a tiny test command
      # inside each ENABLED foreign-OS runtime (Windows/Wine, Android/Waydroid,
      # macOS/Darling) + the Linux runtimes (Flatpak/AppImage) and writes an HONEST
      # per-runtime status (ok/failed/ready/no-image/skip) to /run/hart/compat-status
      # — so the OS's cross-OS capability is MEASURED, not claimed unconditionally
      # by the installer. On by default (hart.subsystems.smoketest.enable, gated on
      # the subsystems master toggle). FAIL-SAFE: each probe records failed on a
      # hang/error (never aborts the others), absent tool => skip; the unit always
      # succeeds so it can never block or fail the boot.
      ./modules/hart-compat-smoketest.nix
      ./modules/hart-ai-runtime.nix
      ./modules/hart-sandbox.nix
      # AI-Native Everything OS modules
      ./modules/hart-model-bus.nix
      # Robot Model-Bus capability probe (embodied twin of hart-compat-smoketest):
      # a post-boot oneshot that REACHES the Model Bus for each core intelligence a
      # robot needs (LLM / vision / VLA / on-node /think fusion) and writes an honest
      # per-capability verdict to /run/hart/robot-capability-status. Auto-enables
      # wherever the Model Bus runs (hart.robotics.probe.enable defaults to
      # hart.modelBus.enable); adds NOTHING to a node with no Model Bus (server/edge).
      # Never-fail (oneshot + RemainAfterExit + the python probe always exits 0,
      # bounded TimeoutStartSec) and runs IN PARALLEL with the desktop (NOT before
      # greetd), so it can never delay first paint, block, or fail the boot.
      ./modules/hart-robot-probe.nix
      ./modules/hart-compute-mesh.nix
      ./modules/hart-liquid-ui.nix
      ./modules/hart-app-bridge.nix
      # Native desktop notifications: mako (the wlroots-native org.freedesktop.
      # Notifications daemon) as a glass-styled graphical-session user service, so
      # foreign apps (Wine/Android), AI-composed .hartapp surfaces, and the robot can
      # raise a real toast via notify-send / D-Bus. Never-fail (a mako crash only loses
      # native toasts; the in-shell SSE toast is the fallback on every tier).
      ./modules/hart-notify.nix
      # Never-blank-screen session tier-drop supervisor (Phase 1 / B4). Opt-in
      # (hart.sessionSupervisor.enable=false default) -> pure no-op for every
      # variant (gated config; lazy sway default never enters a disabled
      # closure); imported so the option exists + the nixosTest can enable it.
      ./modules/hart-session-supervisor.nix
      # GPU smoke-test gate: a boot-time oneshot (BEFORE greetd) that probes
      # whether the GPU can create a GL context + report a hardware renderer and
      # writes the verdict (hardware/software) to /run/hart/gpu-render. A safe
      # consumer (Tier-2 sway, hart-layer-shell-host.nix) reads it to DEFAULT to
      # hardware GL only when the GPU is proven, else forces software. Opt-in via
      # the always-true-by-default hart.gpu.accelerate; gated on cfg.enable. The
      # cage Tier-3 floor + the GTK4 GSK cairo renderer + hart-comp pixman stay
      # forced-software regardless (the probe NEVER touches the floor). FAIL-SAFE:
      # any error/timeout/missing-tool writes `software`; the unit always succeeds
      # so it can never block or fail the boot.
      ./modules/hart-gpu-probe.nix
      # Post-boot DISPLAY-HEALTH snapshot: the real-HW observability for the
      # never-black tier ladder. A oneshot ordered AFTER greetd (never before it,
      # so it can never delay first paint) records an honest per-dimension verdict
      # (tier/gpu/painted/input/scanout/screen) to /run/hart/display-health, with
      # `unknown` for the unbuilt #131 scanout + #134 input markers (never a faked
      # positive). Gated on hart.sessionSupervisor.enable so it ships ONLY where
      # the ladder exists (adds nothing to server/edge). FAIL-SAFE: missing markers
      # record their fail-safe value, the unit always succeeds (oneshot, exit 0).
      ./modules/hart-display-health.nix
      # Display management (#158): resolution / per-output scale / font scaling
      # (GDK_DPI_SCALE + fontconfig dpi from hart.display.fontScale, mkDefault so the
      # a11y magnifier wins) + multi-monitor via wlr-randr + a never-fail kanshi USER
      # daemon. Boot-safe, degrade-not-die, no-op on a tier without wlr-output-manager.
      # hart.display.enable defaults true; the shell_desktop_apis.py backend drives it.
      ./modules/hart-display.nix
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
      # Secure DNS (DoH/DoT via systemd-resolved). Opt-in (hart.dns.enable=false
      # default) -> pure no-op for every variant; the desktop config turns it on
      # (privacy-first: encrypted resolution by default). Imported here so the
      # option exists + the desktop closure can enable it without an un-imported-
      # module eval failure.
      ./modules/hart-dns.nix
      ./modules/hart-power.nix
      ./modules/hart-accessibility.nix
      # Boot-time audio rescue: a graphical-session USER oneshot that UNMUTES the
      # default sink + rescues its level to a sane floor (60%) when it reads 0, so
      # the desktop never boots silent because of a persisted mute / volume-0 state
      # (a real-HW "no audio out" the steward hit). Gated on cfg.enable AND
      # services.pipewire.enable -> a PURE no-op on server/edge (no PipeWire) and
      # active on desktop/phone. Also a clean no-op at runtime when no default sink
      # or no wpctl/pactl. Imported so the option exists + tests/audio.nix gates it.
      ./modules/hart-audio.nix
      # Desktop management
      ./modules/hart-cups.nix
      # Thunderbird email client + default mailto handler + GNOME-keyring creds.
      # Opt-in (hart.email.enable=false default) -> pure no-op for every variant;
      # the desktop config turns it on and it OWNS the mailto MIME association
      # (the desktop xdg.mime block no longer sets x-scheme-handler/mailto, so the
      # two can't collide). Imported here so the option exists.
      ./modules/hart-email.nix
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
      # Boot / root-mount / initrd hardening (USB-root enumeration): ENSURES the
      # initrd carries the usb_storage/uas/sd_mod + xhci/ehci host-controller module
      # set a USB root needs to enumerate before the pivot, and ASSERTS the critical
      # subset survived into boot.initrd.availableKernelModules — so a profile/mkForce
      # change that stripped them is a BUILD failure, never a silent real-HW "VFS:
      # Unable to mount root fs" brick. A pure eval/closure guard (adds initrd modules
      # + an assertion, nothing at runtime — it can never block/slow/fail a boot, only
      # fail the build loudly). Opt-in (hart.bootRootInitrd.enable=false default) ->
      # pure no-op for every variant + every minimal test node (which boot a virtio
      # root and must NOT inherit a USB-root assertion); desktop.nix turns it on for
      # the USB-boot ISO. tests/boot-root-initrd.nix is the behavioural proof (boots,
      # confirms root mounted, extracts the built initrd to prove the modules were
      # really PACKED, re-proves the hartlog-create boot-disk-GPT guard).
      ./modules/hart-boot-root-initrd.nix
      # Journal export to an EXTERNAL removable USB stick: a low-level systemd
      # TIMER (+ a shutdown oneshot) that dumps the current-boot journal
      # (journalctl -b, capped ~5 MB, + the last 200 warning lines) to
      # hart-journal-<hostname>.txt on any plugged-in removable FAT/vfat stick that
      # is NOT the live boot medium (the HART_OS ISO disk + the HARTLOG partition +
      # the disks backing / and /nix/store are excluded). It rides journald + the
      # block layer ONLY (NOT any graphical target), so it keeps exporting even
      # when the software-rendered glass shell pegs the CPU and wedges — capturing
      # the PRE-hang state on a stick the user can read on any host. Opt-in
      # (hart.journalExport.enable=false default) -> pure no-op for every variant;
      # ALSO a clean no-op at runtime when no eligible external stick is present.
      # Imported so the option exists + tests/journal-export.nix can enable it; the
      # live/desktop config turns it on (desktop.nix).
      ./modules/hart-journal-export.nix
      # Stateful-across-boots persistence onto the HARTSTATE partition: IF a
      # partition labelled HARTSTATE (carved on the USB by the flasher) is present,
      # a boot oneshot mounts it (by-label, the same lookup hart-boot-log uses) and
      # bind-persists the Wi-Fi credentials (/etc/NetworkManager/system-connections
      # — the "every boot asks for wifi" fix), the HART state (cfg.dataDir: active
      # theme, custom skins, HartSession, the onboarding/identity seal), and
      # /home/hart-admin, so they SURVIVE reboot. The Wi-Fi keyfiles are persisted
      # SECURELY (0700/0600 root:root) and ONLY on a POSIX fs (fail-secure). Ordered
      # BEFORE NetworkManager + the session; nothing REQUIRES it, so a missing/
      # unreadable HARTSTATE is a pure NO-OP that NEVER blocks boot (the OS stays
      # stateless, exactly as today). Opt-in (hart.statePersist.enable=false default)
      # -> pure no-op for every variant; desktop.nix turns it on. Imported so the
      # option exists + tests/state-persist.nix can enable it.
      ./modules/hart-state-persist.nix
      # Cross-OS storage interop (#145): read/write NTFS, exFAT, FAT32/vfat,
      # ext4, and btrfs disks from any operating system, with on-demand udisks
      # auto-mount + the per-filesystem format/repair tooling. It adds only
      # AVAILABLE drivers + an on-demand mount authority (NEVER an fstab/.mount
      # unit), so a missing or corrupt plugged disk can never block or fail boot.
      # Opt-in (hart.storage.enable=false default) -> pure no-op for every
      # variant; desktop.nix turns it on. Imported so the option exists +
      # tests/storage-filesystems.nix can enable it.
      ./modules/hart-storage.nix
      # LAN-path diagnostics + network-up (the steward's "log to the network path
      # journalctl instead of in pendrive, or periodically sync to local network"):
      # netconsole (kernel ring over UDP), a token-gated read-only HTTP diag
      # endpoint (GET /diag?t=TOKEN -> journalctl/dmesg/lspci/lsusb/rfkill/wpctl/ip
      # + the boot-log), and an optional periodic PUSH to a LAN target - so the dev
      # box reads the live-OS box's journal over the shared network instead of
      # yanking a USB stick. Plus network-up: a boot rfkill-unblock + the USB-NIC
      # drivers so a plugged USB-ethernet DHCP-auto-connects (the "debug wifi
      # without wifi" shortcut). READ-ONLY, token-gated + LAN-scoped, OFF unless
      # hart.netDiag.enable -> pure no-op for every variant; desktop.nix turns it
      # on. Imported so the option exists + tests/net-diag.nix can enable it.
      ./modules/hart-net-diag.nix
      # Curated FOSS app registry + offline-first App Store / Appearance (#154). Reads
      # the ONE canonical catalog (modules/hart-app-catalog.json) shared with the Python
      # backend (app_catalog.py). Default import adds ZERO closure (inert HART_APP_CATALOG
      # pointer); the preinstall-bake + wallpaper bundle are opt-in (desktop.nix). Every
      # package is attr-guarded so a renamed nixpkgs attr can never fail eval.
      ./modules/hart-apps.nix
      # Endpoint security (#155): ClamAV (clamd + freshclam; signature updates are the
      # only egress, gated like the fwupd check + OTA pull) + defense-in-depth firewall/
      # kernel hardening that is purely ADDITIVE to hart-firewall (never strips the shell/
      # SSH/netdiag ports; an eval assertion enforces it). OFF unless hart.security.enable.
      ./modules/hart-security.nix
      # Automatic GPU allocation (#156): hybrid PRIME render-offload. Intel iGPU drives the
      # display (+ the shell's software floor, unchanged); the NVIDIA dGPU is armed for
      # heavy-app offload ONLY when a boot probe proves it present (#132-safe; the native
      # force-load arm stays in an opt-in specialisation). Writes its OWN /run/hart/gpu-
      # offload verdict, DECOUPLED from the shell's gpu-render, so it can never flip the
      # WebView shell into the expensive effects tier (no lag regression).
      ./modules/hart-gpu-offload.nix
      # Memory sanity (#157): compressed-RAM zram swap (priority 100, never blocks boot) +
      # graceful systemd-oomd + coordinated swappiness + a boot memory-health snapshot.
      # Pure no-op unless hart.memory.enable. Companion to the hart-storage disk utilities.
      ./modules/hart-memory.nix
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
      # crane (incremental Rust builder) for hart-comp.nix's deps-only cargoArtifacts
      # split, so a compositor/src edit recompiles only the app crate (keeps the
      # iso-desktop Release build well under the GitHub 6h job limit). Only
      # hart-comp.nix consumes it; every other module ignores it (same threading
      # pattern as hartRustNixpkgs above). Passed as the raw flake input; the module
      # calls crane.mkLib with its own instantiated 25.05 (rust_1_88) pkgs.
      hartCrane = crane;
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
        # hartImageKind = "raw": these formats are INSTALLED systems (writable
        # root disk image), not live media. The variant config drops the CD
        # profile + isoImage branding on this signal and adds first-boot root
        # growth. mkSystem deliberately does NOT pass it, so every ISO eval
        # keeps the default "iso" and stays byte-identical (no regression).
        specialArgs = mkSpecialArgs variant // { hartImageKind = "raw"; };
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

        # ─── Nunba native daemon (dedicated CI build target) ───
        # `nix build .#packages.x86_64-linux.nunba` builds the full Nunba (Python +
        # React) closure ONCE so CI can (a) surface the FOD hashes (nunbaHash /
        # npmDepsHash — seeded lib.fakeHash) and (b) walk the import-domino boot loop,
        # WITHOUT the desktop ISO closure pulling this heavy build (hart.nunba.enable
        # stays false until it is green). SAME expression the modules callPackage — one
        # path, no second definition. x86_64-ONLY, exactly like the hart-comp /
        # hart-rust-precedent aliases above: the CI build runs on x86_64, and guarding
        # it here keeps `nix flake check --no-build` from cross-eval'ing the Python
        # closure on riscv64/aarch64 legacyPackages (the desktop is the target).
        nunba = pkgs.callPackage ./packages/nunba.nix { };
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
      # External-USB journal export: a desktop node enables hart.journalExport +
      # attaches a spare disk the test formats vfat (the stand-in for a user's
      # SECOND FAT32 stick). It runs the REAL export script via the documented
      # test seam and asserts hart-journal-<host>.txt lands with the journal
      # sections, AND that a HART_OS/HARTLOG-labelled disk is REFUSED (the never-
      # clobber-the-boot-medium invariant), AND that the no-stick path is a clean
      # no-op. Distinct attr name -> clean //; desktop-variant node (mkNode).
      journalExport = import ./tests/journal-export.nix desktopTestArgs;
      # Stateful-across-boots persistence: a desktop node enables hart.statePersist +
      # NetworkManager + attaches a spare disk. It asserts the unit exists + is
      # ordered Before NetworkManager and nothing REQUIRES it (never boot-blocking),
      # a no-HARTSTATE boot is a clean DECISION=NOOP that still reaches multi-user,
      # persisting onto a real ext4 HARTSTATE stand-in bind-mounts the paths with the
      # SECURE 0700/root:root Wi-Fi perms and lands written data on the partition, and
      # a non-POSIX (vfat) HARTSTATE FAIL-SECURE skips the Wi-Fi bind. The "survives a
      # real reboot off the USB" end still needs real HW. Distinct attr -> clean //.
      statePersist = import ./tests/state-persist.nix desktopTestArgs;
      # Boot / root-mount / initrd (USB-root enumeration): a desktop node enables
      # hart.bootRootInitrd + hart.hartlogCreate, BOOTS, confirms the root actually
      # mounted (findmnt /), then EXTRACTS the built initrd and proves usb_storage /
      # xhci / sd_mod were really PACKED (not just listed) — the link a virtio-root VM
      # otherwise never exercises (the "boots in CI, bricks on the real USB stick"
      # gap). It also re-proves the hartlog-create boot-disk-GPT guard two ways: the
      # boot-time auto-detect run NEVER carved the VM's internal root disk (status =
      # NOOP, root still mounted, partition count unchanged), and the test-seam guard
      # refuses to complete a (stand-in) boot medium's GPT (the duplicate-LABEL root
      # race). Distinct attr name -> clean //; desktop-variant node (mkNode).
      bootRootInitrd = import ./tests/boot-root-initrd.nix desktopTestArgs;
      # Power-action polkit grant (#133): a desktop node asserts the hart-base
      # security.polkit rule actually AUTHORIZES the `hart` service user for the
      # login1 power actions — the half the Python unit tests cannot cover (they
      # mock _logind_call). Uses logind's Can* probes (non-destructive: never
      # reboots) to prove hart gets "yes" (not the #133 "challenge" denial) and
      # that the grant is scoped (a plain sessionless user is NOT authorized).
      # Distinct attr name -> clean //; desktop-variant node (mkNode).
      powerActions = import ./tests/power-actions.nix desktopTestArgs;
      # Suspend/resume agent-state + backend-reconnect (#6, the dormant hart-power
      # module): a desktop node turns `hart.power.enable = true` ON (which only
      # EVALUATES because of the ppd/TLP mutual-exclusion fix) and stands up a mock
      # backend that RECORDS the request path of each POST. It proves BEHAVIOURALLY
      # that the checkpoint hook reaches the REAL /api/shell/power/checkpoint route
      # (not the old /api/power 404), the resume hook reaches /api/shell/power/resume
      # + reconfigures the network, the hook still exits 0 with the backend DOWN
      # (never blocks suspend), and the unit/lid wiring is correct. Distinct attr
      # name -> clean //; desktop-variant node (mkNode).
      powerSuspendResume = import ./tests/power-suspend-resume.nix desktopTestArgs;
      # DISPLAY tier-ladder never-black: the degrade-not-die proof for the display
      # dimension. TWO nodes (distinct attrs -> clean //):
      #  (a) display-tiers-neverblack-paint-ladder — hart-comp (Tier-1) AND sway
      #      (Tier-2) both come up but NEVER first-paint; the paint-watchdog walks
      #      the FULL ladder hart-comp -> sway -> cage ONE rung at a time and never
      #      below the cage floor (the gap session-supervisor.nix's paint test leaves
      #      open by setting compCommand = null, so it only ever drops one rung).
      #  (b) display-tiers-neverblack-gpu-failsafe — a VM with no hardware GL boots,
      #      hart-gpu-probe RUNS + SUCCEEDS + writes the `software` floor verdict to
      #      /run/hart/gpu-render (re-derived per boot), is bounded (oneshot + finite
      #      TimeoutStartSec) so a wedged GPU can't wedge boot, and greetd (the
      #      never-black supervisor it is ordered BEFORE) still comes up.
      # desktop-variant nodes (mkNode); specialArgs carries hartSrc.
      displayTiersNeverBlack = import ./tests/display-tiers-neverblack.nix desktopTestArgs;
      # Cross-OS storage interop (#145): a desktop node enables hart.storage +
      # hart.hartlogCreate + attaches a spare disk. It proves, BEHAVIOURALLY (real
      # mkfs + mount + read/write, not grep): boot.supportedFilesystems covers
      # ntfs/exfat/vfat/ext4/btrfs (each round-trips a file r/w), the udisks2
      # auto-mount authority mounts a removable disk on demand, an UNMOUNTABLE
      # (corrupt) disk fails CLEANLY + FAST and the system stays up (degrade-not-
      # die, never wedges boot), NO boot-blocking external mount exists, AND the
      # HARTLOG persistence guard never completes the boot-disk GPT (the cross-link
      # to the boot dimension — the full proof lives in hartlogCreate 6e). Distinct
      # attr name -> clean //; desktop-variant node (mkNode).
      storageFilesystems = import ./tests/storage-filesystems.nix desktopTestArgs;
      # Boot-time audio rescue (never boot silent): a desktop node enables
      # services.pipewire + hart.audio.bootUnmute, brings up a real PipeWire for a
      # lingering user, loads a null sink, MUTES + zeroes it, runs the REAL rescue
      # script, and asserts it came back UNMUTED + at the 60% floor (the steward's
      # "no audio out" bug). Also proves the degrade contract on the artifact (no
      # sink -> exit 0) + that a deliberate non-zero level is NOT clobbered. The
      # rescue's decision logic is additionally covered by a portable unit test
      # (tests/unit/test_hart_audio_unmute.py). Distinct attr -> clean //; desktop node.
      audio = import ./tests/audio.nix desktopTestArgs;
      # network-wifi degrade-not-die: a desktop node turns NetworkManager +
      # redistributable firmware ON, then drives the REAL _ConnectivityCache wifi
      # probe against the LIVE kernel rfkill subsystem + LIVE nmcli inside the VM
      # (which has no wifi chip). It proves the integration the mocked unit tests
      # cannot: rfkill 'absent' beats nmcli's `radio wifi: enabled` so a chipless
      # box reads available=False (the honest "hardware not detected", never a
      # false-on), a soft-block stays available=True + blocked='soft' (distinct
      # from no-hardware), the rfkill parser reads soft/hard/none/absent/unknown
      # off a REAL on-disk sysfs tree, and the probe never crashes/hangs. Plus the
      # preemptive HW levers: the iwlwifi/ath/brcm/rtw firmware blobs are shipped
      # and the driver modules are in the kernel set (udev auto-loads, never
      # force-loaded). The parser logic is also unit-tested on the dev box
      # (tests/unit/test_wifi_probe_degrade.py). Distinct attr -> clean //; desktop node.
      networkWifi = import ./tests/network-wifi.nix desktopTestArgs;
      # LAN-path diagnostics (the steward's "log to the network path / periodically
      # sync"): a desktop node enables hart.netDiag with a token, BOOTS, and proves
      # the read-only HTTP diag contract BEHAVIOURALLY over a real loopback curl - a
      # valid token returns 200 + the diagnostic sections (journalctl/ip/rfkill/
      # lspci), a wrong token AND a missing token return 403 (fail-closed), the
      # firewall opens the port, the boot rfkill-unblock oneshot ran, and the diag
      # CLI is on PATH + runs read-only. The "the dev box curls the live-OS box
      # across the home LAN" end still needs two physical machines; this proves
      # every link short of the second box. Distinct attr -> clean //; desktop node.
      netDiag = import ./tests/net-diag.nix desktopTestArgs;
      # Input / seat / pointer (#134): a desktop node attaches a USB HID keyboard +
      # a RELATIVE-motion mouse (the #134 cursor device) and proves, BEHAVIOURALLY,
      # that the OS seat the compositor rides EXPOSES + GRANTS input: the session
      # user is in input/seat/video/render, /dev/input evdev nodes are group-`input`
      # and the unprivileged user can OPEN them (no EACCES dead-input, FM4), the
      # `libinput list-devices` real-HW probe enumerates a keyboard + a pointer, a
      # RELATIVE pointer exists (the input the #134 cursor-not-pinned fix applies),
      # a virtual touchscreen is enumerated + classified `touch` (best-effort
      # uinput/evemu), and the seat is a non-blocking EVENT SOURCE so a removed
      # input device never wedges the box (FM5). The compositor's own wl_seat
      # advertisement + the relative-motion clamp math are proven by its Rust unit
      # tests; this proves the OS seat layer beneath. The armed-watchdog touch-only
      # mis-flap guard + the offline real-HW probe are covered in session-supervisor
      # .nix + boot-log.nix. Distinct attr -> clean //; desktop-variant node (mkNode).
      inputSeatPointer = import ./tests/input-seat-pointer.nix desktopTestArgs;
      # Endpoint security (#155): a desktop node enables hart.security, BOOTS, and proves
      # the clamd + freshclam units generate, the hardening sysctls took effect, and the
      # shell (6777) / SSH (22) / netdiag (6699) ports SURVIVE the hardening. Distinct attr.
      security = import ./tests/security.nix desktopTestArgs;
      # Automatic GPU offload (#156): the boot probe's armed/intel/software decision + the
      # prime-offload wrapper env-apply/passthrough, degrade-not-die. Distinct attr.
      gpuOffload = import ./tests/gpu-offload.nix desktopTestArgs;
      # Memory sanity (#157): zram active (priority 100), swappiness coordinated to 100,
      # systemd-oomd active, the boot memory-health snapshot ok=1/zram_present=1. Distinct.
      memory = import ./tests/memory.nix desktopTestArgs;
      # Display management (#158): wlr-randr + kanshi ship, the font lever materialises from
      # hart.display.fontScale, kanshi is a never-fail USER unit, the seed is safe/idempotent.
      displayManagement = import ./tests/display-management.nix desktopTestArgs;
      # Robot Model-Bus capability probe (embodied): a desktop node enables
      # hart.modelBus + hart.robotics.probe, BOOTS, and asserts the post-boot probe
      # oneshot RAN + SUCCEEDED (never-fail measurement), wrote HONEST per-capability
      # verdicts to /run/hart/robot-capability-status (one key=value per line, each an
      # honest value from the documented vocabulary), degrades honestly with a dead
      # backend, and reads model_bus=ok once the bus port is up (best-effort positive,
      # proving a REAL bus not a stub). Distinct attr -> clean //; desktop-variant node.
      robotProbe = import ./tests/robot-probe.nix desktopTestArgs;
      # Native notification daemon (mako) + privacy gate (#113): a desktop node proves
      # mako + makoctl + BOTH clients (notify-send, the AI's hart-notify-send) are on
      # PATH, the daemon is a graphical-session USER service (never boot-critical) with
      # the pinned glass config, and hart-notify-send fail-CLOSES (exit 77) when the
      # 'screen' kill-switch is cut OR the authority is down, and ALLOWS when on.
      # Distinct attr -> clean //; desktop-variant node (mkNode).
      notify = import ./tests/notify.nix desktopTestArgs;
    in vmTests // floorLock // supervisor // desktopShellBoot // layerShellHost // portalScreencast // otaCentral // nativeSubsystems // bootLog // hartlogCreate // bootContinuity // journalExport // statePersist // bootRootInitrd // powerActions // powerSuspendResume // displayTiersNeverBlack // storageFilesystems // audio // networkWifi // netDiag // inputSeatPointer // security // gpuOffload // memory // displayManagement // robotProbe // notify;

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
