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
  The split is therefore invisible to the agent.
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
  compile OFF the render thread, and atomically swap the pipeline. A new visual
  component with zero OS recompile.
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
be insane and would fork the product. The rule, once, for all of it:

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

## THE BAR: better than Windows 12 and macOS (steward 2026-07-20)
Parity with our own HTML shell is the FLOOR, not the goal. The goal is a desktop
that beats the incumbents. That requires naming which fights are winnable,
because a small project that attacks their strengths loses.

**Where we do NOT compete (accept and route around):** driver/hardware breadth,
app-ecosystem size, decades of accumulated small-detail polish, enterprise
management + certifications, localization breadth. We buy these where possible
(Nix + Linux drivers, Flatpak/Wine/Waydroid for apps) rather than rebuild them.

**Where they STRUCTURALLY cannot follow us:**
1. **The agent IS the shell.** Windows bolts Copilot onto a 1995 shell; macOS
   bolts Apple Intelligence onto a 1984 one. Both are assistants INSIDE a
   compiled UI. Ours COMPOSES the UI. They cannot retrofit this without
   rewriting their shell -- their UI is compiled, ours is codegen'd from data.
2. **Runtime component genesis.** Neither can invent a new native component
   while you work. We can (SDF tree -> WGSL/GLSL -> validate -> hot-compile).
   This is a capability gap, not a polish gap.
3. **Local-first + consent.** They trend cloud-tethered; we are local by default
   with explicit opt-in. A privacy claim they cannot make credibly.
4. **Ownership.** 90/9/1 to contributors, crowdsourced compute, no monopoly --
   unmatched without cannibalising their own business model.
5. **The hive.** Recipes AND agent-authored UI components shared peer-to-peer.
   Nothing equivalent exists.

**Where we WIN on feel (measurable, and the fastest visible win):**
The single most "premium" quality of macOS is not looks, it is LATENCY and the
absence of jank. We own the whole path, so that one is winnable.
| Metric | Windows 11/12 typical | macOS typical | HART TARGET |
|---|---|---|---|
| input-to-photon (drag/hover) | 60-120ms, high variance | 50-80ms | **< 25ms, low variance** |
| dropped frames under load | common | rare | **p99.9 zero on the shell** |
| shell stutter during app launch | visible | slight | **none (shell never shares a thread with app work)** |
| UI coherence | 3 eras of dialogs coexist | high | **one language, zero legacy surfaces** |
Rationale: we have NO legacy surfaces to carry and NO compositor we do not own,
so coherence and latency are ours to lose, not theirs to defend.

