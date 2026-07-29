# ═══════════════════════════════════════════════════════════════
# HART OS DESKTOP — the variant FEATURE PROFILE (canonical home)
# ═══════════════════════════════════════════════════════════════
#
# This is "what a desktop IS": the hart.* feature block, MOVED VERBATIM out of
# configurations/desktop.nix (2026-07-28). It is a pure option-set — deliberately no
# `config`/`lib`/`pkgs` captures (verified 0 non-comment scope refs at move time),
# no media/image concerns (ISO branding, repart sizing, growPartition stay in the
# configuration), no hardware assumptions.
#
# WHY A SEPARATE FILE: three consumers need exactly this block and nothing else,
# and until now it lived entangled with image concerns so only the images got it:
#   - configurations/desktop.nix        (the iso/raw images — imports this)
#   - tests/lib.nix mkNode          (the nixosTest VMs; their nodes ran with every
#                                    hart.* feature at default-false, which is why
#                                    25 nixosTests were red — task #15)
#   - the hardware-agnostic installer (task #17: an installed system must be the
#                                    UNION of nixos-generate-config hardware +
#                                    THIS profile — never stock NixOS)
# One canonical home, consumed everywhere; per the steward's rule the union of
# NixOS's hardware layer and HART's OS layer, and never two drifting copies.
#
# hart.package is NOT here on purpose: it captures pkgs+hartSrc, so each consumer
# wires it (configurations use packages/hart-app.nix; mkNode builds its own).

