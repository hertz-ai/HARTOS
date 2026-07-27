# HART OS — Native Architecture: Validation & Test Report (v2)

> **Companion to** [`HART_OS_NATIVE_ARCHITECTURE.md`](HART_OS_NATIVE_ARCHITECTURE.md)
> (the intent-first v2 architecture + phased build plan) and
> [`../../compositor/ROADMAP.md`](https://github.com/hertz-ai/HARTOS/blob/main/compositor/ROADMAP.md).
>
> **Purpose:** This is the adversarial validation of the v2 architecture against the
> actual tree, plus the real test-suite run. Every claim below was checked by reading
> code, parsing ASTs, or running pytest — not by trusting the design doc. It records
> what is **confirmed-solid**, what **must be fixed before building**, the **test
> results**, and the **AI-native grade**.
>
> **Verdict in one line:** the v2 diagnosis is **correct and sharper than v1**, its
> open risks are carried rather than quietly closed, the never-fail ordering is sound
> — but as *landed* the system is **still Linux + an agent at the OS face**, and three
> concrete brain-side moves (all dev-box-authorable today) are what convert it to
> AI-native. **Grade: 5/10 as-landed, with a validated 4.5 → 7 path that does NOT
> require the compositor.**

---

## 1. Executive summary

v2's central thesis — *intent must be the primary operating surface, and the one wire
that lets the brain compose the desktop is physically severed* — is **confirmed in
code, three independent ways**. The plan's never-fail ordering (freeze the cage floor →
build the out-of-process tier-drop supervisor → close the A2UI/audit/ai_sensing gaps →
only then any compositor) is buildable and correctly sequenced. The landed safety
prerequisite (commit `8cc4e95`) is accurately described and **passes its own behavioural
tests today**.

The gap is **execution scope, not truthfulness**. The AI-native primitives
(`should_yield_to_user`, Recipe CREATE/REUSE, the 33-rule guardrail kernel, the
autonomous daemon, the AI-sensing kill-switch) are all **real and display-independent** —
they would run identically on vanilla Ubuntu. The moat that beats "Linux + Copilot"
(agents owning native window-placement policy via `com.hart.Compositor`) is **0 lines of
production code** and correctly deferred behind sway-as-Tier-1. v2 is therefore an
**accurate PLAN to become AI-native**, not yet a description of a system that **is**.

The single highest-leverage correction folded into the v2 architecture doc: the orb/
command-bar (`acSend`) is graded **M (Migrated)**, not E (Enhanced) — because the surface
must be **inverted** from "string-match-or-text-bubble launcher" to "intent → decompose →
compose," and that inversion is a real owed deliverable, not a preserved pipeline.

---

## 2. Overall confidence

| Dimension | Verdict | Score |
|---|---|---|
| Claims grounded + nothing lost | minor-gaps | 8/10 |
| Plan buildable + tests real + never-fail ordering | minor-gaps | 8/10 |
| **AI-native bar (does v2 reach 7+?)** | **minor-gaps** | **5/10 as-landed; 7 is a validated path, not a current state** |

**What is validated:** the diagnosis, the landed gate, the never-fail ordering, every
carried open-risk, the migration-map dispositions for every named
capability, and the buildability of Phases 0–2 + the brain-side parts of 6 + 8.

**What is at-risk / unproven:** the never-blank-screen guarantee (depends on a net-new
out-of-process supervisor, untested until VM); the first-ever Rust-in-Nix build under the
June-2025 pin (may not resolve); the GTK3→GTK4 layer-shell host-window port (re-litigates
the software-GL floor); and the AI-native grade itself, which **rises with code, not with
the doc** — moves (1) transport-fix + (2) `acSend`-inversion + (3) wire the dead
screen-kill-switch are what cross 4.5 → 7 on the existing shell.

**AI-native grade for v2: 5/10 as-landed.** The compositor is the 7 → 9 upgrade;
the 4.5 → 7 gate is three brain-side moves, none of which require the compositor.

---

## 3. Confirmed-solid (validators verified against code)

Each row was checked by reading the cited file or running an AST parse / pytest.

