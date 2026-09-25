# NATIVE OS PROGRAM: every HART OS functionality, native, ultra-low-latency, no regression

Status: BINDING umbrella plan, created 2026-09-22 on the steward's instruction. It does not
replace any existing plan; it binds them together and adds what none of them covers: the
whole OS, one gate, one way of working for many agents in one clone.

Steward instruction (2026-09-22, verbatim): "each component of the OS and each functionality
shd be the best version out there in the world create parallel agents to get things done on
native rendering and adapt other components and functionality we have for Native out of the
world experience with ultra low latency and awesome UX". And: "no worktrees all in same clone
so that we always integration test all changes live together so that all regressions are
caught immediately".

Owner redirect that still governs (2026-09-02): "the only thing we worry about is the native
awesome fast low latency rendering and the rest of the functionality shd stay intact without
any regression."

## 0. Binding sources (read these before touching the area they own)

This program adds no second dialect, palette, transport, budget table or checklist. It points.

| Source | Owns | Status of record |
|---|---|---|
| `docs/architecture/NATIVE_SHELL_PARITY_PROGRAM.md` | the native shell lane: SceneNode = Nunba `ServerDrivenUI.jsx` vocabulary 1:1, the semantics trap (no easing between pointer and pixel), the split rule (chrome native, app surfaces stay Nunba microfrontends, WebView never deleted), M0..M6, THE BAR, the binding NFRs | M0..M3 landed, M4 partial, M5 open, M6 not flipped: all four pre-flip obligations closed in code, evidence on hardware pending |
| `docs/design/HOME_DESKTOP_DESIGN_CHECKLIST.md` | look, layout, feel; EMPHATIC rules a1..a4 (no page scroll, one-screen canvas), b1..b8 (spectrum, duotone teal 70 / violet 30, no em dashes), c (orb), d (cards), e (bars), f (interaction: taps must register), g, h (onboarding speaks via the ONE TTS path), i (agentic, 100x measured), j, k (never reinvent, parity-or-better on every micro detail), GL1..GL3 (glass windows) | audits recorded per group; GL2 Linux glass MISSING |
| `docs/architecture/HART_OS_FULL_DESKTOP_SPEC.md` | the 16 RULES and W1..W11; verification contract (self review, adversarial verify, behavioural tests, render proof, real hardware proof) | in force |
| `docs/architecture/latency_budgets.json` + `docs/architecture/LATENCY_HARNESS.md` | per component budgets (drag/hover/scroll/window-move/resize 16 ms, press/key 25 ms, animate-start 33 ms), the instrument contract, the journal line, anti-gaming rules, L1/L2/L3 | L1 built; L2, L3 unbuilt |
| `docs/architecture/NATIVE_SHELL_CSS_PARITY_LEDGER.md` | 93 components, 42 keyframes, 192 vars; rule 1 (absence is a recorded decision, never an oversight); cascade order is part of the spec | binding |
| `compositor/IPC_PROTOCOL.md` | `com.hart.Compositor`: envelope, verbs, events, the security boundary (fail closed, no phantom windows, senses supreme), `shell.native`, `shell.compose`, `shell.activate` | binding |
| `compositor/ROADMAP.md`, `docs/architecture/SESSION_TIER_CONTRACT.md` | never-blank ladder hart-comp > sway > cage, 3 crashes / 5 min, monotonic downgrade, one writer | binding |
| `docs/architecture/HART_OS_NATIVE_ARCHITECTURE.md` | the three invariants (compositor is a limb not a master; nothing built is lost; OS native but never a blank screen), the 12 preservation gaps, the mustNotLose contracts | binding |
| `docs/architecture/HART_OS_RUST_MIGRATION.md` | strangler fig, Python tests are the parity oracle, every op returns the same contract on every tier | binding |
| `core/constants.py::LATENCY_BUDGETS` + `tests/unit/test_latency_budgets.py` | the non-render budgets (chat 1.5 s, draft 300 ms, cache 1 ms, boot) with the no-orphan-budget guard | enforced |
| `docs/VERIFICATION.md` | the evidence ledger; a claim lives in the verified table or the unverified one | binding rules for evidence |
| `.hart-devenv/COMPOSITOR_PLAN.md` (machine local) | the owner redirect; C1 app-launch and C3 window drag DROPPED (keep working, no new investment) | binding |

