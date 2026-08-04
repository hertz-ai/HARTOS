# HART-comp Roadmap

> **Scope:** the phased build plan for the HART OS native architecture, lifted from the
> verified plan. Phases, deliverables, tests, **never-break gates**, and **verifyVia** are
> canonical here. The architecture rationale lives in
> [`../docs/architecture/HART_OS_NATIVE_ARCHITECTURE.md`](../docs/architecture/HART_OS_NATIVE_ARCHITECTURE.md);
> the IPC contract in [`IPC_PROTOCOL.md`](./IPC_PROTOCOL.md).
>
> **No compositor C/Rust source is authored ahead of CI.** Every L1/L2 paint, software-GL
> fallback, XWayland map, portal screencast, and session-lock claim is compiled + VM-proven,
> never authored blind.

## Ordering principle (invariant)

1. A **never-fail floor** (today's audited cage) exists from **Phase 0**.
2. The **out-of-process tier-drop supervisor** is built and VM-proven **before** any
   compositor becomes default (Phase 1).
3. The pre-existing **A2UI / ai_sensing / audit** gaps are **closed before** `window.*` or
   portal screencast ship (Phase 2).
4. **Smithay is a later moat upgrade behind a sway-as-Tier-1 fast path**: OS-native
   windowing is not blocked on a first-ever Rust-Nix bring-up.

> **Phase-3 foundations landed (dev-box-authored, VM/CI-pending):** the sway-Tier-1-now
> fast path (`nixos/modules/hart-sway-tier1.nix`, sway single-output kiosk running the
> canonical glass shell + a `swaymsg` agent-windowing shim), the FIRST Rust-in-Nix
> `buildRustPackage` precedent on the existing `claw_native/rust` crate
> (`nixos/modules/hart-rust-precedent.nix`, `cargoLock.lockFile`, pin `50ab793`), the
> HART-comp Smithay skeleton (`compositor/Cargo.toml` + `compositor/src/main.rs`: event
> loop + mandatory pixman software-render path + layer-shell mount, all `todo!()`/stub +
> `compile-pending`), and its opt-in session module (`nixos/modules/hart-comp.nix`,
> `buildRustPackage` with a fixed `cargoHash` placeholder, **defaultSession stays cage**).
> None are compiled/booted on the Windows dev box; every paint/scanout/`swaymsg`/build
> claim is VM-pending (CI nixosTest llvmpipe / QEMU-KVM). The three new session modules are
> NOT yet added to the shared flake `hartModules[]`; that registration + the real
> `cargoHash`/`Cargo.lock` are the Phase-3 CI bring-up step, gated on the build resolving.

Every phase is **additive behind the watchdog**; every claim becomes a tested deliverable or
a labeled openRisk. **A phase is NOT done until its `nixosTest` is green on the
software-GL VM AND its supervisor fault-injection proves the floor still paints AND its
never-break gates pass.**

---

## Phase 0: Floor-lock + CI/VM gate harness

**Goal:** before any compositor work, freeze today's audited cage + WebKitGTK +
forced-software-GL session as the immutable **Tier-3 floor** and stand up the CI `nixosTest`
+ local QEMU harness every later phase is proven against. Nothing here changes runtime
behavior; it makes *"never blank screen"* a tested invariant from line 1.

**Deliverables**
- Pin `desktop.nix` `defaultSession` on the **existing** cage `hart-shell` (no change) + a
  labelled regression `nixosTest` that boots it under forced software GL
  (`WLR_RENDERER_ALLOW_SOFTWARE` / `LIBGL_ALWAYS_SOFTWARE` / `WEBKIT_DISABLE_DMABUF_RENDERER`
  / `HardwareAccelerationPolicy.NEVER`) on an llvmpipe VM and asserts first WebView frame
  paints.
- Dead-husk-aware boot health-check helper: a **real** Flask `test_client` / curl fetch of
  `/shell/static/*` (NOT inline-render) asserting 200 + non-empty body, carrying the f294f52
  lesson into CI.
- CI `nixosTest` job wired into the #70 eval-gate discipline (`flake.nix` `hartModules[]` +
  `providedSessions` intact); a local QEMU-KVM runbook (`scripts/vm/boot-tier.sh`) that boots
  any tier headless with serial console.
- A `/var/lib/hart/session-tier` state-file contract doc (values: `hart-comp | sway | cage`),
  read by the greeter, written by the supervisor. **Spec only, no consumer yet.**
- Source-guard test asserting no desktop-shell frontend fetches `/api/ui` (generative path
  stays terminal/Conky-only) and the Tier-4 Conky/terminal degraded surface still renders
  from `generate_ui`.

**Tests**
- `nixosTest`: boot cage on llvmpipe VM, assert shell paints + `/shell/static` fetch 200.
- `nixosTest`: kill the cage WebView process, assert systemd `Restart=on-failure` brings it
  back without `WatchdogSec` self-kill (preserve the `sd_notify`-once lesson).
