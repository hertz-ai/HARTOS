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
