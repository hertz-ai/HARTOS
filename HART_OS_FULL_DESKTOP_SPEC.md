# HART OS — Full Desktop & OS Program: Master Spec + Tracker

> Single source of truth consolidating EVERY rule and ask the steward gave the week of
> 2026-06-23 to 2026-06-28. Nothing here is lost. Companion memory:
> `memory/hartos_desktop_polish_program_remaining_2026-06-27.md`,
> `memory/hartos_liquid_ui_is_agentic_llm_is_heart_2026-06-27.md`,
> `memory/hartos_full_os_spec_app_integration_and_system_mgmt_2026-06-28.md`.
> Every workstream below is tracked to: implement -> self-review -> adversarial verify ->
> ~100% behavioral test coverage -> render proof -> real-hardware proof.
>
> **Home / desktop LOOK + LAYOUT + FEEL instructions live in the recovered checklist:**
> `docs/design/HOME_DESKTOP_DESIGN_CHECKLIST.md` (the anti-lost-instruction artifact -
> every desktop/home design ask, grouped + verbatim + message-numbered, plus the W1 audit).

## 0. THE RULES (binding — every task obeys ALL of these)

1. **Real shell, not mocks.** Work the actual `liquid_ui_service.py` + static JS/CSS, not flat HTML.
2. **Brand SPECTRUM, not monochrome.** Full Hevolve palette (teal/cyan/blue/violet/magenta/amber); mono was rejected.
3. **NO EM DASHES** (U+2014) in any user-visible product text.
4. **Verify by render AND on real hardware** (the flashed stick), not just inline.
5. **Never reinvent. Reuse the canonical path.** No parallel paths, no DRY violations. Extend existing helpers/modules.
6. **Zero regression.** Existing features must keep working.
7. **Intuitive by default.** Every capability discoverable with no docs (omnibox, context menus, taskbar, listings, labels, empty states).
8. **Be visual.** Render UI to PNG and show it; diagrams/tables over prose.
9. **~100% test coverage + verification.** Self-review, self-critique, adversarial pass, behavioral tests (no grep tests).
10. **Liquid UI is AGENTIC + FLUID, not a theme.** The local LLM COMPOSES the surface live via `agent_ui_update`.
11. **The local LLM is the HEART.** On-device, snappy; a slow LLM = a stuttering UI. It drives everything.
12. **No vertical PAGE scroll** of the desktop. Fixed canvas; rows scroll HORIZONTALLY; deep content opens in a panel.
13. **Zero Nunba code changes.** Every gap is a minimal, backward-compatible OS-side shim.
14. **All agents execute via HART OS's OWN agent_engine + local LLM**, never proxied to a remote backend.
15. **Every customization is an API; the API is the SDK** for app building. Where Nix gives a zero-customization default, bridge it in and ENHANCE it.
16. **100x optimization is first-class + MEASURED.** Buttery, snappy at every level: local-LLM latency (the heart), UI 60fps no-jank, fast boot/first-paint, instant launch, lean CPU/GIL/memory, ZERO hangs. Budgets: chat 1.5s, draft 300ms, cache <1ms. Measure before/after; never regress a budget. Reuse the existing perf infra (llama scheduler, foreground preempt, governor, lazy imports).

## 1. SHIPPED + FLASHED (verified green)

- **`61494f16`** (flashed to SanDisk, bootable): tap-to-launch fix (touch+mouse), right-click context menus, breathing no-ring spectrum orb, v3 cinematic CSS, Nunba-companion onboarding phase, determinate install% + all-5-platform verification, keyboard focus, native notification daemon (mako), dns/email/firewall, local semantic media index, centered plug-occluding heart logo. 548+530+35 tests green.
- North star: **best of all worlds (Netflix, Android HyperOS, macOS, Windows, Linux) + an agentic AI soul none of them have.**

## 2. IN FLIGHT

- **Compat audit `wxvqutc37`** (read-only): can the Nunba UI + AI setup wizard run on HART OS with ZERO Nunba changes + minimal backward-compatible OS shims. Produces the contract + gap list.