| # | Claim | Evidence (file:line) |
|---|---|---|
| C1 | **The A2UI push channel is severed THREE independent ways.** | (1) `core/platform/service_registry.py` **does not exist** (only `core/platform/registry.py`); the import `from core.platform.service_registry import ServiceRegistry` is dead at `agentic_router.py:632`, `agent_voice_bridge.py:579`, `channels/agent_tools.py:269/836`, `model_orchestrator.py:880`. (2) `registry.py:152` `def get(self, name)` is an **instance** method, so `ServiceRegistry.get('LiquidUIService')` (called at `agentic_router.py:634`, and **even** at `hart_intelligence_entry.py:2680/2771/3150/3651/3690/3930` which uses the *correct* import) binds the name to `self` → `TypeError`. (3) `register('LiquidUIService', …)` appears in **zero** production files — only `_probe_conversational_onboard_e2e.py:74` + tests. **Sharper than v1:** even the "working" caller is broken by the class-vs-instance bug. |
| C2 | **Landed gate `8cc4e95` gates `agent_ui_update` on the hive kill-switch + immutable audit, fail-OPEN on guardrail import error.** | `liquid_ui_service.py:394–470`: `HiveCircuitBreaker.is_halted()` refusal (`421–426`), `get_audit_log().log_event('a2ui_push', …)` (`438–442`), both wrapped `try/except: pass` (fail-OPEN). `git log -1 8cc4e95` = the exact title. `tests/unit/test_a2ui_guardrail_audit.py` has exactly 6 behavioural tests. |
| C3 | **The screen kill-switch is a DEAD FLAG.** `core.ai_sensing 'screen'` has **zero** enforcing consumers; `frame_capture`/`window_capture` do not check it. | `core/ai_sensing.py:23` `_state = {'mic','camera','screen'}`; grep for `ai_sensing`/`allowed('screen')` across `integrations/remote_desktop/` and `integrations/vision/` = **ZERO hits**. The module docstring even admits screen is only *"gated for any consumer that honours it"* — and none do. Only mic (`/api/voice`) and camera (vision-stop) are actually enforced. |
| C4 | **`GuardrailEnforcer.before_dispatch`, server-side sanitization, and a per-agent rate cap are STILL OWED** inside `agent_ui_update`. | `before_dispatch` EXISTS (`hive_guardrails.py`) and is called fail-CLOSED at `dispatch.py:670`, plus `agent_daemon`/`agentic_router`/`worker_loop` — but **not** inside `agent_ui_update` (`liquid_ui_service.py:394–470` only does kill-switch + audit). No sanitize/rate-cap code there. |
| C5 | **`acSend` is a string-match-or-text-bubble launcher — the surface is NOT inverted.** | `liquid_ui_service.py:3751–3796`: `open X`→`openPanel` (launcher spine), theme-words→preset, else POST `/api/agent/ask`→`agent_ask` (`4610–4627`) → `/chat` with hard-coded `prompt_id='desktop_agent'` → `typing.textContent = reply; speakText(reply…)` (text bubble + TTS). `hartHero.js:104` dims the orb when any panel opens. **No path composes the desktop from intent.** |
| C6 | **Zero compositor source authored ahead of CI.** | grep `HartWmClient` / `com.hart.Compositor` / `place_window` / `def summon_app` / `def tile_window` across production `*.py` = **EMPTY**; all only in `compositor/ROADMAP.md` + `IPC_PROTOCOL.md`. Spec-only, as claimed. |
| C7 | **Smithay HART-comp would be the FIRST `buildRustPackage` in this tree.** | grep `buildRustPackage`/`rustPlatform`/`cargoHash`/`cargoLock` across `nixos/` = **ZERO**. `claw_native`/`hevolvearmor` referenced by **zero** `.nix`. `claw_native/rust/Cargo.toml` **does** exist (so "land the precedent on an existing crate first" is real). Confirms sway-as-Tier-1-now / Smithay-as-later-moat. |
| C8 | **`node_watchdog` cannot own the tier-drop; the supervisor is NET-NEW.** | `node_watchdog.py:89` `self._thread: Optional[threading.Thread]` — in-process thread supervisor only, zero subprocess/systemctl/session code. grep `session-tier`/`hart-session-supervisor`/`reset-tier` across `nixos/`+`integrations/`+`core/`+`security/` = **ZERO** (correctly unbuilt). `greetd` already packaged for `phone.nix:142` → real precedent, lowers Phase-1 risk. |
| C9 | **15-state `ActionState` FSM preserved verbatim, incl. PREVIEW_PENDING / PREVIEW_APPROVED reused for destructive-geometry approval.** | AST parse of `lifecycle_hooks.py`: ActionState has **exactly 15** members incl. `PREVIEW_PENDING` (14) + `PREVIEW_APPROVED` (15), `RECIPE_REQUESTED`, `EXECUTING_MOTION`, `SENSOR_CONFIRM`. Transitions at `:776/:807/:808` are real. Phase-6's destructive-window approval has a real substrate. |
| C10 | **Today's floor = cage + forced-software-GL; central/server/edge are headless (no compositor).** | `hart-liquid-ui.nix:159–160` `WLR_RENDERER_ALLOW_SOFTWARE=1` + `LIBGL_ALWAYS_SOFTWARE=1` + `exec cage -- hart-glass-shell`. `server.nix:222` + `edge.nix:74` `services.xserver.enable = false`. The compositor-independence `mustNotLose` contract is grounded. |
| C11 | **The realtime fabric is the real delivery spine, consumed by external clients.** | `realtime.publish_event` → WAMP `com.hertzai.hevolve.social.{user_id}` + `broadcast_sse_safe` (`core/platform/events.py`); `peer_link/message_bus.py` maps `chat.social`. Consumed by Nunba desktop, landing-page React, Hevolve_React_Native mobile — NOT the on-box compositor. The two cross-cutting `mustNotLose` contracts are well-founded. |
| C12 | **Carried open-risks are real, none falsely solved.** | Android `exec sleep infinity` (`hart-subsystems.nix:288`, `hart-liquid-ui.nix:510`); `_install_android` just `shutil.copy2` the apk (`app_installer.py:582/616`); Wine `success=True` unconditional at installer (`app_installer.py:568–572`); LiveKit `{ok:False}` no-egress (`livekit_service.py`); `agent_voice_bridge` partial stub (`agent_voice_bridge.py:63` — real TTS queue, no audio wiring). |
| C13 | **The AI-native distinction rests on real primitives.** | `should_yield_to_user` defined `core/foreground.py:185` + `dispatch.py:308`; `full_boot_verification` `master_key.py:202`, invoked at boot by `hive_guardrails.py:834` + `origin_attestation.py:191`. Both real — though "HART-comp boots only after `full_boot_verification`" is **design intent** (no compositor exists yet to gate), not shipping behaviour. |

