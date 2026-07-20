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

## AGENTIC COMPONENT GENESIS -- the agent CREATES native components at runtime
Steward, 2026-07-20: "composable steerable drivable by agent and create native
components on the fly for new ui fragments and new views and new user
experience." This is strictly stronger than "the LLM sets uniforms on fixed
widgets": the agent must INVENT a component that did not exist at build time and
have it render NATIVELY at full speed. That requirement decides the stack.

**Why a widget toolkit cannot do this and shaders can.** A retained widget tree
(iced/slint/egui/GPUI) only composes what was compiled in; a new component means
a new Rust build. A shader/SDF pipeline can be CODEGENERATED and hot-compiled at
runtime, so a new component is DATA the agent authors. "Liquid" is also
literally an SDF look (merging blobs, refraction, glow), so the medium and the
requirement agree.

**THE SCHEMA ALREADY EXISTS -- DO NOT INVENT A SECOND ONE.** Nunba ships the
canonical server-driven Liquid UI at
`Nunba-HART-Companion/landing-page/src/components/shared/LiquidUI/`
(`ServerDrivenUI.jsx` ~1115 lines + `SocialLiquidUI.jsx`, exported via
`index.js`: `ServerDrivenUI`, `LiquidUIProvider`, `LiquidUIContext`,
`buildStylePresets`, `buildSocialTokens`). Its node vocabulary is the contract
the native renderer MUST speak, verbatim:
`view|box|column|row|grid|scroll|list|text|button|icon|image|input|spacer|
divider|card|chip|progress|animated`, plus the control forms `loop`/`repeat`
and `conditional`, plus the interaction contract (`node.action` + `onAction`,
`navigate`, `setState`, `bind`, and `{{variable}}` template interpolation in
both text AND style values).
Consequences, binding:
- The native SceneNode enum is a 1:1 mapping of THAT vocabulary -- not a new
  dialect. Same node types, same bindings, same action names.
- One agent payload renders on EITHER surface: Nunba's React renderer (app
  surfaces, per the split rule) or hart-comp's native scene (shell chrome).
  That is what makes the split invisible to the agent.
- `buildStylePresets` / `buildSocialTokens` are the style-token source; the
  native side consumes the SAME tokens (with the conky-themes palette) rather
  than forking a second token table (Gate 4).
- The SDF/L2 layer is an ADDITIVE node type for the liquid surfaces the web
  vocabulary cannot express (field/orb/ring/glass), NOT a replacement.
- Before implementing any node, READ ServerDrivenUI.jsx's case for it; parity is
  measured against that behaviour.

Four layers, each independently agent-addressable:
- **L1 SCENE (data).** Declarative node tree: what exists, where, z-order,
  bindings. This IS the A2UI payload, in Nunba's existing vocabulary above.
  Hot-swappable, no compile.
- **L2 FORM (SDF expression tree -> WGSL codegen).** The agent composes
  primitives (circle/box/field/noise) with operators (smooth-union, subtract,
  displace, refract, glow). We codegen WGSL from the tree, VALIDATE it (naga),
  compile OFF the render thread, and atomically swap the pipeline. A genuinely
  new visual component with zero OS recompile.
### THE SEMANTICS TRAP -- binding rule for `animated` and for user-driven motion
Adopting Nunba's vocabulary costs nothing at runtime (it is parsed once per
COMPOSE, never per frame) and nothing in nativeness (nativeness lives in the
renderer, not the words). The REAL risk of a web-shaped vocabulary is web-shaped
SEMANTICS smuggling web-shaped IMPLEMENTATIONS in. Concretely:

**If `animated` is implemented as "declare a transition, interpolate toward a
target", we will have re-imported the exact bug that made the orb drag
rubber-band on 2026-07-20 -- only in Rust, where it is harder to see.** That bug
was not a CSS defect; it was the MODEL: CSS animates TOWARD a value, but a drag
must BE the value.

Binding rules, non-negotiable:
1. `animated` maps ONLY to the L3 binding/clock system: the value is COMPUTED
   from (clock, energy, state) every frame. It never stores a "target" and eases
   toward it.
2. **User-driven motion NEVER routes through `animated`.** Drag, hover, scroll,
   resize and window-move write the scene transform DIRECTLY from the input
   event, applied the SAME frame (the <=1-frame input-to-photon NFR). No easing
   layer may sit between the pointer and the pixel.
3. Easing is legal ONLY for agent/system-initiated state changes the user is not
   physically dragging (a panel opening, a mood shift, a fade) -- and even then
   as a clock-driven binding, not a retained tween chasing a target.
4. Any node whose motion is user-driven must be reviewable against rule 2 before
   it lands; a milestone that adds a draggable/hoverable surface states in its
   audit how input reaches the transform.

