# ═══════════════════════════════════════════════════════════════
# HART OS EDGE — the variant FEATURE PROFILE (canonical home)
# ═══════════════════════════════════════════════════════════════
#
# This is "what a edge IS": the hart.* feature block, MOVED VERBATIM out of
# configurations/edge.nix (2026-07-28). It is a pure option-set — deliberately no
# `config`/`lib`/`pkgs` captures (verified 0 non-comment scope refs at move time),
# no media/image concerns (ISO branding, repart sizing, growPartition stay in the
# configuration), no hardware assumptions.
#
# WHY A SEPARATE FILE: three consumers need exactly this block and nothing else,
# and until now it lived entangled with image concerns so only the images got it:
#   - configurations/edge.nix        (the iso/raw images — imports this)
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
  # ─── HART OS (minimal) ───
  hart = {
    enable = true;
    variant = "edge";

    # Observer: no agent, no LLM, no vision
    agent.enable = false;
    llm.enable = false;
    vision.enable = false;

    # Kernel: minimal — only agent sandbox for security
    kernel = {
      enable = true;
      androidNative.enable = false;
      windowsNative.enable = false;
      aiCompute.enable = false;
      agentSandbox.enable = false;     # No agents on edge
    };

    # Sandbox for diagnostics
    sandbox.enable = true;

    # ── AI-Native: Compute Mesh Only ──
    # Edge devices contribute compute to the user's mesh.
    # No local models, no UI, no subsystems — just compute donation.
    computeMesh = {
      enable = true;
      maxOffloadPercent = 80;          # Edge donates most of its compute
      allowWAN = true;
    };
    # No modelBus (edge has no local models — uses mesh)
    # No liquidUI (edge has no display)
    # No appBridge (edge has no subsystems)
  };
}
