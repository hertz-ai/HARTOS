# HART OS - Home / Desktop Design Checklist

> ## WORK QUEUE = THESE THREE DOCS TOGETHER
> Drive all desktop / OS work from three companion docs, read together:
> - **DESIGN (intent):** `HOME_DESKTOP_DESIGN_CHECKLIST.md` (THIS DOC) - groups
>   a-k, ~55 rules, verbatim steward quotes + message numbers.
> - **MASTER SPEC (workstreams):** `../../HART_OS_FULL_DESKTOP_SPEC.md` - the
>   W1-W11 workstreams + the binding rules.
> - **GAPS / TODO (reality + the queue):** `HARTOS_FUNCTIONAL_GAPS.md` - the
>   EXISTS / PARTIAL / STUB / MISSING audit AND the unified "TO BE DONE" work
>   queue (every unimplemented item, single backlog).
>
> When you CLOSE an item: update the gap-doc row + the W-stream status + this
> checklist's audit. The single unified queue lives in the GAPS doc.

> **Steward's consolidated desktop / home design instructions (recovered from the
> full session history).** Source of truth. Update HERE; never let it scatter again.
>
> Distilled from the steward's own typed words across the week (full extracted
> transcript: 1797 messages, 2026-05-24 to 2026-06-29). Message numbers in
> `[#NNNN]` reference that transcript. Verbatim quotes are kept in the steward's
> exact spelling (typos included) so nothing is paraphrased away.
>
> Companion docs: `../../HART_OS_FULL_DESKTOP_SPEC.md` (the master spec + W1-W11
> tracker), `memory/hartos_liquid_ui_is_agentic_llm_is_heart_2026-06-27.md`,
> `memory/hartos_desktop_polish_program_remaining_2026-06-27.md`,
> `memory/feedback_intuitive_by_default.md`.
>
> Status legend used by the W1 audit at the bottom:
> APPLIED / PARTIAL / MISSING / CONTRADICTED.
>
> Functional reality / explicit gap list: `HARTOS_FUNCTIONAL_GAPS.md`

---

## (a) LAYOUT / CANVAS - the desktop is NOT a webpage (REPEATED, EMPHATIC)

This is the steward's most-repeated structural rule. Capture every nuance.

- **a1. NO vertical PAGE scroll of the desktop.** A desktop that scrolls
  vertically looks bad / browser-y.
  - *"we spoe this looks like a webpage when we have vertocal scrolling and I gave
    tons of instructions along with it earlier don;t tell me they are lost"* `[#1797]`
  - *"but shd we scroll vertically in a desktop that will look bad"* (paraphrased
    in memory from the 06-28 thread; the verbatim concern recurs at `[#1797]`).
  - *"after we implemented the teams kinda window the bottom is clipped after we
    fixed height based scroll"* `[#658]` `[#694]` - height/scroll fixes kept
    clipping content; the fix is a fixed canvas, not a taller scroll page.
  - *"Also with our new teams kinda window frame I see a page level scroller
    instroduced"* `[#658]` - a page-level scroller is a regression to be removed.
- **a2. Fixed-height canvas that fits ONE screen.** Fixed top (omnibox + orb), a
  hero, 2-3 rows, fixed taskbar. Deep content opens in a PANEL/app that scrolls
  INTERNALLY (never the desktop). Real app windows float over the fixed canvas.
  - Netflix rows scroll HORIZONTALLY (sideways = native / console-like), the
    canvas itself never page-scrolls.
- **a3. Responsive to every device dimension** (multi-screen desktop, single big
  monitor with DPI scaling, touch surfaces) while staying a fixed canvas.
  - *"responsive for all device dimensions"* `[#1463 etc, the standing ultracode condition]`
  - *"all the layouts fully responsive for HARTOS installatio in a multi screen
    desktp, for a single screen big monitor with dpi scaling customisable , font
    size customisable"* `[#1751]`
- **a4. Touch surfaces may have multiple desktops/workspaces;** the pager must not
  look naive (see f-cluster on the "1 2 3 4" pager).
  - *"it's fine for touch surcaes where we can have multiple desktops"* `[#1403]`

---

## (b) LOOK / BRAND - cinematic, spectrum, image-rich, no em dashes

- **b1. Full brand SPECTRUM, never monochrome.** A monochrome/green-only design
  was explicitly REJECTED. Palette: teal `#00E6C3`, cyan `#29C5FF`, blue
  `#3B82F6`, violet `#9B5CFF`, magenta `#FF2E9A`, amber `#FFC83D`. Use tastefully,
  not "palette slapped".
  - *"WHY MONOCHROMATIC?"* `[#1720]`
  - *"NOT PALLETE IT SHD BE LIKE A REAL DESKTOP WHICH OUR ORIGINA VERSIO N HAD"* `[#1721]`
- **b1.1 Teal/violet brand ANCHORS over spectrum cards (reconciliation, 2026-06-30). APPLIED.**
  Real-HW regression: the home rendered a single-hue BLUE wash (orb, cards, earnings, CTA, logo all
  blue) - neither the mockup brand NOR the spectrum. Roots: the orb hardcoded `#6C63FF` indigo;
  `hartBrandArt.gradient` crushed every hue toward navy INK `[9,13,22]`; the amount/CTA leaned
  cyan/blue; the wordmark was plain. FIX: orb -> teal `#00E6C3` (render-confirmed); INK neutralised
  to `[14,14,17]` + less crush (0.72->0.60, 0.52->0.34) so each hue reads distinct; earnings -> solid
  teal + glow; CTA -> bright-teal; `"HART"`(teal)+`"OS"`(violet `#9B5CFF`) wordmark split. Does NOT
  regress b1: brand ANCHORS are teal/violet (logo, eyebrow, earnings, CTA, orb); CARDS keep the full
  SPECTRUM (now visible per card, no navy crush) - exactly how the steward mockup is built.
  - *steward 2026-06-30: "HARTOS color in this html is not used?" + "this is how it looks now" (blue-wash screenshots)*
