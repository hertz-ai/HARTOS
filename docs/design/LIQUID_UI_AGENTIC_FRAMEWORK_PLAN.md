# Liquid UI → a design-agnostic, agent-controllable runtime + two ready-made designs

> **This upgrades Pillar 1 of `docs/architecture/AI_NATIVE_OS_VISION.md`** ("Generated/
> liquid UI — agents render the right interface for the moment, A2UI"). The ledger marks
> P1 ✅ load-bearing, but its probe only checks "agent pushes a VALID component → stored+
> stamped; invalid rejected; off-switch honored". That checks the PLUMBING, not the PROMISE.
> The gap analysis proved the real capability (agent composes/extends/DRIVES the whole surface;
> morph-into-Aura) is absent (LLM authority = 2 string fields). Per the ledger's own rule
> ("erected ≠ load-bearing; a 🟡 relabeled ✅ is the trap"), P1 is OVER-CREDITED: contract-
> proven, not fully liquid. **Definition of done for THIS plan = upgrade the P1 probe** in
> `tests/probes/test_os_pillars.py` from "a component was stored" to a behavioural round-trip:
> a HART agent recomposes the surface into the Aura design / registers a NEW component type at
> runtime / drives an existing element's attribute, proven rather than asserted. Also touches P4
> (Composable: components/panels as addressable primitives) and P6 (One Fabric: cross-OS
> install). Reuse the ledger + its probe suite; do not fork a parallel vision doc.

Status: 2026-07-12. Steward thesis: a user (or the local LLM) must be able to reshape the
desktop into ANY design: tweak the existing, load an entirely new one (the "Aura" design
the steward supplied), compose a hybrid, install new ones, or have agents bake new UI on
the fly, with ALL components still working. If they can't, the desktop is "not liquid
enough." Ship BOTH designs ready-made (HART default + Aura), switchable, both fully working,
as the proof.

## VERDICT (from the gap analysis vs the Aura template): NOT liquid enough today.
The surface can retint 2 accent roles, swap 1-of-5 orb presets, change wallpaper, nudge
Glow/Density. Aura needs more than that:
- **Palette is a 2-role duotone** (`--hart-accent`, `--hart-a2`); Aura is a **4-hue mood
  quad** (violet/cyan/rose/amber). `hartPersonalize.js:126-132`, `theme_service.py:371-379`.
- **Ambient aurora field is HARDCODED rgba** (`hartResponsive.css:139-144`, `hartHero.js:76-79`)
  → a palette change never retints it.
- **Fonts: one var, no hub picker**, Space Grotesk not offered, the display/mono split
  missing. `theme_service.py:256-268,381`.
- **Layout/components: A2UI is a FIXED `COMPONENT_TYPES` allowlist** (`liquid_ui_service.py:432-517`);
  desktop chrome is hardcoded; reposition/resize and runtime-registered component types are
  both absent.
- **No live on-desktop mood dock** (theme presets force `location.reload()`).
- **The local LLM's REAL authority = `{eyebrow, feature}`**, 2 string fields (`_llm_curate_home`
  `liquid_ui_service.py:7843-7872`). The "theme by talking" path is a keyword router that
  **bypasses the LLM entirely** and force-reloads (`handleThemeCommand` :5169-5247). i1's
  transport is wired; its PROMISE (LLM composes the surface) is not met.

## The minimal path: 6 extensions, each on the file that already owns the concern (NO parallel path)
1. **Var-drive the ambient field.** Add `--hart-amb-1..4` to `theme_service.get_css_variables()`
   (:346-402); replace the hardcoded blobs (`hartResponsive.css:139-144`, `hartHero.js:76-79`)
   with `rgba(var(--hart-amb-N-rgb),…)`. Steward hybrid lands here: `--hart-accent`=teal on
   FUNCTIONAL signifiers, ambient roles = violet/cyan/rose/amber.
2. **Widen palette duotone → hue quad.** `PALETTES` shape `{a,a2,b}`→`{a,a2,a3,a4,b}`
   (`hartPersonalize.js:46-57,122-134`); `paintPalette` also sets `--hart-amb-*`;
   `theme_service._palette_overrides` already round-trips extra colours (:192-206), extend it.
