# NATIVE SHELL PARITY PROGRAM -- hart-comp renders the desktop

**Steward mandate (2026-07-20):** "i wanted a native ui and we are settling for
a lesser 1" -> "do it on what destination demands by creating parity with what
we have in html." This document IS that program: the HTML shell is the parity
SPEC; hart-comp (Smithay/Rust, the moat) is the renderer that reaches it.
WebView fixes remain bridge work; every milestone here retires a slice of the
browser.

## Why native (evidence, real-HW 2026-07-18..20)
Three boots, three web-stack-inherent failures: GSK-vulkan surface-lost freeze
on hover, WebKitWebProcess SIGSEGV from GStreamer capture on mic click,
rubber-band drag from CSS transition semantics. None of these classes exist in
a native scene owned by our compositor. hart-comp already owns scanout, input,
GLES + pixman floor, layer-shell, IPC (com.hart.Compositor), screencopy --
9,895 lines of proven Rust (compositor/src/).

## Architecture invariant (the agentic heart survives)
Liquid-UI stays AGENTIC: the local LLM composes the interface. Today it emits
A2UI -> HTML. The native shell keeps the SAME A2UI contract but renders to a
**native scene graph** in hart-comp: `A2UI component -> SceneNode (Rust enum:
Field, Orb, Ring, Card, Row, Text, Glyph)`. ONE composer (the LLM), two
renderers during the bridge, converging on one. No parallel A2UI dialect --
the schema is the existing one served by liquid_ui_service/A2UI.

## BINDING instruction sources (parity = these, not just the mock)
The steward's instructions are already documented; the native shell is built TO
them, milestone-audited against them exactly as the HTML shell is (CLAUDE.md
binding rule):
1. **docs/design/HOME_DESKTOP_DESIGN_CHECKLIST.md** -- THE instruction record
   (~55 rules, 11 groups a-k, steward's verbatim quotes). EMPHATIC rules (fixed
   one-screen canvas, no page scroll, breathing orb, no mic inside the orb,
   spectrum-not-mono, NO em dashes) bind every milestone; consult BEFORE any
   native design change, update it WITH any new instruction.
2. **HART_OS_FULL_DESKTOP_SPEC.md** -- the 15 RULES + W1-W10 workstreams.
3. **docs/architecture/HART_OS_NATIVE_ARCHITECTURE.md** -- the native
   architecture this program executes (hart-comp = the moat, L2 host windows).
4. **docs/design/LIQUID_UI_AGENTIC_FRAMEWORK_PLAN.md** -- the agentic A2UI
   contract the native scene graph must keep serving.
5. The Aura mock (steward's reference HTML) + the aura theme JSON -- the visual
   target the checklist's rules govern.
Every milestone closes with a checklist AUDIT (APPLIED/PARTIAL/MISSING), the
same W1-audit pattern the checklist already carries.

## Parity inventory (what the HTML shell has == the checklist)
P1 background: runtime-composed pre-blurred bloom field (hartBloom.js model:
   compose once, palette-driven --hart-amb-*, recompose on mood).
P2 orb: canvas viz (voiceOrbViz styles vibrant/ring-orb/nebula/minimal/pulse),
   breathing, energy-reactive, orbital dashed rings, hover scale, drag with
   clamp + persisted position, dock/compact/merge states.
P3 hero: brand mark, command bar/omnibox, chips, transcript reveal.
P4 home canvas: earnings hero, Continue row, Flagship agents row, cards with
   art/accents (Netflix-cinematic rows), See-all.
P5 top bar: brand | nav tabs | agent status | omnibox pill | orb-sm | avatar |
   tray glyphs | clock.
P6 system: senses cluster (mic/eye), toasts, panels/windows chrome, start menu,
   taskbar, workspaces, notifications.
P7 states: listening/thinking/speaking tints, reduced-motion, a11y, themes
   (conky-themes JSON = the palette source for BOTH renderers).
P8 voice: click-to-talk capture + TTS playback (native: PipeWire directly --
   no GStreamer-in-WebKit, the exact class that segfaulted).
P9 ONBOARDING -- the first-run "Light Your HART" ceremony. PARITY SOURCE IS
   **NUNBA'S CANONICAL MICROFRONTEND**, not the shell's copy: the HTML shell
   serves its own vanilla `static/hartOnboarding.js`, a REIMPLEMENTATION of
   `Nunba-HART-Companion/landing-page/src/components/HART/LightYourHART.js`
   (steward flagged 2026-07-19; the standing native-wiring task
   [[native_wiring_all_nunba_hartos_functionality_2026-07-09]] already names
   onboarding "the template case" for parallel paths to retire).
   Therefore: do NOT port the shell's copy. Inventory the CANONICAL Nunba
   ceremony (narration beats, language pick, name seal, companion progress,
   its motion + brand treatment) and build the NATIVE scene from THAT, so the
   native shell RETIRES the duplicate instead of immortalising it in Rust.
   Backend contract stays the ONE existing `/api/onboarding/*`.
   It is also the first thing a new user ever sees, so it carries the same
   perf bar as the desktop (no lag, no jank on the ceremony).

## Milestones (each = shippable, OTA-able, tier-guarded)
M0 scene plumbing: SceneNode enum + A2UI->Scene decoder in hart-comp; render
   via existing element pipeline (comp_core render elements). Feature-flagged
   `hart.comp.nativeShell` default OFF; cage/webkit tiers untouched.
M1 NATIVE BLOOM (first pixel): replace the solid splash clear
   (udev.rs ~line 422 SolidColorBuffer) with a bloom TEXTURE composed at
   runtime from the active theme's ambient palette (CPU gaussian once ->
   GLES texture element in render_all; pixman floor gets the same texture).
   Parity target: hartBloom.js output. Reads the SAME
   /run/hart + theme JSON sources (no parallel palette).