- **b1.2 Teal LEADS, violet ACCENTS with intent - the duotone-weighting rule (2026-07-01). APPLIED in the mockup.**
  The steward re-audited `hartos_home_mockup.html`: the teal->violet brand gradient was used in exactly ONE
  place (the `.brand` wordmark); everything that draws the eye was teal-only, so it "still looks majority teal".
  The fix is NOT "spread violet everywhere" (that clutters); violet must carry a CONSISTENT MEANING.
  - **Weighting: teal ~70% (lead), violet ~30% (accent).** *steward: "need the best of all worlds without
    losing functionality" + "without looking cluttered"* `[2026-07-01]`.
  - **STAYS TEAL (steward-confirmed CORRECT, do NOT recolor to violet):** the earnings headline number
    (`.h1 b` = white->teal, NOT violet), the PRIMARY CTA (`#5cffd9`->teal), the eyebrow, progress bars,
    badges, the orb CORE. These are the "you / local / your earnings / primary action" surfaces + every
    FUNCTIONAL signifier (recoloring them loses readability/function). *steward marked both the teal headline
    and the teal primary CTA "this is correct" against the OS render `hartos_home_software.png`.*
  - **CARRIES VIOLET (the "hive / collective / cosmic aura" meaning):** the wordmark `OS` half, the orb's
    OUTER AURA/halo + the `.ring.b` outer ring, the SECONDARY CTA's identity (subtle violet bg/border, which
    also sharpens primary-vs-secondary hierarchy), and the "from the hive" / Top-10 collective sections
    (subtitle + the big `.num` numerals violet-stroked).
  - Net: teal stays dominant, violet pulls real visual weight on ~4 meaningful surfaces, nothing functional
    changes color. Mirror this exact weighting into the OS shell (held #159, after the design pass clears
    `hartBrandArt`/`hartHome.css`/`voiceOrbViz`). Render-verified in the mockup at `?v=3`.
- **b2. Netflix-Home aesthetic, image-RICH ("lots of images").** Image cards,
  text-over-art with gradient scrims, varied formats (landscape / portrait /
  square / wide / live), content sourced/inferred from news + web, dynamic-website
  feel.
  - *"UI shd be like Netflix Home and user experiece while being 100x optimal and
    crazy to see"* `[#1267]` `[#1361 (5361)]`
  - *"show images searching sourcing inferencing from news web with text overlaid...
    Different formats like dynamic websites"* `[#1765 summary]`
- **b3. Best of all worlds.** Take each OS's STRENGTH without its baggage, plus an
  AI-native soul none of them have.
  - *"best of all worlds Netflix, Android HyperOS, Macos, Windows , Linux"* `[#1738]`
  - Netflix (cinematic rows/Continue/text-over-art), Android HyperOS (buttery
    fluidity, widgets, control center, app drawer), macOS (elegance, dock,
    Spotlight=omnibox, vibrancy glass), Windows (taskbar+hover previews, Start,
    snap, tray, explorer), Linux (open + fully customizable + YOUR machine), made
    INTUITIVE.
- **b4. Behance-grade, futuristic, elegant - not "outdated" web styling.**
  - *"the styling looks outdated and not elegant like a behance designer woould
    have designed a futuristic desktop with awesome user experience and UI"* `[#1708]`
  - *"wear clear designers hat like a behance UX designer and overhaul in ultracode
    style with max effort"* `[#1675]`
- **b5. Better than Windows / macOS** at microanimations, usability, buttery-smooth
  snappy feel - the areas where modern OSes are genuinely better.
  - *"make HARTOS UI better than Microsoft desktop and mac OS"* `[#1285]`
  - *"app opening app to app trasitions everything shd be better than windows ,
    explorer, desktop shd be better than windows and mac"* `[#1434]`
  - *"it looks worser than Windows 12 obviously"* `[#238]` (the bar it must clear).
- **b6. NO EM DASHES (U+2014) in any user-visible product text.** Hard, repeated
  rule. Use periods / commas / colons / parentheses / " - " hyphen.
  - *"NO EM DASHES"* `[#1722]`
  - *"rremove em dashes in the visual text anywhere in OS"* `[#1671]`
- **b7. Use the REAL Hevolve brand mark** (the hourglass/heart logo from
  hevolve.ai), heart centered + occluding the plug, retaining the existing
  colors/animation. Brand identity preserved without clipping.
  - *"the heart within logo is not centereds"* `[#1741]`; *"entire plug shd be
    occluded"* `[#1742]`
  - *"DO WE USE ACTUAL hartos ICONS WHERE NEEDED FROM HEVOLVE.AI"* `[#1370]`