- unit: source-guard `/api/ui` not wired to desktop boot; Conky degraded surface still builds.
- unit: flake eval succeeds with `hartModules[]` + `providedSessions` unchanged (eval-gate
  canary).

**Never-break gates**
- Tier-3 cage software-GL contract **bit-for-bit unchanged** (`hart-liquid-ui.nix` GTK3 path).
- No `WatchdogSec` added to serve_forever units (LiquidUI/ModelBus/AppBridge);
  `Restart=on-failure` + `sd_notify` `READY=1` once only.
- Guardrail kernel + master-key boot verification gate the session; never weakened.
- `/shell/static` `static_url_path` route (f294f52) still relied on, and untouched.

**verifyVia:** CI `nixosTest` VM (llvmpipe software-GL) + local QEMU-KVM boot runbook.

---

## Phase 1: Out-of-process tier-drop supervisor + watchdog signal

**Goal:** build the SAFETY-CRITICAL mechanism the architecture mislabels as a `NodeWatchdog`
"reuse": a real **out-of-process** session supervisor that selects the session binary from a
latched state file, drops one tier per crash-loop, latches, and exposes an operator reset.
This must exist and be VM-proven **before** Tier-1/Tier-2 ever become default; without it a
half-finished compositor can leave the box on a black screen.

**Deliverables**
- `hart-session-supervisor`: a systemd/greetd-level unit (**NOT** a Python thread) that reads
  `/var/lib/hart/session-tier`, launches the chosen session (`hart-comp | sway | cage`), and on
  N crash-loops (**3 restarts / 5 min**) writes the next-lower tier and restarts the greeter,
  **latching** the choice across boot.
- Operator "reset to Tier 1" path: `hartctl session reset-tier` (clears latch) + telemetry
  that surfaces the current latched tier so users don't silently run on cage forever after one
  bad boot.
- `NodeWatchdog` integration as a **SIGNAL EMITTER only**: extend `node_watchdog` to publish a
  "compositor unhealthy" event (reusing 60s startup grace, frozen-thread detection,
  LLM-call-awareness) that the supervisor consumes. `node_watchdog` explicitly does **NOT**
  own session selection.
- Wire **sway as Tier-2** and **cage as Tier-3** into the supervisor's ladder NOW (both already
  exist / are packaged); Tier-1 slot reserved, `defaultSession` still cage.
- GNOME remains user-selectable at the greeter (`desktop.nix` guarantee) as the ultimate human
  escape hatch, verified intact.

**Tests**
- `nixosTest`: register a deliberately-crashing fake Tier-1 binary, loop-kill it, assert the
  supervisor writes the latch and the session **lands on cage** with the latch file = `cage`.
- `nixosTest`: from a latched `cage` state, run `hartctl session reset-tier`, reboot, assert the
  next session attempts Tier-1 again.
- unit: `node_watchdog` emits compositor-unhealthy on missed heartbeat and does **NOT** attempt
  a session switch itself.
- `nixosTest`: GNOME still selectable at greeter after supervisor install.

**Never-break gates**
- A crash-loop at any tier **ALWAYS lands on a tier that paints**; the screen is never blank.
- Supervisor is out-of-process (greetd/systemd); `node_watchdog` never owns session selection.
- Latch is operator-clearable; a transient Tier-1 bug cannot permanently mask as a downgrade
  with no recovery path.
- Cage Tier-3 remains the audited floor; supervisor cannot drop below it.

**verifyVia:** CI `nixosTest` VM (loop-kill fault injection) + local QEMU-KVM.

---

## Phase 2: Close the pre-existing A2UI / audit / ai_sensing gaps in the BRAIN

**Goal:** fix the three verified pre-existing defects the architecture wrongly assumes are
already solved, so later phases build on **real** controls, not vaporware: (a) the A2UI
cross-process push gap, (b) the missing guardrail/audit/escaping on `agent_ui_update`, (c) the
unenforced `'screen'` sense. Display-layer-independent; lands on **every** tier including
headless.

**Deliverables**
- Resolve the A2UI transport **for real**: verify runtime topology, then **EITHER** co-locate
  `LiquidUIService` into the brain process + `get_registry().register('LiquidUIService',
  instance)` at boot, deleting the `theme_service.py` cross-process HTTP-to-`:6800` hack;
  **OR** keep split and route all callers (the ~10 brain call sites + `channels/agent_tools.py`
  + `oauth_api.py` + `agent_voice_bridge.py` + `model_orchestrator.py`) through one
  out-of-process transport helper (WAMP-required with Crossbar reconnect supervisor, or HTTP
  `POST /api/a2ui`). **Pick ONE in code.**
- Add the MISSING controls to `agent_ui_update`: `GuardrailEnforcer.before_dispatch` +
  `HiveCircuitBreaker.is_halted` (fail-closed), `get_audit_log().log_event()` per push, and
  server-side string sanitization. Re-verify the existing A2UI card path + 16 callers still
  work after.