3. **Live mood dock.** Render `HART_PALETTES` (named) as on-desktop swatches calling the
   already-reload-free `applyPalette(p)` (`hartPersonalize.js:135-152`); mirrors Aura's dock.
4. **Fonts in the hub + display role.** Add `--hart-font-display` + Space Grotesk/JetBrains
   Mono to `get_font_options` (:256-268); add a Font section to `hartRenderPersonalize`.
5. **Glass + glow depth sliders.** In the Feel section (`hartPersonalize.js:457-482`) bind Blur/
   Opacity/Radius to the already-consumed `--hart-blur/-panel-opacity/-radius`; widen the
   `--hart-glow` consumer set beyond hero orb + primary button.
6. **Real LLM compositional authority (the i1 fix).** Widen `_llm_curate_home`'s JSON
   contract (still validated against `HOME_ROW_ACCENTS`/`HOME_CARD_ACTIONS`/`HOME_PANEL_TARGETS`)
   so the LLM drives per-row accent/emphasis + mood + palette; turn `COMPONENT_TYPES` into a
   RUNTIME component REGISTRY an agent can extend (register a new component type + template/
   behaviour), reusing the existing `agent_ui_update` rate-cap/kill-switch/audit/XSS gate.

## The two ready-made designs (built ON the widened surface)
- **HART** (default): the current brand, teal-lead, the shipped look. Preset = current var values.
- **Aura**: violet-forward ambient (hue quad 295/210/350/75 = violet/cyan/rose/amber), teal on
  functional signifiers (steward hybrid), Space Grotesk + JetBrains Mono, heavier glass/glow,
  the mood dock. Preset = a values file over the widened vars + a compose layout.
- **Switcher**: the mood/theme dock flips between them LIVE (no reload); all components (orb,
  cards, launch, voice, panels) keep working under either, since skin is orthogonal to function.

## Discipline
Every step extends an existing hook (theme_service.get_css_variables, hartPersonalize
sections/PALETTES/Feel, hartResponsive.css blobs, _llm_curate_home + COMPONENT_TYPES); nothing
here adds a second palette or a second transport. Behavioural test per step (render + assert the
var/component reaches the DOM). Design-checklist: this REALISES i1/i2 (LLM composes the surface)
and keeps b1.2 teal on functional signifiers as the DEFAULT while making the ambient/mood
fully reskinnable. Real-HW verify each design renders + all components work.

---

## Audit 2026-07-13: PENDING gaps (erected ≠ load-bearing)

Two parallel read-only audits (impl + tests) against these requirements. Server spine is
solid + governed (one allowlist gate: kill-switch/rate-cap/XSS/audit, reused by
agent_ui_update + register_component_type + compose_home; HART-agents-only; both presets
shipped; theme-var quad depends on CSS; compose_home hero/rows works end-to-end via
`/api/home/compose`). But the AGENTIC promise is only half-wired to the client:

### P0: a steward ask that is dead end-to-end today
- **G1 LLM `mood` handle dies before the DOM.** Produced by `_llm_curate_home`
  (liquid_ui_service.py:8235), carried by `compose_home(mood=)` (:1246) + `compose_home_now`
  (:1282), then DROPPED: `/api/home/compose` omits the mood kwarg (:6438); `HartHome.compose`
  merges only hero+rows (hartHome.js:909); the SSE consumer never calls `applyPalette`
  (:5850); NO mood-id→palette resolver on the client. P1 probe asserts mood is STORED, not
  rendered. → wire route→compose→SSE→applyPalette + add a mood→palette resolver.
- **G2 `register_component_type` has no caller + custom types can't render.** Registry works
  (:1098) but only tests invoke it; no agent tool/route; client falls back to JSON.stringify
  for unknown types (:6129); stored `template` (:1151) has no renderer. → add an agent
  entry point + a client renderer that honours the registered spec/template.
- **G3 Aura not live-switchable from the desktop.** Presets ship, but the picker `PRESETS`
  (hartPersonalize.js:79) + keyword router (:5531) have no `aura`, the shell never fetches
  `/api/appearance/presets` (which includes it), and `applyPreset` force-reloads (:5553)
  vs the plan's flip-live. Only Aura MOODS are live. → surface server presets in the picker +
  make preset apply live (no reload).

