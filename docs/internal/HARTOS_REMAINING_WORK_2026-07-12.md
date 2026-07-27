# HART OS — ALL remaining work, sequenced (2026-07-12)

Consolidated master plan for every pending item. INDEXES the detailed sub-plans (does not
duplicate them). Discipline (binding): reuse-hungry / zero-reinvent / zero-parallel-path;
prove-don't-assert (a pillar is real only when its probe is green AND load-bearing —
`AI_NATIVE_OS_VISION.md`); every item = a behavioural test + real-HW verify where observable.

Sub-plans this indexes: `LIQUID_UI_AGENTIC_FRAMEWORK_PLAN.md`, `../architecture/AI_NATIVE_FIRSTBOOT_AND_TIER1_PLAN.md`,
`../architecture/NUNBA_NATIVE_DAEMON_PLAN.md`, `docs/design/HOME_DESKTOP_DESIGN_CHECKLIST.md` (item ids), the vision ledger.

Legend — **CODE-NOW** (buildable on the dev box) · **CI** (needs nix/node build → CI only) ·
**HW** (needs a booted target) · **PROOF** (built, needs verification).

---

## DONE this session (shipped, in the nightly / committed) — for context, not remaining
Journald routing (bdd29ba3) · libEGL/libglvnd GPU (24726e1b) · gpu_mode hover-hang+vibrance
(47f67b92) · current-tier/display-health (cfcd9f5a) · desktop icons + fuzzy search (17cf71e1) ·
agentic Liquid UI framework + HART/Aura designs (b71e5aa8).

---

## PHASE R0 — IN FLIGHT (the os-completeness workflow, CODE-NOW)
Being built + reuse-hunted now; verify + commit on completion.
| Item | Reuse path | Verify |
|---|---|---|
| MyComputer / Windows-partitions panel (apps + partitions VISIBLE, not search-only) | panel registry (PANEL_MANIFEST + openPanel) + shell_os_apis disk/partition read-ops + hartFiles.js + a default desktop icon | panel registers + lists partitions; icon shows |
| Uninstall registry UI | app_installer.uninstall + shell_manifest installed set + a panel | lists installed + uninstall calls app_installer |
| Cross-OS install CODE gaps (e.g. Wine unconditional-success) | app_installer.py — real result check | behavioural test on the fixed path |
| Flatpak code-verify | app_installer flatpak --user path (98a307af) | behavioural probe (real proof = R4) |

## PHASE R1 — CODE-NOW, not started (the design-checklist gaps; each EXTENDS an existing hook)
Home: `HOME_DESKTOP_DESIGN_CHECKLIST.md` item ids. Sequence by user-visibility.
| # | Gap | Reuse path (file) | Test |
|---|---|---|---|
| f2 | non-touch single-click LAUNCHES; should SELECT, dbl=open | `static/hartDesktop.js` (`isTap` branch on `pointerType==='mouse'`) | click→select, dblclick→open |
| e2 | omnibox has no semantic "find by caption" route | `static/hartHero.js` dispatch + `media_semantic_index.py` | typed caption → media result |
| d7 | semantic-media-index → card-image pipeline unwired | `static/hartHome.js` card hydrate + `media_semantic_index.py` | card gets index image |
| e4 | taskbar: no hover-previews / live minimise-progress | `liquid_ui_service.py` taskbar block / `static/hartDock.js` | preview shows; minimise animates |
| k8 | system sounds (USB connect/notify/error chimes) | `compositor/src/udev.rs` (or udev rules) + hart-notify (V2 audio foundation) | event → sound played |
| d4 | Netflix listings NOT on every surface (settings/explorer/registry) | `static/hartFiles.js` + settings panels (reuse `.hh-card` row pattern) | surfaces render as listings |
| j4 | Win/macOS parity apps: recycle bin, event viewer, startup manager | `shell_manifest.py` + new panels (reuse panel registry) | panels register + function |
| a3/j3 | true multi-monitor + per-screen DPI | `compositor/src/udev.rs` (per-output) + shell scaling | 2-output VM test |

## PHASE R2 — OWED PROOFS (built or nearly; need verification to leave 🟡)
| Item | What proves it | Blocked on |
|---|---|---|
| Liquid UI P1 probe upgrade (agent recomposes into Aura / registers a component at runtime / drives an attribute) | new `tests/probes/test_os_pillars.py` P1 behavioural round-trip | CODE-NOW (write it), then keep it green |
| Agents/chat run on first boot (P1/P4) | package `langchain_classic`+`autogen` into `hart-app.nix`; behavioural `/chat`→recipe-write on the booted node | CI (nix build) + HW boot |
| GLES actually engages (libglvnd) | next flash's journal shows the GLES-init success line (not the caught panic) | HW (next flash) |
| Vibrant + snappy + hang-free + icons + search | next flash real-HW observation | HW (next flash) |

## PHASE R3 — CI-BLOCKED (Nunba native daemon; unblock = the nix build)
Home: `../architecture/NUNBA_NATIVE_DAEMON_PLAN.md` (A+D done; B/C/F written). Unblock ALL THREE below at once:
pin `nunbaHash`/`npmDepsHash` for the Nunba rev in ONE commit → green `nix build .#packages.x86_64-linux.nunba`
via the import-domino loop → flip `desktop.nix nunba.enable=true`.
| Item | Delivered by the daemon |
|---|---|
| Nunba microfrontends (serve Nunba's real React pages; retire the HARTOS forks — E) | the daemon serves `landing-page` same-origin via the LiquidUI reverse-proxy (D done) |
| **LightYourHART** onboarding (retire the inferior `hartOnboarding.js` fork) | Nunba's `LightYourHART.js` (18-lang, live TTS) served by the daemon |
| Nunba AI setup wizard as extended-install-on-internet | Nunba's `llama_installer` / `/api/llm/auto-setup` surfaced via the daemon + a first-connection trigger |
Why blocked: **no nix/node/docker on the dev box** — the FOD hashes can only be pinned + the build walked green in CI. Steward chose "CI builds it."

## PHASE R4 — HW-BLOCKED (need a booted target / real hardware)
| Item | Why | Unblock |
|---|---|---|
| Real macOS (Darling) / Android / Wine app INSTALL + run | needs the booted subsystems | boot the ISO with the subsystems armed |
| 100x profiling (measured) | needs real-HW timing, not a dev-box guess | flash + profile on-device (chat 1.5s / draft 300ms / cache <1ms budgets) |
| Flatpak end-to-end | code fixed; proof needs a real boot | next flash |
| First-boot LLM up + online model download + first-connection WIZARD | `hart-llm-provision` exists; the same-boot re-trigger + `hart-gpu-scheduler` NotifyAccess fix + a LightYourHART-served wizard are CODE-NOW, but the LLM-actually-serving proof is HW | see `../architecture/AI_NATIVE_FIRSTBOOT_AND_TIER1_PLAN.md` P2 |

## Cross-cutting fixes surfaced by the real-HW journal (CODE-NOW, fold into R1/R2)
`hart-gpu-scheduler` NotifyAccess=main timeout (coordinate — AI-runtime) · `hart-vision` RO-FS log
crash · `hart-world-model` unbalanced-quoting ExecStart · `hart-liquid-ui` Model-Bus 1s startup race ·
`hart-sandbox` awk-missing.

## Critical path
R0 (finishing now) → R1 f2+e2 (visible app UX) → R2 P1-probe (prove the framework) → **CI: R3 Nunba
daemon** (unblocks onboarding + microfrontends + wizard together) → **HW: R4** on the next flashes.
Nothing is marked done until its probe is green AND load-bearing.