**The rule this implies:** every milestone reports its measured input-to-photon
and frame-time distribution in the journal. "Feels fine" is not evidence. A
milestone that regresses latency fails even if it looks better.

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
  + ready markers apply identically (write shell-ready on first composed
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
- 2026-09-05: M0..M3 LANDED and CI-green on main; M4 partly wired; M6 not flipped,
  so `hart.comp.nativeShell` / `HART_NATIVE_SHELL` is still default OFF and the box
  shows the WebView shell. What exists natively: bloom, orb, the scene tree
  (`scene.rs`: SceneNode + layout_home + hit_test + flatten), GL/pixman lowering
  (`comp_core::lower_scene`), cosmic-text run rasterization with a per-run cache,
  rounded rects, the `shell.compose` IPC verb feeding a live HomeCompose, pointer
  reactivity (orb hover + press, card hover), a retained scene tree and a pooled
  solid buffer (the zero-per-frame-alloc NFR). Proven HEADLESS in CI: the demo
  scene lowers, its buffers import, and it composites real pixels over a sentinel
  on a pixman target. NOT proven: on-screen DRM scanout, fps, and input-to-photon
  p50/p99. Those need a person at the box (see the injection-seat finding: a
  uinput device is not granted to the compositor's libseat session, so synthetic
  motion never reaches the input path and emits no samples).

### Text measure: LANDED, and it unblocks the rest of P5
P5 could not be finished as specified, and neither could the row See-all
affordance, for one reason rather than five: `layout_home` is pure geometry and
had no way to ask how wide a string would be, so every text node it emitted was
either left-aligned in a known box or a fixed-size slot. That is why the native
bar had only the omnibox pill and the orb-sm, and why `TextAlign` sat unused.

`scene::TextMeasure` is now that capability: the trait is DEFINED in scene.rs (so
scene.rs stays pure, with no text stack dependency) and IMPLEMENTED by
`text_render::TextRasterizer`, which already shapes with cosmic-text and so
already knows the answer. `layout_home` and `SceneCache::tree_for` take it, and
`lower_scene` hands over the `&mut TextRasterizer` it already holds beside the
caches as a disjoint borrow. It takes `&mut self` because cosmic-text shaping
does, which is affordable only because the tree is retained: a measure runs on a
real layout rebuild, never per frame. `scene::MonoMeasure` is the font-free
fallback, used by unit tests so scene layout stays testable with no fonts, and by
the rasterizer itself when its font database is empty (cosmic-text panics on an
empty database, so this is the same guard `compose` carries).

First consumer: the two-tone HART OS wordmark, whose second run has to begin
exactly where the first ends. Still to do on the bar, all now unblocked and none
needing new infrastructure: the five nav tabs, the avatar letter, the three tray
glyphs (blocked separately on Image lowering, which needs the shell's
image-source contract), and the clock, which is the only one that also needs an
input the compose feed does not carry, since the shell's clock is local time and
not part of the A2UI payload.

### BEFORE FLIPPING M6: four obligations nothing enforces
Found by reading the consumers rather than the compositor, which is the only way
any of them show up: each crosses a process boundary, so no Rust test and no
headless run can fail on them. Two are code, two are contract decisions that
touch the shell and must not be settled unilaterally.

Obligation 4 has since split again on re-measurement: the card ART half is built
and needed no contract at all, leaving only the optional photo layer. See it
below. The lesson generalises: measure what the shell actually PAINTS before
recording something as blocked, because "the card shows a picture" and "the card
shows a gradient with an optional picture over it" are different problems, and
only the first one is blocked.

1. **shell-ready has no native writer.** DONE (the compositor now writes it from
   the vblank reaper when the frame that scanned out carried the scene; three
   conditions, each load-bearing, and flag-off is byte-identical). Kept here for
   the reasoning: The supervisor's paint watchdog reads
   HEALTHY off `/run/hart/session/shell-ready`, and the ONLY thing that ever
   writes it is the WebView host (hart-layer-shell-host.nix, on
   LoadEvent.FINISHED with the surface mapped). M6 demotes the WebView. If it
   stops running, nothing writes the marker, the watchdog calls Tier-1 unhealthy
   and the ladder demotes straight back off the native shell. This program's own
   Rules section already says "write shell-ready on first composed frame
   containing the scene"; that is still unbuilt. Note the honest-paint bar the
   WebView host holds itself to: the marker means MAPPED and painted, not
   "started", so the native writer owes the same.

2. **The panel reservation points the wrong way.** The shell publishes how much
   chrome it owns and `work_area` subtracts it from every placement path. Once
   the compositor paints the bars, the compositor is what knows their size, so
   the contract has to invert and the native scene has to become the publisher.
   Until it does, scene.rs hardcodes 40/44 against a value the theme can move,
   which is the 2026-08-29 "taskbar unreachable" report waiting to happen again
   through the new renderer. Pinned meanwhile by
   tests/unit/test_panel_reservation.py so the two cannot drift silently.

3. **Three surfaces need bar content that home_compose does not carry.** These
   looked like separate gaps and are one decision, which is whether the A2UI feed
   grows a second payload for chrome state or a new IPC verb carries it.

   - **Taskbar.** The shell's taskbar lists ITS OWN web panels, DOM elements
     inside one fullscreen WebView surface, so the compositor cannot see them.
     Filling chips from `space().elements()` would show different things than the
     shell shows, which is a parallel path, not a shortcut.
   - **Agent status** (the bar's centre). Populated by a poll of
     `/api/social/dashboard/agents`, filtered to running agents, four chips max.
     Not in home_compose at all. The compositor must not grow an HTTP client to
     fetch it: wrong process, wrong user, and a network dependency on the render
     path.
   - **Clock.** The one that looks like it should be free, and is not. The
     compositor has a monotonic clock but formatting LOCAL time needs a timezone,
     and this crate sets `unsafe_code = "deny"` so `libc::localtime_r` is out.
     That leaves a timezone crate, and local-offset on Unix inside a
     multithreaded process is a known hazard. The shell already knows the local
     time; sending it is cheaper and safer than the compositor learning
     timezones. Its slot is deliberately not reserved in the layout meanwhile,
     because space held for something that never draws is a hole in the cluster.

   Everything ELSE in P5 is done and needed no contract: brand wordmark, nav
   tabs, omnibox with its search glyph and shortcut hint, orb-sm, avatar, tray
   glyphs. The glyphs were the surprise, and the general lesson is worth keeping:
   an icon here is a LIGATURE NAME in a Material face, so it is text, and the
   fonts are already installed. Reach for the text path before an image pipeline.

4. **Image lowering.** Was recorded as "no source contract". That was wrong, and
   the real picture splits in two, one half of which is nearly free.

   **Card icons are TEXT, not images.** `card.icon` is a Material Symbols NAME
   ("storage", "sd_card_alert") and hartBrandArt's glyphHTML puts it in a span
   with the icon font, which resolves the name as a LIGATURE. Anything that is
   not a Material name (an emoji) renders as plain text too. So the native path
   needs no image pipeline for icons at all: it needs the Material face loaded
   into the same cosmic-text FontSystem that already shapes every other run, and
   `Shaping::Advanced` (already used) does ligature substitution. The obstacle is
   packaging, not rendering: the shell bundles the face as `.woff2` for the
   browser, fontdb reads TTF/OTF, and the box's fonts.packages carries only noto
   and liberation. Get an OTF/TTF of the face in front of fontdb and icons come
   out of the existing text path.

   **Card art was never the picture.** This was measured wrong the first time and
   the correction is worth keeping, because the wrong reading turned a mostly-done
   thing into a blocked decision. `.hh-card-art` is one element whose BACKGROUND is
   a brand-spectrum gradient and whose optional `<img>` child fades in over it.
   hartHome.js paints the gradient unconditionally, with its own comment "no empty
   flash", and only then attaches a photo. So the surface every card always has is
   a gradient, computed from the row's accent hue and the card's index, and the
   picture is decoration on top of it that most cards never carry.

   That surface is now drawn (`SceneNode::Art`), from hartBrandArt's own literals,
   through the same cached rounded-tile rasterizer the solid rects use, with a
   cross-language guard pinning the two together. It needed no dependency, no
   decoder and no new IPC, and it closed the three visible defects the picture
   reading had hidden: a RANKED card drew nothing at all (its own background is
   transparent because the art is the card), every other card drew a flat tile,
   and a card carrying `image_url` rather than `image` decoded as art-less and so
   drew the icon glyph the shell suppresses.

   What is left is only the photo layer, and it is now cosmetic rather than
   structural. The files are real paths on disk, so no HTTP is needed: the
   sanitizer constrains `card.image` to two prefixes, `/shell/static/app_art/...`
   (the service's own static dir) and `/shell/agent-art/<slug>` (through
   HART_AGENT_ART_DIR, default /var/lib/hart/agent-art, then the bundled
   app_art/agents). Slugs are `[a-z0-9-]` only, so they cannot encode traversal.

   Every one of the 51 bundled files is `.svg`, so there is no PNG path to lean
   on. The subset is narrow: gradients in 12 (linear and radial), and ZERO uses of
   filters, `<text>`, masks, clipPaths or patterns, which are the hard parts. Each
   is about 1.5 KB, drawn `preserveAspectRatio='xMidYMid slice'`, so they fill and
   crop rather than letterbox.

   The options stay (a) the compositor renders them, which for that subset is
   `usvg` + `tiny-skia` (pure Rust, no C dependencies) plus a root path handed in
   as a deployment fact; or (b) the shell hands over decoded pixels through the
   existing IPC, which is framed JSON and would need a binary channel rather than
   base64 at image sizes. Not settled here, and no longer urgent: a card without
   its photo is now a correct card, not a hole.

Already handled, listed so nobody re-derives them: the scene claims
NATIVE_CHROME_ORB itself (the M2 block that used to set it is skipped exactly
when the flag is on, so the flip would otherwise have left two orbs breathing);
a drawn native scene holds the frame-budget gate open (its orb breathes off the
clock, and the 200ms idle heartbeat would have rendered that at 5 Hz); and the
scene is skipped under the killswitch (it is hidden anyway, and holding the gate
open behind a blacked-out screen is the worst time to composite at full rate).

### The native home now decodes what the producers actually send
Worth recording how this went wrong, because it was invisible for a long time and
the same trap is open for every future field. The native decoder had been written
against an IMAGINED payload: `Hero` read `title` and `copy`, `Row` read `label`,
`Card` read `subtitle`. Not one of those four keys is emitted by any producer. On
a live compose the desktop therefore rendered a blank hero and unlabelled rows,
while looking perfect in every unit test and every headless render, because
`HomeCompose::demo` filled the imagined names.

The authority is `liquid_ui_service.py`: `_home_sanitize_hero`,
`_home_sanitize_card` and the row builder beside them, plus the backbone builder
that emits the same shapes. Read those before adding any scene field. They are
allowlists, so a key not on them cannot arrive no matter what the LLM writes.

Decoded and drawn now: the earnings hero (eyebrow, amount, unit, agents, tasks,
local, payout_pending, the two action labels), row title/note/see_all/accent, and
card title/meta/progress/icon/badge/live. Deliberately NOT decoded: `usd_equiv`
and `spark_series`, which hartHome.js reads but no producer emits, and card
`format`, same. Adding those would repeat the exact mistake above.

### Both row variants are handled
`row.flagship` is BEHAVIOUR, not appearance: it stops refresh() replacing that row
with live dashboard rows. Nothing for the scene to draw.

`row.ranked` is the hive leaderboard and it is DONE. It was recorded here as
needing a decision, between adding stroked text to the rasterizer and
approximating with a low-alpha fill. Only the approximation needed permission;
building the capability is just parity work, so the capability was built.
`text_render` now takes a stroke width: gather the glyph's coverage, dilate by the
stroke, subtract the original, and the middle stays hollow, which is what an
outline is. The dilation is separable (horizontal max then vertical), turning r
squared per pixel into 2r, which matters at 116px. The stroke joins the run's
cache identity, since a stroked run and a filled one are different pictures of the
same string.

It was paid for by retiring `TextAlign`, which was written at twenty construction
sites and read at none. Layout aligns by computing x now that it can measure, so
alignment never reaches the rasterizer at all.

### How compositor Rust is actually verified from here
`python .hart-devenv/deepbox-check.py [cargo args]` ships compositor/ to a
container on deepbox and runs cargo there in about 50 seconds. Use
`deepbox-check.py test --features smithay`, not just the default check: `cargo
check` does NOT compile `#[cfg(test)]` code, so a check-clean tree can still have
broken tests. A test filter goes BEFORE any `--`. That container has fonts, so
the real cosmic-text shaping path is exercised, not only the fallback.

### The native home was drawn at two thirds of the shell's scale
The other half of the decoder lesson, and a sharper one, because it survived
every test in the tree. In scene.rs every layout constant that carried a CSS
citation in its comment was correct, and every constant that did not was a guess
left over from the M3 sketch. Nothing could tell them apart, so the whole desktop
was laid out small: the hero figure at 40px against `.hh-amount`'s 88, row
headings at 15 against 23, cards at 210x128 against 258x150, the omnibox 420 wide
with 14px ink against 360 and 13.

The Rust tests all passed throughout, and that is the point. They pin
RELATIONSHIPS, because relationships are what a layout test can assert without
duplicating the layout: the note follows the label, the See-all clears it, the
chip stays in its corner, the progress bar spans the card. Every one of those
holds at any scale. A test suite made entirely of relationship assertions cannot
see a uniform scale error, and a uniform scale error is exactly what a first cut
produces.

`EDGE_PAD` was two of those bugs in one constant. It served both the top bar's
inset (`.top-bar` pads 12) and the content gutter (`--hh-gutter` is 60) at a value
of 24, belonging to neither, so the bar was indented twice as far as the shell's
and the content less than half. One name for two measurements is worth looking
for elsewhere.

The responsive layer was missing outright. hartHome.css has four sizing media
blocks and the compositor honoured none of them, which matters more than it
sounds: a row that does not fit the band is dropped SILENTLY, so laying a
1366x768 panel out at full desktop scale costs a row with no error anywhere.
`HomeMetrics::for_output` resolves all four in the cascade's own order, and the
order is load-bearing, since max-width:1400 and max-height:820 both set the hero
figure and the later block wins.

Both directions are now pinned. A Rust test asserts every common panel size still
fits all its rows and a strip of cards, because correcting a scale UPWARD is
precisely what could cost one. A Python test in test_panel_reservation.py reads
hartHome.css and scene.rs side by side, matching each media block by what it
DECLARES rather than by its position in the file, so a new breakpoint the
compositor has not implemented fails the build instead of quietly rendering a
different desktop. That guard is the same shape as the bar-height one above, and
for the same reason: the drift is across a language boundary, so it has to be
checked somewhere that can read both.

### Finishing the constant audit, and the one left open
Having found that a CSS citation in the comment was a reliable marker of a correct
constant, the rest of scene.rs was audited the same way. Four more in the top-bar
cluster were wrong and nothing could see them: the docked orb 28 against
`.top-bar-orb`'s 30, the avatar 28 against `.top-bar-avatar`'s 30, the wordmark
15px against `.top-bar .start-btn`'s 13, and the tray glyph 18 against
`--hart-icon-size`'s 20. All four now carry their source and are pinned across the
language boundary with the rest.

Two of those sources are NOT in hartHome.css; they are in the service's own inline
sheet, which is why they had drifted furthest. The bar's CSS is split across two
files and the audit has to read both.

**HERO_H is left open, deliberately.** It is `EDGE_PAD`'s shape again: one name
doing two measurements. It is both the home orb's slot and the vertical budget the
hero takes before the rows begin. The shell sizes the orb at
`.hart-hero-orbwrap`'s 300px, but it has no number at all for the second: `.hh-hero`
is `flex: 0 0 auto` so its height is its content's, `.hh-rows` takes the rest, and
the orb is not in that flow (it floats above the home at z 1450).

So setting HERO_H to 300 in place would spend 100px of row budget the shell never
spends, and a row that does not fit is dropped silently. The correct shape is a
separate floating `HERO_ORB_D = 300` with the band's height coming from where the
hero's content actually ends, which changes what overlaps what on screen. That is
a visual call, so it waits for the box rather than being guessed at. The
conflation is recorded at the constant itself so the next reader does not have to
rediscover it.

### The compositor was its own counter-example to Gate 4
bloom.rs's header states the rule plainly: the backdrop reads
`nixos/assets/conky-themes/<id>.json`, "one palette source for both renderers, Gate
4: no parallel theme table". The scene's `Theme` was a hardcoded Rust copy of
colours that same file already carries, in the same binary, ten lines away. So
changing the theme restyled the wallpaper under a desktop that did not move, and
neither renderer was wrong on its own.

Both now read one file through one reader. `bloom::ThemeFile` is the scanner (a
key scan, not a JSON parse, so a malformed or hostile file cannot panic or be made
to allocate), `palette_from` is the backdrop's consumer, and
`Theme::with_theme_colors` is the scene's. The scene's constructor stays pure: the
file reading happens in comp_core, resolved once behind a OnceLock for the same
reason BloomCache resolves its palette once, and carrying the same documented gap,
that a runtime theme change does not restyle either until restart. They are now
wrong in the same direction, which is the point.

Two boundaries were drawn deliberately rather than folded in:

**Alpha is the surface treatment's, never the palette's.** A theme names hues. How
opaque a bar or a card is belongs to the material, and letting a palette set it
would let a theme make the top bar transparent or the cards solid. Every folded
colour keeps the alpha it replaced.

**The live-tag scrim does not follow the theme.** The shell's `.hh-card-live` is a
fixed `rgba(8,12,20,0.72)`, not a theme colour, because it exists to keep the tag
readable over whatever art is behind it. A pale theme background would turn that
guarantee into pale-on-pale.

An absent or unreadable theme file is byte-identical to before this existed, which
is the safety property that matters: this runs in the process that owns scanout.

**Still open: `mood` is decoded and dropped.** `HomeCompose.mood` carries the
palette id the agent picked per compose (§6a, the `HART_PALETTES` vocabulary in
hartPersonalize.js), and nothing reads it, so at M6 every agent-composed mood would
render identically. That is NOT the same vocabulary as the conky theme ids just
wired up: HART_PALETTES is 16 entries the shell calls its authoritative client
list, and resolving it natively means either a second copy of that table in Rust
(guarded like the spectrum copy is) or the shell sending resolved colours over the
existing IPC. That is a contract question with a shell side, so it is recorded
here rather than settled. Note also its own internal rule, which any resolution
must keep: the six Aura moods pin the functional accent to teal and let their quad
drive ONLY the ambient field, while the ten classic palettes set the accent itself.

### The native shell was the one thing that could defeat the frame-budget gate
The #137 frame-budget gate exists so a still desktop stops re-importing textures,
re-running the damage pass and attempting a page-flip on every 16ms tick. A drawn
native scene held it open unconditionally, because the orb breathes. That was
right as far as it went and wrong where it mattered: on the pixman software floor
it meant a still native desktop CPU-compositing at 60fps forever, on the weakest
hardware in the fleet, which is exactly the case the gate was built for.

The HTML shell has never done this. Its breathing is `body.gpu-hardware
#hart-voice-orb` and nothing else, and liquid_ui_service records why in its own
words (real-HW 2026-07-12): GPU-only effects armed on a CPU renderer
"re-rasterised a 60fps canvas + an animated software blur on the ONE WebKit thread
and HUNG the whole shell". So the native rule is the same rule, sourced the same
way: the scene animates only while the compositor is GPU-compositing.

The signal is truthful rather than a boot-time guess. `render_all` publishes
`gles.is_some()` onto State every tick before the gate reads it, so all three ways
the floor is reached are covered together: the probe never authorised GLES, GLES
init failed, or a runtime fault demoted it mid-session. A demotion stands the
animation down on the very next frame.

Two details worth keeping:

**The transients stay unconditional.** A workspace fade and a window-map animation
are a few hundred milliseconds of motion the user just asked for, not a permanent
hold, and they must play out on the floor as well. Only the perpetual one is gated.

**With motion off the orb RESTS, it does not freeze.** `animation: none` is not
`animation-play-state: paused`. The shell's software floor never starts the
breathing, so the orb sits at its resting scale; freezing it wherever the last
painted frame caught it would leave a randomly half-inflated orb on screen for the
session. Passing a zero elapsed to the same `motion_at` gives exactly that resting
state, so there is no second resting-state constant to drift. Energy still reads
through, because the canvas viz reacts on both floors in the shell too; only the
CSS float and breathe are GPU-gated.

The orb's motion and the gate now read ONE bool, passed into the state-free
lowering rather than fetched twice. If they could disagree, the failure is a
stuttering orb (animating while the gate holds the rate down) or a gate held open
for an orb standing still, and both are the kind of thing that only shows up on
hardware.

### The wire contract: what the shell SENDS is now pinned to what the desktop DRAWS
Every bug found in the native decoder has been one bug wearing a different key. It
was written against an IMAGINED payload: `hero.title`, `hero.copy`, `row.label`,
`card.subtitle`, four keys no producer has ever emitted, so a live compose would
have rendered a blank hero and unlabelled rows. Then `card.image_url` turned out to
be what news and app cards actually carry, so those decoded as art-less and drew
the icon glyph the shell suppresses.

Unit tests on either side are structurally incapable of catching this, and it is
worth being precise about why: each side builds its own fixtures, so a decoder
written against the wrong shape is tested against the wrong shape, forever. The
only fixture that can catch it is one the REAL producer wrote.

`liquid_ui_service._sanitize_home_payload` is that producer. It is the single
authority on what reaches a client (the LLM composes freely; that function is the
only thing between its output and the wire). `tests/unit/test_native_wire_contract.py`
runs it on a realistic home carrying every shape that has ever gone wrong, and
pins the result into `compositor/src/wire_fixture.rs`, which scene.rs's own tests
decode and assert every field of, right through to the text runs the layout emits.

Both directions are proven by mutation: drop `image_url` from the fixture and the
Python half fails as stale while the Rust half fails as undrawn; revert the decoder
to reading only `image` and the Rust half fails alone.

Three details that matter for whoever touches this next:

**It is a `.rs` file, not the `.json` it plainly is.** hart-comp.nix's crane source
filter keeps `Cargo.toml`/`Cargo.lock` and `*.rs` ONLY, so a `.json` beside the
crate would be filtered out of the build sandbox and `include_str!` would fail in
CI while passing on a dev box. The Python half reads and rewrites the raw string
literal inside it.

**A second test guards the guard.** A fixture that quietly lost its interesting
cases would still pass the comparison while proving nothing, so the shapes it must
carry are asserted by name: an `image_url`-only card, a same-origin `image` card, a
ranked row, two different accents, a progress bar, a live tag, a badge, a See-all.

**Regenerating without reading the diff is the failure mode this exists to
prevent.** The test's own message says so and carries the command. A wire change is
allowed; a wire change the decoder has not been taught is what put four phantom
keys in the tree.

### The scene can now say WHICH component an input touched
latency_budgets.json carries 23 per-component budgets and not one of them has
ever been consulted. The instrument reports `component=shell` for every sample,
so every measurement is checked against the `_defaults` and a slow orb is
indistinguishable from a slow marketplace. latency.rs says why in its own header,
and says the blocker has MOVED: "the scene graph now EXISTS and hit-tests ... what
is missing is carrying a node identity from the input that produced a sample
through to the frame that presented it."

`SceneNode::component_at(x, y)` is that identity. Deepest wins, matching hit_test
and hover_leaf, so a pill inside the top bar names the omnibox rather than the bar
it sits in. The names are the budget file's OWN keys, pinned to it by a Python
guard, because a second vocabulary here would mean the budgets stay dead in a new
way.

Three design points:

**The layout names its own groups.** `Container` carries the component, set where
the group is built, rather than a lookup elsewhere re-deriving it from geometry.
Deriving it would be a second copy of everything layout already decides, and it is
exactly what put `EDGE_PAD` at a value belonging to neither of its two jobs.

**The orb is a leaf, not a group.** `OrbSlot` already IS the orb, so wrapping a
tagged container around it would be two ways of saying one thing. Both slots (the
home orb and the compact one docked in the bar) name the orb, which is right: they
are the same control and the shell treats them so.

**Bare desktop names nothing.** A point over no component returns None rather than
falling back to the nearest group. A sample attributed to something it did not
touch is worse than an unattributed one.

The omnibox became a real group in the process. Its pill and its three runs were
four siblings of the bar's other children, which is why it could not be named even
though the budget table has a row for it; collecting them costs one node and makes
the surface addressable. The taskbar strip is likewise a group now rather than a
bare rect, which is also where its content will hang when it gets any.

**What remains for per-component attribution to be LIVE:** the instrument still
buckets by kind alone. `note_input` has to take the component, the aggregator has
to bucket by (component, kind), the budget lookup has to join, and the journal
line has to stop hardcoding `component=shell`. That is the next slice; the
identity it needed now exists and is tested.

### Per-component latency attribution is now LIVE
The identity landed above is threaded through the instrument. Samples bucket by
(surface, kind) rather than kind alone, so a window closes into one summary per
surface, and a slow card can no longer hide behind a fast orb. The resolution
happens once per input event against the retained tree; the render path pays
nothing.

Findings worth keeping:

**No component actually overrides its default budget.** The 23-row table looked
like it needed mirroring into Rust beside the `_defaults`. Every value in it
equals the default for its kind, so what the table really declares is WHICH
interactions each surface is expected to support, at the standard budget. That is
now asserted rather than assumed: a Python guard fails the moment someone lands a
genuinely different number, because the instrument would otherwise keep checking
against the default and the override would silently do nothing.

**`Surface::Shell` is not a failure case, and its line is byte-identical.** Bare
desktop, WebView chrome, and every sample taken while the native scene is not on
screen belong to it. The harness wants the web shell measured by this same
instrument so "native is faster" is a demonstrated delta rather than a claim, and
a format change would have broken every historical number. A test pins the exact
string.

**Two enums, one bridge, both pinned.** latency.rs knows nothing about scene.rs on
purpose: no Smithay, no scene, no clock, which is what lets its state machine run
under `cargo test` on the default no-feature build where `scene` is not even
compiled. So the surface names exist twice, and a Python guard asserts the two
sets agree and that both match latency_budgets.json's keys.

**One honest limitation, stated in the module doc rather than hidden.** A relative
motion sample is attributed to the surface the pointer is LEAVING, because T_input
is captured before the event is applied and moving that capture would bias the
clock estimator toward busy periods. It differs only at a boundary, and only for
the one sample that crosses it.

What the box will now print, per 10s window, is a line per surface per interaction
kind with its own verdict. That is the difference between "the desktop is slow"
and "the cards are slow and the orb is fine", which is the whole reason the budget
file was written with 23 rows.

### Obligation 2's actual bug, found by looking at the themes
The obligation reads "the panel reservation points the wrong way", and the
inversion it asks for is a contract change with a shell side. But it also names a
concrete bug inside it: "scene.rs hardcodes 40/44 against a value the theme can
move". That half needed no contract at all, and it was not hypothetical.

Reading the ten shipped themes: FOUR of them move the top bar height (36, 38, 40,
44), three move the tray icon size (18, 20, 22), and every one of them sets its own
corner radius, spanning 4 to 22. The shell publishes the panel reservation from
`shell.topbar_height` and renders from the same variable. The native scene drew a
fixed 40. So on `potato` the native bar would have drawn 40px over a 36px
reservation, which is the 2026-08-29 "taskbar unreachable" report arriving through
the new renderer, and on the DEFAULT theme the cards were already drawing a 16px
corner against aura's 22.

All three now come out of the theme file, through the reader the colour
unification added. One number each, read by both renderers, so they cannot drift.

**The taskbar deliberately did NOT join them.** The theme has no key for it: it is
a Python constant beside a CSS literal, and inventing a theme key here would be a
third source rather than one. The existing guard keeps pinning those two, and the
reservation guard now says plainly that its two halves are different kinds of
thing: the top cannot drift by construction, the bottom still can and is watched.

Two details:

**The numbers are clamped, because they come from a file.** A zero or negative bar
inverts the content band's arithmetic and an enormous one leaves no desktop. The
bounds are wide, and a guard asserts no shipped theme is altered by them, reading
the bounds OUT of the Rust rather than restating them, because a restated bound is
how a tightened clamp would pass unnoticed.

**scene.rs's constants are now the FALLBACK for an unreadable theme**, and they
equal the fallbacks theme_service publishes for the same case. That is what the
reservation guard compares now; a separate guard asserts both sides read the same
theme keys by name, since agreeing today is exactly what the hardcoded 40 also did.

What is still open in obligation 2 is only the inversion itself: who PUBLISHES the
reservation once the compositor paints the chrome. That remains a contract question
with a shell side.

### The parity ledger's rule 4, and the one motion switch the scene honoured
NATIVE_SHELL_CSS_PARITY_LEDGER.md is a binding contract and only one thing in the
tree reads it, for something else entirely. Its rule 4 is unambiguous: "Three
independent motion kill-switches must all exist natively: the
`prefers-reduced-motion` media query, the `html.a11y-rmotion` class mirror
(server-applied from `get_a11y_settings()`), and the potato tier."

The native scene honoured none of them. It gained the GPU floor earlier today,
which is rule 5's first DEGRADATION floor and a different thing: a slow renderer
is a reason to skip the breath, a stated preference is a reason to stop.

So a user who had declared reduced motion would have got a breathing orb the
moment the shell went native. That is an accessibility guarantee, not a nicety.

`/etc/hart/accessibility.json` is the declarative half, and it is the half both
renderers can see: shell_os_apis.py seeds `_A11Y_SETTINGS` from that exact path at
import. The compositor reads the same key out of the same file through the same
scanner the theme uses, and a Python guard pins the path, the key, and the fact
that the gate actually consults it, because reading a setting nothing acts on is
the same dead-contract shape as a budget row nothing measures.

Three things worth keeping:

**It wins over the transients, unlike the hardware floor.** A workspace fade the
user asked not to see is exactly what the preference exists to stop, where a slow
CPU is a reason to skip the perpetual breath and still show the fade. The gate
returns false before it looks at anything else.

**The reader stopped being theme-specific and says so.** `ThemeFile` became
`SettingsFile`: two files, one scanner, named for the shape rather than the
subject. A second copy of it is how the drift it was written to end would start
again.

**Only the DECLARATIVE half is visible.** A runtime PUT to
/api/shell/accessibility lives in the shell process's memory. It reaches the
compositor at the next start, which is the same documented gap the theme and the
backdrop palette already carry rather than a new one; whoever lands the
theme-change signal should carry this with it.

The other two switches: `prefers-reduced-motion` stays unbuilt and stated, being
an OS/browser preference with no native equivalent the compositor can read today.

The POTATO tier turned out to be much closer than that sentence originally
claimed, and the correction is worth keeping. liquid_ui_service computes it as
`is_potato = perf.disable_blur or gpu_mode == 'software'`. The GPU half was
ALREADY mirrored (that is the hardware motion gate), and the theme half is one
boolean in a file the compositor already reads. Calling it "Python-side, nothing
to mirror" was reading the description rather than the expression.

It sheds exactly what the hardware floor sheds and no more, per rule 5's "degrade
gracefully, never gut": the perpetual breath goes, the brief transients stay, and
only a stated preference stops those.

Its sibling `performance.disable_animations` is deliberately NOT read: nothing in
the tree reads it, so it is a dead key rather than a contract, and honouring it
natively would invent a behaviour the shell has never had. The guard asserts that
too, because reading it instead would look identical in every Rust test (both are
booleans in the same block, and only potato.json sets either) while mirroring a
verdict the shell does not make.

### The accessibility font scale, and how small its real reach is
The ledger maps `a11y_fontscale` to a **Text** metric override. Following it to
what actually consumes the tokens is worth recording, because the answer is much
narrower than the row suggests and being exact stopped this from being a sweeping
change that would have made the two renderers differ MORE.

The override rewrites three tokens (`--hart-font-size`, `--hart-heading-size`,
`--hart-icon-size`) and the entire served shell has exactly TWO consumers of them:
`html,body{font-size:var(--hart-font-size)}`, the root size, and
`.top-bar-right .tray-btn .mi{font-size:var(--hart-icon-size)}`, the tray glyphs.
`--hart-heading-size` has no consumer at all.

The native scene draws those tray glyphs and ignored the scale, so a user at
font_scale 1.5 got 30px glyphs in the shell and 20px natively. Fixed, with the
shell's own arithmetic including its rounding: it emits `str(round(icon_size *
fs))`, so both renderers land on the same integer and a 20px glyph at 1.13 is 23px
on each rather than 23 on one and 22.6 on the other.

The clamp (0.8..2.0) and the deadband (ignore within 0.01 of 1.0) are the shell's
too. Both matter because the value arrives from a file: "no change" has to mean
the metric is untouched, not multiplied by something near one and rounded.

**Recorded, not fixed, because it is a SHELL gap rather than a parity gap:** the
home surface does not scale in EITHER renderer. hartHome.css sizes everything in
absolute px, so it inherits nothing from the root font-size, and a user who has
asked for larger text gets a scaled tray and an unscaled desktop today. Making the
native scene scale its own type would not fix that; it would make the two
renderers disagree. The fix belongs where the sizes are declared.

### The vignette, and how subtle it actually is
`.hart-vignette` is emitted by the shell UNCONDITIONALLY: no potato gate, no GPU
gate, always there. The ledger files it under Field/M1, which is the milestone the
compositor has supposedly done, and the native scene had nothing like it. Standing
the WebView down at M6 would have taken the framing with it and left the desktop
reading flat at the corners.

It is folded into the bloom's own buffer rather than pushed as a second element.
It is deterministic given the output size, so it recomposes exactly when the
backdrop does, costs no extra per-frame blit, and lands in the right place in the
stack for free: in the shell it sits at z-index 2 with only the grain between it
and the bloom canvas at z 1, and all chrome is above it. Here the native scene is
pushed after the backdrop, so the same is true. Multiplying is exact because the
backdrop is opaque: black at alpha `a` over an opaque ground is that ground scaled
by `1 - a`, with no alpha term left over.

**The number worth writing down: it is about a 7% darkening at the corner of a
16:9 screen, not the 30% the last stop names.** The ellipse is 120% of the box in
each axis, so the far corner sits only t = 0.66 along the gradient ray. Anyone
reimplementing this by eye would make it several times too strong. The test pins
that figure rather than merely asserting "darker at the edges".

Two of the tests here were written wrong first, and the mutation checks are what
said so, which is worth recording because both mistakes are easy ones:

**A "corner darker than centre" check against the real palette proves nothing.**
The bloom's own blobs already make the centre brighter in every channel, so that
assertion passes with the vignette entirely removed. Composing against a palette
whose ambient hues are all black isolates it: every pixel is then exactly `base *
factor`, which is a statement about the vignette rather than about the blobs.

**Summing the channels hides a partial application.** Darkening only two of three
still makes a summed corner darker than a summed centre, while tinting the whole
desktop. Each channel is checked on its own now, and dropping any one of the three
multiplies fails.

### The 1px rule between chrome and desktop, and why it was invisible
`.top-bar` draws `border-bottom: 1px solid var(--hart-glass-border)` and explicitly
sets `border-top: 0`; `.taskbar` draws the mirror on its top edge. One edge each,
facing the desktop. The native strips had neither, so their edge was wherever the
translucency happened to stop, which is chrome dissolving into the desktop rather
than sitting on it.

The reason it was missed is worth keeping: `--hart-glass-border` is written as
`rgba(...)`, not hex. The compositor's colour reader only knew `#RRGGBB`, so it
found nothing, returned None, and the caller drew no rule at all. A value the
reader cannot parse fails exactly like a value that is not there, which is the
quietest failure shape there is.

