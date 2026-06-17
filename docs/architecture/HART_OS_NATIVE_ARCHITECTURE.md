# HART OS — Native Architecture

> **Status:** Design artifact (specs + plan only). No compositor C/Rust source is
> authored here — every L1/L2 paint, software-GL fallback, XWayland map, portal
> screencast, and session-lock claim must be **compiled and VM-proven in CI**, never
> authored blind. This document is the persistent north star, the migration ledger,
> and the phased, gated build plan.
>
> **Companions:** [`compositor/ROADMAP.md`](../../compositor/ROADMAP.md) (phase list +
> deliverables + gates) and [`compositor/IPC_PROTOCOL.md`](../../compositor/IPC_PROTOCOL.md)
> (the agent ↔ compositor contract).

---

## 1. North Star (v2 — intent-first)

**HART OS becomes genuinely AI-native by making INTENT the primary operating surface —
not by cloning Windows 11 and bolting an agent onto the app layer.**

The self-review graded the compositor-first v1 at **4.5/10** with a precise verdict:
*"HART OS built the AI-native engine and then wrapped it in Windows 11."* The brain's
`intent → decompose → compose` loop is **real** (`hart_intelligence_entry.py:8213` `/chat`
runs a draft intent classifier routing free-form prompts to CREATE / REUSE / tool / vision /
casual) — but **it never reaches the glass**, for two structural reasons this document now
treats as the load-bearing fix, ahead of any compositor:

1. **The surface is still a chatbot-launcher, not an intent composer.** `acSend`
   (`liquid_ui_service.py:3751`) string-matches `open X` → `openPanel` (launcher is the
   spine), matches theme words → preset, else POSTs `/api/agent/ask` → `/chat` with a
   hard-coded `prompt_id='desktop_agent'` and **narrates the reply as a text bubble + TTS**
   (`typing.textContent = reply; speakText(reply…)`). Nothing composes the desktop. The orb
   is even **dimmed** the instant any panel opens (`hartHero.js:104`), so app-launching
   visibly owns the screen and the intent surface recedes.
2. **The one wire that would let the brain compose the desktop — `agent_ui_update` pushing
   live UI fragments — is PHYSICALLY SEVERED three independent ways** (verified in code):
   a dead `ImportError` on `core.platform.service_registry` (the module does not exist —
   only `core.platform.registry`); callers invoking `ServiceRegistry.get()` on the **class**
   not an instance (a `get()` that is an instance method, `registry.py:152`); and
   `'LiquidUIService'` registered in **zero** production files (only a probe + tests). Even
   the "working" caller in `hart_intelligence_entry.py` has the same class-vs-instance bug —
   so the channel is severed everywhere, not just in the dead-import callers.

**The 4.5 → 7 path is therefore three brain-side moves, not a rewrite — all authorable on
the dev box today, none requiring the compositor:**

1. **FIX THE SEVERED PUSH CHANNEL** so an intent literally rearranges the desktop instead of
   narrating from a chat bubble — resolve the topology, register the instance, fix the dead
   import + the class-vs-instance call (Phase 2).
2. **INVERT `acSend`** so `intent → decompose → compose` is the DEFAULT operating surface —
   the orb/command bar composes windows, data layouts, and actions from what the human wants,
   and `launch a named app` becomes the **fallback fast-path**, not the spine (Phase 2/8).
3. **PUT AI INSIDE EVERY SYSTEM LOOP WITH A STRUCTURAL HUMAN VETO** — agents place / tile /
   summon REAL native windows via `com.hart.Compositor` (the moat stock Linux + Copilot
   cannot match: an AI that **owns window-placement policy**, not one that scripts `swaymsg`),
   but every mutation passes a fail-closed guardrail + circuit-breaker + immutable-audit gate,
   destructive geometry routes through the PREVIEW FSM for human approval, and the kill-switch
   (`core.ai_sensing`) stays the single supreme gate with **no AI write path**. This is the
   **7 → 9 moat upgrade**, correctly deferred behind sway-as-Tier-1 — NOT the 4.5 → 7 gate.

A purpose-built compositor (**HART-comp**, Smithay/Rust) ultimately owns the displays, inputs,
and window tree, while the **brain** drives it over a structured IPC. The glass shell —
`voiceOrbViz`, `hartHero`, A2UI overlays, the AI-senses kill-switch, all 13 static modules, the
MD3 design system, the ~60 system panels — is **NOT discarded**. It is re-hosted from "the only
window in a kiosk" to a first-class layer-shell surface (the background / desktop) **plus**
top-layer overlays, so it keeps being painted by WebKitGTK but now floats above real,
agent-placeable native windows (Wine / Android / Flatpak / PWA) that the compositor tiles and
**the agent can summon / place / arrange** — the moat no GNOME or Windows can match. But the
honest distinction from "Linux + an agent" is **structural and lands brain-side first**: the AI
is the deciding intelligence of the *session* (it composes the desktop from intent and arbitrates
the shared local-LLM slot via `should_yield_to_user`), while humans hold control all the way
down to who can place a window. **v1 was AI-at-the-app-layer-on-a-conventional-session; v2 is
AI-as-the-operating-surface with human veto wired into the most dangerous new privilege BEFORE
it ships.**

Every prior capability is **preserved, migrated, or enhanced — nothing is rewritten
away**:

- the 33-rule guardrail kernel and master-key kill switch,
- recipe CREATE / REUSE and the 15-state FSM,
- the ~493 social routes, 31 channels, the economy,
- A2UI's 26-component contract (`COMPONENT_TYPES`, verified by AST — not "30+"),
- the never-fail-paint floor.

The compositor is **additive**: when it or the GPU fails, a watchdog drops to a proven
wlroots WM, then to the **exact cage floor that ships today**, so the screen is never
blank and *"humans are always in control"* is expressed all the way down to **who can
place a window**.

### 1.1 Three invariants that override everything

1. **HARTOS is the heart and brain.** The compositor is a surface the brain drives. The
   brain (recipe pipeline, FSM, daemon, social/economy organism, realtime fabric) is
   the product; the compositor is a new limb, never a new master.
2. **Nothing built is lost.** Every capability in §4 has a row: a disposition
   (`preserved` / `migrated` / `enhanced`), a new home, and a concrete *how*. A reader
   must be able to confirm, line by line, that no feature was rewritten away.
3. **OS-native, not kiosk — but never at the cost of a blank screen.** The constitution
   gates the display (HART-comp boots **only after** `full_boot_verification` passes),
   and a never-fail tier ladder guarantees that a half-finished compositor can never
   brick a user.

### 1.2 The honesty discipline of this document

This architecture was written against the tree, not against optimism. Several earlier
"preserved" / "enhanced" claims were **verified false in code** and are corrected here
rather than shipped on assumption. The corrections are load-bearing:

- The A2UI push path is **already broken cross-process** — `agent_ui_update`
  (`integrations/agent_engine/liquid_ui_service.py:394`) does only an allowlist check +
  a 5-item ring-buffer cap + an EventBus emit. There is **no** `GuardrailEnforcer`, **no**
  `ImmutableAuditLog`, **no** server-side escaping, and **zero**
  `ServiceRegistry.register('LiquidUIService')` anywhere in the tree (verified by grep).
  The shell runs as its own systemd unit on `:6800`, separate from the `:6777` brain. So
  the claim that `window.*` verbs "inherit" audit/guardrail/cap is **false**, and the
  "FIXED by in-process co-location" claim is **asserted, not built**.
- `core.ai_sensing` is per-process memory and `'screen'` has **zero enforcing consumers**
  today. Adding native windows + a screencast portal **net-regresses** the kill-switch
  unless a real cross-process gate is built first.
- The shell is **GTK3 + WebKit2-4.1** single fullscreen window — `gtk4-layer-shell` forces
  a **GTK3 → GTK4 host-window port**, so "JS untouched / same WebView" hides re-litigating
  the never-fail software-GL paint floor on an unproven GTK4 stack.
- There is **zero Rust-in-Nix precedent** in the tree — Smithay HART-comp is the **first**
  `buildRustPackage`, a new toolchain + eval-gate class, not a "reuse."
- `node_watchdog` is an **in-process Python thread supervisor** that structurally **cannot**
  drop a display-manager session — the never-blank-screen tier-drop is a **net-new
  out-of-process supervisor** (greetd/systemd), the single most safety-critical unbuilt
  mechanism.
- The foreground yield gate is **HTTP-request-driven** and **must stay display-independent**
  for the headless central / server / edge tiers where the entire social / economy organism
  actually runs.

Everything below is ordered so a **never-fail floor exists from Phase 0**, the **tier-drop
supervisor is built and VM-proven before any compositor becomes default**, the pre-existing
A2UI / ai_sensing / audit gaps are **closed before** `window.*` or portal screencast ship,
and Smithay is treated as a **later moat upgrade behind a sway-as-Tier-1 fast path**.

---

