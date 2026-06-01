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
| 1 | Liquid UI / A2UI | agent pushes a valid component → stored+stamped; invalid type rejected; off-switch honored | ✅ **load-bearing** |
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
- **Broken wire — the one in-repo gap that fails a pillar outright:** **#66** — Direction-B cross-process skill relay. The data primitives exist (P8), but a skill learned on node A does not reach node B across processes. Until #66 is built, "hive learning compounds" is aspirational.
- **Also pending the CI ISO boot:** glass-shell-as-session (#69), Plymouth/branding render, GIL fix (#68).

## How to use this doc
A pillar is **real** only when its probe is green AND load-bearing (not merely
contract/scaffolding). To promote a 🟡 to ✅: build the missing wire/runtime, then
upgrade its probe from contract-level to a behavioral round-trip on the real
target. Keep the ledger honest — a 🟡 that's quietly relabeled ✅ is how "erected"
became a trap in the first place.