- Wire the EXISTING screen-capture consumers (`remote_desktop/frame_capture.py`,
  `window_capture.py`, the visual/time-agent capture path) to check
  `core.ai_sensing.allowed('screen')` and refuse when cut, closing the pre-existing dead-flag gap.
- Make `ai_sensing.status()` report **observable** screen proof (active-capture-session count /
  blocked flag), not just the unenforced flag, to honor the un-fakeable-proof invariant.
- Preserve the foreground gate as **display-independent**: confirm `enter_foreground` stays
  HTTP-request-driven (`is_genuine_user_request`) and add a note that it must function with
  **ZERO compositor present** (server/edge/central Docker).

**Tests**
- integration (cross-process if split): a separate-process caller pushes an A2UI card and it
  **IS** observed on the SSE stream; `ServiceRegistry.get('LiquidUIService')` is non-None after
  boot (if co-located).
- behavioural: `agent_ui_update` with `HiveCircuitBreaker` halted is **REFUSED** and logged to
  the immutable audit chain; a malicious string field is sanitized server-side.
- behavioural: `frame_capture`/`window_capture` **REFUSE** to capture when
  `ai_sensing.allowed('screen')` is False; `status()` reports blocked.
- regression: `should_yield_to_user` behaves correctly with **NO** compositor signal source
  registered (headless tier).

**Never-break gates**
- `core.ai_sensing` stays THE single gate with no AI write path; no WM/portal verb can
  re-enable senses.
- `agent_ui_update` remains never-fail for delivery but now **fail-CLOSED** for
  guardrail/circuit-breaker.
- Foreground gate remains HTTP-driven and functional on headless tiers; compositor signal is
  additive only (augment, never replace).
- Realtime fabric (`realtime.publish_event` → WAMP `com.hertzai.hevolve.social.{user_id}` +
  `broadcast_sse_safe`) egress UNAFFECTED by any brain process re-topology, proven by test.

**verifyVia:** local QEMU-KVM + cross-process integration tests (CI).

---

## Phase 3: HART-comp skeleton (tinywl-class) + software-render floor, opt-in only

**Goal:** establish the Rust-in-Nix toolchain (the **FIRST** in this tree) and ship a minimal
Smithay compositor that paints a solid KMS splash and ONE layer-shell client, with a
from-scratch pixman/llvmpipe software-render path proven on broken-GPU fixtures. It is opt-in
(`defaultSession` stays cage); the Phase-1 supervisor guarantees it can never brick the box.

**Deliverables**
- **[LANDED, dev-authored]** Ship **sway as Tier-1 NOW** (`nixos/modules/hart-sway-tier1.nix`):
  sway in single-output kiosk running the SAME canonical glass shell (`hart-glass-shell`, no
  parallel renderer) under the forced-software-GL contract, with agent tile/summon/focus/move
  shimmed via `hart-swaymsg-shim` (`swaymsg`), so OS-native agent windowing ships WITHOUT the
  Rust bring-up. Registers a greeter-selectable `hart-sway` session; `defaultSession` stays cage.
  The shim is transport-only; the fail-closed gate stays brain-side (Phase 2/6). VM-pending:
  real boot + `swaymsg` arrangement + paint proof on llvmpipe.
- **[LANDED, dev-authored]** The `buildRustPackage` precedent **FIRST**, on an existing crate
  (`nixos/modules/hart-rust-precedent.nix` → `claw_native/rust`), using `cargoLock.lockFile`
  (the crate ships a committed `Cargo.lock`) under the pinned nixpkgs `50ab793`, proving the
  stock pinned toolchain resolves a real crate graph before HART-comp depends on it. The fixed
  `cargoHash` placeholder model is documented inline for crates lacking a committed lock. If the
  graph does not resolve on the June-2025 pin, this module fails the gate in ISOLATION (surfaced,
  not hidden). VM/CI-pending: the actual `nix build`.
- **[LANDED, dev-authored]** `compositor/` Smithay HART-comp **skeleton** (`Cargo.toml` +
  `src/main.rs`, tinywl-class): a calloop event loop + a MANDATORY pixman software-render path +
  a real `--force-software` flag + a KMS solid brand-color clear before first frame
  (no flash-of-black) + a `wlr-layer-shell` glass-shell mount point. Every real Smithay handler
  body `todo!()`/stub + `compile-pending`, with pure-logic unit tests asserting the never-fail
  floor (unprobed/forced boot ALWAYS selects software). NOT compiled on the Windows box.
- **[LANDED, dev-authored]** `nixos/modules/hart-comp.nix`: `buildRustPackage` of `compositor/`
  (fixed `cargoHash` placeholder, since `compositor/` has no committed lock yet; CI commits the real
  hash), udev/DRM/GBM backend deps + the pixman C dep, `--force-software` session launcher,
  opt-in `hart-comp` session, **`defaultSession` stays cage**. Asserts `hart.rustPrecedent.enable`
  (the toolchain dependency is structural) + the canonical glass-shell renderer (no third
  renderer). **Not yet added to flake `hartModules[]`**; that registration +
  `passthru.providedSessions=['hart-comp']` wiring is the CI bring-up step gated on the build
  resolving (mirror `kioskSession`).