### P1
- **G4 SSE store→client round-trip is NOT integration-tested.** `/api/notifications/stream`
  is never fetched by any test; every "SSE" assertion stops at the in-process
  `_agent_components` dict. → a Flask test_client SSE-drain integration test.
- **G5 component `events`/`behaviors` are declarative-only.** `metric` declares emits:['click']
  (:452) but its render branch has no click emitter. → wire declared events on the client.
- **G6 agent-readable spec catalogue has no consumer.** `list/get_component_spec` called only
  by tests; the local intelligence has no wired accessor. → expose to the composer.

### P2 (minor erected-not-load-bearing)
- mood dock `window.hartRenderMoodDock` defined + unit-tested but never mounted, no CSS.
- `--hart-density` only scopes the Personalize panel, not the desktop.
- glow bloom applies only after the Personalize hub is opened once (ensureFeelStyle at boot).
- plain `--hart-amb-1..4` hex vars unread (only the `-rgb` triples consumed); `--hart-a2`
  consumed by one marketplace gradient only.

### Test-coverage truth
Integration-covered: compose_home + agent_ui_update WRITE path (route→store), shell
render/serve path, ThemeService apply routes. Unit/probe only: component registry,
_llm_curate_home/mood, hartPersonalize JS, hhCardRow, quad palette vars. NO behavioural
coverage: the SSE store→client leg. CI-only (node) + vacuous-pass risk:
test_shell_hhcardrow.mjs / test_liquid_ui_shell_panels.mjs print ALL-PASS+exit0 if neither
node nor a rendering python is present.

---

## Reuse-first WIRING plan (2026-07-13): implement + fully integration-test, ZERO regression

Principles: **absolute reuse** (every wire extends an existing hook in HARTOS or mirrors
existing logic in Nunba-Companion, with no parallel path), **zero regression** (run the relevant
existing tests green BEFORE and AFTER each change), **fully integration-tested** (each new
wire gets a REAL cross-boundary test, run locally in the fresh base-3.12.10 venv that has
working ctypes). Reuse map from the 2026-07-13 dual reuse-hunt (HARTOS + Nunba React).

### Reuse inventory (the existing pieces every gap leans on)
- **Live palette apply (client):** `window.HartPalette.paint(entry)` / `.apply(entry,opts)`,
  hartPersonalize.js:139/166/192. Sets `--hart-accent(-rgb)`, `--hart-a2(-rgb)`,
  `--hart-amb-1..4(-rgb)`, `--hart-background` from a palette OBJECT `{accent,a,a2,a3,a4,b}`.
  Mirrors Nunba `injectCSSVars` (ThemeContext.js:52-97), same setProperty approach.
- **Palette vocabulary (client, authoritative):** `PALETTES`/`window.HART_PALETTES`
  hartPersonalize.js:53-74, 16 ids incl 6 Aura moods with the full quad. No `byId` resolver.
- **SSE consumer (client JS in the shell f-string):** liquid_ui_service.py:5837-5862;
  home/home_compose branch :5850-5855 (already RECEIVES `ev.mood`, drops it);
  `renderAgentOverlay` type dispatch :5927-6133, generic fallback :6129.
- **Markup/emit helpers (client JS):** `_esc` :5881, `dsBtn` :3043, form field-iter :6072-6087,
  event emit `data-action`+`shellA2UIListSelect` :6099/:5890, `/api/a2ui` ingest :6417.
- **Transport (server):** `agent_ui_update` :912 (allowlist already accepts custom types :932);
  `compose_home(mood=)` :1220/1246; `/api/home/compose` :6432 (drops mood, a one-line fix);
  `list_component_specs`/`get_component_spec` :1074/1089; `_llm_curate_home` prompt :8164-8184
  (only 2 example mood ids); registry via `get_registry().get_or_none('LiquidUIService')` :8287.
- **ThemeService:** `get_css_variables` theme_service.py:347; `list_presets` :39;
  `/api/appearance/presets|apply|active|css` api_theme.py:25/39/31/87. `apply_theme` persists
  only (no DOM emit); live apply stays client-side via paintPalette.