- **b8. Communicate VISUALLY + by VOICE, not text walls.** Minimal text. Lead with
  graphical imagery, data-viz, micro-animations, and SPEECH (TTS) when the moment
  needs it - all NATIVELY AGENTIC (the local LLM decides what to show / animate /
  say, composed via agent_ui_update; it is not hardcoded copy). The current
  paragraph-heavy hero (e.g. "Your hive earned ... 3 agents ran 41 tasks overnight,
  fully local, on your own machine. Pick up where you left off...") is a REGRESSION
  of this rule: render the earnings as a number that animates up + a small graphic,
  and let the orb SPEAK the rest, rather than printing a paragraph.
  - *"not lots of text but graphical images microanimations etc and speech when
    needed all natively agentic"* `[#1799, steward 2026-06-29]`
  - Reinforces (c) the orb speaks, (i) agentic Liquid-UI, (b2) image-rich.

---

## (c) THE ORB - varieties, switchable, breathing, voice-viz, NO mic inside

- **c1. NO outer solid ring/circle; transparency control; brand intact (no
  clipping/breaking).**
  - *"ORB WITHOUT A OUTER SOLID CIRCLE TRANSPARENCY CONTROL"* `[#1721]`
  - *"hart VOICE VIZ BRAND IDENTITY PRESERVED WWITHOUT CLIPPING BREAKING IT"* `[#1721]`
- **c2. BREATHING.** A living, breathing iridescent orb (the FEEL-alive pillar).
- **c3. Voice-viz look, and the orb VARIETIES are SWITCHABLE.** The voice
  visualizer regressed; restore the prior look and keep skins switchable
  (visualiser-first default + character toggle).
  - *"voice viz shd look like how it looked before this change 1c4546bd, why do we
    have different orb and are the orb switchable?"* `[#1791]` `[#1790]`
  - *"switchable with sensible defaults"* (orb skins) `[memory / #2802 summary]`
- **c4. Float / compact / minimise / disappear / reappear over windows; merge +
  demerge with background; attach to the HART chat + detach.**
  - *"THE ORB NATURALLY COMPACTING MINIMISING DISAPPEARING REAPPEARIUNG FLOATING
    OVER OTHER WINDOWS APPS, MERGING DEMERGING WITH BACGROUND WHEN NEEDED ATTACHING
    ITSELF TO CHAT OF harT DETACHING WHEN NEEDED"* `[#1723]`
  - Persistent, always-on-top, VUI-style; floats even when Nunba is not the
    foreground window; *"just float like what the VUI does on top of desktop"* `[#632]`
  - Auto-hide like a taskbar when the user is not interacting and the agent is not
    talking: *"this shd also autohide when user not interacting or agent not
    talking like taskbar autohide feature"* `[#640]`
- **c5. The orb must NEVER contain a mic.** A mic inside the orb makes no sense.
    - *"wrong orb shd never have a mic what's the point of mic within orb?"* `[#1672]`
    - *"the orb shd be left alone"* `[#1669]`
- **c6. Realtime voice recognition + realtime responses** wired through the orb /
  agent_engine + local LLM.
  - *"vOICE RECOGNITION REALTIME AND RESPONSES REALTIME"* `[#1723]`
- **c7. Placement.** Home mode: orb floats to the RIGHT of the hero copy. Compacts
  to a SMALL orb (orb-sm) docked in the top bar (always accessible). On the live
  desktop the orb (not a bare mic button) is the center presence.
  - *"it should have been the HART OS Orb"* (not a mic button) `[#1365]`
- **c8. Breathing is TOGGLEABLE (a transparency / quiet control), DEFAULT ON.** The
  user can quiet the orb's breathing to a calm, static presence and restore it; the
  default stays ON so the living look is unchanged. ONE persisted flag
  (`hart_orb_breathing`) gates BOTH the concentric brand rings (buildOrbAura) AND
  the voice-canvas breathe glow (voiceOrbViz) - voice ENERGY still reacts when OFF
  (that is not breathing). Exposed via the orb right-click (reuse the context
  affordance + toast; no second settings panel) until the full orb-varieties picker
  (#140 / c3) lands. A refinement of c1 ("transparency control") + c2 ("breathing").
  - *[steward 2026-06-30]* the orb breathing should be switchable on/off (default
    on); part of the same 2026-06-30 batch as the drag-affordance + pager fixes.

---

## (d) CARDS / ROWS - earnings hero, Continue row, Netflix listings EVERYWHERE

- **d1. Value-first EARNINGS HERO** (the flywheel/earnings story, personal), NOT a
  generic briefing. Lead with the outcome.
  - Mockup line the steward wrote: *"Your hive earned Rs 2,140 while you slept. 3
    agents ran 41 tasks overnight, fully local."* `[#1782 mockup]`
  - Earning is the true measure: *"the goal is to make the user rich (literally
    earn) with every capability the hartos already sports"* `[#1279 summary, #142]`
  - **d1.1 The money must be REAL, earned via the HIVE working together** - not a
    vanity/sample number. The figure is the user's actual share of value their node
    generated by connecting to the hive and doing work alongside other nodes (the
    distributed compute economy: contribute compute -> value created -> the 90/9/1
    flywheel pays the contributor). The hero wires to the REAL earnings ledger, not
    a mock; "Spark" alone is the internal unit - lead with the real money it
    represents. *"real money shd actually be earned via the hive nodes connecting
    togather woriking togather"* `[#1798, steward 2026-06-29]`. Implication: the
    earnings pipeline (nodes co-working -> value -> payout) must be REAL and surfaced
    here; a placeholder hero is a regression of this rule.
- **d2. A "Continue" row** = resumable agent tasks (Netflix "Continue Watching"),
  with progress bars.
- **d3. Image cards with text-over-art + gradient scrim**, varied formats, real
  photos (dynamic, cached) - "lots of images", not flat tiles.
- **d4. Netflix listings EVERYWHERE, every surface** - not just home. App Store,
  agents, recipes, communities, settings, file explorer, AND install / registry /
  uninstall all render as image-card category rows with hover-expand.
  - *"Alll aps shd also look like Netwflixx listingsl"* `[#1731]`
  - Install/registry/uninstall is a DESIGNED Netflix surface (real progress bars,
    minimised views that animate progress), not a plain list `[memory / #1721]`.
- **d5. Hover-expand on cards (Netflix), 60fps staggered reveals** (GPU-gated).
- **d6. Desktop APP ICONS are IMAGE-RICH representations (image cards), paired with
  the app NAME** - not flat minimalist glyphs. Colorful, like macOS/Windows.
  Minimalist glyph only where an image is not warranted.
  - *"How to custoimaise the app icons?"* / *"why are they not colorful?"* /
    *"like macos and windows"* `[#1470]` `[#1471]` `[#1472]`
- **d7. Dynamic image cache + semantic media index feeds the cards.** Idle-time
  captioning agent, vector/semantic search over image+video captions, stored
  compressed locally; no personal data leaves the perimeter without consent.
  - *"all images shd be dynamic cached , files indexed deterministic search with
    semantic search results when possible... an image captioning agent to search
    semabtically... No personal data shd leave the network perimenter without
    explicit consent"* `[#1733]`
- **d8. PER-SOURCE image sourcing, CONTINUOUS (not just first-boot).** Images refresh
  over time, not loaded once at first boot. Source the RIGHT image by card TYPE:
  - **App cards / icons** (Netflix-style app listing): the most appropriate app
    poster/image from the app's OFFICIAL website, OR from the marketplace / app-store
    listing image (per source) - real app artwork, not a generic tile.
  - **Agent cards:** synthetically generated images (image generators), with a
    dark-to-light TRANSPARENT gradient overlay + text on top (the text-over-art scrim).
  - **News / web content:** sourced/inferred from news + web (per b2/d3).
  - All cached locally via the W10 cache (d7); no personal data off-device w/o consent.
  - *"images are not just loaded on first boot, App icons made into netflix style
    listing shd get most appropriate app image poster from official website for each
    app or from marketplace listing etc for each source, Agents we could source from
    image generators and synthetically adding a dark to light transparent overlay with
    text on top"* `[#1801, steward 2026-06-29]`

---

## (e) TOP BAR / OMNIBOX / TASKBAR / START MENU

- **e1. TOP BAR layout** (steward liked the mockup's bar verbatim): brand
  "HART OS" (left) | nav tabs Home / Agents / Apps / Hive / Earn (with active
  state) | spacer | the omnibox PILL "Ask or search anything" | a SMALL orb
  (orb-sm) | the user AVATAR. RESTRUCTURE the existing `.top-bar`, do NOT add a
  second bar.
  - *"Ask or search anything at top is also kinda nice"* `[#1735]`
- **e2. TOP OMNIBOX is the PRIMARY surface** (Cmd/Super+K). One input, agent-ROUTED
  by the local LLM into three outcomes: (1) deterministic -> installed apps / files
  / registry (instant), (2) semantic -> images/videos by caption, (3) ask ->
  natural language -> the agent answers/composes. REUSE the existing hero command
  bar (hart-hero-bar / acSend); do NOT build a new search system. Lead with it,
  do not bury it.
  - *"w e already had ask hart and a central search text input"* `[#1736]`
  - *"there is a seach apps ask the agent where I could not talk type anything"*
    `[#1365]` (it must actually be typeable/talkable - was dead).
  - *"redudnant search ASk HART text input at the bottom right"* `[#1397]` (one
    omnibox, not a redundant second search).
- **e3. Deterministic search lists installed apps + registry items** (e.g. typing
  "terminal" must list the Terminal app).
  - *"when I type it does not list the terminal as a app in the start menu kinda
    search"* `[#1758]`
- **e4. Real TASKBAR** with running-app HOVER PREVIEWS, system tray, minimise-to-
  taskbar-icon with LIVE animated progress, configurable dock direction.
  - *"PREVIEW IN TASKBAR ON HOVER"*, *"MINIMISED VIEWS SHOWING PROIGRESS WITH
    ANIMATIONS"* `[#1721]`
  - *"change the taskbar direction and where to dock"* `[#1403]`
- **e5. Windows-style START MENU** that opens a pane: all apps, searchable,
  categories, pinned, power.
  - *"DO WE HAVE A PANE LIKE START MENU WHEN CLICKED OPENS?"* `[#1370]`
  - *"start menu like windows, settig page , and other context menu insertions"* `[#1752]`
- **e6. Settings page** = the hub for all customization (below). Plus Connect,
  install/uninstall.
- **e7. EVERY Nunba page available as a NAMED microfrontend** to open from the
  Start menu + search (Home, chat, Controls, CreateAgentForm, admin, blogs, the AI
  setup wizard...).
  - *"ALL THE PAGES IN NUNBA AVAILABLE AS MICROFRONTEND WITH A NAME TO OPEN THEM IN
    START MENU AND IN SEARCH"* `[#1723]`

---

## (f) INTERACTION - intuitive, draggable, taps register, correct click semantics

- **f1. Taps must REGISTER.** The dead-husk desktop (non-interactive, taps do
  nothing) is the recurring #1 bug. FEELS-alive: tap registers, snappy.
  - *"th desk top is not responsive to any clicks"* `[#1365]`; *"still no buttn
    works"* `[#1396]`; *"TAP STILL NOT REGISTERING"* `[#1721]`; *"mosuse is not
    movable in this , no way to enter the susyem"* `[#1771]`
- **f2. Correct click semantics per surface.** Touch surface: a single tap on an
  icon (with the hover finger/arrow) OPENS. Non-touch desktop: single click =
  SELECT, double click = OPEN.
  - *"whenever we show the hover finger arrow user expects a single tap to open
    particularly in touch surfaces and in desktop with not touchscreen interface it
    will be always double click on icons"* `[#1466]`
  - *"in desktop single click is selction and double click is opening"* `[#1467]`
- **f3. Right-click CONTEXT MENUS like Windows** (on desktop, icons, app store
  entries), canonical (retire any duplicate `#ctx-menu`).
  - *"context menu like windows?"* `[#1368]`
- **f4. Everything draggable / movable / rearrangeable; snap to grid; sortable.**
  All shell UI elements (sensory icons, widgets) float and drag like Android
  widgets with transparent bg, in a grid style.
  - *"ALL PARTS MOVABLE DRAGGABLE, RIGHT CLICKABLE"* `[#1721]`
  - *"this entire list of things we see can be ade floatbale and floated around via
    drag drop jjust like android widgets with trasnparent bg? in a rid style?"* `[#1397]`
  - *"each icon in the screen rearrangeable sortable and draggable snapping to
    grid"* `[#1675]`
- **f5. Movable, dockable, multi-window with multi-app concurrency + snap-zones;
  multi-tap context switching that does NOT lose focus/state.**
  - *"NOT TIGHLY LOCKING OINTO ONE THING WE DO STILL NOT LOSING FOCUS WITH PROPER
    MULTI TAP CONTEXT SWITCHING"* `[#1721]`
  - *"Are we still having the movable dockable multiwoindow functionality , multi
    app concurrency"* `[#1751]`
- **f6. Full customizability** of every desktop + file explorer: add apps to
  desktop, pin to taskbar, change background (video / image / solid), theme
  selection (transparent/translucent menus + windows), resolution, extended
  displays, system font, taskbar direction/dock, install media players (VLC), etc.
  - *"how to add apps to esktop, pin to taskbar, change background , theme
    selections, resolution selection, Extended displays, VLC media player and other
    tings, change system font, change the taskbar direction and where to dock"* `[#1403]`
  - *"wallpaper could be video image or solids? theme explorer like Windows with
    transparent transculent menus"* `[#249]`
  - Icon gallery (like Rainmeter) + font gallery `[#1293]`.
- **f7. The "1 2 3 4" pager looks naive** - it needs a real design element (or the
  workspace switcher redesigned).
  - *"hy do we show 1, 2m, 3 and 4at the botoom they look naive with no design
    element"* `[#1675]`; *"clicking 2, 3,, 4 just selects it but nothing else
    happens"* `[#1682]`
- **f7.1 Pager click actually switches workspaces (Fix C, 2026-06-30). APPLIED.**
  The segmented glass rail (sliding accent thumb + per-desktop occupancy dots)
  already retired the "looks naive" look; this closes the *"nothing else happens"*
  half `[#1682]`. `window.hartSwitchWorkspace(n)` (hartWorkspaces.js) now fires a
  fire-and-forget `POST /api/shell/workspaces/switch {id,name}` AFTER its
  shell-local panel show/hide, so BOTH the pager segments and the Workspaces-
  settings squares (the SAME one fn -> no parallel path) drive a real compositor
  switch. The backend routes hart-comp through HartWmClient (workspace.switch /
  com.hart.Compositor IPC §4.8) and degrades to a 200 no-op, NEVER 500. The
  client-side panel show/hide stays authoritative for the glass UI on every tier,
  so this does NOT regress the f7 rail redesign or the F4 reveal/discoverability
  fix (data-multiws).
  - *"clicking 2, 3,, 4 just selects it but nothing else happens"* `[#1682,
    steward 2026-06-30]`
  - FOLLOW-UP (open gap, NOT faked): HartWmClient is still a swaymsg shim, so
    native-window switching on the real hart-comp desktop stays a no-op until the
    com.hart.Compositor IPC backend (`compositor/IPC_PROTOCOL.md` §4.8) replaces
    the shim. The route reflects this honestly (`switched:false` under the shim),
    rather than reporting a phantom switch.
- **f8. Floating "disable all AI" button** (shut eyes/ears/sensory) with proof it
  has shut its real-world sensory signals, minimalist, AI-native.
  - *"a floating button which disables all AI (shut eyes ears and all things it can
    sense from reality with proof"* `[#1292]`

---

## (g) SENSORY CLUSTER - eye + mic grouped, contextual light-up

- **g1. Group ALL sensory icons** (eye/vision + mic/voice + other sensory signals)
  into ONE clustered panel at the bottom; the orb viz alone is at center.
  - *"eye and mic shd be grouped togatherr at bottom only"* `[#1668]`
  - *"the orb viz alone could be at center and mic can be placed near eye icon
    aligning all perception sensory signals in one place (grouped)"* `[#1400]`
  - *"we were groupig eye vision and mic for voice icon isn't (all sensory icons
    grouped)"* `[#1465]`
- **g2. Sensory elements are a floating, draggable WIDGET** (not rigid like cage),
  appearing/disappearing CONTEXTUALLY + deterministically based on use (mic lights
  up when mic used, eye lights up when AI sees).
  - *"ideally eye mic and all sensory inpouts can be shown loike a floating widget
    still in same place but all these UI elements will be draggable etc in Tier 1?
    not this rigid like cage?"* `[#1673]` `[#1674]`
  - *"any and all elemnts shd be appearing diappearing contextually determisnitically
    based on what's needed at the time when it gets used like when mic used mic
    lights up and ewhen Ai sees eye lights up"* `[#1675]`
  - **g2.1 Drag affordances appear ONLY during an active drag** (the corollary of
    contextual-by-use). The sensory-pod GRIP and the orb's minimise control stay
    hidden at rest AND on a passive hover, and reveal only while a drag is in
    progress (the orb minimise control additionally STAYS revealed while the orb is
    compact, so the restore affordance is always reachable). The whole widget body
    remains draggable - the grip is visual only (hidden via opacity, so its width +
    pointer-events stay part of the drag hit-area).
    - *[steward 2026-06-30]* drag affordances should show only when dragging, not at
      rest / on hover.
- **g3. The clustered sensory panel is retained** across HARTOS installs (embodied).
  - *"The clustered grouped sensory panel we had for mic and vision and other
    sensory signals for embodied HARTOS installations?"* `[#1751]`

---

## (h) ONBOARDING - "Light your HART"

- **h1. First-boot "Light Your HART" language + preference wizard** after OS
  installation (REUSE `hartOnboarding.js`, phase-driven via the server). The Nunba
  AI setup wizard is part of it.
  - *"we were suppsosed to do light your hart onoarding"* `[#1365]`
  - *"LIGHT YOUR hart FOR UNDERSTANDING USER'S LANGUAGEG, PREFERENCE ON OINTIAL
    ONBOARDOING"* `[#1721]`
  - *"it shd be part of HART onboarding light your heart we have"* `[#1729]`

---

## (i) AGENTIC LIQUID UI + LLM-AS-HEART + 100x PERF (the soul)

- **i1. Liquid UI is AGENTIC + FLUID, not a static theme.** The local LLM COMPOSES
  and re-composes the surface live via the EXISTING `agent_ui_update` / A2UI
  transport. Fluid, never rigid.
  - *"Liqquid Ui is a agentic UI feature"* `[#1726 / memory]`
  - *"Not rigid, it shd fulfil its name Liquid UI"* `[memory]`
  - *"we already have liqquid ui"* `[#1726]` - extend it, never reinvent.
- **i2. The local LLM is the HEART - paramount.** On-device intelligence is the
  whole point; a slow LLM = a stuttering UI. The llama scheduler / foreground
  preempt responsiveness IS the foundation of Liquid UI.
  - *"an LLM running and doing its work is paramount since that is the HEART of
    HARTOS running locally with intelligence"* `[memory / #1258]`
- **i3. 100x optimization is first-class + measured.** Buttery, snappy, 60fps no
  jank, fast boot/first-paint, instant launch, lean CPU/GIL/memory, ZERO hangs.
  Budgets: chat 1.5s, draft 300ms, cache <1ms.
  - *"100x optimizationis not in the list? why?"* `[#1753]`
  - *"user experiece while being 100x optimal and crazy to see"* `[#1267]`
- **i4. GPU-accelerated compositor** (the snappy lever; software-render is the safe
  default floor). The steward wants real GPU acceleration.
  - *"why the fuck are we not making GPU accelerated compositor boot?"* `[#1789]`

---

## (j) CUSTOMIZATION-AS-API / SDK + system management

- **j1. Every customization is an API; the API is the SDK** for app building.
  Where Nix gives a zero-customization default, ENHANCE it.
  - *"all customisations shd be exp[osed as an API (API consumed via SKD for app
    building )... since HARTOS is not NIX you will have to enahnce all Nix provided
    Out of the box functionality"* `[#1752]`
