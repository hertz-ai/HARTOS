# Liquid UI → a design-agnostic, agent-controllable runtime + two ready-made designs

> **This upgrades Pillar 1 of `docs/architecture/AI_NATIVE_OS_VISION.md`** ("Generated/
> liquid UI — agents render the right interface for the moment, A2UI"). The ledger marks
> P1 ✅ load-bearing, but its probe only checks "agent pushes a VALID component → stored+
> stamped; invalid rejected; off-switch honored" — the PLUMBING, not the PROMISE. The gap
> analysis proved the real capability (agent composes/extends/DRIVES the whole surface;
> morph-into-Aura) is absent (LLM authority = 2 string fields). Per the ledger's own rule
> ("erected ≠ load-bearing; a 🟡 relabeled ✅ is the trap"), P1 is OVER-CREDITED — contract-
> proven, not fully liquid. **Definition of done for THIS plan = upgrade the P1 probe** in
> `tests/probes/test_os_pillars.py` from "a component was stored" to a behavioural round-trip:
> a HART agent recomposes the surface into the Aura design / registers a NEW component type at
> runtime / drives an existing element's attribute — proven, not asserted. Also touches P4
> (Composable: components/panels as addressable primitives) and P6 (One Fabric: cross-OS
> install). Reuse the ledger + its probe suite; do not fork a parallel vision doc.

Status: 2026-07-12. Steward thesis: a user (or the local LLM) must be able to reshape the
desktop into ANY design — tweak the existing, load an entirely new one (the "Aura" design
the steward supplied), compose a hybrid, install new ones, or have agents bake new UI on
the fly — with ALL components still working. If they can't, the desktop is "not liquid
enough." Ship BOTH designs ready-made (HART default + Aura), switchable, both fully working,
as the proof.

## VERDICT (from the gap analysis vs the Aura template): NOT liquid enough today.
The surface can retint 2 accent roles, swap 1-of-5 orb presets, change wallpaper, nudge
Glow/Density. Aura's soul is inexpressible:
- **Palette is a 2-role duotone** (`--hart-accent`, `--hart-a2`); Aura is a **4-hue mood
  quad** (violet/cyan/rose/amber). `hartPersonalize.js:126-132`, `theme_service.py:371-379`.
- **Ambient aurora field is HARDCODED rgba** (`hartResponsive.css:139-144`, `hartHero.js:76-79`)
  → a palette change never retints it → no cosmic richness.
- **Fonts: one var, no hub picker**, Space Grotesk not offered, no display/mono split.
  `theme_service.py:256-268,381`.
- **Layout/components: A2UI is a FIXED `COMPONENT_TYPES` allowlist** (`liquid_ui_service.py:432-517`);
  desktop chrome is hardcoded; no reposition/resize; no runtime-registered new component types.
- **No live on-desktop mood dock** (theme presets force `location.reload()`).
- **The local LLM's REAL authority = `{eyebrow, feature}`** — 2 string fields (`_llm_curate_home`
  `liquid_ui_service.py:7843-7872`). The "theme by talking" path is a keyword router that
  **bypasses the LLM entirely** and force-reloads (`handleThemeCommand` :5169-5247). So i1's
  transport is wired but its PROMISE (LLM composes the surface) is not met.

## The minimal path — 6 extensions, each on the file that already owns the concern (NO parallel path)
1. **Var-drive the ambient field** — add `--hart-amb-1..4` to `theme_service.get_css_variables()`
   (:346-402); replace the hardcoded blobs (`hartResponsive.css:139-144`, `hartHero.js:76-79`)
   with `rgba(var(--hart-amb-N-rgb),…)`. Steward hybrid lands here: `--hart-accent`=teal on
   FUNCTIONAL signifiers, ambient roles = violet/cyan/rose/amber.
2. **Widen palette duotone → hue quad** — `PALETTES` shape `{a,a2,b}`→`{a,a2,a3,a4,b}`
   (`hartPersonalize.js:46-57,122-134`); `paintPalette` also sets `--hart-amb-*`;
   `theme_service._palette_overrides` already round-trips extra colours (:192-206) — extend.
3. **Live mood dock** — render `HART_PALETTES` (named) as on-desktop swatches calling the
   already-reload-free `applyPalette(p)` (`hartPersonalize.js:135-152`); mirrors Aura's dock.
4. **Fonts in the hub + display role** — add `--hart-font-display` + Space Grotesk/JetBrains
   Mono to `get_font_options` (:256-268); add a Font section to `hartRenderPersonalize`.
5. **Glass + glow depth sliders** — Feel section (`hartPersonalize.js:457-482`) bind Blur/
   Opacity/Radius to the already-consumed `--hart-blur/-panel-opacity/-radius`; widen the
   `--hart-glow` consumer set beyond hero orb + primary button.
6. **Real LLM compositional authority (the i1 fix)** — widen `_llm_curate_home`'s JSON
   contract (still validated against `HOME_ROW_ACCENTS`/`HOME_CARD_ACTIONS`/`HOME_PANEL_TARGETS`)
   so the LLM drives per-row accent/emphasis + mood + palette; turn `COMPONENT_TYPES` into a
   RUNTIME component REGISTRY an agent can extend (register a new component type + template/
   behaviour), reusing the existing `agent_ui_update` rate-cap/kill-switch/audit/XSS gate.

## The two ready-made designs (built ON the widened surface)
- **HART** (default): the current brand — teal-lead, the shipped look. Preset = current var values.
- **Aura**: violet-forward ambient (hue quad 295/210/350/75 = violet/cyan/rose/amber), teal on
  functional signifiers (steward hybrid), Space Grotesk + JetBrains Mono, heavier glass/glow,
  the mood dock. Preset = a values file over the widened vars + a compose layout.
- **Switcher**: the mood/theme dock flips between them LIVE (no reload); all components (orb,
  cards, launch, voice, panels) keep working under either — skin is orthogonal to function.

## Discipline
Every step extends an existing hook (theme_service.get_css_variables, hartPersonalize
sections/PALETTES/Feel, hartResponsive.css blobs, _llm_curate_home + COMPONENT_TYPES) — no
second palette, no second transport. Behavioural test per step (render + assert the var/
component reaches the DOM). Design-checklist: this REALISES i1/i2 (LLM composes the surface)
and keeps b1.2 teal on functional signifiers as the DEFAULT while making the ambient/mood
fully reskinnable. Real-HW verify each design renders + all components work.