- **Nunba portable logic:** AgentOverlay.jsx `OverlayContent` switch + `default:` (713-775);
  ServerDrivenUI `RenderNode` (465-1036); `getPresetById`/`mergeThemeConfig` (themePresets.js).
- **Test harnesses:** svc+client fixture test_home_compose_feed.py:28-37; kill-switch/audit mock
  :80-123; model-bus `pooled_post` mock test_home_producer.py:155-166; **SSE drain**
  test_compute_earnings_e2e.py:274-297 (the ONLY real event-stream drain, reuse for G4);
  render-shell var assert test_shell_software_render_perf.py:110-148; .mjs shim
  test_customization_hub.mjs (+ node-skip wrapper), guard vacuous-pass hhcardrow.mjs:50-59.

### Per-gap wiring (reuse → change → integration test → regression gate)

**G1 mood→DOM.** (a) Add `HartPalette.byId(id)` resolver in hartPersonalize.js (find over
`PALETTES`, return null on miss). (b) In the SSE home_compose branch (:5850-5855) after
`HartHome.compose`, `var mp=window.HartPalette&&HartPalette.byId((ev.payload||ev).mood);
if(mp) HartPalette.paint(mp)`, a no-op on miss (graceful). (c) `/api/home/compose` (:6438) pass
`mood=payload.get('mood')`. (d) Enumerate the 16 palette ids in the `_llm_curate_home` prompt
(:8176) so the model emits resolvable ids. Tests: INTEGRATION route test (POST mood → store
carries it), render test (mood id resolves client-side; .mjs drives byId+paint on the shim),
prompt test (the 16 ids appear in the built prompt). Regression: test_home_compose_feed,
test_home_producer, test_customization_hub.

**G2 custom-type render.** Ship `list_component_specs()` into the shell (a `window.HART_SPECS`
JSON blob in render_desktop_shell, same way other server data is inlined) so the client knows
custom types' props/template. Add ONE generic branch before renderAgentOverlay's `else` (:6129):
if `ev.type` ∈ HART_SPECS, render from `template` (interpolate props) else iterate
`spec.props` as label/value rows, reusing `_esc`+`dsBtn`+form-iter; mirror Nunba AgentOverlay
`default:` contract. Tests: INTEGRATION (register type via svc, render shell, assert the spec
blob carries it), .mjs (drive renderAgentOverlay on a custom type → asserts spec-driven DOM, not
JSON dump). Regression: test_os_pillars P1, test_shell_hhcardrow.

**G3 Aura live-switch.** (a) Populate the picker from `/api/appearance/presets` (fetch on hub
open) instead of hardcoded PRESETS, OR add `aura` to PRESETS + the keyword router (:5535). (b)
Replace `applyPreset` `location.reload()` (:5558) with a live swap: after the apply POST, fetch
`/api/appearance/css` and replace the `:root` style block (mirror paintPalette / Nunba
injectCSSVars). Tests: INTEGRATION (apply aura → `/api/appearance/active` css carries aura quad),
render fidelity (aura preset vars reach the served shell), .mjs (applyPreset live-swaps vars, no
reload call). Regression: test_customization_hub, test_theme_service, test_home_design_fidelity.

**G4 SSE round-trip test.** Reuse test_compute_earnings_e2e.py:274-297 drain pattern on the
liquid-ui client fixture: push via agent_ui_update, GET `/api/notifications/stream`
buffered=False, drain first chunk, assert the component arrives. INTEGRATION. No code change.

**G5 declared events.** In the `metric` branch (:6058) attach `onclick` via the
`shellA2UIListSelect`/`data-action` pattern (:6099/:5890) → `/api/a2ui`; generalize: the custom
renderer (G2) wires each declared `event` the same way. Test: .mjs asserts a declared-event
component renders an emitter that posts to /api/a2ui. Regression: test_os_pillars.

**G6 spec consumer.** In `_llm_curate_home` inject `list_component_specs()` (via
get_registry().get_or_none('LiquidUIService'), pattern :8287) names into the prompt (:8164-8184)
so the LLM composes from real types. Test: test_home_producer asserts the prompt lists the
catalogue types. Regression: test_home_producer.