It varies real amounts by theme (aura white at .10, arctic blue at .15, cyberpunk
pink at .2), so it was never a constant that could have been mirrored.

The shipped-theme check moved to Python after being written in Rust first: the
theme JSONs live outside the crate and crane's source filter ships only `*.rs`, so
a Rust test looking for them finds an empty directory both in the container and in
CI. It found zero themes and said so, which is the right failure but the wrong
home. The rule now sits with the other cross-language pins, asserting that every
shipped theme declares a border in the shape the reader accepts, that its alpha is
visible, and that the compositor both reads the key and draws with it.

### Sweeping the theme file for other keys the readers cannot see
The chrome rule was invisible because `--hart-glass-border` is `rgba(...)` and the
reader only knew hex: a value in the wrong SHAPE fails exactly like a value that is
not there. That is a class, not an incident, so the whole file was swept: every key
in every shipped theme, its value shape, and whether the compositor reads it with a
reader that accepts that shape.

Result: no remaining mismatches among the keys the compositor consumes. The sweep
also surfaced `performance.disable_blur`, which is half the shell's potato verdict
and is now mirrored, and `performance.disable_animations`, which is read by
nothing anywhere and is left alone.

Worth doing again after any new reader lands. The whole audit is a dozen lines of
Python over the shipped JSONs, and it is the only thing that can tell a key the
compositor ignores from a key the compositor cannot parse.