{ ... }:
{
  # ─── HART OS Core Services ───
  hart = {
    enable = true;
    variant = "desktop";

    # AI services
    agent.enable = true;
    llm.enable = true;
    vision.enable = true;

    # Desktop UI
    conky.enable = true;

    # Preinstalled developer toolchain (modules/hart-dev-tools.nix — one writer;
    # see its header for the four consumers and the closure-cost note for #14).
    devTools.enable = true;
    # NATIVE NUNBA DAEMON — the single flip that wires the FULL Nunba (Python +
    # React) into HART OS: `nunba.enable = true` starts hart-nunba.service (binds
    # unix:/run/hart/nunba.sock, no host port) AND auto-enables liquidUI.embedNunba
    # (its default == nunba.enable), so LiquidUI reverse-proxies the daemon same-
    # origin with the SAME React store-path as the graceful static floor.
    #
    # STAYS OFF until CI is green — flipping it before that would fail the desktop
    # ISO. Two CI prerequisites (nixos/packages/nunba.nix):
    #   1. Pin the FOD hashes: nunbaRev (current Nunba HEAD, has HART_NUNBA_SOCKET) +
    #      nunbaHash (nix-prefetch-github hertz-ai Nunba --rev <rev>) + npmDepsHash
    #      (prefetch-npm-deps landing-page/package-lock.json) — all in ONE commit.
    #   2. `nix build .#packages.x86_64-linux.nunba` green — walk the import-domino
    #      boot loop (add curated nixpkgs pkgs / guard Nunba ML imports until main.py
    #      binds the socket), per hart-app.nix's method.
    # Then set this to true (embedNunba follows automatically). Until then the
    # React-static floor path is byte-for-byte the current behaviour.
    nunba.enable = false;

    # ── Unified Kernel Extensions ──
    kernel = {
      enable = true;
      androidNative.enable = true;     # binder + ashmem kernel modules
      windowsNative.enable = true;     # PE binfmt + NTFS + high mmap
      aiCompute = {
        enable = true;                 # GPU scheduling + huge pages
        hugePagesCount = 0;            # Auto (THP); set to 4096 for 8GB dedicated
      };
      agentSandbox.enable = true;      # cgroups v2 + Landlock LSM
    };

    # ── Native Subsystems (no emulation) ──
    subsystems = {
      enable = true;

      # Linux: native + distribution methods
      linux = {
        flatpak = true;                # Flathub app store
        appimage = true;               # Portable apps
      };

      # Android: native ART runtime (not a container)
      android = {
        enable = true;
        playStore = false;             # AOSP + F-Droid; set true for Google Play
      };

      # Windows: native Wine API (not an emulator)
      windows = {
        enable = true;
        gaming = true;                 # Steam + Proton + DXVK
      };

      # Web: PWA as native windows
      web.enable = true;
    };

    # ── AI Runtime ──
    aiRuntime = {
      enable = true;
      gpu.enable = true;
      worldModel.enable = true;
      agents = {
        maxConcurrent = 8;
        maxMemoryPerAgent = "2G";
      };
      # Full semantic intelligence on desktop
      semantic = {
        enable = true;
        serviceIntelligence = true;
        smartFS = true;                # AI-indexed filesystem for desktop users
        predictivePrefetch = true;
      };
    };

    # ── AI-Native Everything OS ──
    # Model Bus: every app (Linux, Android, Windows) gets native AI
    modelBus = {
      enable = true;
      enableAndroidBridge = true;
      enableWineBridge = true;
    };

    # Compute Mesh: aggregate compute across user's devices
    computeMesh = {
      enable = true;
      allowWAN = true;
    };

    # LiquidUI: AI-generated adaptive interface
    liquidUI = {
      enable = true;
      voiceEnabled = true;
      renderer = "webkit";
    };

    # ── Supervisor-managed compositor TIER LADDER (the never-blank boot) ──
    # The out-of-process session tier-drop supervisor (greetd) OWNS the boot
    # session: it starts at the BEST tier and falls back on failure —
    #   Tier-1 hart-comp (Smithay/Rust, --backend drm)
    #     → Tier-2 sway (the hart-glass-gtk4 layer-shell session)
    #       → Tier-3 cage (hart-shell, the audited never-fail paint floor).
    # A crash OR a shell-paint timeout drops + LATCHES one tier down; cage is the
    # floor the supervisor can never drop below. This REPLACES the crude fixed
    # cage-pin (68ce3c3 `defaultSession = "hart-shell"`) with the real tiered
    # design — see the session block lower in this file.
    sessionSupervisor = {
      enable = true;
      # Fresh/un-latched boots start at Tier-1 (hart-comp). The supervisor owns
      # the never-blank guarantee, so an unavailable/crashing/hung Tier-1 falls
      # RE-ARMED to hart-comp: both deferral blockers are fixed —
      #   (1) the GTK4 glass-host paint hang — GSK's GL renderer on a real GPU +
      #       an undefined _on_load_changed that never fired the shell-ready marker
      #       — fixed in 75ba78d (GSK_RENDERER=cairo + the marker handler), and
      #   (2) the iso-desktop build hang — the Release build-iso cores=2 throttle on
      #       the from-source Rust compile — fixed in 48b73d6 (warm the Rust closure
      #       at full cores before the throttled ISO step).
      # The boot now tries Tier-1 first; the shell-paint watchdog still drops to
      # sway then the cage floor if Tier-1 fails on real HW (safe to re-arm).
      startTier = "hart-comp";
      # The glass-shell host blocks on the :6800 LiquidUI server's /health for up to
      # 30s before it can paint its first frame. The default 20s watchdog therefore
      # killed a tier that was legitimately WAITING for the backend (real-HW boot
      # 2026-06-24: Tier-1/2 dropped to cage mid-wait). 45s > the host's 30s wait +
      # load + paint, so a backend that comes up within 30s is NOT killed; a truly
      # hung tier still drops (just 25s later). Paired with the :6800-starts-fast fix
      # (hart-liquid-ui no longer orders after the model bus), this should rarely bind.
      shellPaintTimeoutSeconds = 45;
    };

    # Tier-1: HART-comp, the AI-native Smithay/Rust compositor (--backend drm).
    # Enabling it puts the hart-comp package + the `hart-comp-session` launcher in
    # the desktop closure and arms the supervisor's Tier-1 rung (compCommand via
    # mkDefault in hart-comp.nix). hart-comp reuses the SAME GTK4 layer-shell glass
    # host as Tier-2 sway, so it satisfies the same shell-paint watchdog marker.
    # RE-ARMED (both deferral preconditions met): (a) the GTK4 glass-host paint
    # hang is fixed (75ba78d — GSK cairo renderer + the shell-ready marker handler),
    # and (b) the iso-desktop build no longer hangs (48b73d6 — the Release build-iso
    # job warms the hart.comp Rust closure at full cores BEFORE the throttled ISO
    # step, so it is reused, not recompiled under the cores=2 cap). The shell-paint
    # watchdog still falls back to sway then cage if Tier-1 fails on real HW.
    # Leak attribution: sample shell/compositor RSS + FDs into the journal every
    # 20s. The steward's desktop went 'fast snappy' then hung after a sustained
    # orb drag; the JS/DOM layer was measured clean on the dev box, so the
    # accumulation is below JS (WebKit compositing / GPU memory or hart-comp
    # buffers) and only the node can see it. Cheap + read-only.
    shellMemWatch.enable = true;

    # Capture the local-2B agent baseline on this hardware (hourly, off the
    # boot path). Modelless boot is a clean no-op; a model present records the
    # baseline JSON + a journal PASS/FAIL line.
    agentBaseline.enable = true;

    comp.enable = true;
    rustPrecedent.enable = true;

    # Tier-2: sway running the canonical glass shell + the swaymsg WM shim the
    # brain drives when HART-comp is absent. Registers the sway session + the
    # supervisor's Tier-2 rung. The supervisor's swayCommand is repointed to the
    # GTK4 layer-shell host session (the `hart-glass-gtk4` session) lower in this
    # file so Tier-2 is a TRUE layer-shell desktop, not bare sway.
    swayTier1.enable = true;

    # App Bridge: Android ↔ Linux ↔ Windows cross-subsystem routing
    appBridge = {
      enable = true;
      clipboardSync = true;
      dragAndDrop = true;
      intentRouter = true;
    };

    # ── Subsystem Sandbox ──
    sandbox.enable = true;             # `hart sandbox test-all`

    # ── Self-Building OS ──
    selfBuild = {
      enable = true;                   # OS can rebuild itself at runtime
      autoRebuild = false;             # Require explicit `hart-ota self-build`
      allowAgentBuilds = false;        # Agents propose, humans approve
      maxBuildsPerDay = 10;
    };

    # ── OTA Updates ──
    # Autonomous central-controlled OTA: the node polls CENTRAL on boot (and on
    # `hart-ota check`) and receives CENTRAL pushes at any time over the existing
    # fleet/gossip fabric — NO periodic interval poll. autoApply=true makes the
    # apply hands-off (the `completed` branch switches via `nixos-rebuild switch
    # --flake` with `|| nixos-rebuild switch --rollback`), so a steward publish
    # lands on every node with no per-node USB/flash. The master-key SIGN gate +
    # canary + auto-rollback still run before DEPLOY — central only chooses WHICH
    # commit; the node never force-applies past canary, and the master key is
    # never touched on the node. This replaces the user's last manual flash.
    ota = {
      enable = true;
      channel = "stable";
      autoApply = true;                # hands-off: central publish → auto-apply
    };

    # ── Persistent boot-diagnostic log partition ──
    # The live ISO's journal lives in tmpfs (RAM) — wiped on reboot, never on the
    # stick, unreadable from the Windows host. With this ON, IF a FAT32 partition
    # labelled HARTLOG is present (the flasher creates it in the stick's free
    # space after a successful flash), HART OS writes the full current-boot
    # journal + the session-supervisor tier latch/decisions + the shell-ready
    # paint marker + the GTK4/GSK/GDK/EGL/GBM/WebKit GL diagnostics to
    # /hart-boot-latest.log on it — EARLY in boot, on a ~20s periodic timer (so a
    # HUNG Tier-1 pointer-only boot STILL leaves the journal-so-far), and at
    # shutdown, fsync'ing each write. So the loop becomes: flash → boot (even if
    # Tier-1 hangs) → plug the stick into Windows → read the journal. A pure
    # NO-OP when no HARTLOG partition is present, so an old stick still boots
    # fine and the capture never blocks/slows/fails boot.
    bootLog.enable = true;

    # ── Live-OS self-creation of the HARTLOG partition ──
    # The HARTLOG partition (read by bootLog above) is now created BY THE LIVE OS
    # on first USB boot, NOT by the Windows flasher. The flasher's diskpart path
    # was doubly broken — it HUNG on a wedged Windows VDS, and a half-completed
    # `diskpart create partition` CORRUPTED a freshly-flashed stick's EFI/GPT
    # (boot failed with start_image returned 0x8000000000000001 = EFI_LOAD_ERROR).
    # With this ON, the first boot from the USB carves a FAT32 HARTLOG partition
    # into ONLY the stick's trailing free space (sgdisk --largest-new + mkfs.vfat),
    # ordered BEFORE the bootLog capture so the very first boot's journal lands on
    # it. NEVER touches the in-use ISO/EFI/boot partitions; a pure NO-OP when not
    # USB-booted, when no free space exists, when HARTLOG already exists, or on any
    # error — it can never block or fail boot. The label defaults to bootLog.label
    # so the create-side and read-side stay in lockstep.
    hartlogCreate.enable = true;

    # ── Stateful across boots: persist onto the HARTSTATE partition ──
    # The live ISO is stateless (tmpfs), so the box re-asks for Wi-Fi EVERY boot,
    # forgets the theme/skins/onboarding, and wipes the user's home. With this ON,
    # IF the flasher carved a HARTSTATE-labelled partition on the USB, a boot
    # oneshot mounts it (by-label, the same lookup hart-boot-log uses) BEFORE
    # NetworkManager + the session and bind-persists the stateful paths so they
    # SURVIVE reboot: /etc/NetworkManager/system-connections (Wi-Fi creds — THE
    # "asks for wifi every boot" fix; NM auto-connects next boot), the HART state
    # dir (active theme, custom skins, HartSession, the onboarding/identity seal so
    # first-boot setup is NOT re-asked), and /home/hart-admin. The Wi-Fi keyfiles
    # persist SECURELY (0700 dir / 0600 files, root:root) and ONLY on a POSIX fs
    # (fail-secure: never world-readable on FAT/NTFS; format HARTSTATE ext4).
    # TPM-sealed LUKS on HARTSTATE is the stronger follow-up (needs a key
    # mechanism) — not attempted yet. A pure NO-OP when no HARTSTATE partition is
    # present (the OS still boots stateless, exactly as today); nothing requires
    # the unit, so it can NEVER block or fail boot. [Real-HW-gated — verify on the
    # node via the loop that Wi-Fi + theme + onboarding actually survive a reboot.]
    statePersist.enable = true;

    # ── Boot continuity (return to HART OS on a Live-OS-initiated restart) ──
    # When the user restarts FROM the Live OS, set a ONE-SHOT efibootmgr BootNext
    # to the USB's OWN EFI boot entry so the next boot returns to HART OS without
    # mashing F12. It does NOT change the permanent BootOrder, so the user's
    # Windows still boots normally when chosen — only a Live-OS restart returns
    # here. A no-op if efibootmgr is missing, not UEFI-booted, or the USB entry
    # can't be matched. Intentionally BootNext (one-shot), never BootOrder, so it
    # can never strand the user's Windows boot.
    bootContinuity.enable = true;

    # ── Boot / root-mount / initrd hardening (USB-root enumeration) ──
    # HART OS boots from a USB stick, so the initrd MUST carry the modules that
    # enumerate a USB block device before the root pivot (usb_storage/uas/sd_mod +
    # the xhci/ehci host controllers). The installer-CD profile this config imports
    # already ships them, but a future profile/override change must NOT be able to
    # silently drop them and brick the real-HW USB boot with "VFS: Unable to mount
    # root fs on LABEL=HART_OS". This guard re-ENSURES the USB-root module set is in
    # the initrd AND ASSERTS (at eval time) the critical subset survived the merge —
    # so a stripped module set is a loud BUILD failure, never a silent black-hang on
    # the stick. A pure eval/closure guard: it adds initrd modules + an assertion and
    # does NOTHING at runtime, so it can never block, slow, or fail a boot.
    bootRootInitrd.enable = true;

    # ── External-USB journal export (field recovery for a wedged shell) ──
    # The software-rendered glass shell can peg the CPU and wedge the in-shell
    # terminal/compositor, leaving the user unable to copy anything out. With this
    # ON, plugging in an ordinary FAT32 USB stick (NOT the boot medium) makes HART
    # OS dump the full current-boot journal + the last 200 warning lines to
    # hart-journal-<hostname>.txt on it, on a ~15s timer and at shutdown. It runs
    # as a low-level systemd unit INDEPENDENT of the shell, so it keeps exporting
    # through a hang (capturing the pre-hang state). NEVER writes to the live boot
    # medium (the HART_OS ISO disk + the HARTLOG partition + the disks backing /
    # and /nix/store are excluded); a pure NO-OP when no eligible external stick is
    # present, so it can never clobber the boot stick or block boot/shutdown.
    journalExport.enable = true;

    # ── LAN-path diagnostics + network-up (the steward's "log to the network") ──
    # The dev box and the live-OS box sit on the SAME home LAN, so the journal
    # should be reachable OVER THE NETWORK - no stick to yank. With this ON, the
    # dev box reads the live-OS box's journal with ONE curl over the LAN:
    #   curl "http://<liveos-ip>:6699/diag?t=<TOKEN>"
    # returning journalctl -b + dmesg + lspci + lsusb + rfkill + wpctl + ip -br a +
    # the boot-log - all run through a SECRET-REDACTION filter. The endpoint is
    # READ-ONLY (runs no actions), token-gated (constant-time, FAIL-CLOSED), and
    # LAN-scoped via the firewall.
    #
    # #148 HARDENING (security advisory closed):
    #   - TOKEN: NOT a hardcoded "hart-lan-diag" default any more. With token=""
    #     (omitted) the module GENERATES a random token at first boot, writes it
    #     0600 to /run/hart/netdiag-token (tmpfs, never the store, never in
    #     `systemctl show`), and SURFACES it to the boot-log/journal + the login
    #     MOTD so the operator reads it out-of-band. Find it on the box with:
    #       cat /run/hart/netdiag-token        (or read it off the login MOTD)
    #   - BIND LAN-ONLY: bindAddress = "auto" binds the detected private LAN IP (not
    #     all interfaces); the firewall opens the port ONLY from RFC1918 + link-local
    #     SOURCE ranges (nftables) - never a global/WAN-reachable accept.
    #   - READ-ONLY + SECRET-EXCLUDING: execs only the fixed diag bundle; the bundle
    #     redacts PEM keys / *_PRIVATE_KEY|SECRET|TOKEN|PASSWORD / Authorization /
    #     the diag token itself, and never cats /var/lib/hart key material or
    #     security/*.pem.
    #
    # netconsole (kernel ring over UDP) + the periodic PUSH stay OFF here: each
    # needs a dev-box target IP, and netconsole would pull network-online.target into
    # the boot path (a known boot-stall risk) for a no-op without a target. Arm them
    # per-incident with hart.netDiag.netconsole.{enable,target} / .push.{enable,target}.
    # Network-up (so the diag is reachable): a boot rfkill-unblock clears soft-block
    # on the radio, and the USB-NIC drivers load so plugging a USB-ethernet dongle
    # DHCP-auto-connects instantly (the "debug wifi without wifi" shortcut).
    netDiag = {
      enable = true;
      http = {
        enable = true;
        port = 6699;
        # token = ""  -> generated at first boot (see #148 hardening above).
        # bindAddress = "auto" + RFC1918 firewall scoping = LAN-only (module default).
      };
      wifiUnblock.enable = true;
      usbEthernet.enable = true;
    };

    # ── Cross-OS storage interop (#145): read/write ALL filesystems ──
    # A user plugs in a disk formatted on another OS — a Windows NTFS drive, a
    # camera/phone exFAT card, a Linux ext4/btrfs disk, a FAT32 stick — and HART
    # OS reads AND writes it, like macOS or Windows would. This turns on
    # boot.supportedFilesystems for ntfs/exfat/vfat/ext4/btrfs (kernel drivers +
    # userspace mount helpers), the udisks2 on-demand mount authority the file
    # manager + glass shell call to mount removable media (under /run/media), and
    # the per-filesystem format/repair tooling. PRIVACY-FIRST: reading a plugged
    # disk is a LOCAL capability, so it ships ON (no opt-in friction); nothing
    # here leaves the device. DEGRADE-NOT-DIE: it adds only AVAILABLE drivers +
    # an ON-DEMAND mount path (NEVER an fstab/.mount unit), so a disconnected or
    # corrupt disk is simply never mounted and can never stall local-fs.target or
    # wedge boot — an unmountable disk fails fast and clean. (Proven by
    # tests/storage-filesystems.nix.)
    storage.enable = true;

    # ── Memory sanity (#157): compressed-RAM zram swap + graceful systemd-oomd ──
    # zram is RAM-only (never blocks boot); oomd kills a runaway cgroup not the seat;
    # swappiness is coordinated up for the zram desktop. LOCAL feature -> ON.
    memory.enable = true;

    # ── Automatic GPU allocation (#156): hybrid PRIME render-offload ──
    # Intel iGPU drives the display AND the shell's software floor (unchanged, so the
    # cairo WebView never flips into the expensive effects tier / reintroduces lag);
    # the NVIDIA 940MX is armed for heavy-app render-offload (hart-gpu-offload /
    # prime-run) ONLY when the boot probe proves it present (#132-safe). Degrades to
    # pure Intel, then the software floor. The native force-load arm
    # (gpu.offload.specialisation.enable) stays OFF for the portable ISO.
    gpu.offload.enable = true;

    # ── Privacy-first networking + desktop apps (Category-4 LOCAL features) ──
    # Per the privacy-first principle every LOCAL capability ships ON by default
    # (no opt-in friction); nothing here leaves the device without consent.
    #   - firewall: nftables zones + SYN-flood rate limiting + fwupd firmware
    #     checks. The module enables networking.nftables and uses
    #     extraInputRules (NOT the iptables-only extraCommands) so it coexists
    #     with the rest of the desktop closure WITHOUT the iptables-vs-nftables
    #     assertion that broke iso-desktop before. Ports: hart backend (6777) +
    #     SSH (22) TCP, discovery UDP.
    #   - dns: encrypted resolution (DoT via systemd-resolved, Cloudflare
    #     default). A pure local resolver config; systemd-resolved coexists with
    #     GNOME's NetworkManager (NM uses resolved as its DNS backend).
    #   - email: Thunderbird as the default mailto handler. The email module OWNS
    #     the x-scheme-handler/mailto MIME association (the desktop xdg.mime block
    #     below no longer sets it, so the two definitions can't collide), and its
    #     gnome-keyring/PAM-login settings agree with GNOME's own (both true).
    firewall.enable = true;
    dns.enable = true;
    # A roaming desktop lives on hotel / café / captive / corporate Wi-Fi that
    # routinely blocks or MITMs DNS-over-TLS (port 853). Strict DoT (the default)
    # then fails ALL name resolution with no fallback — the "I connected to the
    # internet and flatpak STILL couldn't reach dl.flathub.org" symptom. Opportunistic
    # DoT (fallbackToPlaintext = true) keeps encrypted resolution when the network
    # allows it and degrades to plaintext when it doesn't, so the box stays usable on
    # any network. dnssec stays ON (unchanged) — this only relaxes the transport, not
    # validation. On the server/edge variants (fixed, trusted egress) strict DoT stays.
    dns.fallbackToPlaintext = true;
    email.enable = true;

    # ── Endpoint security (#155; Category-4 LOCAL feature, privacy-first ON) ──
    #   - ClamAV: clamd LOCAL scanning + freshclam signature updates (the pull is the
    #     ONLY egress, gated like the fwupd check + the OTA pull).
    #   - firewall hardening: defense-in-depth kernel sysctls that COMPLEMENT the
    #     nftables firewall above; purely additive (the shell 6777 / SSH 22 / netdiag
    #     6699 ports all stay open, asserted at eval + tests/security.nix).
    #   - OS + application security fixes are delivered over-the-air via hart-ota.
    security.enable = true;

    # ── Preinstall the curated FOSS gap-fillers (#154) ──
    # Bake the catalog's preinstall set (VLC, Inkscape, Audacity + the GNOME core,
    # de-duped against systemPackages) so the App Store shows Open, not a network
    # Install. The offline catalog route + the Appearance wallpaper fix are already
    # live in the backend. NOTE: the desktop ISO size ceiling is CI-gated (iso-desktop)
    # - if the bake overflows ISO9660, flip bakeMissing off; zstd-22 gives headroom.
    apps.bakeMissing = true;
    apps.wallpapers = true;
  };
}