- **[VM/CI, not yet]** Extend node integrity to cover the Rust binary: add the content-addressed
  Nix store path / artifact hash to the signed release manifest checked by
  `verify_local_code_matches_manifest` (`compute_file_manifest` currently hashes only `.py`), and
  add hart-comp as a brand/origin-attested artifact so a forked compositor fails peer attestation.
  `full_boot_verification` gates HART-comp on its own signature. (Owned by the security task; this
  module exposes `config.hart.comp.package` for that manifest to reach the binary.)
- **[VM/CI, not yet]** HART-comp boots only **AFTER** `full_boot_verification` (guardrail kernel +
  master-key + origin attestation) passes: the constitution gates the display. Enforced by the
  Phase-1 supervisor boot ordering + the signed-manifest gate above, NOT re-implemented here.

**Tests**
- `nixosTest`: eval gate. Flake evaluates with `hart-comp.nix` in `hartModules[]` and a session
  registered; missing-module canary fails loudly.
- `nixosTest`: boot HART-comp with hardware GL **DISABLED** on llvmpipe, assert pixman path
  paints first frame (no crash) + KMS splash shows before frame.
- `nixosTest`: GBM/DMABUF init-failure fixture → HART-comp falls to pixman **WITHOUT** crashing.
- behavioural: a tampered hart-comp binary hash trips `RuntimeIntegrityMonitor` / fails
  `verify_local_code_matches_manifest` within the 300s window.
- integration: crash-loop the HART-comp skeleton under the Phase-1 supervisor → lands on cage,
  latch written (the floor holds).

**Never-break gates**
- `defaultSession` stays cage; HART-comp is opt-in until its software path is VM-proven.
- Phase-1 supervisor latching active, so a Smithay regression silently lands on today's cage
  behavior.
- Rust binary is now IN the trust manifest; `full_boot_verification` gates it.
- Software-render path is a first-class type-checked code path, not an env-var prayer;
  force-software flag is real.

**verifyVia:** CI `nixosTest` VM (llvmpipe + GBM-fail fixture) + local QEMU-KVM.

---

## Phase 4: Glass shell as a layer-shell surface, the GTK3→GTK4 host-window port

**Goal:** re-host the EXACT served shell HTML as a layer-shell surface, treating the
unavoidable **GTK3 → GTK4** WebKitGTK host-window port as an explicit milestone with its OWN
software-GL validation, NOT the architecture's misleading "JS untouched / same WebView"
framing. State which single/dual-WebView model is chosen.

**Deliverables**
- GTK4 + WebKitGTK + `gtk4-layer-shell` host window pointed at the SAME `LiquidUIService :6800`
  `render_desktop_shell` + `/shell/static` tree (JS served unchanged), anchored BACKGROUND
  layer, exclusive zone 0, re-validated under `WEBKIT_DISABLE_DMABUF_RENDERER` /
  NEVER-acceleration equivalents on GTK4: a **fresh** broken-GPU paint proof, not an inherited
  assumption.
- Pick ONE z-order model in code: (1) single layer-shell surface, overlays/orb co-planar with
  desktop (native windows sit ABOVE the orb, a documented limit), OR (2) two WebViews (background
  desktop + top-layer A2UI/orb) with a **real cross-WebView message bus** replacing the implicit
  single-window `window.*` sharing (`openPanel`/`acSend`/`HartSession`/`toggleVoice`/
  `renderAgentOverlay`). If (2), **drop "JS untouched"** and budget the bus.
- Keep the GTK3 cage path **VERBATIM** as Tier-3; the GTK4 layer-shell L2 only becomes a tier
  behind the Phase-1 supervisor after broken-GPU validation passes.
- Boot health = first WebView frame painted + **REAL** `/shell/static` fetch (dead-husk-aware)
  within a bounded window, reusing the Phase-0 helper.

**Tests**
- `nixosTest`: GTK4 layer-shell shell boots on llvmpipe, asserts paint + `/shell/static` fetch 200.
- integration: if model (2), an A2UI card is z-**ABOVE** a placed native window (proves the
  dual-surface promise); if model (1), test asserts co-planar and the limitation is
  documented.
- behavioural: every advertised shell global (`openPanel`/`acSend`/`HartSession`/`toggleVoice`)
  still reachable across the chosen model.
- `nixosTest`: GTK4 host crash → supervisor drops to GTK3 cage Tier-3, shell still paints.

**Never-break gates**
- GTK3 cage Tier-3 software-GL path **bit-for-bit unchanged** as the floor.
- GTK4 path gated behind the supervisor + its own broken-GPU proof before becoming default.
- All 13 JS modules + `_CSS_DESIGN_SYSTEM` + `ThemeService` served identically; `/shell/static`
  route intact.