- **j2. Context-menu insertions, Start-menu entries, Settings panels** registerable
  by installed apps (freedesktop / Shell-integration bridge).
  - *"other context menu insertions after insttalation when apps targets them"* `[#1752]`
- **j3. System management depth** in Settings: devices, accessories, disk, paging/
  swap, environment variables, DPI scaling, font size - each Linux/Nix primitive
  bridged.
  - *"dpi scaling customisable , font size customisable etc"* `[#1751]`
- **j4. Windows/macOS feature parity baked in**: recycle bin, start menu, taskbar,
  startup manager, event viewer, file explorer (full parity), all open-source
  apps, install/uninstall EVERY OS app (Windows/macOS/Linux/Android) from the UI.
  - *"all macos and windows features like recycle bin , start menu and task bar
    startup manager, events viewer, search file explorer and all opensource apps
    natively baked in"* `[#248]`
  - *"explorer with ful functional parity for every featyre winodws and macos
    offers"* `[standing ultracode condition]`

---

## (k) RECURRING CROSS-CUTTING RULES

- **k1. NEVER reinvent; reuse the canonical path.** No parallel paths, no DRY
  violations. Extend existing helpers/modules. (Stated in nearly every session.)
  - *"continue never reinvent"* `[#1727]`; *"do not reinvent anything reuse
    existing colors from what nunba sports"* `[#629]`
