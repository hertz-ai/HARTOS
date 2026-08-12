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
      ./modules/hart-installer.nix
      ./modules/hart-dev-tools.nix
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
      # Claude Code as the resident co-pilot IN the node's terminal (hart.copilot,
      # default OFF so a normal build carries none of its closure). Full autonomy
      # inside the work; commits land on a BRANCH — merge / OTA / master-key signing
      # stay human. Pulls claude-code from the 25.05 input already threaded for Rust.
      ./modules/hart-copilot.nix
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
      # Thermal twin of display-health: reports kernel-forced idle (thermal
      # throttling) so a thermal stall is never mistaken for a software hang.
      ./modules/hart-thermal-health.nix
      # Leak attribution for a desktop that degrades OVER TIME (the 2026-07-20
      # drag hang). Read-only /proc sampling on a timer; opt-in, default OFF.
      ./modules/hart-shell-memwatch.nix
      # Local-2B agent baseline capture on the node (potato-machine profile).
      ./modules/hart-agent-baseline.nix
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
      # ── Written, never wired (found 2026-07-30 by the everything-on sweep) ──
      # These three module FILES have been in the tree with full option sets
      # and config, but were never added to this list — so `hart.openclaw`,
      # `hart.scanner` and `hart.sso` did not EXIST as options and nothing
      # could ever turn them on. A file on disk is not a loaded module; the
      # eval gate proved it the moment the desktop profile set them
      # (`error: The option 'hart.openclaw' does not exist`, run 30567029164).
      # Importing them here only makes the options exist — each stays gated
      # on its own enable, so this alone changes no system.
      ./modules/hart-openclaw.nix
      ./modules/hart-scanner.nix
      ./modules/hart-sso.nix
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
      # The flake INPUT SOURCES, for the installer's offline story
      # (hart-installer.nix). An installed system's /etc/nixos/flake.nix pins the
      # HART flake by path; evaluating it needs the hart flake's OWN locked
      # inputs. Their lock entries carry narHashes, and nix resolves a
      # narHash-pinned input from the LOCAL STORE when the source path is already
      # present — so the installer module references these outPaths to bake the
      # sources into the ISO closure, and `nixos-install` then evaluates with the
      # network cable unplugged (offline-first is a product principle, and a
      # robot in a field has no github). Only hart-installer.nix consumes this;
      # every other module ignores it.
      hartFlakeInputs = {
        inherit nixpkgs nixpkgs-rust crane llama-cpp
                nixos-generators nixos-hardware mobile-nixos;
      };
    };

    # ── THE one recipe for composing a HART system ─────────────────────────────
    # Every way of building a HART system funnels through here: the ISO images
    # (mkSystem), the repart raw image (mkRepartSystem), and — exported as
    # lib.mkHartSystem — the installer's installed systems and any third-party
    # flake putting HART on its own hardware. The parts list (hartModules) was
    # already exported, but a parts list without the recipe produced stock NixOS
    # or an eval error: the modules need mkSpecialArgs (hartSrc, hartVariant,
    # llama-cpp, hartRustNixpkgs, crane) and the nixpkgs config, and the FEATURES
    # live in profiles/<variant>.nix — none of which a consumer can be expected
    # to reassemble by hand. One recipe, so the union (NixOS hardware layer +
    # HART OS layer) is composed the same way everywhere and cannot drift.
    #
    # hartImageKind MUST be a specialArg (not left to the desktop.nix
    # destructuring default): a module argument absent from specialArgs is
    # resolved through the config fixpoint (_module.args), and desktop.nix
    # uses it in `imports` -- which shape that same fixpoint -> "infinite
    # recursion encountered" (run 29508017463). specialArgs are
    # fixpoint-free, so imports may branch on them.
    #
    #   imageKind: "iso" (live media) | "raw" (whole-disk image) | "installed"
    #   (a disk the user owns: fileSystems come from a generated
    #   hardware-configuration.nix passed in `modules`, never from an image
    #   module — the hardware-agnostic installer case, task #17).
    mkHartSystem = { system, variant, imageKind ? "installed", modules ? [] }:
      nixpkgs.lib.nixosSystem {
        inherit system;
        specialArgs = mkSpecialArgs variant // { hartImageKind = imageKind; };
        modules = hartModules ++ [
          { nixpkgs.config = nixpkgsConfig; }  # single source — #70
        ] ++ modules;
      };

    # Build a full NixOS system configuration (live-ISO shape)
    mkSystem = { system, variant, extraModules ? [] }:
      mkHartSystem {
        inherit system variant;
        imageKind = "iso";
        modules = [ ./configurations/${variant}.nix ] ++ extraModules;
      };

    # ── The INSTALLED-system composition: profile + hart.package + caller's
    # hardware modules. THE one generator for a system that lands on a disk the
    # user owns: hart-desktop-installed (the eval-gated fixture), the flake that
    # `hart-install` writes to /mnt/etc/nixos, and (later) the Calamares config
    # module all call THIS — one writer for "what an installed HART system is",
    # so the CLI and the GUI can never produce different systems (the plan's
    # step-5 invariant). hardwareModules is nixos-generate-config's output plus
    # whatever the machine needs (bootloader choice included: the installer
    # writes systemd-boot + canTouchEfiVariables=true on EFI, grub+osProber on
    # BIOS — see hart-installer.nix).
    mkInstalledSystem = { system, variant, hardwareModules ? [], extraModules ? [] }:
      mkHartSystem {
        inherit system variant;
        imageKind = "installed";
        modules = [
          ./profiles/${variant}.nix
          ({ pkgs, hartSrc, ... }: {
            hart.package = pkgs.callPackage ./packages/hart-app.nix { inherit hartSrc; };
          })
          {
            # Offline-rebuild promise (review C:C4): the written flake pins hart
            # by path, and hart's OWN lock resolves its inputs by narHash from
            # the LOCAL store — but only if the sources are actually retained
            # there. The ISO carries them via the installer module; the
            # INSTALLED system must carry them too, or its first offline
            # `nixos-rebuild` reaches for github (a robot in a field has none).
            # extraDependencies pins them into the system closure, GC-proof.
            system.extraDependencies = [
              nixpkgs.outPath nixpkgs-rust.outPath crane.outPath
              llama-cpp.outPath nixos-generators.outPath
              nixos-hardware.outPath mobile-nixos.outPath
            ];
          }
        ] ++ hardwareModules ++ extraModules;
      };

    # Build a bootable UEFI raw disk image WITHOUT qemu, via systemd-repart.
    # Assembles ESP + root OFFLINE in the Nix sandbox (fakeroot systemd-repart in
    # stdenvNoCC) -> config.system.build.image. This is the no-VM replacement for
    # nixos-generators' raw-efi (make-disk-image), whose qemu leg ran ~5h and blew
    # the CI 300-min cap. Keeps hartImageKind = "raw" so the variant config drops
    # the CD profile + adds first-boot growth exactly as the generators path did;
    # the extra module (hart-repart-image) owns the fileSystems + bootloader the
    # generators format used to provide.
    # The SYSTEM and the IMAGE are split so the raw closure is addressable on its
    # own. The image build is closure-bound and, because systemd-repart gets no loop
    # device in the Nix sandbox, it needs roughly TWICE the closure free on disk (it
    # mkfs's into a temp file, then copies that file into the .raw). That makes the
    # closure size the build's disk budget — and with only the image exposed there
    # was no way to ask for the number without building the whole 40 GB artifact.
    # Note this is NOT the same closure as nixosConfigurations.hart-desktop: that one
    # carries hartImageKind = "iso", which changes what desktop.nix imports.
    mkRepartSystem = { system, variant, extraModules ? [] }:
      mkHartSystem {
        inherit system variant;
        imageKind = "raw";
        modules = [
          ./configurations/${variant}.nix
          ./modules/hart-repart-image.nix      # repart + systemd-boot/UKI + fs + growth
        ] ++ extraModules;
      };

    mkRepartImage = args: (mkRepartSystem args).config.system.build.image;

    # Build an image: systemd-repart owns EVERY raw-efi variant; nixos-generators
    # owns every OTHER format (qcow/vmware/vbox/docker/cloud/sd). One builder per
    # format — this is no longer a parallel path.
    #
    # ── THE TEMPORARY PARALLEL PATH IS CLOSED (Gate 4 exit met, 2026-08-08) ────
    # The 2026-07-27 note here demanded two conditions before deleting the
    # per-variant special case, and both now hold:
    #   1. repart raw-desktop BUILT AND BOOTED: built by Nix Build Matrix run
    #      30336832563 (2026-07-28, commit a870e1d9), flashed 2026-07-30 with a
    #      full sha256 read-back verify (D:\hart_flash_tmp\hart_flash.log:
    #      "FULL VERIFY: OK"), booting on the steward's device since.
    #   2. server.nix + edge.nix now wrap their CD-profile import + isoImage
    #      block (and, for server, the whole live-medium access story — the
    #      permissive SSH / baked passwords / ssh-diag that "must NEVER reach
    #      an installed system") in `lib.optionals (hartImageKind == "iso")`,
    #      mirroring desktop.nix, so repart can own every raw-* variant.
    #
    # The `variant == "desktop"` clause is therefore gone: raw-server and
    # raw-edge take the same proven no-VM repart path (no KVM, no sandbox-off,
    # no 5h make-disk-image qemu leg). The generators branch below is NOT the
    # old parallel path surviving under a new name — it builds formats repart
    # does not implement (qcow/vmware/vbox/docker/amazon/sd), one artifact
    # kind per builder. Those installed-disk formats also stop inheriting the
    # CD profile + live passwords now that the variant guards exist: the
    # hartImageKind = "raw" signal they always carried finally does what its
    # comment says.
    mkImage = { system, variant, format, extraModules ? [] }:
      if format == "raw-efi"
      then mkRepartImage { inherit system variant extraModules; }
      else nixos-generators.nixosGenerate {
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
    # The HART module set, as a composable flake output
    # ═════════════════════════════════════════════════════════════
    #
    # `hartModules` was a let-binding, reachable only from inside this file. Every
    # image the flake builds is "hartModules + a variant config", so a system built
    # ANYWHERE ELSE -- most importantly one installed onto a user's disk by an
    # installer -- had no way to be HART. It could only be stock NixOS.
    #
    # That is the whole point (steward, 2026-07-28): "nothing shd be shipped nix
    # only, there is no point of that without our os customisations ... union of
    # features". An installer is allowed to borrow NixOS's hardware layer -- the
    # `nixos-generate-config` hardware-configuration.nix that makes it work on any
    # substrate -- but the system it installs must carry THESE modules on top. The
    # union, not one instead of the other.
    #
    # Exposed under both conventional names: `nixosModules.hart` is what a flake
    # consumer expects to import, `lib.hartModules` is the raw list for code that
    # needs to splice it (the installer writes it into the target's configuration).
    #
    # nixosModules.hart alone is the MACHINERY, not the OS: importing it yields a
    # system with every hart.* feature at its default (mostly off) and hart.package
    # unset — the exact defect that kept 25 nixosTests red (#15). What makes a
    # desktop a desktop lives in the variant PROFILES, exported alongside so no
    # consumer has to reach into this repo's directory layout for them.
    # NOTE (since parity slice 4): profile-desktop takes the `hartSrc`
    # specialArg (its app set builds hart-cli from source). mkHartSystem /
    # mkInstalledSystem wire it automatically; only a RAW import of
    # profile-desktop into a foreign nixosSystem must add
    # `specialArgs.hartSrc = hart.outPath;` (or compose via lib.mkHartSystem,
    # the documented path).
    nixosModules.hart = { imports = hartModules; };
    nixosModules.profile-desktop = ./profiles/desktop.nix;
    nixosModules.profile-server  = ./profiles/server.nix;
    nixosModules.profile-edge    = ./profiles/edge.nix;
    nixosModules.profile-phone   = ./profiles/phone.nix;
    lib.hartModules = hartModules;

    # THE recipe (see its definition above): machinery + specialArgs + nixpkgs
    # config, with the caller supplying profile/hardware/config modules. This is
    # how an installed system or a third-party flake composes the union — e.g.
    #   hart.lib.mkHartSystem {
    #     system = "x86_64-linux"; variant = "desktop";   # imageKind defaults
    #     modules = [ hart.nixosModules.profile-desktop   # to "installed"
    #                 ./hardware-configuration.nix        # nixos-generate-config
    #                 { hart.package = ...; } ];
    #   }
    # The flake's own images build through the same function (mkSystem /
    # mkRepartSystem above delegate to it), so it can never drift from what ships.
    lib.mkHartSystem = mkHartSystem;

    # The installed-system composition (profile + hart.package + your hardware).
    # This is what the flake WRITTEN BY hart-install calls:
    #   hart.lib.mkInstalledSystem {
    #     system = "x86_64-linux"; variant = "desktop";
    #     hardwareModules = [ ./hardware-configuration.nix ./boot.nix ];
    #   }
    # hart-desktop-installed above is the eval-gated fixture of exactly this.
    lib.mkInstalledSystem = mkInstalledSystem;

    # ═════════════════════════════════════════════════════════════
    # NixOS Configurations (nixos-rebuild build --flake .#name)
    # ═════════════════════════════════════════════════════════════
    nixosConfigurations = {
      # ─── x86_64 (PC / Laptop / Server) ───
      hart-server  = mkSystem { system = "x86_64-linux"; variant = "server"; };
      hart-desktop = mkSystem { system = "x86_64-linux"; variant = "desktop"; };
      # GPU-render diagnostic build (task #12): desktop with hart.liquidUI.gpuDiagnostic
      # forced ON -> Tier-1 forces the vulkan rung + logs the VK swapchain failure to
      # the journal, so a real-HW boot CAPTURES the layer-shell hang. Normal iso-desktop
      # is untouched. Build/flash `.#iso-desktop-gpudiag`, boot, hover the orb, pull HARTJRNL.
      hart-desktop-gpudiag = mkSystem {
        system = "x86_64-linux"; variant = "desktop";
        extraModules = [ { hart.liquidUI.gpuDiagnostic = true; } ];
      };
      hart-edge    = mkSystem { system = "x86_64-linux"; variant = "edge"; };

      # The INSTALLED desktop (hartImageKind = "raw"), i.e. exactly the system that
      # `.#raw-desktop` writes into the image — a different closure from hart-desktop
      # above, which is the live-ISO one. Exposed so the raw closure can be built and
      # measured on its own (`…hart-desktop-raw.config.system.build.toplevel`) without
      # producing the 40 GB image; CI reports its size before every image build, since
      # that size IS the build's disk budget. Same nixosSystem the image is made from,
      # via mkRepartSystem, so the two can never drift apart.
      hart-desktop-raw = mkRepartSystem { system = "x86_64-linux"; variant = "desktop"; };

      # The INSTALLED desktop (hartImageKind = "installed"): the composition the
      # hardware-agnostic installer writes to a disk the user owns (#17) — variant
      # profile + a hardware-configuration.nix, never a whole-disk image module.
      # The hardware here is a STUB standing in for nixos-generate-config output
      # (UUID-addressed root + ESP), because the real one only exists on the
      # target machine at install time.
      #
      # This exists FIRST as regression coverage: mkHartSystem's "installed"
      # branch had no consumer, so nothing evaluated it — the eval gate now
      # exercises the exact composition the installer will emit, on every push,
      # before the installer itself exists. It is also the template: hart-install
      # generates precisely this, with the stub replaced by the generated file.
      #
      # canTouchEfiVariables = true is the DELIBERATE inversion of the raw image's
      # false: the portable image must not write NVRAM and boots via the
      # removable-media path, while an installed dual-boot system must register
      # its own NVRAM entry BESIDE Windows Boot Manager — overwriting
      # EFI/BOOT/BOOTX64.EFI (Windows' fallback loader) is exactly what it must
      # never do.
      hart-desktop-installed = mkInstalledSystem {
        system = "x86_64-linux";
        variant = "desktop";
        hardwareModules = [{
          # stand-in for nixos-generate-config's hardware-configuration.nix
          fileSystems."/" = {
            device = "/dev/disk/by-uuid/00000000-0000-0000-0000-000000000000";
            fsType = "ext4";
          };
          fileSystems."/boot" = {
            device = "/dev/disk/by-uuid/0000-0000";
            fsType = "vfat";
          };
          boot.loader.systemd-boot.enable = true;
          boot.loader.efi.canTouchEfiVariables = true;
        }];
      };

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
        # GPU-render diagnostic ISO (task #12): forces the vulkan rung + captures the
        # layer-shell VK hang. Not in the nightly matrix; build on demand.
        iso-desktop-gpudiag = self.nixosConfigurations.hart-desktop-gpudiag.config.system.build.isoImage;
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
      # FIRMWARE BOOT MATRIX (#28): boots the desktop variant on BOTH firmware
      # paths — OVMF/UEFI (Hyper-V Gen 2 shape) and legacy SeaBIOS (Gen 1) —
      # and asserts each node is REALLY on the path its config claims. The
      # parity matrix's 'Hyper-V Gen 1 boots' row was a CONFIG assertion until
      # now; nothing had ever booted the legacy-BIOS path.
      firmwareBootMatrix = import ./tests/firmware-boot-matrix.nix desktopTestArgs;
      # BOOT LATENCY (#29): enforces core.constants.LATENCY_BUDGETS on a REAL
      # booted node. Every other budget is enforced only by a python suite,
      # which is how userspace startup reached 6min36s with nothing failing.
      # The budgets are PARSED from core/constants.py at build time, never
      # re-typed, so this cannot drift from the python suites.
      bootLatency = import ./tests/boot-latency.nix desktopTestArgs;
      # DRIVER MATRIX (#27): attaches a device from each class QEMU can present
      # without a backing file (xhci, USB HID, intel-hda, e1000, virtio blk +
      # balloon) and asserts the kernel actually BOUND a driver, read from
      # sysfs. lsmod would only prove a module loaded — the weaker claim that
      # lets an UNCLAIMED device pass. One node, many devices: a VM job costs
      # ~2h, so per-device nodes would buy the same coverage for 6x the clock.
      driverMatrix = import ./tests/driver-matrix.nix desktopTestArgs;
      # The STORAGE-CONTROLLER slice driver-matrix.nix's header defers: attach a
      # real NVMe controller + an ICH9 AHCI (SATA) controller (null-co disks, no
      # backing file) and assert the kernel BINDS nvme / ahci — the INSTALLED raw
      # image's primary boot media (internal M.2 / SATA SSD), which virtio-root VM
      # boots never exercise. The source-shape half (those modules pinned in the
      # repart initrd) is guarded by test_nixos_configs.py::TestRawImageSinglePath.
      # Distinct attr name -> clean //; desktop-variant node (mkNode).
      driverMatrixStorage = import ./tests/driver-matrix-storage.nix desktopTestArgs;
      # RESIDENT CO-PILOT: the 2026-07-30 flash shipped hart.copilot.enable=true
      # and the co-pilot did NOTHING — `enable` installs only the launcher, the
      # bounded worker is a SECOND opt-in (copilot.daemon.enable) nobody had set.
      # A comment in profiles/desktop.nix now records that, but a comment is not a
      # gate. There is a quieter twin: the module gates the daemon on
      # `claudePkg != null` where claudePkg = newPkgs.claude-code or null, so an
      # upstream attr move DELETES the unit silently with the build still green.
      # This node is built from the REAL desktop profile and sets no hart.copilot.*
      # of its own, so it asserts what an IMAGE ships, not what a test opts into.
      # Honest scope: no OAuth exists in a VM (§5 ships no key), so it asserts the
      # unit is LOADED + BOUNDED, never ACTIVE. Distinct attr -> clean //.
      copilotResident = import ./tests/copilot-resident.nix desktopTestArgs;

      # ORPHANS ADOPTED 2026-08-04. Both files existed, both defined a real
      # check (hart-app-install-verify, hart-llm-provision), and NEITHER was
      # imported here — so neither was ever enumerated by
      # `nix eval .#checks.x86_64-linux --apply builtins.attrNames`, and
      # neither had ever run. Found while verifying that the num2words
      # assertion added to hart-app-install-verify.nix actually executes: it
      # did not, and could not.
      #
      # This is the "31 of 53 never built" failure at its REAL layer. That one
      # was about a workflow's hand-written list and is gone; this one is about
      # the flake's own import list, which dynamic enumeration cannot rescue —
      # enumeration walks `checks`, so a test that never reaches `checks` is
      # invisible to it. Guarded now by
      # tests/unit/test_nixos_configs.py::TestNoOrphanedNixosTests.
      appInstallVerify = import ./tests/hart-app-install-verify.nix desktopTestArgs;
      llmProvision     = import ./tests/llm-provision.nix desktopTestArgs;
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
      # hart-install dual-boot survival (plan step 6): a fake-Windows ESP disk
      # gets HART composed beside it via the REAL hart-install (--no-install
      # seam: everything except the closure build, which is eval-gated upstream
      # by hart-desktop-installed). Asserts the Windows boot files survive
      # byte-identical + the target composes mkInstalledSystem (the union),
      # never stock NixOS. Distinct attr -> clean //; desktop-variant node.
      hartInstaller = import ./tests/hart-installer.nix desktopTestArgs;
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
    in vmTests // floorLock // supervisor // desktopShellBoot // layerShellHost // portalScreencast // otaCentral // nativeSubsystems // bootLog // hartlogCreate // bootContinuity // firmwareBootMatrix // bootLatency // driverMatrix // driverMatrixStorage // journalExport // statePersist // bootRootInitrd // powerActions // powerSuspendResume // displayTiersNeverBlack // storageFilesystems // audio // networkWifi // netDiag // inputSeatPointer // security // gpuOffload // memory // displayManagement // robotProbe // notify // hartInstaller // appInstallVerify // llmProvision // copilotResident;

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