- If "JS untouched" is broken (model 2), it is **stated and budgeted**, not hidden.

**verifyVia:** CI `nixosTest` VM (GTK4 llvmpipe) + local QEMU-KVM.

---

## Phase 5: Native app windows (xdg-shell + XWayland) + a real install→window path

**Goal:** make HART-comp host real native toplevels (Wine/Flatpak/PWA via xdg-shell +
XWayland), with launch-success keyed on a **REAL** `window.opened` map event, never on the
installer/launcher exit code, so the agent and user never see a phantom window. Android stays
explicitly inert.

> **Phase-5 foundations landed (dev-box-authored, Smithay-wiring VM/CI-pending):** the
> compositor's **pure window bookkeeping** is authored, compiles, and is unit-tested on the
> dev box (`compositor/src/main.rs` "PHASE 5" block): handle minting (`WindowHandle`/
> `mint_handle`, `win_<hex>`, the IPC `handle`), the manifest↔toplevel map (`WindowRegistry`
> = the "AppRegistry window-handle field"), the no-phantom-window `SummonApp` state machine
> (`PendingSummon`/`SummonOutcome`, keyed on a real map within `SUMMON_MAP_TIMEOUT`, with
> `summon_precheck` short-circuiting inert subsystems to `Unsupported`), and the
> launch→await-map **orchestration** (`SummonResolver`, `begin`/`resolve`/`expire`: Mapped
> ONLY via a real map, TimedOut never a handle). **19 Rust unit tests green** on the dev box
> (3 Phase-3 + 16 Phase-5).
>
> The Smithay HANDLER BODIES are now **authored** in `compositor/src/wayland.rs`: the real
> `impl XdgShellHandler / XdgDecorationHandler / ForeignToplevelListHandler for State` + the
> XWayland lifecycle (`handle_xwayland_event`, `on_xwayland_mapped`/`_unmapped`) + the live
> `State::on_real_map`/`expire_summons` summon resolution against the Smithay API. **The ENTIRE
> file is gated behind `#![cfg(feature = "smithay")]`, OFF by default** (`Cargo.toml`
> `[features]`), so the dev box compiles + tests ONLY the pure floor (verified: `cargo build
> --features smithay` fails cleanly with "unresolved crate `smithay`"; the git-Smithay dep is
> still commented; that ONE flip is the CI bring-up step). The free-fn `// ⚠️ CI-COMPILE`
> `todo!()` stubs stay in `main.rs` as the feature-OFF placeholders (each now naming its real
> `wayland.rs` body), so a reader of the always-compiled crate still sees an explicit "not wired
> here". The `xwayland` Smithay feature + its X11 C deps are a commented manifest in
> `Cargo.toml`/`hart-comp.nix`, and `hart-comp.nix` `buildFeatures` stays `[ ]` until the same
> CI step sets `[ "smithay" ]`. The brain-side native-window path is wired ADDITIVELY (the
> `AppRegistry` manifest↔handle map + `HartWmClient.summon_app` reuse path); the `SummonApp` IPC
> verb's launch→await-map transport lands with Phase 6.

**Deliverables**
- **[AUTHORED, CI-compile-gated]** `xdg-shell` + XWayland + `xdg-decoration` +
  `wlr-foreign-toplevel-management` in HART-comp; the shell layer-shell surface floats per the
  Phase-4 model above placeable native toplevels. **The real handler BODIES are authored in
  `compositor/src/wayland.rs`** (the `impl XdgShellHandler / XdgDecorationHandler /
  ForeignToplevelListHandler for State` + the XWayland lifecycle + the `State::on_real_map`
  summon resolution) **behind `#![cfg(feature = "smithay")]`, OFF by default**, so they compile
  only where Smithay links (the one CI flip: enable the feature + uncomment the git-Smithay dep
  + the X11 C deps). The `// ⚠️ CI-COMPILE` `todo!()` free-fn stubs stay in `main.rs` as the
  feature-OFF placeholders (`on_xdg_toplevel_mapped` / `on_xwayland_surface_mapped` /
  `on_toplevel_destroyed` / `on_decoration_request` / `on_foreign_toplevel_sync`), each naming
  its real `wayland.rs` body.
- **[LANDED, dev-authored, pure logic]** `SummonApp`/launch path returns success ONLY on a
  `wlr-foreign-toplevel`/xdg-shell **MAP event** within a timeout, NOT on `app_installer`
  return-True (Wine returns 0 even when nothing mapped; Android `_install` just copies the apk).
  Encoded as `SummonOutcome` (no handleless `Mapped` variant) + `PendingSummon::is_timed_out_at`
  (→ `TimedOut`) + `summon_precheck` (→ `Unsupported` for inert subsystems). A failure the
  agent can react to. **Smithay map-event resolution is the CI wiring.**