### Large Cursor: a setting the product offered that changed nothing it named
Sweeping the accessibility file the same way the theme file was swept turned this
up. `/etc/hart/accessibility.json` carries five settings; the shell acts on two
(`high_contrast` via a class, `reduced_motion` via a class), `font_scale` reaches
three CSS tokens, and `large_cursor` reaches NO CSS at all.

It is not dead, though, which is the part that made it worth following:
hart-accessibility.nix exports `XCURSOR_SIZE = "48"` when it is on. That is the
standard every CLIENT already speaks, so a user who turned Large Cursor on got a
48px cursor from every application and a 24px one from the compositor, which draws
the desktop's own arrow from a polygon authored in a fixed 24-unit space.

So the toggle was offered in the accessibility panel, stored, wired through NixOS,
honoured by every client, and had no effect on the arrow the user looks at most.

The arrow now bakes at the exported size with its polygon scaled, so the SHAPE is
identical at any size rather than a small arrow sitting in the corner of a bigger
buffer, and the hotspot stays the tip. The side is clamped because it arrives from
the environment: zero is no cursor and an enormous one is a full-screen arrow.

Pinned across all three files it spans, since agreeing in two of them is exactly
what it did before: the shell offers the toggle, nix exports the variable, the
compositor reads it. The guard also checks the exported 48 survives the
compositor's clamp, so the setting cannot be silently reduced to a size the user
did not ask for.