## 1. Ground truth on 2026-09-22 (measured, not felt)

Box: Samsung NP550P5C, Ivy Bridge, 8 threads, HD 4000, 1600x900, generation 10 (rev d68d8ef).

| What | Measured | Budget | Source |
|---|---|---|---|
| press, WebView shell (owner's own clicks) | p50 122 ms, p99 209 ms, max 238 ms | 25 ms | journal `hart-latency` this boot |
| hover, WebView shell | p50 22 ms, p99 65 ms | 16 ms | same |
| key | p50 16 ms | 25 ms | same (PASS) |
| press, same box, daemons paused | p50 12.4 ms, p99 18.2 ms | 25 ms | journal, synthetic clicks |
| CPU clock under the agent load | 1.3 to 1.5 GHz of 3.4, 440 throttle events, 94 C | | `/proc/cpuinfo`, thermal zone |
| CPU clock with hart-agent-daemon + copilot paused for 120 s | 3.19 GHz, 84 C, load 1.9 | | same |
| llama-server | ~207 percent CPU, one chat completion a minute, ~280 token prompts | | `hart-llm` journal |
| driver of that load | hart-agent-daemon STARVATION OVERRIDE (yield gate says `model_pressure`, override force-ticks every 120 s) | | `agent_daemon.py:1391` |
| why the override thinks nobody is there | `is_user_recently_active()` = chatted recently; governor's Linux idle backend = `xprintidle` only (X11), returns None on Wayland | | `dispatch.py:493`, `resource_governor.py:1001` |
| idle repaint floor | 200 ms `IDLE_HEARTBEAT`, measured as the 218 to 225 ms max | | `main.rs:586`, VERIFICATION.md:124 |
| WebKit main thread from CSS animation | 98 percent of a core; 98 to 55 after the gate fix; `lg-pulse` still 33 points | | VERIFICATION.md:95 |
| agentic UI push | ~440 ms, of which ~396 ms `immutable_audit_log.log_event`, ~206 ms one synchronous sqlite commit | | LATENCY_HARNESS.md:132 |
| shell idle HTTP | ~18 GETs per 5 s observed; ~4.6 per 5 s per document accounted for from source, so roughly four shell documents are polling | | box sniff, hot-path inventory |
| native scene layer cost | layout p99 219 us, 1.3 percent of a frame; retained hit 0 us | 16.6 ms | scene.rs, VERIFICATION.md:63 |
| native scene ON, both renderers drawing | top-bar 25.4/46.4, omnibox 18.0/33.3, orb 24.0/46.9, home-card 31.1/48.8, taskbar 25.4/38.8 (p50/p99 ms); all FAIL | 16 ms | VERIFICATION.md:123 |

Three conclusions that shape the order of work:

1. The largest costs are OFF the render path: CPU starvation by background inference on a
   thermally throttled box, the idle heartbeat, the WebKit main thread, the audit commit. The
   compositor's own scene layer is already two orders of magnitude inside budget.
2. Half the declared budgets have never produced a sample (press, key, scroll, animate-start
   are unmeasured on hardware until this week; window-move and resize are structurally
   exempt), and the two tightest NFRs (frame time p99 < 12 ms, p99.9 zero dropped frames)
   have no instrument at all. Instruments come first or every later claim is a feeling.
3. The gates that exist are partly aspirational: `compositor/tests/smoke_e2e.rs` is the only
   end to end proof and no workflow runs it; the nixosTests fleet has no recorded passing run;
   ~963 root level Python tests and four `.mjs` files run in no CI job; `security-scan.yml`
   cannot fail. A program that promises no regression must first make the gate real.

## 2. Rules of engagement (many agents, one clone, live integration)

1. One clone: `C:\Users\sathi\hartos-reflash`, branch `main`, no worktrees, no branches.
   Every change integrates against every other change the moment it is written.
2. Disjoint ownership per stream (section 4). An agent edits only the files its stream
   owns; a needed change in another stream's file is a message to that stream, never a
   silent edit. Shared files (`comp_core.rs`, `scene.rs`, `liquid_ui_service.py`) have
   region owners named in section 4.
3. Read the whole file before editing it. Check for an opposing test before changing a
   behaviour: the deliberately arranged test states the contract.
4. No parallel paths. Extend the existing implementation, transport, palette, budget table,
   checklist. If something looks missing, grep twice and read the owning doc before adding.
5. Every change ships with tests on both sides of any language boundary it touches, and the
   guard reads the bound rather than restating it.
6. The gate before every commit, in this order: the stream's own test files; the cross
   language guards (`tests/unit/test_native_wire_contract.py`,
   `tests/unit/test_panel_reservation.py`, `tests/unit/test_latency_budget_coverage.py`);
   the compositor suite on deepbox for any Rust change
   (`python .hart-devenv/deepbox-check.py test --features winit --all-targets` and
   `--features smithay`; the loop is locked, so concurrent agents serialise); the full
   Python file the change belongs to. Windows skips the `AF_UNIX` and `.mjs` tests, so a
   change to either must also be run on Linux (deepbox container, see memory
   hartos-release-gate-red-since-0911) before it is called green.
7. Commit only your own files, `git -c core.autocrlf=false commit`, message says WHY in
   prose, no em dashes anywhere. Retry on an index lock. Do not push: the coordinator
   pushes after the gate, and a push to `nixos/`, `compositor/` or `claw_native/rust/`
   cancels the closure build another stream may be waiting on.
8. Budgets: a component with no budget fails the build; a budget may be raised only with a
   justification in the same commit; every milestone journals its measured fps and
   input-to-photon on the box or it does not close. "Feels fine" is not evidence.
9. The box (`192.168.0.69`, `.hart-devenv/box-run.py`) is a shared production target: read
   freely, change nothing without the coordinator (no OTA, no reboot, no service restart).
   The coordinator stages OTAs with the procedure in memory hartos-ota-proven.
10. Never simplify to dodge complexity not yet understood (feedback memory
    ai-native-os-bar). Never disable a component to make a number.

## 3. Per-functionality specification

Each row: what the user has today; the native, low-latency target; the binding rules; the
regression gate that must stay green; the new gate the work must add; the milestone.

### 3.1 Shell chrome: top bar, taskbar, start menu, tray, clock, notifications

| | |
|---|---|
| Today | WebView DOM in the inline shell (`liquid_ui_service.py`), `hartConnectivity.js` tray, `hartDock.js` magnification, `mako` for native notifications. Native twins exist as EMPTY strips (`scene.rs` TopBar/Taskbar) plus the omnibox pill; the compositor claims `bloom,orb` (and `home` when the scene is on), the shell keeps its bars. |
| Target | Bars, tray, clock, start menu and notification toasts drawn by hart-comp from a `shell.chrome` payload the shell already computes (one producer: liquid-ui; one contract: `shell.compose` sibling verb), claimed on `NATIVE_CHROME` only when drawn fully, shell stands down per claim exactly as it does for bloom/orb. Toasts and context menus as native scene nodes so their `animate-start 33 ms` budget is measurable. Hover and press attributed per component. |
| Binding | checklist e1 (restructure `.top-bar`, no second bar), e2 (omnibox is THE search surface), e4 (taskbar previews, live progress), e5, b1.2 duotone, b6; parity program obligation 3 as reframed (claim whole bands only; over-claiming costs an empty desktop); ledger rules 1 and 2; IPC_PROTOCOL 4.10 events. |
| Keep green | `scene.rs` bar tests, `comp_core.rs` reservation and claim tests, `tests/unit/test_native_chrome_bridge.py` (23), `test_panel_reservation.py`, `test_native_shell_bloom_wiring.py` (14), `test_liquid_ui_shell_panels.py+.mjs`, `test_shell_desktop_tap_menus.py+.mjs`, `nixos/tests/layer-shell-host.nix`. |
| Add | `shell.chrome` wire fixture pinned from the real producer (same pattern as `wire_fixture.rs`); headless pixel proofs for tray glyphs and clock text; a scene component for toast and context-menu so the budget rows stop being unattributable; the claim test that a partially drawn bar is never claimed. |
| Milestone | N3 (after instruments and CPU headroom). |

### 3.2 Home and desktop: cards, hero, orb, bloom, icons, drag, context menus

| | |
|---|---|
| Today | `hartHome.js` DOM composed by the local LLM over A2UI `home_compose`; native scene renders the SAME payload via `shell.compose` (hero, rows, cards, icons, meta, progress, ranked rows, stroked text) and relays presses back as `shell.activate`. Card photos decoded and dropped; `mood` decoded and unread; theme is a `OnceLock`; `HERO_H` open; `prefers-reduced-motion` unbuilt. Desktop icons and drag are WebView (`hartDesktop.js`). |
| Target | M6 flipped: the native scene is the home, the WebView stands down for `home`, one executor for a card press. Card art: decision (a) compositor renders the 51 bundled SVGs with `usvg` + `tiny-skia` at compose time (gradients only, no filters), cached per (path, size); `mood` resolved by the shell sending resolved colours over the existing `shell.compose` (no second palette table in Rust); theme hot reload; icons as scene nodes with drag written from the input event the same frame (semantics trap rule 2). |
| Binding | parity program sections on wire contract, split rule, semantics trap; checklist a1/a2 (fixed canvas), b1, c (orb: breathing default on, never a mic), d (cards, real money), d7/k7 privacy; ledger rule 3 (only `--hart-*` is live swappable); no `.hh-usd` fabrication (`usd_equiv` never on the wire). |
| Keep green | `scene.rs` (66), `comp_core.rs` native render tests (105), `orb.rs`, `bloom.rs`, `test_native_wire_contract.py` (7), `test_home_producer.py` (30), `test_home_compose_feed.py` (24, includes the re-listen tests), `test_home_design_fidelity.py`, `test_hart_home_no_fabricated_data.py+.mjs`, `test_shell_desktop_icons.py` (15), `test_native_orb_stands_down.py+.mjs`. |
| Add | second and third wire fixtures from the real producer (ranked row, photo card, mood set) so the contract is not one payload; `mood` end to end test (shell resolves, compositor paints, pixel proof); SVG decode proof for every bundled asset; a desktop icon drag test asserting the transform is applied on the same frame as the input (no easing); on box A/B with the WebView demoted (VERIFICATION rows 19, 24). |
| Milestone | N2 (mood, photos, hot reload, HERO_H decision) then N4 (M6 flip on evidence). |

### 3.3 Windows and apps: panels, iframes, native toplevels, tiling, glass

| | |
|---|---|
| Today | Panels are DOM (`openPanel` registry, drag/resize/snap in JS); Nunba microfrontends are iframes; native toplevels managed by hart-comp verbs (list/focus/place/tile/move/workspace); `window.summon` complete but unreachable (C1 dropped); floating glass windows: Windows reached natively, Linux MISSING (GL2). `wlr-screencopy` unimplemented on the DRM backend. DrmCompositor damage race (bloom flashing through a dropped frame) open. |
| Target | Native glass on HART OS: hart-comp renders the translucent blurred backdrop for floating toplevels and shell panels (GPU shader on GLES, one-time raster on the pixman floor, degrade ladder per GF1/GL2). Panels that are Nunba microfrontends stay iframes inside the WebView (split rule) but their chrome (title bar, controls, snap zones) becomes native so drag and resize are one frame. Damage race fixed. Screencopy implemented on the DRM backend behind the `screen.kill` gate so frame capture can verify pixels (VERIFICATION row 23). |
| Binding | split rule; IPC_PROTOCOL security boundary (fail closed, preview for destructive geometry, no phantom windows); checklist f5 (dockable multi window, snap zones), b1.4 (solid window controls), GL1 (proven by screen pixels over a bright backdrop, never by an API return), GL2 (native GPU glass), GL3 (one look definition, `hartGlass.js` + `theme_service.py`); ROADMAP P5/P7/P8 gates; C1 and C3 remain DROPPED. |
| Keep green | `comp_core.rs` tile/zone/maximize (14), `main.rs` summon FSM, `ipc.rs` (23), `test_phase5_native_windows.py` (32), `test_hart_wm_client.py` (26, Linux for the socket half), `test_window_layout_recipe.py` (12), `nixos/tests/native-subsystems.nix`, `hart-app-install-verify.nix`, `portal-screencast.nix`. |
| Add | `compositor/tests/smoke_e2e.rs` RUN in CI (a Linux job with a nested host, `-- --ignored`); tests for `wayland.rs` handler bodies (0 today); a glass pixel proof over a bright backdrop on the box (the GL1 rule); a damage-race regression test (a dropped frame never exposes the layer below); screencopy consent test on DRM. |
| Milestone | N3 (damage race, screencopy, smoke_e2e in CI), N5 (native glass and native panel chrome). |

### 3.4 Input: pointer, keyboard, chords, touch, IME, voice push-to-talk

| | |
|---|---|
| Today | libinput to hart-comp; chord map tested; wheel notch 120 px; touch only at the WebView level (no `TouchDown` handling in `udev.rs`); IME via fcitx5; OSK squeekboard; latency instrument attributes per component; `input-alive` marker written once per boot. |
| Target | Every input kind measured on hardware against its budget (press, key, scroll, animate-start today have samples only from this week); touch and gestures handled by the compositor and attributed; `input-alive` becomes a rate limited heartbeat that the governor reads (one OS-agnostic idle detector: Windows `GetLastInputInfo`, macOS `ioreg`, Linux the heartbeat); an input event wakes the render loop immediately (no 200 ms idle floor between a click and its frame). |
| Binding | LATENCY_HARNESS anti-gaming rules; checklist f1 (taps must register), f2 (click semantics), a4 (touch workspaces); the semantics trap. |
| Keep green | `comp_core.rs` pointer/chord/wheel tests, `latency.rs` (25), `scene.rs` scroll extent tests, `test_keyboard_focus.py` (12), `nixos/tests/input-seat-pointer.nix`, `test_latency_budget_coverage.py` (9). |
| Add | L2: a nixosTest that replays uinput and asserts p50/p99 per kind on llvmpipe; a frame-time histogram in the compositor with a journaled violation counter (the p99 < 12 ms NFR has no instrument); touch and IME tests; the heartbeat test (rate limited, mtime moves under input, still once-only journal line); governor Linux backend test with a temp marker; override-honours-governor test. |
| Milestone | N1 (heartbeat, governor, override, idle wake) and N1 (instruments). |

### 3.5 Voice: STT, TTS, orb as the control

| | |
|---|---|
| Today | WebView `getUserMedia` raced against an 8 s timeout (kiosk hang class), `POST /api/voice`, Model Bus STT/TTS, six TTS engines with a fallback ladder, browser speech synthesis for instant feedback then server audio; `hart-voice-listener` unit is a sleep stub. |
| Target | M5: click to talk capture and playback through PipeWire from the native side (the parity program's P8), retiring the GStreamer-in-WebKit path that segfaulted; the orb energy fed from the same capture; barge-in preserved; one TTS path (checklist h2). |
| Binding | parity program P8; checklist c5 (no mic in the orb), c6 (realtime), h2; k7 privacy (audio never leaves the device without consent). |
| Keep green | `test_tts_router.py` (43), `test_stt_no_speech_gate.py` (28), `tests/functional/test_tts_fallback_ladder.py` (25), `test_shell_audio_api.py`, `test_voice_endpoints.py`, `test_layer_shell_host_portal_mic.py`, `nixos/tests/audio.nix`. |
| Add | a VM test that boots the voice stack end to end (mic to STT to agent to TTS) with a null sink; `senses-mic` press budget measured on the box. |
| Milestone | N5. |

### 3.6 Onboarding, lock, session

| | |
|---|---|
| Today | `hartOnboarding.js` (a copy of Nunba's canonical `LightYourHART.js`), native GTK4 fallback, lock screen in `hartSessionUI.js` with a dev-stub unlock and a PAM path via logind; the first-run password prompt is a one-shot check 4 s after session ready and its skipped flag is written when OFFERED, not when declined (found 2026-09-22). |
| Target | Onboarding built from the canonical Nunba source (parity program P9, NUNBA_NATIVE_DAEMON_PLAN E), rendered natively with the same perf bar as the desktop; lock screen native (budgets `key 25`, `animate-start 33` already declared) with PAM behind it; password prompt: flag on explicit decline, re-check when onboarding ends. |
| Binding | checklist h1..h3; parity program P9 (retire the copy, not immortalise it); SESSION_TIER_CONTRACT; HART_OS_NATIVE_ARCHITECTURE 7.7 (lock under autologin). |
| Keep green | `test_hart_onboarding.py` (62), `test_session_manager.py` (31), `test_onboarding_companion.py+.mjs`, `test_onboarding_reveal_latency.py`, `test_shell_lock_fouc_and_nav.py`, `test_onboarding_palette_guard.py`, `nixos/tests/session-supervisor.nix`, `desktop-boot.nix`, `floor-lock.nix`. |
| Add | drivers for the two orphan `.mjs` tests (`test_onboarding_skip_keyboard.mjs`, `test_onboarding_speak.mjs`); a test pinning the bounded 5 s x 90 probe retry from e34486a; the password-prompt tests (flag on decline only; re-check on onboarding end). |
| Milestone | N2 (password prompt, orphans) then N6 (native onboarding and lock). |

### 3.7 Settings and connectivity

| | |
|---|---|
| Today | Flask routes under `/api/shell/*` with route level tests (141); UI is DOM panels; tray cluster polls a consolidated summary every 8 s; seven manifest panels have an API and no frontend. |
| Target | No native rewrite of Settings content (application surfaces stay microfrontends per the split rule). What goes native: the tray and quick-settings popover (3.1) and the customization hub's live theme apply (3.2 hot reload). The polling storm is replaced by SSE pushes from the connectivity cache that already exists (`_ConnectivityCache`), so the idle shell issues near zero HTTP. |
| Binding | checklist e6, j1..j4 (customization is an API); OS_PARITY_CLOSURE_PLAN closure criteria. |
| Keep green | the 141 route tests, `test_shell_connectivity_cache.py`, `test_shell_connectivity_inflight.py+.mjs`, `nixos/tests/network-wifi.nix`, `display-management.nix`. |
| Add | an idle HTTP budget for the shell (GETs per minute per document) enforced by a test over the served scripts, and the SSE push test. |
| Milestone | N2 (poll diet), N3 (native tray). |

### 3.8 Agents: daemon, goals, copilot, world model, consent, computer use, kids, books, social

| | |
|---|---|
| Today | Python agent engine (stays Python per the Rust migration boundary); A2UI overlays rendered in the WebView; the daemon's override starves the desktop; agentic push costs ~440 ms of which ~396 ms is the audit commit. |
| Target | Agents never starve the person at the desk (3.4 governor + override); A2UI components render natively where they are chrome (toast, notification, approval card) through the SAME `agent_ui_update` gate, never around it; the audit write moved off the paint path while remaining durable and ordered (append to a queue that is fsynced before the NEXT gate decision; the gate stays fail closed) so an agent can repaint at frame rate. Everything else stays as is. |
| Binding | IPC_PROTOCOL 6.1 (the gate must gain guardrail + audit + sanitisation before native chrome is layered on it); ROADMAP P2; checklist i1..i4; HART_OS_NATIVE_ARCHITECTURE mustNotLose contracts (foreground gate HTTP driven, ai_sensing supreme). |
| Keep green | the ~1,246 agent tests, ~509 VLM tests, `test_a2ui_gate_hardening.py`, `test_a2ui_guardrail_audit.py`, the 571 consent and security tests, `tests/functional/test_security_modules_functional.py` (146). |
| Add | the audit-off-hot-path test (a push returns in < 16 ms wall while the audit row is durable before the next push is admitted); the override-honours-governor test (3.4). |
| Milestone | N1 (starvation), N4 (audit off the hot path: steward decision, see section 6). |

### 3.9 System: OTA, tiers, watchdogs, telemetry, latency instrument, CI

| | |
|---|---|
| Today | OTA staged via `nixos-rebuild boot`, fleet cache on deepbox, tier ladder proven on hardware, latency instrument proven; CI gate red on every run until 2026-09-21; `smoke_e2e.rs` never run; nixosTests no passing run; ~963 root tests ungated; security scan advisory only. |
| Target | The gate is real: release-blocking Python shards green; `smoke_e2e.rs` in CI; nixosTests fleet with a recorded passing run; root tests either gated or deleted with a reason; security scan able to fail on a CVE'd pin; a frame-time instrument; L2 latency replay in CI; the OTA procedure documented and scripted (pause daemons, stage, promote, verify by content). |
| Binding | ROADMAP verification loop; VERIFICATION.md rules; the release gate is tag-and-sign's dependency. |
| Keep green | `test_ota_*`, `test_nixos_*` (~1,002), `main.rs` render floor tests, `udev.rs` (28), the OTA VM tests. |
| Add | the CI changes above; `docs/VERIFICATION.md` rows 19, 20, 23, 24 moved to the verified table with numbers. |
| Milestone | N0 and N1. |

### 3.10 Security: consent gates, kill switches, audit

| | |
|---|---|
| Today | fail-closed window verbs, senses supreme, `screen.kill` one flag, master key, immutable audit chain, boot trust manifest (hashes only `.py`). |
| Target | Unchanged semantics. Native chrome changes nothing about who may do what: every new verb (`shell.chrome`, screencopy on DRM) passes the same gate order and is audited; the compositor binary joins the trust manifest hash set. |
| Binding | IPC_PROTOCOL section 6 and 9; HART_OS_NATIVE_ARCHITECTURE 8. |
| Keep green | all consent and security tests, `nixos/tests/security.nix`, `portal-screencast.nix`, `notify.nix`. |
| Add | a VM proof that `full_boot_verification` gates hart-comp on its own signature. |
| Milestone | N3 alongside screencopy. |

## 4. Streams and ownership (parallel agents, one clone)

Each stream is one agent at a time. Shared files carry region owners. Rust streams verify
through the locked deepbox loop and therefore serialise on the build, not on the edit.

| Stream | Owns (files, regions) | First deliverables |
|---|---|---|
| S1 CPU headroom | `core/resource_governor.py` (`_get_idle_ms_linux`), `integrations/agent_engine/agent_daemon.py` (the override block only), tests for both; `nixos/modules/hart-llm.nix` + `hart-kernel.nix` slice weights (session above inference: compositor and liquid-ui CPUWeight above `hart-agents.slice`) | governor reads the input heartbeat; override yields while the governor says active; llama below the session in CPU weight; measured on the box: clock and press p50 with the daemons running |
| S2 Compositor latency | `compositor/src/main.rs` (scheduler, IDLE_HEARTBEAT), `udev.rs` (frame scheduling, damage race), `comp_core.rs` region: `note_input_alive` + frame-time histogram | input wakes the loop the same tick; frame-time histogram + violation counter journaled; damage race fixed with a regression test; `input-alive` heartbeat |
| S3 Native chrome | `compositor/src/scene.rs` regions TopBar/Taskbar/Tray/Toast/ContextMenu, `ipc.rs` (`shell.chrome`), `integrations/agent_engine/hart_wm_client.py` (`shell_chrome`), `liquid_ui_service.py` region: the chrome producer next to `_push_home_to_native_scene`, IPC_PROTOCOL.md new section, wire fixture | `shell.chrome` contract + producer + native bars drawn from it, claimed per band |
| S4 Native home completion | `scene.rs` regions hero/cards/art, `comp_core.rs` lowering region (Image), `bloom.rs` (mood), theme loading (hot reload) | SVG card art, mood resolved via the shell, theme hot reload, HERO_H proposal for the steward |
| S5 Windows and glass | `wayland.rs`, `screencopy.rs` (DRM), `comp_core.rs` region: toplevel decoration/glass lowering, `compositor/tests/smoke_e2e.rs`, `.github/workflows/flake-checks.yml` (the nested-host job only) | screencopy on DRM behind `screen.kill`; smoke_e2e running in CI; native glass backdrop for floating toplevels with a pixel proof on the box |
| S6 Shell hot-path diet | `integrations/agent_engine/static/*.js` polls and closers, `liquid_ui_service.py` regions: inline pollers, SSE consumer, start menu closer, `_ConnectivityCache` push; tests | idle HTTP near zero via SSE; sheets close on window blur through the context menu's idiom; password prompt fix; `lg-pulse` cost; idle HTTP budget test |
| S7 Agentic push latency | `security/immutable_audit_log.py`, `liquid_ui_service.py::agent_ui_update` region, `core/foreground.py` | audit durable off the paint path (design first, steward decision, then code) |
| S8 Gate reality | `.github/workflows/*.yml` (except S5's job), `tests/*.py` root, orphan `.mjs` drivers, `nixos/tests/` fixes for a green fleet run | ungated tests gated or retired with a reason; security scan can fail; nixosTests green run recorded |
| S9 Voice native | `nixos/modules/hart-liquid-ui.nix` voice listener, a PipeWire capture path, `voiceOrbViz.js` energy source | M5 |
| S10 Onboarding and lock native | `hartOnboarding.js` retirement per NUNBA_NATIVE_DAEMON_PLAN E, `hartSessionUI.js`, native lock scene | N6 |

Coordinator (this session): pushes after the gate, stages OTAs, runs the on-box A/B after
every milestone, keeps `docs/VERIFICATION.md` and this document current, resolves ownership
collisions, and writes the steward decisions into the checklist when they arrive.

## 5. Milestones and their evidence

A milestone closes only with a journal line from the box and a row in
`docs/VERIFICATION.md`. Order is by measured leverage, not by glamour.

| Milestone | Contents | Closes when (evidence on the box) |
|---|---|---|
| N0 Gate reality | S8 first pass: shards green on main, smoke_e2e in CI, root tests gated or retired, security scan can fail | release run green end to end with tag-and-sign reached; smoke_e2e passing in CI |
| N1 Headroom and instruments | S1 (governor, override, weights), S2 (idle wake, heartbeat, frame-time histogram, damage race), L2 replay | with the daemons RUNNING: press p50 < 25 ms and hover p50 < 16 ms on the WebView shell; clock not throttled below 2.5 GHz at idle desk use; frame-time p99 journaled; damage race test green |
| N2 Home completion and shell diet | S4 (mood, art, hot reload), S6 (poll diet, blur closers, password prompt), orphans gated | idle shell HTTP < 1 GET per 10 s per document; mood visible natively; art drawn; theme change without restart |
| N3 Native chrome and capture | S3 (`shell.chrome`, native bars, tray, toasts, context menu), S5 screencopy on DRM, compositor in the trust manifest | native bars claimed on the box; toast and context-menu budgets have samples; a screen capture under Tier-1 proves pixels |
| N4 M6 flip | the WebView stands down for `home` and the bars; native scene default ON | the same five-point sweep, same boot, native alone: every component p99 within budget; VERIFICATION rows 19 and 24 verified; a week on the box with zero failed units |
| N5 Windows, glass, voice | S5 native glass and panel chrome, S9 voice via PipeWire | glass pixel proof over a bright backdrop on HART OS; drag and resize of a panel one frame; mic to TTS with no WebKit capture |
| N6 Onboarding and lock native | S10 | onboarding and lock rendered by the compositor, budgets measured, canonical source retired |

## 6. Decisions only the steward can make (recorded, not re-decided here)

1. The audit commit on the agentic push path (~396 ms synchronous). Proposal: durable
   append with fsync before the next gate decision, off the paint path. A security decision.
2. Card art: (a) compositor renders the bundled SVGs, or (b) the shell hands decoded pixels
   over a binary channel. This document assumes (a).
3. `mood`: the shell resolves palette ids to colours and sends them over `shell.compose`
   (no Rust copy of `HART_PALETTES`). This document assumes yes.
4. `HERO_H`: a separate floating `HERO_ORB_D = 300` with the band height from content. A
   visual call for the box.
5. GL3 capture-and-blur as the universal glass path versus the `screen_capture` consent it
   would require. On HART OS the compositor owns the pixels, so Linux glass needs no
   capture; the question remains for Windows and macOS.
6. The Google Fonts CDN link in the shell head (render blocking on first paint).
7. C1 app launch and C3 window drag stay DROPPED unless re-opened.

## 7. Definition of done for any change under this program

1. The regression gate for its area (section 3, "Keep green") passes on the platform that
   can run it, and the cross-language guards pass.
2. The new gate it promised (section 3, "Add") exists and fails when the change is reverted
   (mutation proven where the harness exists).
3. Its budget line, if it touches input or paint, has a measured sample on the box after
   the next OTA, recorded in `docs/VERIFICATION.md`.
4. The checklist audit for any design touch is updated in the same change (CLAUDE.md
   binding rule).
5. No new parallel path: a reviewer can name the existing implementation it extended.