Rule of thumb: implement each Nunba node the natively-optimal way (a `row` is a
cheap flex solve ON CHANGE, not a reflow; `list` is natively virtualized;
`scroll` is a transform on a retained subtree, not a per-frame relayout). Read
`ServerDrivenUI.jsx`'s case for the node to learn WHAT it must do -- never to
copy HOW the web does it.

- **L3 MOTION (declarative bindings).** Parameters bound to clocks + live signals
  (breathe 0.3Hz, energy from mic RMS, hover -> displace). Curves are data, so
  motion is steerable mid-flight.
- **L4 BEHAVIOR (sandboxed WASM).** When a fragment needs real logic beyond
  bindings, the agent ships a small WASM module against a TYPED capability
  surface (scene + IPC only; no raw syscalls).

**THE RECIPE PATTERN, APPLIED TO UI.** This is HART OS's own CREATE/REUSE
innovation at the interface layer: the agent CREATEs a component once (compose ->
codegen -> validate -> compile -> cache, keyed by tree hash), then REUSEs it for
free forever. The OS accumulates a UI vocabulary it invented, exactly as it
accumulates task recipes -- and peers can share components over the same hive
channels recipes already use.

**NEVER-FAIL RULES (non-negotiable -- this is the OS shell, not an app):**
- Validate + budget BEFORE compile: naga validation plus a static complexity
  budget (bounded loops, instruction ceiling). Unbounded work is rejected.
- Compile and first-draw happen OFF the render thread; the live frame never
  blocks on agent authoring.
- Per-component GPU time budget. A component that overruns is demoted to a
  static fallback and journaled -- never a frozen desktop (the exact class we
  just spent three boots escaping).
- Any agent-component failure degrades to the last good scene; the never-fail
  shell floor is untouched.
- Agent-authored components are user-visible, inspectable and REVOCABLE (the
  human stays in control -- the constitutional rule).

## THE SPLIT RULE -- what goes native vs what stays Nunba-canonical
Nunba is not a handful of pages: the landing-page tree carries ~36 Social
surfaces (Feed, Communities, Chat, Inbox, Profile, Marketplace, Recipes,
Wallet/Compute, Agents, Notifications, Settings, Onboarding, KidsLearning,
Mindstory, ...) plus Admin/Channels/Agent/payments. Porting that to Rust would
be insane and would fork the product. So the rule, once, for all of it:

**SHELL CHROME goes NATIVE. APPLICATION SURFACES stay NUNBA-CANONICAL, served
natively as microfrontends.**

- **Native (hart-comp scene, this program):** the things that must feel like the
  OS and are hit every second -- wallpaper/bloom field, orb + rings + voice
  states, top bar, taskbar/dock, window chrome + placement, workspaces, start
  menu, notifications/toasts, context menus, lock screen, and the FIRST-RUN
  ceremony (P9, from Nunba's canonical source, retiring the shell's copy).
  These are the surfaces where a frame of lag is felt.
- **Nunba-canonical (NOT ported, served as the app layer):** every product
  surface -- Social/*, Admin, Channels, payments, docs. HARTOS must NEVER
  reimplement one (the standing rule
  [[native_wiring_all_nunba_hartos_functionality_2026-07-09]]; onboarding was
  the template violation). They render as app CONTENT inside native windows the
  compositor owns and animates.
- **Consequence:** the WebView is never deleted. It stops being THE DESKTOP and
  becomes the app-content renderer for Nunba surfaces -- which is exactly what a
  browser is good at. Native chrome + web app content is the same split every
  real OS makes.
- **The seam:** native window chrome, motion, focus, and the orb overlay are
  compositor-side; the Nunba microfrontend paints only inside the content rect.
  A2UI can compose EITHER (native scene node OR a Nunba surface to open), so the
  agentic heart drives both halves through one contract.
- **Parity audit obligation:** any HARTOS-local page that duplicates a Nunba
  surface is a parallel path to RETIRE during this program, not to port.

## Milestones (each = shippable, OTA-able, tier-guarded)
M0 STACK SPIKE + scene plumbing: decide the renderer stack on EVIDENCE, not
   preference -- spike a wgpu+SDF aurora + breathing orb in hart-comp and MEASURE
   on the HD 620 (fps, frame time, input-to-photon), evaluating makepad + vello/
   cosmic-text for the typography leg. Then land the SceneNode enum + A2UI->Scene
   decoder. Feature-flagged `hart.comp.nativeShell` default OFF; cage/webkit tiers
   untouched. NOTE: with a shader field the bloom is ~free PER FRAME, so the
   compose-once CPU texture (bloom.rs, written 2026-07-20) is superseded by a
   live-animated shader field -- keep bloom.rs only as the pixman-floor fallback.
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