**Still not mirrored, and now stated: `high_contrast`.** The shell's
`html.a11y-contrast` overrides four tokens and thickens the glass border to 2px.
The native scene reads its colours from the theme file rather than from that
class, so a high-contrast desktop would go native at ordinary contrast. It is the
same shape as the two settings already mirrored and belongs next.

### High contrast completes the accessibility sweep
`html.a11y-contrast` is four token overrides plus a doubled glass border:
`--hart-muted:#e8eef2`, `--hart-glass-bg:#0a0a12`, `--hart-glass-border:#ffffff`,
`--hart-text:#ffffff`, and `.glass{background:#0a0a12;border-width:2px}`. The
native scene read its colours from the theme file and knew nothing about the
class, so a high-contrast desktop would have gone native at ordinary contrast:
translucent bars, a faint rule, dim secondary text. The whole set of things the
setting exists to remove.

It is applied LAST, after the theme's own colours and metrics, because that is
what the cascade does: the class is a later source than `css_vars`, so a theme
cannot opt out of an accessibility setting.

**This is the one place a palette gets to set OPACITY, on purpose.** `#0a0a12`
carries no alpha, so the chrome goes solid. The "alpha belongs to the surface
treatment" rule that governs the ordinary colour fold is the wrong rule here,
since translucency is exactly what high contrast exists to remove, so it is
overridden deliberately rather than by oversight.

