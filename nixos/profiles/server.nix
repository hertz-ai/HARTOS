# ═══════════════════════════════════════════════════════════════
# HART OS SERVER — the variant FEATURE PROFILE (canonical home)
# ═══════════════════════════════════════════════════════════════
#
# This is "what a server IS": the hart.* feature block, MOVED VERBATIM out of
# configurations/server.nix (2026-07-28). It is a pure option-set — deliberately no
# `config`/`lib`/`pkgs` captures (verified 0 non-comment scope refs at move time),
# no media/image concerns (ISO branding, repart sizing, growPartition stay in the
# configuration), no hardware assumptions.
#
# WHY A SEPARATE FILE: three consumers need exactly this block and nothing else,
# and until now it lived entangled with image concerns so only the images got it:
#   - configurations/server.nix        (the iso/raw images — imports this)
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
    variant = "server";

    # All AI services
    agent.enable = true;
    llm.enable = true;
    vision.enable = true;

    # ── Kernel Extensions ──
    kernel = {
      enable = true;
      androidNative.enable = false;    # No Android on server
      windowsNative.enable = false;    # No Windows on server
      aiCompute = {
        enable = true;                 # Full GPU compute
        hugePagesCount = 0;            # Auto (set high for dedicated inference)
      };
      agentSandbox.enable = true;      # Isolate agents
    };

    # ── AI Runtime (full power) ──
    aiRuntime = {
      enable = true;
      gpu.enable = true;
      worldModel.enable = true;
      agents = {
        maxConcurrent = 16;            # Server can handle many agents
        maxMemoryPerAgent = "4G";
      };
      # Semantic intelligence: self-healing services + predictive prefetch
      semantic = {
        enable = true;
        serviceIntelligence = true;
        predictivePrefetch = true;
        smartFS = false;               # No user files on server typically
      };
    };

    # ── AI-Native OS Layers ──
    # Model Bus: every app/service gets native AI access
    modelBus.enable = true;

    # Compute Mesh: share this server's GPU with user's other devices
    computeMesh = {
      enable = true;
      maxOffloadPercent = 70;          # Server donates generously
      allowWAN = true;
    };

    # No LiquidUI (headless server)
    # No App Bridge (no subsystems on server)

    # ── Sandbox ──
    sandbox.enable = true;
  };
}