- **[LANDED, dev-authored, pure logic]** the compositor gains a window-handle field mapping
  manifest ↔ toplevel (`WindowRegistry.by_manifest`/`by_handle`, `on_map`/`on_unmap`/
  `handle_for_manifest`, the Rust-side "AppRegistry window-handle field", mirroring
  `IPC_PROTOCOL.md` §4.1 `manifest_id`). Installed-app entries gain a "launch as native window"
  path **ALONGSIDE** existing iframe panels (additive, panels preserved); the additive
  native-window path is the `run_event_loop` wiring + the brain's launcher (Phase 6 transport).
- Carry forward **VERBATIM as labeled openRisks:** Android = `exec sleep infinity` (no
  ART/Waydroid, native Android windows are vaporware; `hart-subsystems.nix:288`), Wine success
  unconditional at the installer layer (`app_installer.py:_install_windows` returns
  `success=True` regardless of map, now corrected at the WM layer: a Wine launch proceeds to
  AWAIT A MAP via `summon_precheck`→None, the handle is minted only by `on_xwayland_surface_
  mapped`), macOS/Darling default-off (`_install_macos`), no `.crx`/`.xpi` installer.

**Tests**
- integration: launch a Flatpak/PWA toplevel, assert `window.opened` fires and a handle is
  returned; launch a Wine no-map fixture, assert `SummonApp` reports **FAILURE** (not phantom
  success).
- integration: Android `SummonApp` returns "unsupported/inert", with no phantom handle.
- behavioural: a native toplevel maps under the layer-shell shell (z-order per Phase-4 model).
- `nixosTest`: XWayland Wine app + layer-shell shell co-exist; supervisor still drops cleanly on
  compositor crash.

**Never-break gates**
- **No phantom windows:** a handle is returned ONLY when a toplevel actually mapped.
- iframe-panel launch path for installed apps preserved alongside the native-window path.
- Android/Wine/macOS/crx limits carried as labeled openRisks, not claimed solved.
- `AppInstaller` 7 handlers + magic-byte detection + auto-register unchanged.

**verifyVia:** local QEMU-KVM (real XWayland/Flatpak toplevels) + CI `nixosTest`.

---

## Phase 6: AI-native WM IPC (`com.hart.Compositor`) wired to the brain

**Goal:** give the brain the privileged WM client so agents place/tile/summon/arrange real
native windows, with its OWN explicit **fail-closed guardrail + circuit-breaker +
immutable-audit** gate (built in Phase 2), NOT a no-op pipeline. This is the moat and the widest
new privilege in the tree, so it ships only after the audit/guardrail controls exist and the
supervisor proves it can't brick the box.

**Deliverables**
- `com.hart.Compositor` D-Bus interface + low-latency Unix-socket twin:
  `ListWindows`/`FocusWindow`/`CloseWindow`/`PlaceWindow`/`TileLayout`/`SummonApp`/
  `MoveToWorkspace`/`SwitchWorkspace`/`SetOutputMode`/`Subscribe`. `HartWmClient` (Python)
  registered in `ServiceRegistry`, co-located with `LiquidUIService` per the Phase-2 resolution.
- `window.*` verb family added to A2UI `COMPONENT_TYPES`, flowing through the **NOW-real**
  `agent_ui_update` controls (guardrail + circuit-breaker + immutable audit + sanitize from
  Phase 2). Every mutating method is fail-closed behind `GuardrailEnforcer.before_dispatch` +
  `HiveCircuitBreaker.is_halted` at the `HartWmClient` boundary; every op logged with `agent_id`
  + verb + target.
- Destructive geometry (close user-focused window, fullscreen takeover) routes through the
  **EXISTING** `PREVIEW_PENDING`/`PREVIEW_APPROVED` FSM gate + an A2UI approval component wired
  to the real audit sink. A real **per-agent rate cap** on window ops (NOT the 5-item display
  ring buffer).
- Workspaces **AUGMENT not replace**: keep `hartWorkspaces.js` client-side panel show/hide on
  EVERY tier (incl. cage); ALSO drive compositor `MoveToWorkspace` for real native windows ONLY
  when `com.hart.Compositor` is feature-detected. One source of truth per object class (shell
  panels = client state, native windows = compositor).
- Register `Super+Space` PTT AND `Ctrl+Alt+1-4` workspace keys as compositor **GLOBAL keybinds**
  routing to brain/shell; keep in-WebView `keydown` handlers as the cage Tier-3 fallback.
  Compositor focus/idle feeds `core.foreground` as an **ADDITIVE** signal (never replacing the
  HTTP-driven writer).
- NEW MCP tools (`place_window`/`tile`/`summon`/`list_windows`) for Claude co-pilot, audited +
  guardrail-gated identically.
- Compositor render-loop CPU explicitly accounted in `resource_governor` external-CPU
  attribution (excluded from BOTH "own idle work" and "user is busy" signals; real focus/idle is
  the yield trigger), mirroring the governor self-throttle fix.
