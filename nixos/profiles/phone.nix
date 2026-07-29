# ═══════════════════════════════════════════════════════════════
# HART OS PHONE — the variant FEATURE PROFILE (canonical home)
# ═══════════════════════════════════════════════════════════════
#
# This is "what a phone IS": the hart.* feature block, MOVED VERBATIM out of
# configurations/phone.nix (2026-07-28). It is a pure option-set — deliberately no
# `config`/`lib`/`pkgs` captures (verified 0 non-comment scope refs at move time),
# no media/image concerns (ISO branding, repart sizing, growPartition stay in the
# configuration), no hardware assumptions.
#
# WHY A SEPARATE FILE: three consumers need exactly this block and nothing else,
# and until now it lived entangled with image concerns so only the images got it:
#   - configurations/phone.nix        (the iso/raw images — imports this)
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

_:   # module fn, no args used ({ ... } trips statix W10 — the lint gate is fatal)
{
  # ─── HART OS Core Services ───
  hart = {
    enable = true;
    variant = "phone";

    # Backend + discovery + agent (brain of the node)
    agent.enable = true;
    llm.enable = false;      # Offload to peer nodes
    vision.enable = false;

    # Phone UI
    conky.enable = true;
    nunba.enable = true;

    # ── Kernel Extensions ──
    kernel = {
      enable = true;
      androidNative.enable = true;     # binder + ashmem (Android apps)
      windowsNative.enable = false;    # No Windows on phone
      aiCompute.enable = false;        # No local GPU compute
      agentSandbox.enable = true;      # Isolate agents
    };

    # ── Native Subsystems ──
    subsystems = {
      enable = true;

      linux.flatpak = true;            # Adaptive Linux apps from Flathub

      # Android: native ART (the killer feature — run any Android app)
      android = {
        enable = true;
        playStore = true;              # Most phone users need Google Play
      };

      windows.enable = false;          # Not applicable on phone
      web.enable = true;               # PWA for lightweight apps
    };

    # ── AI Runtime (lightweight for phone) ──
    aiRuntime = {
      enable = true;
      gpu.enable = false;
      agents = {
        maxConcurrent = 3;             # Phone has limited resources
        maxMemoryPerAgent = "512M";
      };
      # Semantic: service healing + prefetch (no smartFS — storage limited)
      semantic = {
        enable = true;
        serviceIntelligence = true;
        predictivePrefetch = true;
        smartFS = false;
      };
    };

    # ── AI-Native Everything OS ──
    # Model Bus: Android apps + Linux apps get native AI
    modelBus = {
      enable = true;
      enableAndroidBridge = true;      # Android apps call AI via content provider
    };

    # Compute Mesh: offload heavy inference to desktop/server
    computeMesh = {
      enable = true;
      allowWAN = true;                 # Phone needs WAN to reach desktop
    };

    # LiquidUI: adaptive interface with voice + haptic
    liquidUI = {
      enable = true;
      voiceEnabled = true;
      hapticEnabled = true;
      renderer = "webkit";
    };

    # App Bridge: Android ↔ Linux cross-subsystem (no Windows on phone)
    appBridge = {
      enable = true;
      intentRouter = true;             # Route Android Intents to Linux services
      clipboardSync = true;
    };

    # ── On-Screen Keyboard ──
    osk = {
      enable = true;
      backend = "squeekboard";
      autoShow = true;
      hapticFeedback = true;
    };

    # ── Sandbox ──
    sandbox.enable = true;
  };
}