M2 native orb: energy-driven orb + dashed orbital rings as scene elements
   (SolidColor/Texture + shader later); input: click-to-talk via typed IPC to
   the backend; drag native (pointer delta -> node transform, 1:1 by
   construction).
M3 top bar + home rows as scene nodes; text via glyph atlas (cosmic-text or
   equivalent crate -- decide in M3 spike; fonts from the theme JSON).
M4 A2UI live: the LLM's compose payloads drive the native scene (same topics/
   endpoints the HTML consumer uses today); HTML shell becomes fallback tier.
M5 voice native: PipeWire capture + TTS playback in the backend, orb states
   over IPC; retire getUserMedia entirely.
M6 flip: nativeShell default ON on Tier-1; WebView demoted to app content +
   fallback tiers (never deleted -- it is the never-fail floor's renderer).

## Performance bar (steward 2026-07-20: "ultrafast and snappy closely mirroring
## the mock natively and no lag whatsoever") -- BINDING NFRs, gated per milestone
- 60fps sustained on the HD 620 GLES path; frame budget 16.6ms, p99 < 12ms
  measured by hart-comp's own frame timing (journal a violation counter).
- Input-to-photon: pointer/drag updates applied SAME frame (<= 1 frame latency,
  no animation easing on user-driven motion -- the CSS-transition drag bug class
  is structurally impossible: user input writes the node transform directly).
- Zero per-frame allocation in the render hot path; damage-tracked redraw only
  (idle desktop = zero repaint except the orb region's heartbeat).
- Expensive effects (blur/bloom) composed ONCE to textures at compose/mood time,
  never per frame -- the hartBloom rule, now enforced by architecture.
- Pixman floor stays usable: scene degrades (static bloom, no shader extras)
  but never drops below 30fps or blocks input.
- Every milestone ships with a measured fps + input-latency line in the journal;
  a regression fails the milestone (no "feels fine" sign-offs).

## Rules
- Ladder discipline: native shell rides Tier-1 only until proven; paint-watchdog
  + honest-ready markers apply identically (write shell-ready on first composed
  frame containing the scene).
- Zero parallel paths: palettes from conky-themes JSON; A2UI schema unchanged;
  IPC via the existing com.hart.Compositor socket + typed OS bridge.
- Every milestone lands with: cargo tests (scene decode, layout), a nixosTest
  (VM scanout of the scene), and a real-HW journal verification before the next
  starts (the dev-loop that debugged the web shell).
- CI: hart.comp Rust closure is warmed pre-ISO (48b73d6); nativeShell flag must
  never regress iso-desktop build time.

## Status
- 2026-07-20: program created (this doc). M0/M1 next; owner: hive session +
  steward review at each milestone flip.