- **k2. Zero regression.** Existing features must keep working; *"no functionality
  should break after installation"* `[#1405]`.
- **k3. Intuitive by default.** Every capability discoverable with no docs.
  - *"all existing functyionalities and functionalities we expose shd be intuitive
    to user"* `[#1737]`
- **k4. Be visual.** Render UI to PNG and show it; verify by render AND on real
  hardware (the flashed stick), not just inline.
  - *"be more visual where possible"* `[#1725]`; *"I cannot see the render"* `[#1796]`
- **k5. HART OS IS the Nunba desktop** (colocated; no separate :6777 port in
  desktop). Nunba UI bundled natively, zero Nunba code changes.
  - *"hart os IN ITSELF IS THE NUNBA DESKTOP IN OUR CASE"* `[#1428]`
- **k6. Real money / earn is the through-line** - every surface is an earning
  surface; lead the home with the earnings outcome (see d1).
- **k7. Privacy-first defaults** - all LOCAL features ON by default; anything that
  leaves the device/network stays explicit opt-in + consent (see d7).
- **k8. PARITY-OR-BETTER on every OS micro-detail.** Every small thing a mature OS
  does, HART OS does too - only better. Concretely: system/feedback SOUNDS (USB
  device connect + disconnect chime, notification sound, error/alert tone, volume
  + battery-low feedback), tasteful haptics where present, subtle motion + status
  feedback for every action. None of these may be missing; "it is a small detail"
  is not an excuse to skip it. The pipewire audio stack (desktop.nix) is the
  foundation; wire device connect/disconnect via udev + notifications via
  hart-notify to a cohesive HART sound set (designed, not borrowed; on-brand).
  - *"disk connected not connected sounds, every friggin detail in an OS shd exist
    as parity but only better in hartos"* `[#1800, steward 2026-06-29]`
  - Reinforces b3 (best of all worlds), b5 (better than Windows/macOS), k2 (zero
    regression on the polish bar).