The guard was written weak first and it is worth saying how. It searched the whole
of scene.rs for each literal and PASSED while the value was mutated, because the
doc comment above the function quotes the CSS rule verbatim: prose satisfied a
check meant for code. It now extracts the function body and requires each literal
inside a `solid(...)` call, and the mutation fails as it should. A guard that can
be satisfied by a comment about the thing is not a guard.

That closes the accessibility file: `reduced_motion`, `font_scale`,
`large_cursor` and `high_contrast` all reach the native desktop now.
`screen_reader` and `sticky_keys` are input and assistive-tech concerns with
nothing for the renderer to mirror.

### Tier-1 summon is machinery without a trigger
Sweeping the IPC the way the theme and accessibility files were swept: what verbs
does the compositor accept, and what does the shell send? The verb sets look
mismatched at first and are not. `hart_wm_client` is the Tier-2 sway shim and
talks to `swaymsg`, not to the compositor socket, so its `window.fullscreen`,
`window.summon` and `window.switch_workspace` are its own vocabulary. The IPC
surface itself came back clean.

What the sweep DID find is one level down. `SummonApp` has:

- a resolver with a timeout, `PendingSummon`, `summon_precheck` for inert
  platforms, and no-phantom-handle correctness, all unit-tested;
- `State::resolve_summon` called on every real toplevel map;
- `State::expire_summons` called from the render tick every frame;
- and NOTHING that ever begins a summon. `State.pending` is initialised to
  `Vec::new()` and never pushed to. `SummonResolver::begin`'s only callers are
  main.rs's own tests. There is no `window.summon` verb in the IPC dispatch.

So the expiry walks an always-empty vec sixty times a second and a summon can
never resolve. Both sides describe this path as real: hart_wm_client returns an
honest `unsupported` at Tier-2 and points at Tier-1 as where the map is awaited,
and HART_OS_NATIVE_ARCHITECTURE §5.4 makes real-map success a release gate.
Neither is wrong about the design; both are wrong about it being reachable today.

**Not implemented, deliberately.** The missing piece is a launch, and app-launch
is DROPPED by owner direction. Recording it is the whole action: the next reader
of that code sees a complete, tested resolver and would reasonably assume it runs.

It is left wired rather than removed, because the no-phantom-window correctness is
already proven and the trigger is the only absent part. The state field carries
the same note at its declaration, so it cannot be found by reading the code alone
and misread as live.

### Reading the dead-code warnings instead of filtering them
The summon finding came from asking "what is built and unreachable". The compiler
answers that question every build, and this session had been filtering it out of
every check with `grep -v "never used"`. Reading the list properly:

Most of it is accounted for and correct. The NFR proof hooks (`solid_allocs`,
`rounded_composes`, `flatten`, `rebuilds`, `composes`, `cached_runs`) are used only
by tests, which is what they are for. `clamp_region` / `transform_region` /
`now_secs_nsecs` are called from screencopy.rs, which is `#![cfg(feature =
"winit")]` and so is not compiled in the smithay build at all; they were moved into
comp_core precisely so the smithay `doCheck` exercises their unit floor, and the
file says so.

Two entries were mine, from earlier today, and both were the same mistake.
`TRAY_PX` and `CARD_RADIUS` went dead when their values moved into the `Theme`,
because the fallbacks were written as bare literals rather than as the constants.
The values still agreed, so nothing looked wrong. But the cross-language guard pins
the CONSTANTS, and the layout no longer read them, so the guard was pinning
something that could not affect a pixel: a guard that cannot fail for the reason it
exists. The fallbacks use the constants again, and a test asserts that chain, so
the CSS, the constant, the fallback and the guard are one line rather than four
values that happen to match.

The third was a genuine parallel path. `Component::key()` named the budget keys,
and so did `Surface::label()`. The runtime used the label; nothing used `key`; the
Python guard read `key`. A change to either would have left the guard pinning a
name the instrument never emits. `key` is gone, `Component::surface()` is the
mapping, `Surface::label` is the single source, and the guard follows the mapping
to reach it.