- `UnifiedClipboard`: correct the baseline (today = in-memory string + a side `wl-clipboard` sync
  unit, NOT a poller) and specify `wl_data_device` integration as explicit Smithay glue; keep the
  in-memory + sync-unit fallback so cross-subsystem paste never regresses to nothing.

**Tests**
- behavioural: agent `window.close` on a user-FOCUSED window is **REFUSED** without PREVIEW
  approval and **IS** recorded in the immutable audit chain.
- behavioural: `window.*` mutating verb with `HiveCircuitBreaker` halted is fail-closed;
  per-agent rate cap enforced.
- integration: `SummonApp` + place verb only act on a real mapped handle (Phase-5 guarantee).
- regression: workspace switch still hides/shows SHELL panels when `com.hart.Compositor` is
  ABSENT (cage/Tier-3 path).
- smoke: advertised shortcut list (shortcuts panel) maps 1:1 to registered compositor globals at
  Tier-1/2; falls back to `keydown` at Tier-3.
- regression: the daemon's `should_yield` does NOT trip purely from HART-comp's own render CPU
  (governor self-throttle regression mirror); `charge_goal_work_completed` still fires (flywheel
  not starved).

**Never-break gates**
- No window op without guardrail + circuit-breaker + immutable audit; destructive geometry
  requires PREVIEW approval.
- Workspaces remain functional on cage Tier-3 (client-side) and degrade gracefully on sway
  Tier-2 (`swaymsg` shim).
- Foreground gate stays HTTP-driven + display-independent; compositor signal additive.
- Compositor render CPU cannot silently re-create the governor dormancy bug.
- 15-state `ActionState` FSM untouched; recipe CREATE/REUSE can bank `window.*` steps without
  altering recipe format/identifier semantics.

**verifyVia:** local QEMU-KVM (agent drives real windows) + CI `nixosTest` + unit
(governor/audit).

---

## Phase 7: L4 portals + REAL cross-process AI-senses screencast gate + theming

**Goal:** add Freedesktop portals for native apps AND make the screen kill-switch work
cross-process **before** any screencast surface exists, closing the NET-SECURITY-REGRESSION the
naive design would introduce. Theming becomes OS-wide via portal Settings (labeled **NEW**, not
"enhanced").

**Deliverables**
- Promote `core.ai_sensing` `'screen'` state to a real cross-process authority (owned UDS/D-Bus
  the portal MUST consult, fail-closed) **BEFORE** shipping screencast.
- `hart-portal.nix`: `xdg-desktop-portal-hart` backend (its own systemd unit + dbus policy)
  whose `org.freedesktop.portal.ScreenCast` handler calls `allowed('screen')` and refuses when
  cut; `wlr-screencopy` routed through the same gate so Flatpak/Wine capture is governed too.
  file-chooser/notifications/inhibit bridged to `NotificationService` + realtime fabric.
- `org.freedesktop.portal.Settings` bridges `ThemeService` `--hart-*` tokens so native GTK/Qt
  apps follow dark/light/accent, labeled a **NEW** capability + openRisk (the only thing
  preserved is shell-token application). `theme.changed` still drives in-shell apply on EVERY
  tier even with no portal.
- Real session lock: `ext-session-lock-v1` in HART-comp (input grab + blank app surfaces + PAM
  re-auth) for Tier-1/2; document that the Tier-3 cage lock is UX-only and that autologin
  bypasses PAM at boot, then close it (either disable autologin or add a PAM-backed kiosk lock
  so `loginctl lock-session` is not a silent no-op). LUKS2/TPM2 FDE unchanged.
- Add `hart-portal.nix` to flake `hartModules[]` + bundle accounting.

**Tests**
- integration: start the portal ScreenCast path with screen **DISABLED**, assert capture is
  **REFUSED** (not just flag set): a Flatpak/Wine screencast request denied when screen is cut.
- behavioural: `status()` reports `portal_screencast_blocked` / active-session count (observable,
  un-fakeable).
- integration: native GTK/Qt app follows HART theme via portal Settings; `theme.changed` still
  applies in-shell with no portal present (headless).
- `nixosTest`: `ext-session-lock-v1` blanks app surfaces + requires PAM re-auth at Tier-1;
  documented gap at Tier-3.
- `nixosTest`: a boot with autologin disabled (or PAM-backed kiosk lock): `lock-session` is no
  longer a no-op.

**Never-break gates**
- NO third-party screencast surface ships until the cross-process screen gate fails-closed and
  is tested. Never regress the no-native-capture invariant the cage floor gives for free.
- `ai_sensing` remains the single supreme gate; portal cannot re-enable senses.
- PAM/display-manager remains the true auth boundary at every tier; UX lock never over-claimed
  as security.
- `ThemeService` stays the single token source; never re-hardcode tokens.

**verifyVia:** CI `nixosTest` VM + local QEMU-KVM (portal screencast denial + PAM lock).

---