---

## W1 HOME AUDIT (built `hartHome.js` + `hartHome.css`, renders 2026-06-29)

Audited against the checklist. Files:
`integrations/agent_engine/static/hartHome.js`, `.../hartHome.css`.
Renders: `~/Downloads/hartos_home_GPU.png`, `hartos_home_software.png`,
steward mockup `hartos_your_mockup.png`. Top bar + orb live in sibling files
(`liquid_ui_service.py` top-bar, `hartHero.js`/`voiceOrbViz.js` orb), noted where
the item is out of W1's file scope.

| Item | Status | One-line reason |
|---|---|---|
| a1 No vertical page scroll | **APPLIED** | `.hart-home { position:fixed; inset:0; overflow:hidden }`; no page scroller anywhere. |
| a2 Fixed canvas, horizontal rows, deep=panel | **APPLIED** | `.hh-cards { overflow-x:auto; overflow-y:hidden }`; `appendRowsToFit` MEASURES region height and rolls back any row that would overflow; "See all" calls `openPanel`. |
| a3 Responsive / DPI / multi-screen | **PARTIAL** | Has `@media max-width:1400px` + `max-height:820px` shrink rules + measured row-fit; full multi-monitor / DPI-scaling not in this file. |
| a4 Touch multi-desktop pager | **MISSING** | hartHome has no workspace pager; the "1 2 3 4" pager (f7) lives elsewhere and is still flagged naive. |
| b1 Spectrum not monochrome | **APPLIED** | Full 6-hue spectrum tokens; per-row `hh-accent-*`; amount gradient teal->cyan->blue; explicit "never green-only" comments. |
| b2 Netflix image-rich cards | **APPLIED** | Image cards with lazy `<img>`, gradient-art fallback, scrim, varied formats (wide/portrait/square), badges, live tags. |
| b4/b5 Behance-grade / better than Win+Mac feel | **PARTIAL** | Cinematic + GPU hover-expand + breathing glow present; the "last 20% wow" polish + real-HW buttery proof still pending (GPU lever, render-only so far). |
| b6 No em dashes | **APPLIED** | Hero subtitle built as "tasks overnight" + green "fully local..." + ". Pick up..."; no U+2014 in JS/CSS (the steward's own mockup had the em dash; the build removed it). |
| c (orb) | **OUT OF W1 SCOPE / PARTIAL** | Orb owned by hartHero.js; hartHome only calls `HartOrbHomeMode(true)` to dock it right of the hero. Render shows a breathing sphere with faint concentric rings (not a solid outer ring) and NO mic - good - but c3 "voice-viz look + switchable" is a live open regression `[#1791]` outside this file. |
| d1 Value-first earnings hero | **APPLIED (PARTIAL wording)** | "Your hive earned <amount> while you slept" + "N agents ran M tasks overnight, fully local". Uses "Spark" not "Rs/INR"; steward's mockup led with real-rupees value (k6) - consider showing the money figure. |
| d2 Continue row + progress | **APPLIED** | First row "Continue" with `hh-card-prog` progress bars, fed by running/in-progress agents. |
| d3 Image cards text-over-art | **APPLIED** | `.hh-card-body` over `.hh-card-art` + `.hh-card-scrim`; real photos lazy-load over gradient. |
| d4 Netflix listings everywhere | **PARTIAL (by design)** | Home is fully Netflix; App Store / agents / recipes / communities / settings / explorer as listings = W7, not in hartHome. |
| d5 Hover-expand 60fps | **APPLIED (GPU-gated)** | `body.gpu-hardware .hh-card:hover { transform:scale(1.07) ... }`; flat + calm on software per #137. |
| d6 Image-rich desktop app icons | **APPLIED (hook)** | hartHome.css ships `.di-glyph.di-image` image-plate rules for hartDesktop.js (image + name, glyph fallback); the manifest must supply `image`. |
| d7 Dynamic image cache / semantic index feeds cards | **PARTIAL** | Card `image` field + lazy-load wired; the semantic media index -> card image pipeline (W10) is not yet connected here (cards currently photo-or-gradient from payload). |
| e1 Top-bar restructure (brand/tabs/omnibox/orb-sm/avatar) | **OUT OF W1 SCOPE** | Top bar is liquid_ui_service.py. Render shows brand + tabs + omnibox + avatar present, but the **small orb (orb-sm) between omnibox and avatar is MISSING** in the built top bar (mockup has it). |
| e2 Omnibox 3-way routing, reuse hero bar | **PARTIAL** | hartHome `ask()` reuses `#hart-hero-input` / `toggleAssistantChat` (no fork); the 3-way deterministic/semantic/ask routing itself lives in the omnibox handler, not verified here. |
| d/e earnings + rows fed by real endpoints | **APPLIED** | `fetchEarnings` (wallet), `fetchAgents` (dashboard), `fetchRecipes`; each degrades to sample, instant first paint. |
| f1 Taps register | **APPLIED (in this layer)** | Cards/CTAs are `pointer-events:auto` with click + keydown; empty canvas stays `pointer-events:none` so wallpaper/desktop-icons/context-menu still receive events. (Compositor-level tap bugs are separate.) |
| f4/f5 Drag, snap, multi-window | **MISSING (here)** | hartHome cards are a fixed Netflix grid (not user-draggable); drag/snap/multi-window owned by hartDesktop.js/compositor, retained separately. |
| i1 Agentic Liquid UI via agent_ui_update | **APPLIED** | `window.HartHome.compose(payload)` renders hero+rows from an A2UI payload; sample is only the offline fallback; `refresh()` upgrades from live data. |
| i3 100x perf / instant paint | **APPLIED** | Paints sample instantly, no network on hot path, no continuous timers, lazy images, software-flat default, reduced-motion respected. |
| k1 Reuse, no parallel path | **APPLIED** | Actions route through existing `openPanel` / `acSend` / hero input; data via existing endpoints; comments document the no-fork intent. |
| k3 Intuitive (labels, empty states) | **APPLIED** | Labeled rows, "See all", empty-state card ("Nothing here yet"), aria labels + keyboard activation. |

### Verdict on the "looks like a webpage / vertical scroll" concern

**The built W1 home does NOT read as a webpage - the concern is structurally
resolved in this layer.** Concrete evidence:

1. `.hart-home` is `position: fixed; inset: 0; overflow: hidden` - a pinned
   desktop canvas, not a document that scrolls.
2. There is **no vertical scroll container anywhere**. The only scroll is
   `overflow-x` on the card rows (horizontal, Netflix-style), exactly as asked.
3. `appendRowsToFit()` **measures** the real region height and appends rows ONLY
   while they fit, rolling back the first row that would overflow. At 1080p the
   render shows hero + 2 rows; the 3rd sample row ("Top agents in the hive") is
   correctly dropped rather than pushed below the fold. So content never
   overflows the viewport and never needs a page scroll, and it never clips a
   row's header.
4. OS chrome frames it as a desktop: the render shows a real taskbar (Start +
   tray + clock) and the orb floating over the hero, with `--hh-top-safe` /
   `--hh-bottom-safe` reserving space so the canvas never collides with chrome.

**Why it could still feel slightly webpage-y / fall short, and the focused fixes
(report-only, not yet applied):**

- **F1 - Small orb missing in the top bar (e1).** The mockup's top bar docks a
  compact orb (orb-sm) between the omnibox and the avatar; the built top bar omits
  it, so the always-accessible orb dock the steward specified is absent. Fix:
  add `orb-sm` to the `.top-bar` restructure in `liquid_ui_service.py` and have
  the hero orb compact into it. (Outside hartHome; flag for the top-bar task.)
- **F2 - Earnings hero shows "Spark", not the money figure (d1/k6).** The steward
  led the hero with "Rs 2,140" (real-money outcome). Showing only "2,140 Spark"
  is less value-forward. Fix: surface the rupee/earnings figure (or both) in the
  hero amount, keeping the spectrum accent.
- **F3 - Only 2 rows visible at 1080p; the hive row drops silently.** Correct for
  "no scroll", but a user may not realise more rows exist. Fix (intuitive-by-
  default): the dropped row should still be reachable - e.g. a visible "More in
  Hive / Earn" affordance or nav-tab hint, so depth is discoverable without a
  scroll.
- **F4 - The orb/voice-viz regression (c3) is still open** (`[#1791]`: "voice viz
  shd look like before 1c4546bd... are the orb switchable?"). hartHome only docks
  the orb; the visualiser look + switchable skins must be restored in
  hartHero.js / voiceOrbViz.js. Until then the home's centerpiece is a plain
  sphere, not the branded breathing voice-viz.
- **F5 - "Wow" polish + real-HW proof (b4/b5).** The cinematic layer is render-
  proven on a GPU box only; the buttery-smooth, taps-register, GPU-accelerated
  experience still needs the compositor GPU lever + a real-hardware boot to clear
  the "better than Windows/macOS" bar.

Net: the desktop is now a genuine fixed canvas (the core webpage complaint is
addressed); the remaining gaps are the missing top-bar orb-sm, the money-figure
hero wording, discoverability of dropped rows, the orb voice-viz restoration, and
real-hardware polish proof.

---

## 2026-06-30 refinements - drag affordances + breathing toggle + pager switch

Three steward instructions from 2026-06-30, captured here so they cannot scatter
(consult-first / update-on-new-intent / audit-after). None contradict an EMPHATIC
rule; each REFINES an existing item.

| # | Steward 2026-06-30 (captured intent) | Checklist item | Status | Evidence |
|---|---|---|---|---|
| FIX A | Drag affordances should show only WHILE DRAGGING, not at rest / on hover. | g2.1 (new) + c4 (orb compact) + f4 | **APPLIED** | Sensory-pod grip default `opacity:0`, revealed only under `.hart-senses.dragging` (no `:hover` reveal) in `liquid_ui_service.py`; `hartSenses.js` adds/removes `.dragging` on drag start/end. Orb minimise control (`hartHero.js`) revealed by the drag handlers (onDown -> showMin / onUp -> hideMin), the hover/focus reveal removed, kept visible while compact (restore affordance). Body stays draggable; grip is visual only (hidden via opacity, width + pointer-events preserved). Behavioural `.mjs` + CSS source-guard in `tests/unit/test_orb_drag_affordances_breathing.mjs`. |
| FIX B | The orb's breathing should be switchable on/off (default ON). | c8 (new) - refines c1 transparency control + c2 breathing | **APPLIED** | One persisted flag `hart_orb_breathing` (hartHero is the sole writer) gates BOTH the `buildOrbAura` brand rings AND `voiceOrbViz`'s breathe glow (`setBreathing`); default ON keeps today's look; flipped via the orb right-click (reuses the context affordance + toast, no second settings panel). Behavioural `.mjs` proves rings build/tear + glow damps. |
| FIX C | Clicking the pager should actually SWITCH workspaces, not just select. | f7 | **APPLIED (client + honest backend no-op)** | `hartWorkspaces.js` `hartSwitchWorkspace` now also fire-and-forgets `POST /api/shell/workspaces/switch {id,name}` (covers BOTH the pager segments and the settings squares - one fn, DRY); the backend degrades hart-comp/Wayland to a 200 no-op (never 500). FOLLOW-UP (not faked): native-window workspace switching on the real hart-comp desktop stays a no-op until the `com.hart.Compositor` IPC backend replaces the `HartWmClient` swaymsg shim. (Implemented under a sibling task; recorded here for the audit trail.) |

Audit note: FIX A + FIX B were implemented on the orb/hero side (`hartHero.js`,
`voiceOrbViz.js`, `liquid_ui_service.py` CSS); they do not regress any APPLIED W1
item (c1/c2 preserved by the default-ON breathing; f4 "everything draggable"
preserved - the affordance is hidden, the drag is not).

---

## 2026-07-01 - software floor must DEGRADE GRACEFULLY + mockup fidelity + packed art

Steward (real HW d8c1567): *"the lightyourhart.js we have in js is lot better than
what I see in the OS, same for voice viz and same for the mock.html I gave many
design are deviating"* + *"bring the mockup and check for yourself on design
deviations for a netflix style, also all the prebundled apps and agents we can
statically pack with awesome icons and images"* + *"look at the html styling"*.

Root cause (audit, NOT a guess): the served shell DOES load the latest JS in the
right order. The home looked cheaper than the `hartos_home_mockup.html` because
the software-render floor (`body.gpu-software` + `is_potato`) OVER-SHED. It
discarded STATIC depth (card drop-shadows, the 3 ambient cinematic glows, the
earnings/CTA glow) along with the genuinely per-frame-expensive effects
(backdrop-filter blur, continuous drift animation, hover transforms). A static
box-shadow or a static radial glow rasters ONCE and composites cheaply forever,
so dropping them bought no per-frame saving and gutted the look. On the steward's
Intel-iGPU box the gpu-probe verdict is `software`, so the home was permanently
locked to the flat floor. Compounding: the wallpaper sat lighter/purpler than the
mockup's `#05070d`, and the orb kept a leftover indigo `#6C63FF` drop-shadow
(the same indigo b1.1 flagged) instead of the mockup's teal-inner + violet-outer
halo.

| # | Steward 2026-07-01 (captured intent) | Item | Status | Evidence |
|---|---|---|---|---|
| GF1 | The reduced-effects / software floor must DEGRADE GRACEFULLY, not GUT the look. Keep the richness (gradients, glow, the brand spectrum, the orb depth) on software; only drop the genuinely expensive live-blur / backdrop-filter / continuous animation / hover-transform. | i3 + i4 + b2/b4 (refines #137) | **APPLIED** | (1) The 3 ambient cinematic glows are now EMITTED on software (`liquid_ui_service.py` `emit_ambient = (not is_potato) or gpu_mode=='software'`) and rendered STATIC + low-blur via `body.gpu-software .hart-ambient` (animation:none, blur reduced, NOT display:none) - depth restored for ~zero per-frame cost. (2) `hartHome.css` moves the STATIC card drop-shadow + the primary-CTA teal glow to the BASE rule (software gets them); only hover-scale + transitions stay gated to `body.gpu-hardware`. The grain overlay (a per-frame blend) stays dropped on software. |
| GF2 | Match the mockup palette: the deep blue-black `#05070d` canvas, not a lighter purple wash. | b1.1 (background half) | **APPLIED** | `hartResponsive.css` `.wallpaper`, `body.gpu-software .wallpaper`, and `--hart-background` deepened toward `#05070d` (was `#0A0A11`/`#07070B`/`#0F0E17`); the brand-hue blooms are unchanged (still spectrum, never flat black). Teal/violet anchor hues stay per b1.1 (intentional, not reverted). |
| GF3 | The big orb should read like the mockup: teal core with a teal + VIOLET layered halo, not a flat indigo bloom. | c (orb) + b1.1 | **APPLIED** | `#hart-voice-orb` drop-shadow: the leftover indigo `rgba(108,99,255,.25)` (the #6C63FF b1.1 flagged) -> a teal-inner + brand-violet-outer pair (`drop-shadow(... rgba(0,230,195,.34)) drop-shadow(... rgba(155,92,255,.26))`), matching the mockup's `0 0 90px teal + 0 0 160px violet`. Core/body stay teal (no blue wash); the hero aura already frames it with a violet ring. |
| GF4 | Statically pack the prebundled apps + agents with awesome on-brand icons + images (bundled, no-network). | d6 + d8 | **APPLIED (offline pack)** | Bundled brand-art SVG posters in `integrations/agent_engine/static/app_art/` (one source generator + emitted files), referenced as the OFFLINE-default `image` on the Flagship agent cards (`hartHome.js`) and on high-recognition app manifest entries (`shell_manifest.py`). The network per-source sourcing (`app_poster.py` -> `card.image_url`, #143/d8) stays the CONTINUOUS enhancement layered ON TOP - the static pack is the no-network floor, the network poster wins only where no static `image` is set. |

Audit note: GF1-GF4 do not contradict any EMPHATIC rule. They REINFORCE i3
(buttery, no jank - static raster is free per frame), i4 (GPU is the snappy lever,
software is the safe floor that now degrades gracefully), b2/b4 (Netflix image-rich,
Behance-grade), d6/d8 (image-rich app/agent art). #137's keystroke-lag kill is
preserved: the per-FRAME costs (backdrop blur, drift animation, grain blend, hover
transforms) are still shed on software; only the FREE static depth is kept.
FOLLOW-UP (not faked): the richer Nunba landing-page "Light your HART" React tree
is a separate W2/onboarding workstream (no `lightyourhart.js` exists in the OS
shell static dir - only `hartOnboarding.js`); this pass did not pull it cross-repo.