Worth doing at the end of any run of changes. Dead code is where the compiler
tells you a contract has come loose, and it costs one `cargo check` to read.

### Rows scroll sideways now, which the checklist has asked for all along
a2 says it in the same breath as the rule against page scroll: "Netflix rows
scroll HORIZONTALLY (sideways = native / console-like), the canvas itself never
page-scrolls." The shell does it with `.hh-cards { overflow-x: auto }`. The native
scene CLIPPED: the sanitizer allows twelve cards a row, about seven fit a 1920
screen, and the rest were unreachable rather than merely off-screen.
latency_budgets.json has been carrying a `home-row: {scroll: 16}` entry for an
interaction that could not happen.

Landed in three parts so each was green and reviewable on its own: the clamped
offset model, the row becoming a group, and the input wiring. The first two
changed no behaviour at all.

Three things worth keeping:

**A notch is 120px, because that is the browser's step.** libinput reports a mouse
notch as 15 units, so the per-unit distance is 8. The shell's `overflow-x` rails
already move by the browser's step, so the same gesture travels the same distance
on both renderers; a different constant here would make the two desktops feel
different under the same hand.

**The vertical wheel scrolls a horizontal rail.** That is what a browser does over
an `overflow-x` element with nothing to scroll vertically, so it is already what
this desktop's users get from the shell. A sideways swipe scrolls it too and the
two axes are SUMMED rather than one winning, so a diagonal touchpad gesture moves
the row by what the finger actually travelled.

**The clamp and the band are one number, asserted.** The clamp lives on the input
path and the band on the layout path, so they are two readings of the view width.
If they disagreed a row would stop short of its last card or scroll past it, and
nothing else would notice, so `row_view_width` is the single reading and a test
compares it against the band the layout actually emits at three panel sizes.

The scroll is a cache KEY, unlike the pointer: scrolling moves where the cards are
while hovering only changes which leaf lights up. A rebuild per wheel event is the
honest cost of that.


### The mutation check itself was the unreliable instrument
Every fix in this run was verified by breaking the code on purpose and requiring a
test to notice. Doing that by hand went wrong three distinct ways, and each one
made a check REPORT a result it had not measured, which is worse than not checking
at all:

**Restoring with `git checkout --` reverts to the last COMMIT.** Four times an
assertion added since the checkpoint vanished on restore, and the next mutation
ran against the weaker test that preceded it. Caught each time only by grepping
for the assertion's own text afterwards.

**Restoring from a `.bak` written at mutate time cements damage.** If the file was
already broken, the snapshot captures the breakage and hands it back. `latency.rs`
reached zero bytes that way; git had it, nothing was lost, but the "restore" is
what did it.

**An ambiguous anchor mutates the FIRST match.** `.take(MAX_ROWS)` appears twice
in scene.rs, so a check aimed at `row_extents` silently mutated `reclamp` and then
reported "not caught" about code it had never touched. That one had a silver
lining: it revealed `reclamp`'s cap guards a direct index into a three-slot array,
which is now tested.

A harness now removes all three, and the rules it enforces are the point rather
than the script: snapshot in memory immediately before the edit, refuse to run at
all unless the anchor matches EXACTLY once, restore in a `finally`, and then VERIFY
the restore rather than trusting it.

It lives in `.hart-devenv/` beside `deepbox-check.py`, which is excluded from the
repo (`.git/info/exclude`) because that directory holds machine-local tooling: a
LAN address, a port, a username and a key path. So the tool is not in this commit
and this section is the part that travels. Anyone rebuilding it needs only those
four rules.

Worth stating plainly, because it is the lesson under all of them: a mutation
check that can quietly measure the wrong thing is a confidence machine, and
confidence is exactly what these guards exist to withhold until it is earned.

### The chrome strips were the pre-fix look, twice over
The top bar and the taskbar are `.glass`: a translucent white over a
`backdrop-filter: blur`. The native path has NO blur and never will on this
renderer, so copying that alpha would put unreadable chrome over the aurora. That
is why the apparent mismatch could not just be "fixed" toward the blurred value.

The shell already has a floor for exactly this case. `body.webkit-flat` and
`body.gpu-software` replace the glass with a flat-rastered fill, and its comment
says why: "cairo cannot paint backdrop-filter, so the frosted brand colour
collapsed to grey". The native path is permanently in that situation, so the
FLOOR's value is its spec, not the blurred rule above it.

That value is `linear-gradient(155deg, rgba(7,29,26,.985), rgba(20,22,32,.985)
46%, rgba(25,16,37,.985))`: a three-stop diagonal, teal leading, violet accenting,
opaque at every stop. Both properties carry a recorded real-HW bug:

- a flat colourless grey "read MONOCHROMATIC" (2026-07-12, the mockup gap);
- translucent edges let the home bleed through, the "cluttered/overlap" report
  (2026-07-15), which is why every stop is 0.985 rather than 0.07 at the ends.

The native chrome was `rgba(11,12,16,0.72)`: flat, colourless, and translucent.
It reproduced both fixed bugs at once.

Three things came out of doing it:

**The rasterizer took a third stop rather than growing a second path.** A TWO-stop
gradient is the three-stop one with its middle on the line between the ends, so
card art passes a derived midpoint and gets byte-identical pixels through the same
ramp. The 280 tests that existed stayed green across that change, which is the
proof it is the same arithmetic.

**`SceneNode::Art` became `SceneNode::Fill`.** It was named for its only user, and
the moment the top bar became one too, "every Art node is a card's" stopped being
true. Several tests were counting Fill nodes to count cards; they ask for the
leaves of a `HomeCard` container now, which says what they mean and survives the
next regrouping.

**High contrast flattens the ramp rather than tinting it.** The a11y class sets a
flat `background:#0a0a12` and is a later source than the floor, so it wins. Three
identical stops IS that flat fill through the same tile, rather than a second fill
path for one case.

The Python pin parses the gradient out of hartResponsive.css and compares all
three stops, the midpoint and the angle. Writing it turned up the same
ambiguous-anchor bug as the mutation harness: `chrome_fill: \[` matched the FIELD
DECLARATION `pub chrome_fill: [Color; 3],` before the initialiser, captured
"Color; 3", and reported "zero stops" about a literal it had never looked at.

### The cards were flat rectangles, which the shell had already named
`.hh-card`'s own comment is the whole argument for this one: "STATIC drop-shadow =
the mockup's card depth. It rasters ONCE and composites cheaply forever, so the
software floor KEEPS it (degrade gracefully, not gut): only the per-frame
hover-scale + transition are GPU-gated. **Without this the software home read as
flat rectangles.**"

The native cards had no shadow. That last sentence is a report of the exact
symptom the native desktop would have shown.

`SceneNode::Shadow` carries the CASTER's box rather than a pre-expanded one, so
the scene says "this card casts a shadow" and the lowering grows it by the blur;
a second geometry beside the card's own is the kind of thing that drifts.

**The blur is approximated, and it is worth saying which way.** A CSS box-shadow
is the shape convolved with a Gaussian of about `blur/2`. This ramps the alpha
across `blur`, centred on the edge, with a smoothstep instead: a fraction of a
pixel of softness lost at the extremes, and no convolution at all. That trade is
the point rather than a shortcut, because the shell keeps this shadow on every
tier PRECISELY because it "rasters ONCE and composites cheaply forever", and a
real per-card blur would make it the opposite of what it is for.

One buffer serves every card, since they are all one size, and a test asserts
exactly that: twelve cards, one compose. It shares the tile cache with a sentinel
in the key, and a second test proves a shadow and a tile of the same dimensions
cannot answer for each other, because a collision there would hand a card its own
shadow as its art.