---

## 4. Must-fix-before-build (severity-ranked)

These are the issues the validators found. None block authoring the architecture doc;
they block **shipping `window.*`** (the agent-drives-real-windows verbs) and gate the
AI-native grade.

### 4.1 BLOCKERS — `window.*` must NOT ship until all are green

| # | Issue | Fix |
|---|---|---|
| **B1** | **The A2UI cross-process transport is severed (3 ways)** — so a brain-originated intent **cannot** rearrange the desktop today; cards/approvals/theme-applies are silently dropped unless WAMP happens to be up. | **Phase 2.** Repoint the dead import from `core.platform.service_registry` → `core.platform.registry` in `agentic_router.py:632`, `agent_voice_bridge.py:579`, `channels/agent_tools.py:269/836`, `model_orchestrator.py:880`; change every `ServiceRegistry.get('LiquidUIService')` → `get_registry().get('LiquidUIService')` (**including** the 6 sites in `hart_intelligence_entry.py`); and **register the instance once** (co-locate `LiquidUIService` into the brain process and `get_registry().register('LiquidUIService', instance)`, deleting the `theme_service :6800` HTTP hack) **OR** route all callers through one out-of-process transport. **Pick ONE in code after verifying the real ISO topology.** Note the router lives at `integrations/agentic_router.py` (not `…/agent_engine/agentic_router.py`). |
| **B2** | **`agent_ui_update` is not yet fully gated** — `GuardrailEnforcer.before_dispatch`, server-side string sanitization, and a real per-agent rate cap are still owed. A `window.close`/`fullscreen-takeover` verb would otherwise get the same fail-OPEN, unguardrailed treatment as a cosmetic card — the inverse of "every dispatch is provable." | **Phase 2.** Add `GuardrailEnforcer.before_dispatch` (fail-CLOSED for destructive verbs), server-side escape of all string fields, and a token-bucket per-agent rate cap inside `agent_ui_update`. The landed `8cc4e95` (kill-switch + audit) is the prerequisite, not the close. |
| **B3** | **The screen kill-switch is a dead flag** — adding native windows + a screencast portal is a **net security regression** (the cage floor's no-native-capture property is silently lost) unless the cross-process gate exists first. | **Phase 2c (wire existing consumers) + Phase 7 (promote to cross-process authority).** Make `frame_capture`/`window_capture`/`/api/shell/screenshot`/visual+time-agent check `ai_sensing.allowed('screen')` and have `status()` report observable proof; then promote `core.ai_sensing 'screen'` to a real UDS/D-Bus cross-process authority the portal MUST consult, fail-closed, **before** any `org.freedesktop.portal.ScreenCast` surface ships. |
| **B4** | **The never-blank-screen guarantee is unproven** — it depends on a NET-NEW out-of-process greetd/systemd supervisor (`node_watchdog` structurally cannot drop a DM session). Until VM-proven, the migration's core safety promise is a spec. | **Phase 1 (built BEFORE any compositor becomes default).** Build `hart-session-supervisor` reading `/var/lib/hart/session-tier`, dropping one tier per crash-loop (3/5 min), latching across boot, with `hartctl session reset-tier`. **VM-prove via loop-kill fault injection** (crash a fake Tier-1 binary → assert it lands on cage with `latch='cage'`; GNOME still greeter-selectable). `node_watchdog` becomes a **signal emitter only**. |

### 4.2 MAJOR — the AI-native grade depends on these (brain-side, dev-box-now)

| # | Issue | Fix |
|---|---|---|
| **M1** | **`acSend` is graded "Enhanced/pipeline-preserved" but the thesis demands the pipeline be INVERTED.** As landed the surface is "orb-as-chatbot-launcher" (text bubble + TTS), not "intent → compose." This contradiction is the single place the migration map undershot its own thesis. | **Resolved in the v2 doc:** `acSend` re-graded **M (Migrated)** with a concrete deliverable — route free-form intent through the brain's CREATE/REUSE decomposition and render the result as composed UI fragments / placed windows / actions via `agent_ui_update` (after B1), with `open <app>` demoted to the fallback fast-path. **The 4.5 → 7 move; it does NOT need the compositor.** |
| **M2** | **The orb is dimmed to wallpaper the instant any panel opens** (`hartHero.js:104`) — an intent-first OS keeps the composition surface primary; demoting it the moment apps open re-asserts the launcher-is-the-spine model. | Stop dimming the intent surface on panel-open; keep the orb/command bar the primary composition surface with the app-grid as a discoverable fallback. (Product call flagged in §Open-Decisions — but the dim is the visible symptom of the wrong default.) |

### 4.3 MINOR — accuracy corrections (folded into the v2 doc)

| # | Issue | Fix |
|---|---|---|
| **m1** | **"A2UI has 30+ COMPONENT_TYPES"** — AST count is **26** keys (`card…channel_connected`, incl. `meet_copilot`). | Corrected to "26-component contract" in the architecture doc. Immaterial to the thesis. |
| **m2** | **"kill-switch stops agent UI exactly like a goal dispatch (`dispatch.py:668`)"** — the actual pre-dispatch guardrail is `dispatch.py:670` (`before_dispatch`, fail-CLOSED) and audit is `:680`; `:668` is a comment line. Dispatch is fail-CLOSED vs `agent_ui_update` fail-OPEN. | Cite `:670`/`:680`; note the fail-CLOSED-vs-fail-OPEN asymmetry (the doc acknowledges the fail-open intent elsewhere). |
| **m3** | **"every capability carries a concrete new home"** is **not literally true** — 5 subsystems with real code (`browser_research` 6, `openclaw` 6, `agent_lightning` 6, `ui_actions` 3, `skills` 2) had no line-item. They are preserved in practice (additive compositor + wholesale-preserved brain) but had no disposition row. | **Resolved in the v2 doc:** added an "All remaining brain subsystems = P verbatim" catch-all row to §4.6 naming all 5. |
| **m4** | **"demoted to wallpaper" (orb)** overstates a CSS dim — `hartHero.js:104` toggles a `dimmed` class, it does not remove the orb. | Softened to "dimmed when panels open"; the intent-first argument does not depend on the exaggeration. |
| **m5** | **"`agent_voice_bridge` is a no-op stub"** is slightly understated — it has a real TTS outbox queue (enqueue/dequeue) but no actual audio wiring. | Note "partial stub (real TTS queue, no audio wiring)"; directionally accurate, meet-copilot pinning stays gated. |
| **m6** | **"HART-comp boots only after `full_boot_verification`"** is design-intent, not shipping behaviour (no compositor exists to gate). | Flag as NET-NEW Phase-1/3 wiring; the gating primitive (`full_boot_verification`) is real. |

---

## 5. Test report — what the suite run proves

**Environment:** Python 3.11.4, pytest 8.4.2, dev box (Windows), flags `--noconftest -p
no:capture` (avoids the OOM-prone full suite + the `TestMediaAgent` tempfile-handle
corruption). **6 targeted foundation suites, all RAN TO COMPLETION and PASSED. Green
423 / Red 0. Zero import-skips** — every file imported cleanly (none hit the missing-
autogen skip path), so the green means the test ran, not that it skipped.

| Suite | Result | What it proves |
|---|---|---|
| `tests/unit/test_a2ui_guardrail_audit.py` | **6 passed** (13.77s) | **The landed `agent_ui_update` gate is real and behavioural.** Constructs the REAL `LiquidUIService`, mocks ONLY the two security boundaries (`immutable_audit_log.get_audit_log` + `HiveCircuitBreaker.is_halted`), calls the real method, asserts observable side-effects: a valid push is recorded once in the immutable audit log AND reaches the SSE store; a push is **REFUSED** (returns False, never reaches `_agent_components`) when the human has halted the HiveCircuitBreaker — an agent painting the screen is governed exactly like an agent dispatching a goal; invalid type rejected with NO audit; a2ui-disabled short-circuits; benign consent cards fail-OPEN if the guardrail/audit module is unavailable. **Not a grep/string guard.** |
| `tests/unit/test_shell_ai_sensing.py` | **5 passed** (13.74s) | **The AI sensory kill-switch is real and wired end-to-end.** Drives the REAL `core.ai_sensing` gate + REAL Flask routes: default sensing on; `disable_all()` flips mic/camera/screen with a live-proof field (`camera_service_running`); the REAL mic-ingestion route `/api/voice` returns **403** (`sensing_disabled=True`, NO audio consumed) when hearing is cut and stops refusing when re-enabled; `/api/shell/ai-sensing` off/on round-trips + `status()` reflects it live; the floating kill-switch button + `hartSenses.js` are wired into the rendered shell, hit the real route, and are WebKitGTK-safe (`HartTimeoutSignal`, no `AbortSignal.timeout`). **Substantiates "humans are always in control" in working code.** ⚠️ Note: this proves the **mic** gate; the **screen** gate is the dead flag of B3 — `ai_sensing.allowed('screen')` exists but no capture consumer honours it yet. |
| `tests/unit/test_liquid_ui_shell_static_route.py` | **10 passed** (9.52s) | Guards the 2026-06-15 "dead husk" fix (`f294f52`) — `_create_flask_app` serves shell assets under `static_url_path='/shell/static'` via a REAL `test_client` fetch (200), not blind inline render. The same "verify served assets, inline-render is blind" discipline is carried into the Phase-0 compositor smoke test. |
| `tests/unit/test_liquid_ui_meet_copilot.py` | **4 passed** (10.13s) | The meet/co-pilot bridge surface of the liquid UI service registers + responds. |
| `tests/unit/test_shell_app_routes.py` | **3 passed** (9.90s) | Shell desktop app routes register and respond. |
| `tests/unit/test_nixos_configs.py` | **395 passed** (0.77s) | Static-config validation of the NixOS HART OS image definitions (`hart-*` services / module structure) — confirms the image definitions are well-formed. |

**Net:** the two human-control-critical guarantees — (1) the `agent_ui_update` safety
gate that every `window.*` verb will ride, and (2) the AI-sensing kill-switch — are
**grounded in code that passes its own behavioural tests today** (mock only the security
boundary, call the real code, assert observable side-effects). The shell-route/static
suites confirm the desktop serves its assets correctly. **What the suite does NOT prove
(by design — dev-box has no Wayland/KMS):** any L1/L2 paint, software-GL fallback,
XWayland, portal screencast, ext-session-lock, or the never-blank-screen tier-drop —
those are CI-nixosTest-VM / QEMU-only and gate the later phases (B4 especially).

---

## 6. Validated phase plan (corrected for validator findings)

Never-fail floor first. `[DEV]` = dev-box-authorable now (Python brain / nix expressions
/ unit guards). `[VM]` = must run in a Linux CI nixosTest VM or local QEMU-KVM (no
compositor source authored ahead of CI).

| Phase | Goal | Where |
|---|---|---|
| **0 — Floor-lock + CI/VM gate harness** | Freeze today's cage + WebKitGTK + forced-software-GL session as the immutable **Tier-3 never-fail floor**; stand up the CI nixosTest + local QEMU harness (`scripts/vm/boot-tier.sh`) every later phase is proven against. Land the `/var/lib/hart/session-tier` contract (spec) + labeled source-guards freezing the "preserved-not-resurrected" `/api/ui` path + the eval-gate canary. | `[DEV]` canary + source-guards; `[VM]` boot-cage-on-llvmpipe + **dead-husk-aware `/shell/static` fetch 200** |
| **1 — Out-of-process tier-drop supervisor (B4)** | Build the SAFETY-CRITICAL never-blank-screen mechanism v1 mislabeled as a NodeWatchdog reuse: a real out-of-process greetd/systemd `hart-session-supervisor` selecting the session from the latched state file, dropping one tier per crash-loop (3/5 min), latching across boot, with `hartctl session reset-tier`. Wire sway as Tier-2 + cage as Tier-3 NOW; `defaultSession` still cage. `node_watchdog` → **signal emitter only**. | `[DEV]` node_watchdog-emits-not-switches unit; `[VM]` **loop-kill fault injection** (lands on cage, latch persists, GNOME still greeter-selectable) |
| **2 — Close the pre-existing A2UI / audit / ai_sensing gaps (B1, B2, B3-wire, M1)** | DISPLAY-INDEPENDENT, lands on every tier incl. headless. **(a)** Resolve the A2UI transport FOR REAL — pick co-locate-and-register-instance OR one out-of-process transport; fix the dead import + the class-vs-instance call (B1). **(b)** Add `GuardrailEnforcer.before_dispatch` + server-side sanitization + per-agent rate cap to `agent_ui_update` (B2). **(c)** Wire existing capture consumers to `ai_sensing.allowed('screen')` + observable `status()` (B3). **(d) INVERT `acSend`** to intent → compose, `open <app>` as fallback (M1). **After this phase an intent can reach the glass.** | `[DEV]` all gate/inversion logic (mocks+call+observe); `[VM]` cross-process A2UI-card-observed-on-SSE integration |
| **3 — HART-comp skeleton (tinywl-class) + software-render floor, opt-in only** | Establish the **first Rust-in-Nix** toolchain; ship a minimal Smithay compositor painting a KMS splash + ONE layer-shell client with a from-scratch pixman/llvmpipe software path on broken-GPU fixtures. **Land the `buildRustPackage` precedent FIRST on an existing crate** (`claw_native/rust`, fixed `cargoHash`, pin `50ab793`) — if the crate graph doesn't resolve on the June-2025 pin, the compositor is blocked. Extend node integrity to hash the Rust binary into the signed manifest + origin-attest it. `defaultSession` stays cage. | `[DEV]` tampered-binary-trips-integrity-monitor unit; `[VM]` eval-gate-green + pixman-paints-on-llvmpipe + GBM-fail-fixture-falls-to-pixman + crash-loop-lands-on-cage |
| **4 — Glass shell as layer-shell surface (the GTK3 → GTK4 host-window port)** | Re-host the EXACT served shell HTML as a layer-shell surface, treating the unavoidable GTK3 → GTK4 WebKitGTK host-window port as an explicit budgeted milestone with its OWN software-GL validation — **NOT "JS untouched / same WebView."** Pick ONE z-order model in code (single co-planar surface, OR two WebViews + a real cross-WebView message bus replacing implicit `window.*` sharing — then drop "JS untouched"). Keep the GTK3 cage path verbatim as Tier-3. | `[VM]` GTK4-layer-shell-boots-on-llvmpipe + **real `/shell/static` fetch 200** + (if dual) A2UI-card-z-ABOVE-a-native-window + GTK4-crash-drops-to-GTK3-cage |
| **5 — Native app windows (xdg-shell + XWayland) + an install→real-window path with no phantom success** | Host real native toplevels (Wine/Flatpak/PWA) with **launch-success keyed on a REAL `window.opened` map event within a timeout — NEVER on installer/launcher exit code** (Wine returns 0 even when nothing mapped; Android `_install` just copies the apk). A handle is returned ONLY when a toplevel actually mapped. AppRegistry gains a window-handle field; native-window launch is ADDITIVE alongside the preserved iframe panels. Android stays explicitly inert (no phantom handle). | `[VM]` Flatpak/PWA-maps-fires-window.opened + Wine-no-map-reports-FAILURE + Android-reports-unsupported + XWayland+layer-shell-coexist |
| **6 — AI-native WM IPC (`com.hart.Compositor`) wired to the brain — agents arrange REAL windows, fully audited (THE MOAT)** | Give the brain the privileged `HartWmClient` so agents (+ Claude via new `place_window`/`tile`/`summon`/`list_windows` MCP tools) place/tile/summon/arrange real native windows — flowing through the **NOW-real (Phase-2) fail-closed guardrail + circuit-breaker + immutable-audit + sanitize + rate-cap gate**, NOT a no-op pipeline. Destructive geometry (close user-focused window, fullscreen takeover) routes through the EXISTING `PREVIEW_PENDING`/`PREVIEW_APPROVED` FSM + an A2UI approval card wired to the real audit sink. Workspaces + Super+Space PTT + Ctrl+Alt+1-4 AUGMENT not replace (client-side every tier incl. cage; compositor globals when feature-detected). **Compositor render CPU explicitly accounted in `resource_governor`** (excluded from BOTH "own idle work" and "user is busy") so it can't re-create the governor-dormancy bug. **Intent → compose → desktop becomes the default operating surface here.** | `[DEV]` agent-window.close-on-FOCUSED-window-REFUSED-without-PREVIEW-+-audited; window.*-with-hive-halted-fail-closed-+-rate-cap; should_yield-not-tripped-by-HART-comp-render-CPU; workspace-switch-still-toggles-SHELL-panels-when-compositor-absent. `[VM]` agent-drives-real-windows + SummonApp-only-on-real-handle + shortcut-1:1 |
| **7 — L4 portals + REAL cross-process AI-senses screencast gate + theming (B3-promote)** | Add Freedesktop portals for native apps AND make the screen kill-switch cross-process **BEFORE any screencast surface exists** — closing the net-security-regression. Promote `core.ai_sensing 'screen'` to a real cross-process UDS/D-Bus authority the portal MUST consult (fail-closed); `xdg-desktop-portal-hart` ScreenCast + wlr-screencopy refuse when cut. Theming OS-wide via portal Settings (labeled NEW). Real `ext-session-lock-v1` (PAM re-auth); close the autologin-bypasses-PAM + cage-lock-no-op gap. | `[VM]` portal-ScreenCast-with-screen-DISABLED-REFUSED (not just flag set) + `status()`-reports-blocked + native-app-follows-HART-theme + ext-session-lock-blanks-surfaces-+-PAM-re-auth |
| **8 — Effects, polish, sway Tier-2 parity, meet-copilot pinning, telemetry, route consolidation** | Visual/interaction effects (gated behind software-render → broken-GPU degrades to flat, never black); sway Tier-2 swaymsg-shim parity (degraded moat when HART-comp crashes); meet-copilot transcript pinned beside the call window ONLY behind a REAL `agent_voice_bridge` + the Phase-2 A2UI fix (LiveKit egress gap stays open); tier-drop telemetry + operator dashboard; **recipe-banked window-layout REUSE** (CREATE banks summon+tile+switch as `window.*` steps, REUSE replays the exact desktop with NO LLM calls); `/api/shell/apps` vs legacy `/api/apps` consolidation onto ONE route. | `[DEV]` meet-copilot-only-fires-when-bridge-real-AND-push-works; CREATE-banks-then-REUSE-replays-window-layout-without-LLM; route-collision-guard-green. `[VM]` effects-degrade-to-flat + sway-shim-arranges-windows |

**Ordering invariants honoured:** (i) a never-fail floor exists from Phase 0; (ii) the
out-of-process tier-drop supervisor is built + VM-proven (Phase 1) before any compositor
becomes default; (iii) the A2UI / audit / ai_sensing gaps close (Phase 2) before
`window.*` (Phase 6) or the screencast portal (Phase 7) ship; (iv) `buildRustPackage` is
proven on an existing crate before HART-comp depends on it (Phase 3); (v) the constitution
gates the display and a half-finished compositor can never brick the box.

---

## 7. Next actions (concrete, ordered — the realistic 4.5 → 7 path)

These are the **highest-leverage builds**, brain-side and dev-box-authorable, that move
the AI-native grade with code rather than docs. The compositor moat is the 7 → 9 upgrade
and is correctly deferred.

1. **[DEV] Fix the A2UI transport (B1)** — the single highest-leverage move. Repoint the
   dead `core.platform.service_registry` import → `core.platform.registry` and change every
   `ServiceRegistry.get('LiquidUIService')` → `get_registry().get('LiquidUIService')` across
   `agentic_router.py`, `agent_voice_bridge.py`, `channels/agent_tools.py`,
   `model_orchestrator.py`, **and the 6 sites in `hart_intelligence_entry.py`**; co-locate
   `LiquidUIService` into the brain and `get_registry().register('LiquidUIService', instance)`
   (delete the `theme_service :6800` hack), OR commit to one out-of-process transport. Add a
   behavioural test: cross-process A2UI card observed on SSE. **Without this, intent cannot
   rearrange the desktop — every brain-originated card is dropped unless WAMP is up.**

2. **[DEV] Finish gating `agent_ui_update` (B2)** — add `GuardrailEnforcer.before_dispatch`
   (fail-CLOSED for destructive verbs), server-side string sanitization, and a per-agent rate
   cap. Behavioural tests for each. This is the prerequisite the landed `8cc4e95` (kill-switch
   + audit) does not yet cover; `window.*` must not ship until it is green.

3. **[DEV] Invert `acSend` (M1)** — make the default path decompose intent via the brain's
   CREATE/REUSE loop and render composed UI fragments via `agent_ui_update` (after move 1),
   with `open <app>` as the fallback fast-path. Stop dimming the orb on panel-open (M2).
   **This converts "orb-as-chatbot-launcher" into "intent-as-operating-surface" WITHOUT the
   compositor — the realistic 4.5 → 7 move.**

4. **[DEV] Wire the dead screen kill-switch into capture consumers (B3, first half)** — make
   `frame_capture`/`window_capture`/`/api/shell/screenshot` check `ai_sensing.allowed('screen')`
   and have `status()` report observable proof, BEFORE any native-window / screencast surface
   ships. (The cross-process promotion is Phase 7.)

5. **[VM, Phase 1] Build + loop-kill-prove the out-of-process tier-drop supervisor (B4)** —
   the never-blank-screen guarantee is a spec until this passes fault injection in a VM. Build
   it BEFORE any compositor becomes default.

6. **[VM, Phase 3] Land the `buildRustPackage` precedent on `claw_native/rust`** under pin
   `50ab793` — if the crate graph doesn't resolve, the compositor is blocked; prove the
   toolchain on an existing crate first.

**Close:** the diagnosis is correct and the open risks are recorded rather than closed. The
score rises with **code, not the doc** — land moves 1–3 on the existing shell and the
system crosses from "Linux + an agent" to "AI-as-the-operating-surface." The
compositor (`com.hart.Compositor`, agents owning window-placement policy) is the moat that
takes 7 → 9; it is correctly NOT on the critical path to 7.

---

## 8. Residual risks (carried forward UNCHANGED, not silently solved)

- **The never-blank-screen guarantee is UNPROVEN** until Phase-1 VM loop-kill (B4).
- **The first-ever Rust-in-Nix build may not resolve** on pin `50ab793` (Phase-3 gate).
- **"JS untouched / same WebView" is unbuildable** — GTK3 → GTK4 host-window port with its
  own broken-GPU validation (Phase 4); single-vs-dual-WebView z-order is an open decision.
- **The screen kill-switch is a net-security-regression risk** until B3 + Phase-7 land.
- **The social/economy organism + flywheel run headless** (no compositor) — any re-topology
  for the A2UI co-location fix must be proven NOT to sever the WAMP/SSE fabric mobile depends
  on; the yield gate must stay HTTP-driven/display-independent.
- **The flywheel itself is bring-up, not operational** — central-tier auth 401s the daemon's
  own `/chat` dispatch and `/app/agent_data` is unmounted (`FLYWHEEL_RECOVERY_BRIEF`). The
  OS-native work does not depend on it, but "the flywheel works" is not yet a true statement.
- **Carried open-risks:** Android = `sleep infinity` vaporware; Wine launch-success
  unconditional at the installer (corrected only at the WM layer via SummonApp-on-real-map);
  macOS/Darling default-off; no `.crx`/`.xpi`; LiveKit egress `ok:False`; `agent_voice_bridge`
  partial stub; the Android `pupit` topic-namespace mismatch.
