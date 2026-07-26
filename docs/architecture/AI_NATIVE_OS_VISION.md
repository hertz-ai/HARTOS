# HART OS — AI-Native OS: Vision + Proof Ledger

**Not a feature list. A claim-validation ledger.** Every pillar below has a
*falsifiable probe* (an executable test that must pass IFF the pillar bears
load). A pillar that's "erected" but cracked fails its probe. This document is
the thing you **audit against** — when something looks like vapor, the ledger
says what "real" means and whether it's proven.

> Hard-won rule (2026-06-01): *erected ≠ load-bearing.* In two days, six things
> that were "erected" turned out not to hold load — the glass shell (booted
> GNOME), the branding (README placeholder), in-process armor (raw key as
> passphrase, never ran), the app installer (killed by a route collision), the
> hive skill relay (#66), the flywheel (starvation-override only). So we prove,
> we don't assert.

## Thesis
Intelligence stops being an app you open and becomes the substrate the machine
runs on. The interface is **generated per-moment, not shipped**. It spans three
surfaces — **screen, body, and hive** — and through all of them, **control stays
human**: the AI proposes, you dispose; every autonomous act is consented,
reversible, audited; the kill switch is human-held.

## The 8 pillars
1. **Generated/liquid UI** — agents render the right interface for the moment (A2UI).
2. **Sees you** — ambient perception (screen/camera/voice), not a summoned chatbot.
3. **Humans hold the wheel** — consent, reversibility, tamper-evident audit, kill switch.
4. **Composable** — apps/agents/skills/panels/models are addressable primitives.
5. **Learns you, you own it** — a personal model that's local-first, exportable, deletable.
6. **One fabric** — Windows + Linux + Android + web apps, side by side, all AI-augmented.
7. **Embodiment-native** — perceive→world-model→act; a body is just another surface.
8. **Hive-native** — every node a cell; compute + skills compound across the mesh; collectively owned.

## Proof ledger
Probes: `tests/probes/test_os_pillars.py` (+ `tests/unit/test_shell_route_no_collision.py` for P4).
Run: `pytest tests/probes/test_os_pillars.py tests/unit/test_shell_route_no_collision.py -q`

| # | Pillar | Falsifiable probe | Verdict (2026-06-01) |
|---|--------|-------------------|----------------------|
| 1 | Liquid UI / A2UI | agent pushes a valid component (stored+stamped); **REGISTERS a new component type at runtime → the ONE allowlist accepts a push of it**; **RECOMPOSES the whole home into a new design (Aura) + its ambient mood through the SAME transport**; every component exposes an **agent-readable spec**; invalid-type / builtin-override / off-switch / XSS all rejected | ✅ **load-bearing** (upgraded 2026-07-12: runtime framework-extension + whole-desktop recompose, not just one card) |
| 2 | Sees you | voice + camera perceive-surfaces register as routes | 🟡 **contract proven**; STT/capture/VLM→action **runtime-gated** (mic/camera/model) |
| 3 | Humans hold wheel | audit chain verifies on DB round-trip; tamper detected; secrets redacted | ✅ **load-bearing** (fixed #48: created_at now persisted) |
| 4 | Composable | shell_os + shell_system register with no endpoint collision; app installer registers | ✅ **load-bearing** (fixed: dup routes silently killed the installer) |
| 5 | Learns you / own it | resonance profile saves to a LOCAL file + round-trips the learned value | ✅ **load-bearing** |
| 6 | One fabric | unified `detect_platform` classifies .apk/.appimage/.flatpakref; InstallerPlatform spans Android+Linux+Windows | 🟡 **contract proven**; real install/run **runtime-gated** (booted subsystems / ISO) |
| 7 | Embodiment | WorldModelBridge exposes the embodied-skill relay (ingest/distribute) | 🟡 **scaffolding proven**; perceive→act ML in HevolveAI + a body — **runtime-gated** |
| 8 | Hive | a node can export its learning delta | 🟡 **primitives proven (export/ingest/import)**; cross-process **transport BROKEN → #66** |

**Score:** 5 load-bearing (P1,P3,P4,P5 + P8 primitives), 2 contract-proven/runtime-gated (P2,P6), 1 scaffolding/runtime-gated (P7), **1 known-broken wire (P8 transport, #66).**

## What "proven" does NOT yet mean (the honest last-mile)
- **Runtime-gated (need the target, not this dev box):** P2 (real mic/camera + VLM action), P6 (actually installing a Win/Android app in the booted subsystems — needs the CI ISO + boot), P7 (a real robot + HevolveAI perceive→act loop).
- **P1 nuance:** the probe now proves the AGENTIC framework contract is load-bearing — an agent extends the component set at runtime, recomposes the whole home into Aura, and every handle is spec-introspectable, all through the ONE governed transport. What is still **flash-gated** is the *rendered pixels* of that recompose on real HW (the shell actually painting Aura at speed) — verified by booting the ISO, not on this dev box.
- **Broken wire — the one in-repo gap that fails a pillar outright:** **#66** — Direction-B cross-process skill relay. The data primitives exist (P8), but a skill learned on node A does not reach node B across processes. Until #66 is built, "hive learning compounds" is aspirational.
- **Also pending the CI ISO boot:** glass-shell-as-session (#69), Plymouth/branding render, GIL fix (#68).

## CI proof status (2026-06-02)
- **Python pillars (P1/P3/P4/P5/P8-primitives):** the probe suite is green locally, and the pushed commits pass the CI checks that run on them (Security Scan ✅, Docker Build ✅).
- **NixOS pillars (glass-shell #69, branding, cross-OS #6):** my changes are **CI-confirmed eval-clean** — CI's `nix flake check` caught one real bug I'd written blind (the kiosk session package missing `passthru.providedSessions`), I fixed it (28dd0fc), and CI no longer reports it.
- **BUT the ISO build can't go green yet** — blocked by a **pre-existing (red since 05-23, 9 days before the pillar work) flake-evaluation gate** (`nix flake check --no-build`): (1) `nixpkgs.config defined multiple times` (read-only pkgs vs module-level `nixpkgs.config` in hart-base/desktop/server), and (2) `mobile` option missing (mobile-nixos is an input but not imported as a module in the phone config). Tracked as **#70**. Until #70 is fixed, every `iso-*` build is **skipped**, so glass-shell/branding/cross-OS can't be promoted from 🟡 to ✅ via the ISO build.
- **Hardware pillars (P2 vision, P7 embodiment):** need the **Live USB boot** (operator will provide) — not provable in CI or on the dev box.

## How to use this doc
A pillar is **real** only when its probe is green AND load-bearing (not merely
contract/scaffolding). To promote a 🟡 to ✅: build the missing wire/runtime, then
upgrade its probe from contract-level to a behavioral round-trip on the real
target. Keep the ledger honest — a 🟡 that's quietly relabeled ✅ is how "erected"
became a trap in the first place.