A RANKED card casts none: `.hh-card.hh-ranked` clears the background AND the
border, so there is no box to cast one, and its art tile carries the depth.

`card_bg` was wrong beside it in the same way the chrome was: `#0E1320` is an
opaque literal in the shell and the native value was white at 6%. It is the
backstop under the art rather than a visible surface, but a pale wash shows
through as a ghost anywhere the art does not reach.

### The lit CTA and the card hairline, and a third way prose fooled a guard
Two more one-time rasters the shell keeps on every tier, both found by following
`.hh-card`'s neighbours in the same stylesheet.

`.hh-btn-primary` is a `linear-gradient(135deg, #5CFFD9, var(--hh-teal))` plus a
static teal `box-shadow`, kept on the software floor by the same argument as the
card depth: "A one-time raster, so software keeps the lit 'Resume' button". The
native CTA was a flat accent block with neither. The shell's comment also says
what the ramp is NOT and why: "NOT teal->cyan: cyan #29C5FF dominated the small
button and read 'blue'", which is worth carrying because a plausible-looking
brand ramp would have been wrong in a way nobody could have argued with later.

Every card also carries `border: 1px solid var(--hh-bord)`, and `--hh-bord`
resolves to `--hart-glass-border`: the SAME value the chrome strips rule with, so
the card edge and the chrome edge are one colour rather than two that agree. The
native cards had no edge at all. Four hairlines rather than a stroked outline,
because the scene has one fill primitive and four rects is the honest way to say a
border with it.

**The guard read the comment instead of the rule, and this is the third time that
class has appeared.** `.hh-btn-primary`'s comment quotes the MOCKUP's
`0 12px 36px rgba(0,230,184,.35)` directly above the rule's own
`0 12px 30px rgba(0,230,195,0.30)`, so a regex over the block matched the
description rather than the declaration and failed against correct code. Before
that, a Rust doc comment quoting a CSS rule satisfied a check for that rule's
literals, and a struct field declaration stood in for its initialiser.

All three are the same mistake: prose that DESCRIBES a value is not the value.
There is now a `_css_strip_comments` used by the shared reader, so the CSS half of
that class is closed rather than dodged three times.

### The scrim, and the third stop paying for itself
`.hh-card-scrim`'s comment is one line and it is the entire argument: "Scrim so
text-over-art always reads. Static gradient, no blur (software-safe)." The native
scene drew the title, the meta and the chips straight onto the art, so a pale
photo or a bright brand hue took the text with it. On a desktop whose cards are
brand-coloured gradients by default, that is not an edge case.

It is `linear-gradient(transparent 32%, rgba(4,7,13,0.78) 100%)`, and the
three-stop tile added for the chrome floor expresses it EXACTLY rather than
approximately: `from` and `mid` both fully transparent with `mid_at` at the 32%
stop, then a ramp to the wash. A flat clear run followed by a ramp is what the CSS
says, and a two-stop fade from the top would have washed the art it exists to
protect. The stop that was added for one caller turned out to be the natural
shape for the next.

**The BASE rule is what is mirrored, not the `body.gpu-hardware` variant.** That
class means "the WebView composites", which the native path never does, so it gets
neither class and the base is its value. The guard asserts the negative too: if
the native scene ever carries the GPU variant's numbers it fails, because agreeing
with the wrong rule is the same kind of silent wrong as agreeing with none.

The test distinguishes the scrim from the art by what a scrim IS, a wash that
starts transparent, rather than by its position among a card's children. Two card
tests had been counting fills to count cards, which stopped meaning that the
moment a card had two.

### The third emphasis did nothing
Sweeping the home wire for the usual defect (a declared contract nothing consumes)
turned it up on the shell side rather than the native one. The home prompt offers
the curator `"emphasis": <flagship|ranked|normal>`. `_home_curate` maps flagship
onto the row, `_sanitize_home_payload` validates and forwards it, and
`test_home_producer.py` pins that the flag arrives. Nothing then read it.
`ranked` restyles every card in its row and `normal` is the absence of both, so
one of the three emphases the curator is offered was a no-op the whole way down.

The protection that DID hold was accidental. `_replaceRow` matches on a DISPLAY
TITLE, so the "Flagship agents" row survived the live dashboard only by never
colliding with 'Continue' or 'Recipes'. Retitling the row, or a curated row of its
own titled 'Continue', silently handed it over. `_replaceRow` now reads the flag,
which is what its own comment already claimed happened.

One trap in the fix worth naming: `fetchRecipes` passes `appendIfMissing`, so a
"found but protected" row that fell through to the append would put a SECOND row
under the same title on the home, which is worse than the replacement it was
protecting against. The guard covers that case explicitly.

The native path needs nothing here. `flagship` governs how the client merges live
fetches into a payload; the native scene receives an already-composed home and
runs no such merge, so mirroring the flag would be adding a mechanism, not parity.

Two neighbours were checked and deliberately left alone. `card.format` is read by
`makeCard` and styled by `.hh-wide` / `.hh-portrait` / `.hh-square`, but no
producer sets it and the sanitizer does not carry it, so all four formats collapse
to landscape today; the native scene drawing every card landscape IS parity, and
lighting the feature up would be new work, not this program's. `card.empty` is
synthesized client-side for a row with no cards, and the sanitizer drops empty
rows before they reach the wire, so it never travels either.

### The whole desktop was set in one weight
Both of `text_render.rs`'s `set_text` calls passed a bare `Attrs::new()`. That is
the shaper's default, which is Regular. Meanwhile every single text element on this
desktop has an explicit `font-weight` in the shell's rules and all but two are 600
or heavier: the eyebrow, the card titles, the row titles and the CTA are 700, the
Spark figure and the avatar are 800, the rank numeral is 900. So the native path
painted a design built on typographic hierarchy at one flat weight, and a title
stopped looking like a title.

The image already ships Inter and Noto Sans, both with real bold faces, so this was
a request the font system could always have answered. `SceneNode::Text` now carries
`weight` as a CSS NUMBER, which is what the shell's rules say and what
`cosmic_text::Weight` is a newtype over, so it passes end to end without a mapping
table.

The measure had to take it too. `text_width` is what decides where the next run
starts, so measuring the Spark figure at 400 and painting it at 800 would put the
unit inside the numeral. Both shaping calls now go through one `attrs_for`, because
two call sites that must agree will eventually not.

`letter_spacing` came with it, for one element. `.hh-eyebrow` is `letter-spacing:
3px` at 16px, close to a fifth of an em between every pair of letters, and that is
not a refinement of the label, it IS the label. cosmic-text's `Attrs` exposes it, so
it rides the same path; the mono fallback measure adds it arithmetically, since
spacing is a literal px count after each character rather than a property of a face.

**The eyebrow was wrong in three ways at once**, all in `.hh-eyebrow`: it is teal,
uppercase and letter-spaced, and the native drew it muted grey, as sent, and tight.
It is the label directly over the money figure and the only other teal thing in the
hero, so the muted version broke the visual link between the label and the number it
names. `text-transform` is a property of the SURFACE, so the uppercasing lives in the
scene and the wire keeps the sentence the composer actually wrote.

Each native construction site names its shell rule in a comment directly above its
weight. That is what makes the cross-language pin possible at all: it reads
`// .hh-row-title` + `weight: 700` on one side against `.hh-row-title { font-weight:
700 }` on the other, so a designer changing a weight in the CSS fails the guard
instead of silently splitting the two surfaces. The Rust side adds the negative
case: a run left at the shaper's default must be a ligature glyph or an inheriting
meta line, or the site never read its rule.

Two remainders, named rather than hidden. The `<b>` inside `.hh-stat` is 800 against
the line's own 400, which needs the stat split into four measured runs; and
`.hh-pill` / `.hh-local-mini` are an amber badge and a teal shield-dot that the
native still folds into one grey sentence. Both are now expressible; neither is done.