## 3. OUTSTANDING WORKSTREAMS (the queued gap-closing ultracode, after the audit)

- **W1 — The assembled Netflix HOME.** Fixed-height cinematic dashboard (NO vertical page scroll): value-forward earnings hero ("earned while you slept"), a "Continue" resumable row, horizontal-scroll rows, image-card desktop icons, top-bar restructure (brand · nav tabs Home/Agents/Apps/Hive/Earn · omnibox pill · small orb · avatar), promote the hero command bar to a top OMNIBOX with 3-way routing (deterministic apps/files · semantic media · ask-the-agent). Composed live by the LLM (`agent_ui_update`), not a fixed page.
- **W2 — Nunba UI bundled + served (zero Nunba changes).** Build the landing-page React dist (~few MB), bundle in the ISO, serve from LiquidUIService; every Nunba page a NAMED native microfrontend (Start menu + omnibox). NO AppImage (redundant; HART OS is the backend). Apply the audit's minimal OS shims only.
- **W3 — App-integration API + SDK + freedesktop bridge.** (A) scan XDG dirs (.desktop/MIME/autostart) so installed apps auto-appear with file-associations + context entries. (B) a Shell-integration API (Model-Bus shape) + SDK: apps register Start-menu entries, Settings panels, context-menu insertions, file handlers, tray/notifications.
- **W4 — Windows-style Start menu + Settings page.** Start: all apps, searchable, categories, pinned, power. Settings: the hub for everything below.
- **W5 — System management depth.** Devices (udev/lsusb/lspci), Accessories (libinput/audio), Disk (lsblk/udisks: partition/format/mount/usage), Paging/swap, Environment variables, DPI scaling, Font-size. Bridge each Linux/Nix primitive; runtime OR declarative (rebuild) change; all via one Settings API.
- **W6 — Feature gaps.** GPU acceleration (hart-comp is software-rendered today; hardware GL is the unsolved lever, REAL-HW-gated), Android-style notification SHADE UI (daemon exists), multi-monitor/multi-screen, edge-docking, embodied sensory signals (beyond mic+vision).
- **W7 — Netflix listings EVERYWHERE.** App Store + agents/recipes/communities/settings/file-explorer as image-card category rows with hover-expand.
- **W8 — Canonicalize the context menu.** Retire the duplicate shell `#ctx-menu`; `hartContextMenu.js` fully replaces it (the one DRY debt from 61494f16).
- **W9 — Realtime voice** (recognition + responses), wired to the agent_engine + local LLM.
- **W10 — Wire the semantic media index into the UI** (omnibox semantic results + the dynamic image cache feeding the home cards).
- **W11 — 100x OPTIMIZATION (performance), measured.** Hit + beat budgets across the stack: (a) local-LLM latency (first-token/chat snappy, the heart) via the llama priority scheduler + foreground preempt; (b) UI 60fps no-jank (GPU-friendly transforms; depends on the GPU lever W6); (c) boot to first-paint speed; (d) instant app/panel launch; (e) resource efficiency (CPU/GIL/memory, lazy cold-start); (f) ZERO hangs/freezes. Budgets: chat 1.5s, draft 300ms, cache <1ms. Reuse the existing perf infra; measure before/after; never regress a budget. The rule-16 work, tracked.

## 4. VERIFICATION CONTRACT (definition of done, every workstream)

implement -> SELF-REVIEW + fix -> ADVERSARIAL verify (no block) -> behavioral tests toward
~100% coverage of the new paths (import real code, mock boundary, assert behavior; NO grep
tests) -> RENDER proof (headless Edge/Chrome) -> REAL-HARDWARE proof on the flashed stick.
Reuse-first (no parallel path), zero regression, no em dashes, intuitive.

## 5. ALREADY PRESENT + RETAINED (do not rebuild)

movable multi-window + concurrency (panels + compositor xdg-shell + snap-zones), breathing
orb, sensory mic+vision cluster (`hartSenses`), responsive viewport layouts, native
notification daemon, the agent_engine (CREATE/REUSE recipe pipeline + 96 expert_agents),
the Model Bus, local llama-server + scheduler, the 82-endpoint social + channels.
