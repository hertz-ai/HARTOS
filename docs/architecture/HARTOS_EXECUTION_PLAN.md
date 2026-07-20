# HART OS — Execution Plan (all pending items)

Steward asked for a plan across every pending item, to verify. This maps EVERY
open task to a phase with a verification gate. Sequenced by dependency + value.

## Cross-cutting invariants (apply to EVERY item, every phase)
- **Zero regression** — existing tests stay green; a path is replaced only when the
  replacement covers 100% of the old cases.
- **Verify everything verifiable; 100% coverage on NEW code** — behavioural tests
  (no grep-tests); the existing tests are the parity oracle; real-HW for anything
  HW-observable; CI (nix build + nixosTests) is the backstop.
- **No parallel paths** — reuse the canonical home (DRY); the live hunter gates any
  agent-built code; respect the concurrent **sharded-inference session**
  (`core/shard_runtime/` etc.) — never touch/duplicate its files.
- **Tier parity** — standalone NixOS + Nunba-packed frozen bundle (Win/macOS/Linux);
  a Rust/native equivalent NEVER removes capability, only accelerates where it can;
  Python stays the cross-tier fallback.
- **Hang-free + degrade-not-die + #132 never-brick + never-black tier ladder** hold.
- **Offline-first + decentralization** (Hive-egress opt-in); **reuse Nunba** (don't
  fork); **the OS feels alive** (calm-but-alive floor, icon-hover micro-anim, default
  voice-viz orb has NO breathing rings); **no em dashes**; **OTA is the update path**.

---

## PHASE 0 — Stabilize the base (unblocks everything)  [NEXT]
Reconcile + commit the work already built + tested this session.
- Commit the HW/AI-native core: OS-bridge native power (#133), compositor
  RepaintScheduler + first-SCANOUT (#131), Model-Bus socket + robot probe,
  hart-notify (#113); finish **#165** (RISK-5 probe-port collapse).
- Commit the shell pass: onboarding-speaks (#159), volume 100% (#160), palettes
  (#161), orb switching (#140), video/lottie/gif bg (#162), offline app-art +
  Credits (#143/#153 part).
- HOLD the onboarding/orb forks for Phase 2; HOLD the W2 nunba nix (fakeHash);
  commit AROUND the sharded-inference session's files (sequence behind if it's about
  to land).
- **Gate:** hunter clean · structural guard + full unit suite green · py/node/cargo
  checks · then push → CI nix build + nixosTests.

## PHASE 1 — Real-HW correctness + flash verify
- **#166** desktop flashes before the lock screen (FOUC + security) — seed
  `#lock-screen` opaque in the served HTML.
- **#134** pointer frozen at 0,0 + no visible onboarding skip.
- **#167** accept-name dead — interim wire, or fully fixed by Phase 2.
- Close **#17** (superseded: orb has idle animation).
- Flash the reconciled build → real-HW verify #149-158 (wifi/mic/panels/apps/
  security/GPU/disk/display) + the above + the hang-free baseline.
- **Gate:** a flashed build correct on real HW, no hang all-session.

## PHASE 2 — Nunba reuse (W2) — delete the forks
- **#135** finish the nunba dist-build (pin npmDepsHash/srcHash + private-repo token,
  cache nunba-static) → unblocks W2.
- **#116** serve Nunba's React UI as native microfrontends: `LightYourHART`
  (18-language presynth + live-TTS for the generated name), `VoiceVisualizer`,
  `HARTSpeechPlayer`, chat/agent overlays.
- Retire `hartOnboarding.js` / `voiceOrbViz.js` / the live-TTS speak fork;
  **#167** fixed by the real accept→seal.
- **Gate:** the real Light-Your-HART works bundled-in-Nunba AND standalone; forks
  deleted; tier parity proven.

## PHASE 3 — Rust OS-native migration (#168) — strangler-fig
- New `hart-os-native` crate; `zbus`→logind/udisks/NM; behind the UNCHANGED
  `os_bridge` contract (`/api/os/invoke`). Order: **power → disk → network →
  display**. Each: parity oracle = the Python tests, `cargo llvm-cov` 100%, op×tier
  matrix, ship behind Python, flip only on real-HW proof.
- Compositor GLES-on-DRM + perf (#125/#131) continue in parallel (already Rust).
- **Gate:** the Linux-OS tier accelerated natively; Python fallback intact on every
  tier; zero regression.

## PHASE 4 — OS completeness (W-streams + system management)
- **#117** W3 app-integration API + SDK + freedesktop bridge · **#118** W4 Start menu
  + Settings · **#119** W5 system management (devices/disk/accessories/paging/env/
  DPI/font) · **#120** W6 feature gaps (notif shade, multi-monitor, edge-dock,
  embodied) · **#121** W7 Netflix listings everywhere · **#122** W8 canonicalize the
  context menu · **#123** W9 realtime voice wired to the agent engine.
- App lifecycle (macOS/Android route): **#163** light Force-Quit · **#164** AI "not
  responding → restart?" nudge · **#142** system-sound layer · **#141** edit-desktop
  mode (drag/place orb + widgets, save layout).

## PHASE 5 — "Feel alive" + art + polish
- Icon-hover micro-animations (the aliveness lever) + the calm-but-alive floor;
  strip breathing rings from the DEFAULT voice-viz orb ONLY (picker orbs keep theirs).
- Mirror the b1.2 teal→violet duotone into the shell brand.
- **#143** central-instance agent images (by name) + magnific/lummi curated art
  (license-gated: rasterize magnific + credit; central/lummi/Flathub free) + the live
  Credits ledger · **#144** federated content-addressed asset cache (generate-once,
  P2P + central reuse).

## PHASE 6 — Interop + platform reach (big bets)
- **#145** interop by design (read/write NTFS/exFAT/ext4/btrfs/... + run all-OS apps
  + open all-OS files) · **#147** Apple-Silicon (Metal GPU + MLX LLM parity) ·
  **#125** W11 100x perf with measured budgets across the stack.

## PHASE 7 — Infra / CI / debt (parallel, ongoing)
- **#136** cache the hart-comp Rust build · **#129** fix the perma-red "Shell UI
  WebKit Safety" CI · **#48** docker-server firewall conflict · **#128** flasher
  HARTLOG/persistence carve · **#130** firmware-prep checklist · **#137** reduced-
  effects floor (largely done) · **#138** terminal fetch-timeout/pipes UX · **#146**
  wifi-route residual + corrupted local venv · **#148** verify/close net-diag token ·
  **#60** OTA anti-rollback nonce.

## Long-running phases (fold into the above)
- **#7** floor-lock/CI-VM harness → Phase 0/7 · **#13** portals + screen kill-switch +
  ext-session-lock → Phase 1/4 · **#14** effects/sway-Tier-2/recipe layouts → Phase
  4/5 · **#126** real-HW boot blocker (cage tap/GPU) → Phase 1/3.

---

## Critical path (the order that unblocks the most)
**P0 stabilize → P1 real-HW correctness + flash → P2 Nunba reuse (kills the forks) →
P3 Rust power slice → then P4/P5/P6 breadth, P7 in parallel.**