## Phase 8: Effects, polish, and full moat hardening behind the proven floor

**Goal:** land the visual/interaction effects, sway Tier-2 `swaymsg`-shim parity, meet-copilot
window-pinning (only after its dependencies are real), and final telemetry, all additive
behind the Phase-1 supervisor, none allowed to weaken any floor or constitution.

**Deliverables**
- Compositor effects (transitions, magnification, snap zones, animated ambient) gated behind
  software-render fallback so broken-GPU boxes degrade to flat, **never black**.
- Tier-2 sway parity: WM IPC `tile`/`summon` mapped onto `swaymsg` so the moat is
  reduced-but-present when HART-comp crashes; same glass shell as a layer-shell client;
  supervisor drop verified.
- meet-copilot transcript pinned beside the call window, gated behind a **REAL**
  `agent_voice_bridge` (currently a no-op stub) and the Phase-2 A2UI push fix; LiveKit
  egress-worker gap added to openRisks (server-side recording returns `ok:False` today).
- Tier-drop telemetry + operator dashboard surfacing current latched tier + reset path;
  recipe-banked window-layout REUSE (CREATE banks summon+tile+switch as `window.*` steps, REUSE
  replays the exact desktop).
- Final consolidation: `/api/shell/apps/*` vs legacy `/api/apps/*` onto ONE route surface
  preserving BOTH the `@_require_shell_auth` gate AND per-app permission endpoints AND
  detect/history/platforms, keeping `test_shell_route_no_collision.py` alive.

**Tests**
- `nixosTest`: effects ON with hardware GL disabled → flat software render, never black.
- integration: HART-comp crash → sway Tier-2 → agent tile via `swaymsg` shim still arranges
  windows (degraded moat present).
- behavioural: meet-copilot overlay only advertised/fired when `agent_voice_bridge` is real AND
  A2UI push works; otherwise it no-ops and says so.
- integration: CREATE banks a window-layout recipe; REUSE replays the exact summon+tile+workspace
  without LLM calls.
- regression: route-collision guard green after `/api/apps` consolidation; both auth gate +
  permission endpoints preserved.

**Never-break gates**
- Effects never block paint on broken GPU; software fallback always wins.
- sway Tier-2 keeps the desktop + A2UI overlays working when HART-comp is down.
- No capability advertised whose dependency (voice bridge, LiveKit egress) is a stub; each is
  gated or labeled an openRisk.
- Recipe format / identifier semantics / dashboard grouping unchanged by window-layout banking.
- Social/economy/channels organism + realtime fabric untouched throughout.

**verifyVia:** CI `nixosTest` VM + local QEMU-KVM (sway parity + effects software-render).

---

## The invariant verification loop

Every phase is proven on real-ish hardware via a two-tier loop, **never** on inline-render or
grep.

1. **CI `nixosTest` VM (authoritative gate):** each phase adds a `nixosTest` node built from
   `hartModules[]` alone (preserving the #70 eval-gate discipline), run on an llvmpipe/software-GL
   VM so the broken-GPU never-fail floor is exercised every run. Boot health is the
   dead-husk-aware check: a **real** `/shell/static` fetch (curl/`test_client`), not
   inline-render. Fault injection is first-class: loop-kill the active tier's binary and assert
   the out-of-process supervisor drops + latches to a tier that paints (never blank). The
   eval-gate canary fails loudly on any missing module so `hart-comp.nix` / `hart-portal.nix`
   additions can't silently break the ISO.
2. **Local QEMU-KVM (fast iteration):** `scripts/vm/boot-tier.sh` boots any tier headless with
   serial console for sub-CI-cycle debugging of compositor paint, XWayland app maps, portal
   screencast denial, PAM lock, and agent-driven window arrangement.
3. **Unit / behavioural (display-independent, runs on dev box + CI):** the brain-side controls,
   `agent_ui_update` guardrail + circuit-breaker + immutable-audit, ai_sensing cross-process
   screen gate, foreground-works-with-no-compositor, governor-doesn't-trip-on-compositor-render-CPU,
   `ServiceRegistry` non-None after boot, `SummonApp`-only-on-real-map, route-collision guard,
   all assert observable side-effects via mocks + call + observe, **never** string-survival.

**The loop order is invariant:** a phase is NOT done until its `nixosTest` is green on the
software-GL VM AND its supervisor fault-injection proves the floor still paints AND its
never-break gates pass. Only then does the next additive phase unlock behind the same watchdog.

---

## Hardware limit of the dev box

No Wayland compositor, KMS/DRM, libinput, `gtk4-layer-shell`, or Smithay/Rust build can be
compiled or booted on the Windows dev box. Every L1/L2 paint, software-GL fallback, XWayland,
portal screencast, and `ext-session-lock` test **must** run in a Linux CI `nixosTest` VM or local
QEMU-KVM. Windows-side work is limited to Python brain logic, nix expression authoring, and
unit/static guards. **No compositor C/Rust source is authored ahead of CI.**