**G7-G10 (P2).** Mount hartRenderMoodDock in the shell + add `.hart-mood-sw` CSS (reuse the
gallery-card CSS); apply `--hart-density` to a desktop spacing consumer; move glow `ensureFeelStyle`
into boot `restore()`; consume-or-remove the unread hex amb vars + `--hart-a2`. Small, each with a
render/.mjs assertion. Regression: test_customization_hub.

### Test env
Fresh venv from base Python 3.12.10 (`$CLAUDE_JOB_DIR/tmp/venvtest`, ctypes OK) + pip flask/
flask-cors/werkzeug/requests/pydantic/cryptography/pyjwt/cachetools/pytest, runs the liquid-ui
route + probe suites locally (`--noconftest -p no:capture`). Baseline 2026-07-13: 33 passed.
.mjs need node (CI-only); validate logic by reading + the python render half locally.

---

## RESOLVED 2026-07-13: all gaps wired, integration-tested, zero regression

Implemented reuse-first (every wire extends an existing hook; no parallel path), each
with a REAL cross-boundary test (server via pytest, client via node + a DOM shim driving
the actual shell JS -- run locally via HART_TEST_PYTHON and in CI). Zero regression: the
relevant existing suites stayed green before/after (the only failures are the pre-existing
test_theme_service TestThemeAPI blueprint-fixture 404s, unchanged).

| Gap | Resolution (reuse) | Tests |
|---|---|---|
| G1 | mood -> DOM: HartPalette.byId resolver + SSE branch paints via existing HartPalette.paint; /api/home/compose carries mood; prompt enumerates the 16 palette ids (HART_MOOD_PALETTE_IDS mirrors PALETTES, drift-guarded) | route-carries-mood x3, prompt-enumerates-16, drift-guard, byId->paint .mjs, SSE-glue render |
| G2 | custom-type render: agent_ui_update stamps ev._spec={props,template,events} on the push; renderAgentOverlay branch renders template/props (reuses _esc), live on first push | push-carries-spec, builtin-no-spec, template-fills-props + prop-rows + xss-escape .mjs |
| G3 | Aura live-switch: applyPreset LIVE-swaps via /api/appearance/css (no reload); gallery renders from /api/appearance/presets (fallback offline); list_presets adds secondary+surface; keyword router gains aura/high-contrast | list_presets-surfaces-aura, applyPreset-live-swap render, gallery-from-server .mjs |
| G4 | SSE store->client round-trip integration test (reuses the earnings SSE-drain pattern) | stream-delivers-pushed-component |
| G5 | declared events: shellA2UIEmit (same /api/a2ui ingest); metric emits its click; custom types with 'click' wrap the emitter; events ride _spec | declared-events-ride-spec, metric+custom emit + no-event .mjs |
| G6 | GET /api/a2ui/specs exposes list_component_specs() (discovery ingress; mirrors /api/appearance/presets) | specs-route-returns-catalogue (builtins + custom) |
| G7 | mood-dock .hart-mood-sw/.hart-mood-label CSS added to the one injected feel-style (component completed). NOT force-mounted on the fixed desktop -- a design-checklist decision; mood switching already exists via the Personalize Palette section. | mood-sw-CSS-defined .mjs |
| G8 | --hart-density now scales the DESKTOP home rows (.hh-rows) via the same injected feel-style (was panel-only). density=1 = pixel-identical. | density-scales-hh-rows .mjs |
| G9 | glow blooms at boot: restore() injects ensureFeelStyle() (was hub-open only) | feel-style-at-boot .mjs |
| G10 | NOT a defect: the hex --hart-amb-1..4 are the CANONICAL palette values (asserted by test_hart_and_aura_presets); the -rgb triples are the derived compositing form consumers use. --hart-a2 IS consumed (marketplace). No dead output to remove without regressing the tested contract. | (existing preset-css tests) |

Local test env: a fresh venv from base Python 3.12.10 (working ctypes) + flask/cryptography/
pytest; `.mjs` run via `HART_TEST_PYTHON=<venv>` + node. Both are also exercised in CI.