## 2. The Layered Stack

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Security (cross-layer, imported at boot — gates the display, never gated) │
│  Guardrail kernel · master-key kill switch · cert chain · integrity mon.  │
├──────────────────────────────────────────────────────────────────────────┤
│ L5  App install / marketplace / bridge                                    │
│     AppInstaller(7) · AppRegistry · marketplace · app_bridge :6810 · ext  │
├──────────────────────────────────────────────────────────────────────────┤
│ L4  Freedesktop portals & session services                               │
│     xdg-desktop-portal-hart · screencast(gated) · Settings · notify      │
├──────────────────────────────────────────────────────────────────────────┤
│ L3  Nunba daemon — the BRAIN + compositor IPC driver                      │
│     recipe CREATE/REUSE · 15-state FSM · daemon flywheel · 96 experts ·    │
│     A2UI ingress · MCP · HartWmClient (NEW privileged WM client)          │
├──────────────────────────────────────────────────────────────────────────┤
│ L2  Glass shell as layer-shell surface (re-hosted, not rewritten)        │
│     voiceOrb/hero · A2UI overlays · ~60 panels · MD3 · WebKitGTK          │
├──────────────────────────────────────────────────────────────────────────┤
│ L1  HART-comp compositor (NEW — the OS-native replacement for cage)      │
│     DRM/KMS · libinput · window tree · com.hart.Compositor IPC            │
├──────────────────────────────────────────────────────────────────────────┤
│ L0  Kernel & native-binary substrate                                      │
│     binder/ashmem · PE binfmt→wine64 · GPU · cgroups2 · Landlock         │
└──────────────────────────────────────────────────────────────────────────┘
```

### L0 — Kernel & native-binary substrate
- **Role:** make every foreign binary a first-class kernel citizen and provide the secure
  execution floor for windows the compositor will host (Wine PE, Android ART, Flatpak,
  AppImage, PWA).
- **Tech:** Linux (`linuxPackages_latest`) + `binder_linux`/`ashmem_linux`, PE
  `binfmt_misc` → `wine64`, `ntfs3`/`vfat`/`exfat`, GPU modules, cgroups v2 + Landlock +
  lockdown + yama, `vm.max_map_count=2M`.
- **Reuse:** **REUSED VERBATIM** from `nixos/modules/hart-kernel.nix` +
  `hart-subsystems.nix`. The compositor *consumes* the surfaces these produce; it does not
  replace them. Zero change to the *"Wine IS native / no emulators"* identity.

### L1 — HART-comp compositor (NEW)
- **Role:** own DRM/KMS scanout, libinput, the Wayland socket, and the live window tree.
  Composite three planes: (a) native app toplevels (xdg-shell + XWayland), (b) the
  glass-shell layer-shell surfaces (background + top overlays), (c) HW cursor / software
  cursor fallback. Expose the AI-native WM IPC.
- **Tech:** Smithay (Rust). udev/DRM/GBM backend with a **mandatory** pixman/llvmpipe
  software-render path. `wlr-layer-shell-unstable-v1` + `xdg-shell` + `xdg-decoration` +
  `wlr-foreign-toplevel-management`. A Unix-socket + D-Bus (`com.hart.Compositor`) control
  channel.
- **Reuse:** **BUILDS FIRST-PRINCIPLES** (no compositor exists today; cage is the only
  Wayland layer). Reuses the exact software-GL hardening contract from `hart-liquid-ui.nix`
  (`WLR_RENDERER_ALLOW_SOFTWARE` / `LIBGL_ALWAYS_SOFTWARE` / no-DMABUF) as Smithay's pixman
  renderer + a *force-software* boot flag, and reuses the OS-mode port registry +
  systemd-hardening + NO-WatchdogSec lessons from every `hart-*.nix` unit.

### L2 — Glass shell as layer-shell surface (re-hosted, not rewritten)
- **Role:** the desktop itself: voiceOrb/hero centerpiece, A2UI overlays, AI-senses eye,
  taskbar/dock, start menu, lock screen, desktop icons, virtual desktops, ~60 system
  panels. Now a background layer-shell surface for the desktop **plus** a top-layer surface
  for A2UI overlays / toasts / approvals that float above native app windows.
- **Tech:** same WebKitGTK WebView (`gtk4-layer-shell` binding instead of cage's
  single-window) pointed at the **same** `LiquidUIService :6800` `render_desktop_shell` +
  `/shell/static/*` tree. A thin shim translates window-management JS calls (open a panel of
  a NATIVE app, workspace switch) into compositor IPC instead of client-side div show/hide.
- **Reuse:** **REUSED:** `liquid_ui_service.render_desktop_shell`, the `/shell/static`
  `static_url_path` fix (f294f52), all 13 JS modules, `_CSS_DESIGN_SYSTEM`, `ThemeService`
  tokens, `shell_manifest`, shell os/desktop/system APIs. **Honest caveat (corrected from
  source):** the shell ships as **GTK3 + WebKit2-4.1**; `gtk4-layer-shell` forces a GTK3→GTK4
  host-window port — so this is a *budgeted port with its own broken-GPU validation*, **not**
  a config flag, and **not** "JS untouched" if the dual-WebView model (Phase 4) is chosen.

### L3 — Nunba daemon: the BRAIN + compositor IPC driver
- **Role:** the agentic runtime stays the heart/brain: recipe CREATE/REUSE, the 15-state
  FSM, daemon flywheel, 96 experts, A2UI ingress, MCP. **New responsibility:** it is the
  privileged client of the compositor's WM IPC — it (and agents through it) place / tile /
  summon / arrange real native windows.
- **Tech:** `hart_intelligence_entry` Flask `:6777` + `LiquidUIService :6800` +
  `agent_daemon`, unchanged. **New:** a `HartWmClient` (Python) wrapping the
  `com.hart.Compositor` D-Bus/socket API, registered in `ServiceRegistry`.
- **Reuse:** **REUSED VERBATIM:** the entire BRAIN domain (`create_recipe` / `reuse_recipe`
  / `lifecycle_hooks` / `agent_daemon` / dispatch / MCP). The WM IPC is a **new thin
  capability** the brain gains, not a rewrite. A2UI `agent_ui_update` keeps its exact
  contract; a new component verb family (`window.*`) **extends — never replaces** —
  `COMPONENT_TYPES`.

### L4 — Freedesktop portals & session services
- **Role:** give native (non-HART) apps standard OS plumbing — screenshot, screencast,
  file-chooser, settings (dark/light/accent), notifications, inhibit/idle.
- **Tech:** `xdg-desktop-portal` + a `hart` backend (`xdg-desktop-portal-hart`) bridging
  `org.freedesktop.portal.Settings` to `ThemeService` tokens and `Notification` to the
  existing realtime/SSE+WAMP fabric; `wlr-screencopy` for screenshot/cast routed through
  `core.ai_sensing` so the kill-switch governs app screencast too.
- **Reuse:** reuses `ThemeService` (`--hart-*` tokens), `NotificationService.create`,
  `core.ai_sensing` as the single screencast/mic gate, `realtime.publish_event`. **Honest
  caveat:** GTK/Qt portal theming and the screencast backend are **NET-NEW code**, labeled
  as such — the only thing *preserved* is in-shell token application.

### L5 — App install / marketplace / bridge
- **Role:** install and surface every app type; route intents/clipboard/files across
  subsystems; recipes-as-apps economy.
- **Tech:** `AppInstaller` (7 handlers), `AppRegistry`/`AppManifest`, `app_marketplace`,
  `app_bridge_service :6810`, `ExtensionSandbox`.
- **Reuse:** **REUSED VERBATIM.** The only change: a freshly-installed native app's toplevel
  is now a real compositor window the `AppRegistry` can ask the WM to summon, **alongside**
  the existing iframe panels.

---

## 3. Compositor Choice & Rationale

**Choice:** Smithay (Rust library compositor); build **HART-comp** as a thin in-ecosystem
compositor — **but** ship `sway`-as-Tier-1 as the fast path so OS-native windowing is **not
blocked** on a first-ever Rust-Nix bring-up.

### 3.1 Why Smithay is the eventual moat

1. **Smithay is a LIBRARY, not a finished WM.** We need a custom, **agent-driven** window
   tree and a first-party IPC (the moat). Smithay lets us own the event loop, the
   layer-shell / xdg-shell handling, and the placement policy directly in Rust with no
   upstream WM semantics to fight — exactly the control GNOME/Mutter and a forked sway won't
   cede.
2. **Memory-safety + a clean software-render path.** Smithay's renderer abstraction supports
   a pixman/software backend, letting us reproduce the verbatim cage software-GL guarantee
   (paint on any broken GPU) as a first-class, type-checked code path rather than an env-var
   prayer.
3. **The glue IS the value.** Smithay is lower-level than wlroots, so we write more glue
   (XWayland, decorations, layer-shell stacking). That glue is *where the AI-native WM value
   lives*, and the never-fail tiering means a half-finished HART-comp can never brick a user.

### 3.2 The honest correction to the original rationale

The original decisive reason — *"in-ecosystem already; reuses existing Rust CI/packaging
muscle; adds no new toolchain class"* — is **factually wrong about this repo**. There is
**zero** `buildRustPackage` / `rustPlatform` / `cargoHash` anywhere in `nixos/` (verified),
and the `claw_native/rust` crates + `hevolvearmor` are **not** referenced by any `.nix`
module. So HART-comp would be the **first** Rust-in-Nix build in the tree — a brand-new
toolchain class, a new bundle-accounting surface, and a new #70-VM-test-eval-gate risk
**with no existing precedent to reuse**.

**Therefore the decision is re-weighted, not abandoned:**

- **Establish the precedent first** — Phase 3 lands `buildRustPackage` for an **existing**
  crate (`claw_native/rust`) with a fixed `cargoHash` under the pinned nixpkgs `50ab793`,
  proving the crate graph resolves on the June-2025 pin *before* HART-comp depends on it.
- **Ship sway as Tier-1 to deliver agent-driven windowing NOW.** Tier-2 already mandates a
  working sway + layer-shell setup that runs the same shell and shims tile/summon via
  `swaymsg`. So sway-as-Tier-1 delivers the OS-native moat immediately; Smithay HART-comp is
  a **later upgrade** behind the same never-fail watchdog.

### 3.3 The never-fail floor below HART-comp

- **Tier 2 — sway** (proven wlroots WM) in single-output kiosk config running the **same
  glass shell** as a layer-shell client; covers the case where HART-comp crashes but the
  GPU/Wayland stack is fine. WM IPC `tile`/`summon` map onto `swaymsg` — the moat is reduced
  but not gone.
- **Tier 3 — the EXACT cage + WebKitGTK + forced-software-GL session that ships today**
  (`hart-liquid-ui.nix`), the audited never-fail paint floor, for the broken-GPU/llvmpipe
  case.
- The watchdog (an **out-of-process** greetd/systemd supervisor — **not** `node_watchdog`,
  see §6) drops one tier per crash-loop and latches, so a Smithay regression degrades
  gracefully to today's known-good behavior instead of a black screen.
- **GNOME remains the user-selectable greeter fallback** exactly as `desktop.nix` already
  guarantees — the ultimate human escape hatch.

---

## 4. Complete Migration Map

Every capability in the system, its **disposition**, its **new home**, and **how** it gets
there. The reader can confirm nothing is lost.

Legend: **P** = preserved (verbatim or near-verbatim) · **M** = migrated (moves home,
behavior preserved) · **E** = enhanced (gains capability, never loses).

### 4.1 The glass shell & desktop UX

| Capability | Disp. | New home | How |
|---|---|---|---|
| Glass Desktop Shell (`render_desktop_shell`, tiers, start menu, panels) | **M** | L2 layer-shell background surface, served unchanged from `LiquidUIService :6800` | Replace cage's single fullscreen WebView with a layer-shell-anchored WebView (BACKGROUND layer, exclusive zone 0). Same HTML/CSS/JS, same `/shell/static`. The page **is** still the desktop; it now sits beneath agent-placed native windows. |
| Voice Orb Visualizer (`voiceOrbViz.js`, 3-band WebAudio) | **P** | L2 top-layer region inside the shell WebView | Verbatim canvas renderer in the same WebKitGTK context. Only change is z-order: the orb's hero surface can be promoted to a top layer-shell overlay so it floats over native windows when speaking (subject to the Phase-4 single-vs-dual-WebView decision). |
| Boot Splash (`hartBootSplash.js` + Lottie, 8s ceiling) | **P** | L2 in-shell overlay; compositor shows a solid-color KMS splash before the shell paints | JS overlay unchanged. HART-comp additionally paints a brand-color clear before the first WebView frame so there's no flash-of-black. |
| Onboarding ceremony "Light Your HART" | **P** | L2 in-shell (primary); L3 `onboarding_routes` unchanged | In-shell `hartOnboarding.js` is canonical and unchanged. The opt-in native GTK4 pre-launch becomes "run as a HART-comp toplevel before the shell layer mounts" — same timeout-bounded, Esc-skippable contract. |
| Lock screen / password / desktop widgets (`hartSessionUI.js`, salted SHA-256) | **E** | L2 lock overlay (UX) + L1 input-grab + display-manager/PAM (real boundary) | `hartSessionUI` stays the UX lock. HART-comp can grab input and blank app surfaces during lock via `ext-session-lock-v1`, closing the "lock is a removable div" gap **without** changing that PAM/display-manager remains the true auth boundary. See §7 for the autologin honesty gap + fix. |
| Desktop icons (`hartDesktop.js`, grid-snap, persisted) | **P** | L2 | Pure client JS over `HartSession` blob; renders in the background shell surface unchanged. |
| Virtual desktops / workspaces (`hartWorkspaces.js`) | **E** | L2 JS UI ↔ L1 **real** compositor workspaces | **Augment, not replace:** keep client-side panel show/hide on every tier (incl. cage); ALSO drive `MoveToWorkspace` for real native windows **only when** `com.hart.Compositor` is feature-detected. One source of truth per object class: shell panels = client state, native windows = compositor. |
| Themes & wallpaper gallery (`hartPersonalize.js`, 8 presets, `ThemeService` `--hart-*`) | **E** | L2 + L4 portal Settings | `ThemeService` stays the single token source. `org.freedesktop.portal.Settings` bridges tokens so native GTK/Qt apps follow the HART theme — **labeled NET-NEW** portal code, not "free." |
| Dock magnification (`hartDock.js`) | **P** | L2 | Client JS enhancing taskbar chips; unchanged. |
| Shared session state (`hartSession.js` one-blob, one-writer-per-key, `HartTimeoutSignal`) | **P** | L2 + L3 `shell_os_apis` session-state | Unchanged. `HartTimeoutSignal` still papers over WebKitGTK's missing `AbortSignal.timeout` (same engine). |
| Voice I/O pipeline (`toggleVoice`/`speakText`/barge-in/hybrid TTS, mic kill-switch) | **P** | L2 JS + L3 `/api/voice` gated by `core.ai_sensing` | Unchanged semantics. `Super+Space` global push-to-talk now registered as a compositor global keybind (L1) so it works even when a native app has focus — an enhancement. |
| Floating assistant chat / **the intent surface** (`acSend`, 10 pills, fast-paths, agent pill) | **M** | L2 top-layer surface + L3 brain CREATE/REUSE | **The 4.5→7 crux — INVERTED, not "preserved."** Today `acSend` is a chatbot-launcher: `open X`→`openPanel`, theme-words→preset, else POST `/api/agent/ask`→`/chat` (`prompt_id='desktop_agent'`) → reply rendered as a **text bubble + TTS**. **Migrated deliverable (Phase 2/8):** the default path routes free-form intent through the brain's `intent→decompose→compose` loop and renders the result as **composed UI fragments / placed windows / actions via `agent_ui_update`** (once the transport is fixed), with `open <app>` demoted to the explicit fast-path **fallback** that may also `→ WM IPC summon` a native window. Until `acSend` itself stops being string-match-or-text-bubble, the surface is still "orb-as-chatbot-launcher" — so this row is **M (Migrated)**, NOT E. |
| Panel manifest (`PANEL_MANIFEST` + `DYNAMIC_PANELS` + ~60 `SYSTEM_PANELS`) | **P** | L2/L3 `shell_manifest` + `shell_*_apis` | Unchanged. Session-management panels (resolution/brightness/scale) now have a real backend: they call compositor IPC instead of being cosmetic. |
| ~63 shell OS/desktop/system APIs (wifi/audio/bt/power/display/files/terminal/taskmgr) | **E** | L3 `shell_*_apis`; display/output/input ones gain L1 backends | Most endpoints unchanged. Display-mode/brightness/scale and a NEW "window manager" panel (list/focus/close/tile) wire to `com.hart.Compositor` — the task-manager panel can now actually kill a window. |
| HART Design System (MD3 tokens, `ds-*` primitives, `dsBtn`/`dsModal`/`showToast`) | **P** | L2 inline `_CSS_DESIGN_SYSTEM` + `ThemeService` | Verbatim; same WebKitGTK engine renders it identically. |
| Generative & adaptive UI (`ContextEngine` + `generate_ui`, `/api/ui`) | **P** | L3 — terminal/Conky degraded surface only | **NOT resurrected** as a boot path (zero desktop frontends fetch `/api/ui`). Remains the static/Conky fallback for headless/edge. A labeled source-guard (Phase 0) freezes this. |
| Multi-surface render + kiosk compositor (WebKitGTK + cage, software-GL hardening, health-wait, Nunba fallback) | **M** | L1 HART-comp primary; cage = Tier-3 floor | cage moves from PRIMARY to the never-fail Tier-3 floor — its software-GL/DMABUF-off/health-wait/Nunba-fallback hardening preserved **verbatim** as the bottom tier. HART-comp reproduces the same guarantee at Tier-1. Browser + Nunba-desktop surfaces untouched. |
| `/shell/static` static asset route (`static_url_path` fix, the dead-husk lesson) | **P** | L2/L3 `LiquidUIService._create_flask_app` | Unchanged and load-bearing. The "verify served assets with a real `test_client` fetch — inline-render is blind" lesson is carried into the compositor smoke test. |

### 4.2 The brain (recipe pipeline, FSM, daemon, A2A, MCP)

| Capability | Disp. | New home | How |
|---|---|---|---|
| 15-state `ActionState` FSM (`validate_state_transition`, `force_state_through_valid_path`, recovery edges, embodied + preview states) | **P** | L3 `lifecycle_hooks` | Verbatim — every recovery edge (`recipe_requested`, force-to-terminal, post-terminated no-op) and the embodied/preview states carried unchanged. The compositor does not touch the FSM. |
| Recipe CREATE/REUSE + autogen GroupChat + on-disk format + identifier semantics | **P** | L3 `create_recipe`/`reuse_recipe` + `prompts/*.json` | Verbatim. `prompt_id`(int\|UUID)/`flow_id`/`action_id`/`session_id` semantics and recipe filenames untouched. |
| Trace-derived action banking (`_bank_action_recipe_from_trace`, no-op marker, secret redaction) | **P** | L3 `create_recipe` | Verbatim — the flywheel-completion fix is independent of the display layer. |
| `should_yield_to_user()` single yield gate (4 reasons + `core.foreground`) | **E** | L3 dispatch + L1 compositor foreground signal | All 4 reasons preserved. **Augment, never replace:** the HTTP-request-driven `enter_foreground` (`is_genuine_user_request`) stays the canonical, **display-independent** writer that works on every tier; the compositor focus/idle feed is ONE more input on desktop tiers only. See §5 + §8. |
| Autonomous daemon supervisor + ~60 goal types + 3-tier dispatch + 4-tier cohorts + speculative draft-first | **P** | L3 `agent_daemon`/`goal_manager`/dispatch/`speculative_dispatcher` | Verbatim. The flywheel is display-agnostic; the compositor is just another surface it can drive. |
| A2A skill registry + `TaskDelegationBridge` (delegation-resume-through-valid-FSM-path) + 96 experts | **P** | L3 `internal_comm` + `expert_agents` | Verbatim — including the reapplied delegation-resume commit `1c559b8`. |
| Distributed coding agent + `claude_hive_session` + tool backends + `SelfHealingDispatcher` | **P** | L3 `coding_agent` | Verbatim; idle-compute coding + self-heal loop untouched. |
| SmartLedger persistence + dynamic tasks + `restore_action_states_from_ledger` + dashboard hierarchy | **P** | L3 `helper_ledger` + `agent_ledger` | Verbatim — cross-session recovery is critical and display-independent (statelessness wiped the flywheel before; never again). |
| MCP server surface + steering/co-pilot inject bridge | **E** | L3 `mcp` + `instruction_queue` | All existing MCP tools preserved. NEW optional MCP tools (`place_window`/`tile`/`summon`/`list_windows`) let Claude co-pilot the desktop layout via the same WM IPC — audited and guardrail-gated identically. |
| Google A2A agent cards + dynamic registry | **P** | L3 `google_a2a` | Verbatim A2A blueprint routes. |
| Spark economy completion-charge + budget gate | **P** | L3 `budget_gate` + `revenue_aggregator` | Verbatim charge-on-COMPLETED-work; local = 0 Spark. The flywheel-starvation risk (compositor CPU vs the local 4B) is tied to this in §7 + §8. |
| World Model Bridge embodied loop + safety envelope (RALT witness, consent/CCT gates, ConstructiveFilter, tamper halt) | **P** | L3 `world_model_bridge` + lifecycle PREVIEW gate | Verbatim; ML stays in HevolveAI, the safety envelope + preview gate untouched. |
| Visual/time/scheduled agents + recipe-carried `scheduled_tasks` (APScheduler cron in REUSE) | **P** | L3 `create_recipe`/`reuse_recipe`/`helper` | Verbatim. Visual agent's screen-capture path now ALSO flows through `core.ai_sensing` + compositor screencopy — consistent with the kill-switch (and one of the gaps Phase 2 closes first). |
| A2UI protocol (`agent_ui_update`, 30+ `COMPONENT_TYPES`, fanout, 5/agent cap, `renderAgentOverlay`, 16+ callers) | **P (ingress) / E (transport)** | L3 brain (ingress) → L2 top-layer overlay surface | `agent_ui_update` keeps its **exact signature, allowlist, cap, and fanout**. Overlays render into a top layer-shell surface instead of an in-kiosk div stack. **The PUSH gap is a real, untested migration deliverable (Phase 2), NOT "already FIXED."** See §7 preservation-gap #1. |

### 4.3 App install, marketplace, bridge

| Capability | Disp. | New home | How |
|---|---|---|---|
| Unified `AppInstaller` (7 handlers) + `InstallRequest`/`Result` + auto-register | **P** | L5 | Verbatim. Dataclasses/enums, magic-byte detection, per-platform commands, auto-register unchanged. |
| App-install HTTP APIs (`/api/shell/apps/*` AND legacy auth-gated `/api/apps/*` + permissions) | **M** | L5 — consolidate onto one route surface | Deliberately consolidate, preserving **both** the `@_require_shell_auth` gate **and** per-app permission endpoints **and** detect/history/platforms — keep `test_shell_route_no_collision.py` alive. No behavior dropped; the duplication is resolved. |
| AI App Marketplace (recipes-as-apps, publish/install/rate/compare/trending, recipe→`prompts/` wiring) | **P** | L5 `marketplace_bp` + `agent_data/marketplace/*` | Verbatim. Distinct from the binary installer; recipe-wiring into `prompts/` for REUSE unchanged. |
| Marketplace 90/9/1 Spark split + reconciliation | **P** | L5 `app_marketplace` + `revenue_aggregator` constants | Verbatim; constants stay sourced from `revenue_aggregator`, never re-hardcoded. |
| Cross-store manifest generators (Play/MSIX/Apple/Web/Flatpak) | **P** | L5 | Generation-only, unchanged; the known limit (no automated store upload) carried forward explicitly. |
| `AppPromotionAgent` + `SEED_APP_MARKETPLACE_PROMOTER` | **P** | L3 daemon goal | Unchanged; gated on the same flywheel completion rate. |
| Extension system (`.hartpkg`, `ExtensionSandbox` AST blocklists, lifecycle, registry) | **P** | L5 `core/platform/extensions` + `extension_sandbox` | Verbatim — the AST block → event → audit chain untouched; it is the only sandboxed-code install path. |
| Universal `AppManifest` + `AppRegistry` (9 types, `purpose` field, `to_shell_manifest`) | **P** | L5 `core/platform` | Verbatim; the single catalog the shell reads. Gains a **window-handle field** on launched native apps so the WM can map manifest ↔ toplevel. |
| Browser/PWA install (Flathub browsers + `hart-pwa-install chromium --app`) | **P** | L0/L5 `hart-subsystems` | Unchanged; PWA `.desktop` launches a chromium toplevel HART-comp now manages natively. **No `.crx`/`.xpi` path** — carried as a known gap. |
| App Bridge (`CapabilityRegistry`/`SemanticRouter`/`UnifiedClipboard`, `/v1/* :6810`, D-Bus, hardened unit) | **E** | L5 `app_bridge_service` + L1 compositor clipboard authority | Unchanged API. `UnifiedClipboard` is corrected to its real baseline (in-memory string + a side `wl-clipboard` sync unit, **not** a poller) and gains `wl_data_device` integration as explicit Smithay glue — keeping the in-memory + sync-unit fallback so cross-subsystem paste never regresses to nothing. |
| NixOS subsystem layer (Flatpak/AppImage/Android-ART/Wine/PWA/Darling) | **P** | L0/L5 `hart-subsystems.nix` | Verbatim. **Honest limits carried forward UNCHANGED, not silently fixed:** Android ART is still `exec sleep infinity` inert; Wine success = unconditional `True` at the installer layer; macOS Darling default-off; no `.crx`/`.xpi`. Documented as openRisks (§9), not claimed solved. |
| Unified kernel layer (binder/ashmem/PE-binfmt/GPU/Landlock) | **P** | L0 `hart-kernel.nix` | Verbatim; HART-comp consumes the surfaces (XWayland for Wine toplevels, ART surfaces when/if real). |

### 4.4 Security (cross-layer, never weakened)

| Capability | Disp. | New home | How |
|---|---|---|---|
| Structurally-immutable Guardrail Network (10 nodes, `_FrozenValues`, 33 rules, hash chain, 300s re-verify, i18n overlay) | **P** | Security (imported at boot) | **READ-ONLY, VERBATIM, NEVER WEAKENED.** The 4-level immutability, hash re-verify, and gossip rejection are untouched. HART-comp boots only **after** `full_boot_verification` passes — the guardrail kernel gates the display, not vice versa. |
| Master key trust anchor + PUBLIC verification (kill switch) + `MASTER_PUBLIC_KEY_HEX` | **P** | `security/master_key.py` | **OFF-LIMITS to AI, carried VERBATIM.** The private key is never read/loaded/signed-with by any layer including the compositor. `MASTER_PUBLIC_KEY_HEX` immutable. |
| Cert-chain delegation + domain challenge, node integrity + code-hash + pycache purge, origin attestation, `RuntimeIntegrityMonitor`, `NodeWatchdog` | **E** | Security/* + L1 watchdog integration | All verbatim. **Enhancement:** node integrity is extended to cover the HART-comp **Rust binary** (today `compute_code_hash` hashes only `.py`); the binary's content-addressed Nix store hash joins the signed release manifest + becomes a brand/origin-attested artifact so a forked compositor fails peer attestation. `full_boot_verification` gates HART-comp on its own signature. See §7 preservation-gap "compositor binary integrity." |
| HSM signing, immutable audit log, prompt-injection/MCP-sandbox/action-classifier/DLP, crypto-at-rest/secrets/redactor/edge-privacy, Flask middleware/rate-limit/JWT/TLS, pre-trust contract, LUKS, AI-governance | **P** | Security/* + L0 `hart-luks.nix` | Verbatim across the board. AI-EXCLUSION-ZONE markers on HSM/master-key respected. LUKS2/TPM2 FDE unchanged. The compositor adds no new trust assumptions; it runs as a hardened systemd unit like every other `hart-*` service. |

### 4.5 NixOS topology & sessions

| Capability | Disp. | New home | How |
|---|---|---|---|
| NixOS flake topology + 12 configs + 14 image formats + nixpkgs pin `50ab793` + VM-test eval gate (#70) | **E** | L1 new `hart-comp.nix` module + flake | Add **one** module (`hart-comp.nix`) packaging Smithay HART-comp via `buildRustPackage`. `desktop.nix`'s `defaultSession` flips to `hart-comp` **only after VM-proof**, with sway + cage + GNOME as ordered fallback sessions. The pin, image matrix, and #70 eval-gate discipline are preserved; the new module must pass the same VM-test eval gate. |
| Default-session swap + cage software-GL hardening + LiquidUI systemd service + A2UI D-Bus + OS-mode port registry + de-NixOS branding + Plymouth + first-boot ceremony + every `hart-*.nix` module | **M** | L1/L2/L3 + `nixos/*` | `defaultSession` moves to `hart-comp` with the FULL fallback ladder preserved. cage hardening preserved verbatim AS the Tier-3 floor. LiquidUI systemd unit unchanged (NO-WatchdogSec lesson kept). `com.hart.LiquidUI` A2UI D-Bus interface preserved **and** joined by `com.hart.Compositor`. Port registry, branding completeness, Plymouth logo, signed first-boot audit, immutable Ed25519 node key — all carried verbatim. |

### 4.6 The social / economy / channels organism

| Capability | Disp. | New home | How |
|---|---|---|---|
| The entire social platform (~95 tables, ~493 routes, 31 channels, resonance/spark, revenue rails, AP2, gamification, games, federation, geo-social, LiveKit calls, E2E DMs, consent, realtime WAMP/SSE fabric, dashboards/moderation, growth machinery) | **P** | L5/L3 `integrations/social/*` + channels + ap2 + trading | Verbatim — the compositor does not touch the social/economy organism. **Corrected framing (it does NOT "ride for free"):** these surfaces reach the user through the **realtime fabric** (`realtime.publish_event` → WAMP `com.hertzai.hevolve.social.{user_id}` + `broadcast_sse_safe`) and **external clients** (Nunba desktop, landing-page React, Hevolve_React_Native mobile), NOT the on-box compositor. The central/server/edge tiers are **headless** (`xserver.enable=false`, no LiquidUI). §8 elevates the realtime fabric to an explicit cross-cutting `mustNotLose` dependency that any brain process re-topology must be proven NOT to sever. The known Android topic-namespace mismatch (`com.hertzai.pupit.{user_id}`) is carried as a known-open item, not silently assumed fixed. |
| **All remaining brain subsystems** — `integrations/browser_research/` (6), `integrations/openclaw/` (6), `integrations/agent_lightning/` (6), `integrations/ui_actions/` (3), `integrations/skills/` (2) | **P** | L3 `integrations/*` | **Verbatim — the compositor does not touch them** (closes the validator's exhaustiveness gap: these 5 had real code but no line-item before). They are pure brain capabilities (web research, OpenCLAW native bridge, RL training hooks, UI-action primitives, skill packs); the additive compositor + the wholesale-preserved brain leave them untouched. Any brain subsystem not separately line-itemed above is **P verbatim** under this row. |

---

## 5. AI-Native WM IPC Design

> Full message shapes and the security boundary live in
> [`compositor/IPC_PROTOCOL.md`](../../compositor/IPC_PROTOCOL.md). This section is the
> *design intent*.

**`com.hart.Compositor`** — a D-Bus interface (with a Unix-socket twin for low-latency /
in-process use) exposed by HART-comp and driven by the Nunba brain (L3) via a
`HartWmClient` registered in `ServiceRegistry`. It is a **new capability that extends, never
replaces, the existing A2UI contract**: A2UI's `COMPONENT_TYPES` grows a `window.*` verb
family (`window.summon`, `window.place`, `window.tile`, `window.move_to_workspace`,
`window.focus`, `window.close`, `window.snap`).

### 5.1 The critical correction: the pipeline the verbs ride does not exist yet

The original design said `window.*` verbs flow through the "SAME `agent_ui_update`
validate/stamp/audit/fanout pipeline (so the 5/agent cap, XSS-escaping, guardrail check,
and `ImmutableAuditLog` logging all apply)." **Verified false:**
`liquid_ui_service.py:394` `agent_ui_update` does **only** a `COMPONENT_TYPES` allowlist
check + a 5-item ring-buffer truncation (a *display* cap, not a security control) + an
`EventBus` emit. There is **no** `GuardrailEnforcer`, **no** `ImmutableAuditLog`, **no**
server-side escaping (escaping is client-side in `renderAgentOverlay`).

So routing `CloseWindow` / fullscreen-takeover / `SummonApp` through this path **as-is**
would give destructive, OS-level desktop mutation the same unaudited, unguardrailed,
server-trusting treatment as a cosmetic card — a **real security regression**, since window
ops are far more dangerous than the cards the path was built for.

**The fix (Phase 2, before any `window.*` ships):**

1. Add `GuardrailEnforcer.before_dispatch` + `HiveCircuitBreaker.is_halted` (**fail-closed**)
   to the dispatch boundary.
2. Log every mutating window op to `get_audit_log().log_event()` with `agent_id` + verb +
   target window.
3. Route **destructive geometry** (close a user-focused window, fullscreen takeover) through
   the **existing** `PREVIEW_PENDING` / `PREVIEW_APPROVED` FSM gate + an A2UI approval
   component wired to a **real** audit sink.
4. Enforce a **real per-agent rate cap** on window ops (the 5-item ring buffer is not it).

Only when these controls actually exist does the "inherits audit/guardrail/cap" claim
become true.

### 5.2 Methods (semantics; shapes in IPC_PROTOCOL.md)

- `ListWindows()` → `[{handle, app_id, title, workspace, geometry, manifest_id}]`
- `FocusWindow(handle)`, `CloseWindow(handle)`
- `PlaceWindow(handle, {x,y,w,h} | named-zone)`
- `TileLayout(workspace, layout ∈ {cols, rows, grid, master-stack, fullscreen, bsp})`
- `SummonApp(manifest_id)` → launches via `AppRegistry`/`AppInstaller`, returns the new
  handle **only on a real toplevel-map event** (§5.4)
- `MoveToWorkspace(handle, n)`, `SwitchWorkspace(n)`
- `SetOutputMode(output, mode)`
- `Subscribe(events: window.opened/closed/focused/moved/unresponsive)`

Every mutating method is fail-closed behind `GuardrailEnforcer.before_dispatch` +
`HiveCircuitBreaker.is_halted`. `core.ai_sensing` remains **orthogonal and supreme** — no
WM verb can re-enable senses.

### 5.3 What the agent can do (the moat)

- **Summon any installed app by `manifest_id`** (Wine/Android/Flatpak/PWA/native) — *"open
  Blender next to my reference image"* → `SummonApp` + `PlaceWindow` into a named zone. The
  **agent**, not a human dragging, arranges the workspace.
- **Tile and arrange real native windows by intent** — *"put my three coding tools in a
  master-stack on workspace 2"* → `TileLayout(master-stack)` + `MoveToWorkspace`, executed on
  actual xdg-shell/XWayland toplevels.
- **Build a task-specific workspace from a recipe** — a CREATE-mode action banks "summon A +
  B, tile grid, switch workspace" as `window.*` steps so REUSE replays the exact desktop
  layout. The Recipe Pattern extended to window management.
- **Float context-aware A2UI overlays** pinned beside the relevant app window, because the
  compositor tells the agent *where* that window is (`ListWindows` geometry).
- **Co-arrange via Claude over MCP** — `place_window`/`tile`/`summon` MCP tools let the
  human's co-pilot steer the live layout, audited identically to in-hive agents.
- **React to focus/idle truth** — the compositor's focus + last-input feed *sharpens*
  `should_yield_to_user` (background goals yield the instant the user touches a window).
- **Lock/blank surfaces on the AI-senses cut** — when a human cuts `screen`, the compositor
  (via the portal screencast gate) really stops every app's capture, the orb closes its
  eyes; agents observe the cut but have **no verb to reverse it**.
- **Self-heal the desktop** — if a native app window hangs, the WM emits
  `window.unresponsive`; the self-healing daemon can offer to close + relaunch via the same
  IPC, audited.

### 5.4 No phantom windows (the honest-success rule)

`app_installer.py:_install_windows` returns `success=True` after `subprocess.run`
**regardless of whether any toplevel mapped** (the comment literally says *"Wine often
returns 0 even for interactive installers"*); `_install_android` returns `True` merely for
**copying** the apk. Therefore **`SummonApp` must key success on a real compositor
`window.opened` event** (toplevel mapped within a timeout), **never** on an installer/launcher
exit code. Otherwise the agent believes it placed a window that never appeared, then
`PlaceWindow`/`TileLayout` on a non-existent handle — silent no-op arrangement, the moat
failing invisibly.

Until that exists, native-window summon for **Wine/Android/macOS** is **openRisk vaporware**
(§9), not an `agentCanDo` claim. Android is `exec sleep infinity` (no ART/Waydroid) — there
is no surface to map at all.

---

## 6. Never-Fail Tiering

The screen is **never blank**. The constitution gates the display; a half-finished
compositor can never brick the box.

| Tier | What | When it runs | Window mgmt |
|---|---|---|---|
| **1** | **HART-comp** (Smithay/Rust): KMS/DRM + pixman software-render path. Boots **only after** `full_boot_verification` passes. Health = first WebView frame painted + a **real** `/shell/static` fetch (dead-husk-aware, not inline-render) within a bounded window. | Primary, once VM-proven | Full AI-native IPC |
| **2** | **sway** (proven wlroots WM) in single-output kiosk running the **same glass shell** as a layer-shell client. | HART-comp crashes, GPU/Wayland fine | `tile`/`summon` map onto `swaymsg` — moat reduced, present |
| **3** | **cage + WebKitGTK + forced software GL** (`WLR_RENDERER_ALLOW_SOFTWARE` / `LIBGL_ALWAYS_SOFTWARE` / `WEBKIT_DISABLE_DMABUF_RENDERER` / `HardwareAccelerationPolicy.NEVER`): the **EXACT** audited never-fail paint floor that ships today (`hart-liquid-ui.nix`), unchanged. | Broken GPU (nouveau-GSP, llvmpipe) | None (single fullscreen WebView) — desktop, orb, A2UI overlays, kill-switch, all shell JS still run because it's the same served HTML |
| **4** | Graceful-degradation ladder **inside** whichever tier paints: LiquidUI `:6800` → Nunba SPA `:5000` → terminal TUI → Conky metrics. | No GUI tier can start | n/a — if nothing else, Conky shows node/agent/LLM status |

### 6.1 The watchdog — corrected to a net-new out-of-process supervisor

The original design said `NodeWatchdog` "OWNS the never-fail tier-drop … reusing its
fleet-restart-budget + frozen-thread + LLM-call-awareness." **Verified false:**
`node_watchdog.py` is an **in-process Python thread** heartbeat supervisor (daemons call
`heartbeat(name)`; a silent thread is restarted via an in-process `restart_fn`). It has **no
concept** of a Wayland session, DRM/KMS process, `swaymsg`, `/var/lib/hart` tier-latch, or
greetd. A thread-restart supervisor **structurally cannot** drop a logind/display-manager
session from one compositor binary to another and latch the choice across boot.

**The tier-drop is therefore a NEW, concrete out-of-process supervisor** (`hart-session-
supervisor`, a systemd/greetd-level unit — **Phase 1**, built and VM-proven **before** any
compositor becomes default):

- reads `/var/lib/hart/session-tier` (`hart-comp | sway | cage`), launches the chosen
  session;
- on a crash-loop (**3 restarts / 5 min**) writes the next-lower tier, restarts the greeter,
  and **latches** the choice across boot — a Smithay regression silently lands on today's
  known-good cage behavior;
- exposes an **operator reset**: `hartctl session reset-tier` (clears the latch) + telemetry
  surfacing the current latched tier, so a transient Tier-1 bug cannot permanently mask as a
  downgrade with no recovery path.
- `NodeWatchdog` can at most **emit a "compositor unhealthy" signal** that the supervisor
  consumes; it never owns session selection.

**GNOME remains user-selectable at the greeter** (`desktop.nix` guarantee) as the ultimate
human escape hatch.

### 6.2 No WatchdogSec on serve_forever services

The audited `sd_notify`-once self-kill lesson is preserved: LiquidUI / ModelBus / AppBridge
use `Type=notify` `READY=1` **once** + `Restart=on-failure`, **NOT** a periodic systemd
`WatchdogSec`. HART-comp itself follows the same contract.

---

## 7. Preservation Gaps & Their Fixes

The gaps the code contradicted, each with severity and the concrete fix that lands **before**
the dependent capability ships. (Full detail per gap; these are the load-bearing
corrections.)

### 7.1 BLOCKER — A2UI cross-process push path
- **Risk:** `agent_ui_update` writes to a **process-local** `_agent_components` dict; the SSE
  consumer reads the **same** local dict — so it only works when producer and SSE server
  share a process. But the brain (`:6777`, ~10 call sites + `channels/agent_tools.py`,
  `oauth_api.py`, `agent_voice_bridge.py`, `model_orchestrator.py`) calls
  `ServiceRegistry.get('LiquidUIService')` which returns **None** in production (the only
  registration is a test stub). `theme_service.py` even hard-codes a cross-process HTTP POST
  to `:6800/api/theme` as a workaround. So **every** brain-originated A2UI card, approval,
  theme-apply, meet-copilot overlay, and OAuth-success card is silently dropped unless
  Crossbar WAMP happens to be up. The compositor re-host inherits the **broken** bridge.
- **Fix (Phase 2, before `window.*`):** verify runtime topology, then **either** (a)
  co-locate `LiquidUIService` into the brain process + `get_registry().register
  ('LiquidUIService', instance)` at boot, delete the `:6800` HTTP hack, prove
  `ServiceRegistry.get('LiquidUIService')` is non-None **and** a brain-side `agent_ui_update`
  is observable on the SSE stream; **or** (b) keep split and route ALL callers through one
  out-of-process transport (WAMP-required with a Crossbar reconnect supervisor, or HTTP
  `POST /api/a2ui`). **Pick ONE in code.** Stop claiming the in-process fix is "structurally
  done."

### 7.2 BLOCKER — AI-senses `screen` gate is a dead flag → native windows are a NET regression
- **Risk:** `core.ai_sensing` is per-process in-memory state; `allowed('screen')` has **zero
  enforcing consumers** (`remote_desktop/frame_capture.py`, `window_capture.py` do not check
  it). Today the cage floor has no native windows + no screencast portal, so third-party
  capture is **structurally impossible** — the kill-switch is total *for screen* because
  nothing can capture. Adding native windows + an L4 screencast portal **introduces a new
  capture surface the in-process flag cannot cut** — a **net security regression** of the
  constitutional control surface, the exact opposite of the claimed "strictly stronger."
- **Fix (Phase 2 wires existing consumers; Phase 7 builds the portal gate):**
  (1) wire the existing screen-capture paths to check `allowed('screen')` and refuse;
  (2) promote `core.ai_sensing` `'screen'` to a **real cross-process authority** (an owned
  UDS/D-Bus the portal MUST consult, fail-closed) **before** any screencast surface exists;
  (3) make `status()` report **observable** proof (active-capture-session count / blocked
  flag), not the unenforced flag. Do **not** enable third-party screencast until the
  cross-process gate fails-closed and is tested.

### 7.3 BLOCKER — `window.*` inheriting guardrail + audit (see §5.1)
- **Fix:** add the controls to the dispatch boundary first (§5.1); destructive geometry
  through the real PREVIEW FSM + a real audit sink; a behavioural test that an agent
  `window.close` on a user-focused window is **refused without approval and recorded in the
  audit chain**.

### 7.4 MAJOR — Glass shell "JS untouched / same WebView" is not buildable as written
- **Risk:** the entire shell is **one** HTML document where wallpaper, ambient, top-bar,
  panels, taskbar, hero/orb, A2UI overlays and lock-screen are `position:fixed`/`z-index`
  siblings in one DOM. `gtk4-layer-shell` anchors a **surface** (a whole WebView) to one
  layer — it **cannot** promote individual DOM elements to a higher layer than the rest of
  the same WebView. So "desktop below native windows, A2UI/orb above them" requires **either**
  two separate WebViews (which breaks the shared `window.*` globals every module reaches
  across — `openPanel`, `acSend`, `HartSession`, `toggleVoice`, `renderAgentOverlay`) **or**
  per-element promotion that doesn't exist. "JS untouched" + "overlays float above app
  windows" are **mutually exclusive** as written. Additionally the shell is **GTK3 +
  WebKit2-4.1**, so layer-shell forces a **GTK3 → GTK4 host-window port** that re-litigates
  the audited never-fail software-GL paint floor on an unproven GTK4 stack.
- **Fix (Phase 4, explicit budgeted milestone):** pick **one** z-order model in code —
  (1) single layer-shell surface, overlays/orb co-planar with the desktop (native windows sit
  ABOVE the orb — **stated honestly**), OR (2) two WebViews + a **real cross-WebView message
  bus** replacing the implicit single-window sharing (then **drop "JS untouched"** and budget
  the bus). Treat GTK3→GTK4 as its own milestone with its **own** broken-GPU/llvmpipe paint
  proof; keep the GTK3 cage path **verbatim** as Tier-3.

### 7.5 MAJOR — Workspaces / global keybinds degrade on the floor tiers
- **Risk:** `hartWorkspaces.js` is pure client-side JS that works on **every** tier incl.
  cage; **replacing** it with compositor IPC breaks workspace switching on Tier-3/Tier-2.
  Super+Space PTT and Ctrl+Alt+1-4 are document-level `keydown` listeners that fire **only**
  when the WebView has focus — once the shell is a background surface beneath focusable native
  apps, those keystrokes go to the **app**, not the shell.
- **Fix (Phase 6):** **augment, not replace** — keep client-side panel show/hide on every
  tier; drive `MoveToWorkspace` for real native windows only when `com.hart.Compositor` is
  feature-detected. Register **both** Super+Space PTT **and** the workspace keys as
  compositor **global keybinds** routing to the brain/shell; keep in-WebView `keydown` as the
  Tier-3 fallback. A smoke test asserts the advertised shortcut list (shortcuts panel) maps
  1:1 to registered compositor globals at Tier-1/2.

### 7.6 MAJOR — Compositor binary outside the integrity manifest
- **Risk:** `node_integrity.compute_code_hash` hashes **only `.py`**; `RuntimeIntegrityMonitor`
  re-verifies that hash every 300s. HART-comp is a Rust binary owning DRM/KMS, libinput, and
  the Wayland socket — the **most privileged surface on the machine** — entirely **outside**
  the manifest. A tampered compositor would not trip the tamper daemon, would not change
  `_GUARDRAIL_HASH`, and is not a `BRAND_MARKER_FILE`.
- **Fix (Phase 3):** include the HART-comp binary's content-addressed Nix store hash in the
  signed release manifest checked by `verify_local_code_matches_manifest` (add a non-`.py`
  artifact-hash list to `compute_file_manifest`); add the binary as a brand/origin-attested
  artifact so a forked compositor fails peer attestation; `full_boot_verification` gates
  HART-comp on its own signature.

### 7.7 MAJOR — Session lock / PAM boundary under autologin + cage
- **Risk:** `desktop.nix` sets `displayManager.autoLogin.enable=true` + `defaultSession=
  hart-shell`, so a normal boot **never hits a PAM prompt** — the "true PAM boundary" the
  honesty caveat leans on does not gate normal use. `/api/shell/session/lock` shells to
  `loginctl lock-session`, a **no-op under cage** (cage implements no session-lock protocol).
  The `ext-session-lock-v1` fix is scoped to Tier-1/2 only, leaving the audited Tier-3 floor
  with a lock button that does nothing.
- **Fix (Phase 7):** state honestly that an autologin+cage boot has **no real lock**, then
  close it — disable autologin (require PAM at boot) **or** add a PAM-backed kiosk lock so
  `lock-session` is not a silent no-op. For Tier-1/2, ship `ext-session-lock-v1` as actual
  code with a test that `loginctl lock-session` blanks app surfaces + requires PAM re-auth.
  PAM/display-manager remains the true boundary at every tier; the UX lock is never
  over-claimed as security.

### 7.8 MAJOR — Compositor render CPU vs the local 4B (governor/flywheel starvation)
- **Risk:** `should_yield_to_user` (`dispatch.py`) reason #4 trips when `governor_throttle <
  0.3`. A real Smithay compositor doing KMS/DRM scanout + native-window composition is a
  continuous CPU/GPU consumer co-resident with the single-slot local model. If HART-comp's
  CPU is attributed as "external" the governor reads the box as busy → the daemon yields
  forever (the `0 progress` dormancy class — MEMORY `8b17ee6`/`8496825`/`23b2619`); if
  attributed as "own" it never yields to a genuinely busy user. A starved flywheel means
  `charge_goal_work_completed` never fires → user **earnings** (the true flywheel measure)
  stall.
- **Fix (Phase 6):** explicitly account the compositor process in `resource_governor`
  external-CPU logic — compositor render is **excluded from BOTH** the "own idle work" signal
  **and** the "user is busy" signal; the real focus/idle feed is the actual yield trigger. A
  regression test mirrors the governor self-throttle fixes so a compositor render spike cannot
  silently re-create the dormancy bug; cross-reference `budget_gate.charge_goal_work_completed`
  as the thing that breaks if the flywheel starves.

### 7.9 MAJOR — Theming "OS-wide" / portal Settings is net-new, not "enhanced"
- **Risk:** the only thing preserved is in-shell token application; the GTK/Qt portal Settings
  bridge is **net-new code** with no current home, and on headless tiers there is no `:6800`
  and no portal — "OS-wide theming" is meaningless there.
- **Fix (Phase 7):** re-label GTK/Qt portal theming as a **NEW** capability + openRisk; keep
  `ThemeService` the single token source; ensure `theme.changed` still drives in-shell apply
  on every tier even with no portal/native-app surface. Add the portal backend to nix
  bundle-accounting (`hart-comp.nix`/flake) so it is not a missed module.

### 7.10 MAJOR — `agent_voice_bridge` / LiveKit meet-copilot window-pinning depends on stubs
- **Risk:** LiveKit SFU is up but there is **no egress worker** (`start_recording` returns
  `{ok:False}`); `agent_voice_bridge.py` is a degrade-to-no-op stub for bundled deploys, and
  its meet-copilot overlay emission goes through the same broken A2UI push. So the headline
  "pin a live transcript beside the call window" depends on two things that don't work.
- **Fix (Phase 8):** do **not** advertise meet-copilot window-pinning as delivered while the
  voice bridge is a stub + A2UI push is broken. Gate it behind the Phase-2 A2UI fix **and** a
  real `agent_voice_bridge`; add the LiveKit egress-worker gap to openRisks.

### 7.11 MINOR — `UnifiedClipboard` baseline mischaracterized
- **Fix (Phase 6):** correct the baseline (in-memory string + a side `wl-clipboard` sync unit,
  not a poller); specify `wl_data_device` integration as explicit Smithay glue; keep the
  in-memory + sync-unit fallback so cross-subsystem paste never regresses to nothing.

### 7.12 MINOR — generative `/api/ui` drift
- **Fix (Phase 0):** a labeled source-guard asserts (1) no desktop-shell frontend fetches
  `/api/ui` (it stays terminal/Conky-only) and (2) the Tier-4 Conky/terminal degraded surface
  still renders from `generate_ui`. Document `/api/ui`'s sole consumer so it isn't pruned as
  dead code during compositor churn.

---

## 8. Cross-Cutting `mustNotLose` Contracts

These are display-independent and **must survive any brain process re-topology** done for the
A2UI co-location fix.

1. **Realtime fabric.** `realtime.publish_event` → WAMP `com.hertzai.hevolve.social.{user_id}`
   + `broadcast_sse_safe` is the actual delivery spine for the social/economy organism and is
   consumed by **external clients** (Nunba desktop, landing-page React, mobile RN over
   SSE/WAMP), NOT the on-box compositor. A migration test must assert the social SSE + WAMP
   publish paths still fire after any L3 process change. The Android topic-namespace mismatch
   (`com.hertzai.pupit.{user_id}`) is a known-open item, not assumed fixed.
2. **Foreground yield gate stays HTTP-driven + display-independent.** `enter_foreground`
   (`is_genuine_user_request`) is the canonical writer that works on **every** tier including
   headless central Docker `:6777`, `server.nix`, `edge.nix` (verified `xserver.enable=false`,
   no LiquidUI). The compositor focus/idle feed is **additive on desktop tiers only**. A
   regression test asserts `should_yield_to_user` behaves correctly with **no** compositor
   signal registered. The architecture's word "replacing" becomes **"augmenting."**
3. **Guardrail kernel + master-key boot verification gate the session, never weakened**, at
   every tier.
4. **`core.ai_sensing` stays THE single gate with no AI write path** — no WM/portal verb can
   re-enable senses.
5. **AP2 human `authorize_payment` + `budget_gate` charge-on-COMPLETED-work** ride for free
   correctly (display-independent); the only residual is the §7.8 flywheel-starvation tie-in.

---

## 9. Open Risks (carried honestly, not claimed solved)

1. **Smithay glue burden is real.** XWayland, CSD-vs-SSD decorations, layer-shell stacking,
   multi-output, fractional scaling, clipboard/data-device — all things wlroots gives for free
   and we hand-write in Rust. **Mitigation:** ship Tier-1 feature-incomplete behind the
   never-fail tiering and grow it; first milestone is *"HART-comp paints the shell + manages
   ONE native window class (XWayland/Wine)."* Do **not** block the OS on a complete compositor.
2. **A2UI co-location vs split topology must be verified before commit** — it is a real,
   untested migration deliverable (§7.1), not an assumption.
3. **Android is genuinely inert** — `hart-android-runtime` = `exec sleep infinity`, no
   ART/Waydroid. Native Android windows are **vaporware** until a real surface exists.
4. **Wine launch-success is unconditional `True`** at the installer layer; **macOS/Darling is
   a stub** — `SummonApp` must report real toplevel-map success (§5.4), or agents think they
   placed a window that never appeared.
5. **Session-lock hardening (`ext-session-lock-v1`) closes the gap only for compositor tiers**;
   Tier-3 cage still has the div-only UX lock; autologin bypasses PAM at boot (§7.7). PAM/
   display-manager remains the true boundary; the UX lock is never over-claimed.
6. **WebKitGTK pin constraints survive the move** — missing `AbortSignal.timeout`
   (`HartTimeoutSignal` works around it), DMABUF crashes. Bumping the pin to package langchain
   could regress the ISO builder — the langchain packaging gap and the pin are coupled risks
   the compositor work must not destabilize.
7. **Smithay is the FIRST `buildRustPackage` in this tree** — crate-graph resolution under the
   pinned nixpkgs `50ab793` (June 2025) is **unproven**, coupled to the same pin-bump risk.
   Phase 3 must prove `buildRustPackage` on an **existing** crate first.
8. **Tier-drop latching could mask a transient Tier-1 bug as a permanent downgrade** — needs a
   clear operator "reset to Tier 1" path + telemetry (§6.1), or users silently run on cage
   forever after one bad boot.
9. **New nix bundle-accounting surface** — `hart-comp.nix` must be in the flake's `hartModules`
   + register its session via `passthru.providedSessions=['hart-comp']` (mirror `kioskSession`)
   or eval fails; must pass the #70 VM-test eval gate. A missed module = ISO eval failure.
10. **LiveKit egress-worker gap** — server-side recording returns `ok:False` today; meet-copilot
    pinning is gated, not delivered (§7.10).
11. **Compositor render CPU competes with the local 4B** — must be accounted in the same
    external-CPU attribution that fixed the governor self-throttle (§7.8).

---

## 10. Phased Build + Test Plan

> The phase list, deliverables, per-phase tests, never-break gates, and `verifyVia` are the
> canonical source in [`compositor/ROADMAP.md`](../../compositor/ROADMAP.md). This is the
> summary + the invariant verification loop.

**Ordering principle:** a never-fail floor exists from Phase 0; the tier-drop **supervisor** is
built and VM-proven **before** any compositor becomes default; the pre-existing
A2UI/ai_sensing/audit gaps are **closed before** `window.*` or portal screencast; Smithay is a
later moat upgrade behind sway-as-Tier-1. **Every phase is additive behind the watchdog; every
claim becomes a tested deliverable or an honestly-labeled openRisk.**

| Phase | Title | Gate to unlock the next |
|---|---|---|
| **0** | Floor-lock + CI/VM gate harness (today's audited cage as the immutable Tier-3 floor) | cage paints on llvmpipe VM + real `/shell/static` fetch 200; eval-gate canary green |
| **1** | Out-of-process tier-drop supervisor + watchdog signal (built **before** any compositor) | loop-kill fault injection lands on cage with the latch written; operator reset works |
| **2** | Close pre-existing A2UI/audit/ai_sensing gaps in the BRAIN (display-independent) | cross-process A2UI push observable on SSE; circuit-breaker-halted push refused + audited; capture refused when `screen` cut |
| **3** | HART-comp skeleton (tinywl-class) + software-render floor, **opt-in only** | `buildRustPackage` precedent lands; eval gate green; pixman paints on GBM-fail fixture; tampered binary trips integrity monitor; crash-loop lands on cage |
| **4** | Glass shell as layer-shell surface — the GTK3→GTK4 host-window port (explicit, budgeted, re-validated) | GTK4 layer-shell paints on llvmpipe + real `/shell/static` fetch; z-order model proven per chosen option |
| **5** | Native app windows (xdg-shell + XWayland) + honest install→real-window path | `SummonApp` reports FAILURE on a no-map fixture (no phantom window); iframe-panel path preserved |
| **6** | AI-native WM IPC wired to the brain — agents arrange REAL windows, fully audited | `window.close` on a focused window refused without PREVIEW + recorded in audit chain; workspaces still work on cage; governor does not trip from compositor render CPU |
| **7** | L4 portals + REAL cross-process AI-senses screencast gate + theming | screencast REFUSED when `screen` disabled (not just flag set); `ext-session-lock-v1` blanks surfaces + PAM re-auth; theme applies in-shell with no portal |
| **8** | Effects, polish, sway Tier-2 parity, meet-copilot pinning (behind its real deps), telemetry, route consolidation | effects degrade to flat (never black) on broken GPU; sway shim arranges windows; route-collision guard green |

### 10.1 The invariant verification loop (CI / VM)

Every phase is proven on real-ish hardware via a two-tier loop — **never** on inline-render or
grep.

1. **CI nixosTest VM (authoritative gate).** Each phase adds a `nixosTest` node built from
   `hartModules[]` alone (preserving the #70 eval-gate discipline), run on an
   **llvmpipe/software-GL** VM so the broken-GPU never-fail floor is exercised every run. Boot
   health is the **dead-husk-aware** check — a **real** `/shell/static` fetch (curl/`test_client`),
   not inline-render. **Fault injection is first-class:** loop-kill the active tier's binary and
   assert the **out-of-process supervisor** drops + latches to a tier that paints (never blank).
   The eval-gate canary fails loudly on any missing module so `hart-comp.nix` / `hart-portal.nix`
   additions can't silently break the ISO.
2. **Local QEMU-KVM (fast iteration).** `scripts/vm/boot-tier.sh` boots any tier headless with
   serial console for sub-CI-cycle debugging of compositor paint, XWayland app maps, portal
   screencast denial, PAM lock, and agent-driven window arrangement — the things that cannot run
   on the Windows dev box.
3. **Unit / behavioural (display-independent, runs on dev box + CI).** The brain-side controls —
   `agent_ui_update` guardrail + circuit-breaker + immutable-audit, ai_sensing cross-process
   screen gate, foreground-works-with-no-compositor, governor-doesn't-trip-on-compositor-render-CPU,
   `ServiceRegistry` non-None after boot, `SummonApp`-only-on-real-map, route-collision guard —
   all assert **observable side-effects** via mocks + call + observe, **never** string-survival.

**The loop order is invariant:** a phase is NOT done until its `nixosTest` is green on the
software-GL VM **AND** its supervisor fault-injection proves the floor still paints **AND** its
never-break gates pass — only then does the next additive phase unlock behind the same watchdog.

### 10.2 Honest hardware limits (why this is docs-only today)

- **No Wayland compositor, KMS/DRM, libinput, gtk4-layer-shell, or Smithay/Rust build can be
  compiled or booted on the Windows dev box.** Every L1/L2 paint, software-GL fallback, XWayland,
  portal screencast, and `ext-session-lock` test **must** run in a Linux CI `nixosTest` VM or
  local QEMU-KVM. Windows-side work is limited to Python brain logic, nix expression authoring,
  and unit/static guards.
- **No compositor C/Rust source is authored in these docs** — it must be compiled + verified in
  CI/VM, not authored blind.

### 10.3 First build (what to scaffold now, all display-independent / safe on the dev box)

1. **Phase-0 CI/VM gate harness** — a `nixosTest` node that boots **today's** cage `hart-shell`
   on an llvmpipe VM and asserts (a) first WebView frame paints + (b) a **real** `/shell/static/*`
   fetch returns 200 non-empty (carry the f294f52 dead-husk lesson from line 1). Add
   `scripts/vm/boot-tier.sh` for local QEMU-KVM headless boot with serial console.
2. **`/var/lib/hart/session-tier` state-file contract** (`hart-comp | sway | cage`) + scaffold
   `hart-session-supervisor` as a systemd/greetd unit **stub** that reads the latch and launches
   the chosen session — wire cage as the only consumer for now (`defaultSession` unchanged). This
   is the never-blank-screen skeleton.
3. **Eval-gate canary `nixosTest`** — assert `flake.nix` evaluates with `hartModules[]` +
   `providedSessions` intact, and a deliberately-missing-module variant fails loudly.
4. **Labeled source-guards** — (a) no desktop-shell frontend fetches `/api/ui`; (b) the Tier-4
   Conky/terminal degraded surface still renders. Freeze the "preserved, not resurrected"
   invariant before compositor churn.
5. **Begin Phase-2 brain fixes in parallel** (display-independent, safe on Windows-dev) — add
   `GuardrailEnforcer.before_dispatch` + `HiveCircuitBreaker.is_halted` +
   `get_audit_log().log_event()` + server-side sanitization into `agent_ui_update` with a
   behavioural test (circuit-breaker-halted push refused + audited), and the
   `ServiceRegistry.register('LiquidUIService', instance)` topology resolution with a non-None
   assertion test. **Do NOT build `window.*` until these are green.**

---

*This document is the persistent north star. It is faithful to the three invariants: HARTOS is
the heart and brain; nothing built is lost; OS-native, not kiosk — and never at the cost of a
blank screen.*
