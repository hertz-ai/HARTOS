# Native Shell CSS Parity Ledger

**Status:** binding parity contract. **Companion to:** `NATIVE_SHELL_PARITY_PROGRAM.md`.
**Sources inventoried (exhaustive, read in full):**

| # | Source | Scope |
|---|--------|-------|
| A | `integrations/agent_engine/liquid_ui_service.py` :: `LiquidUIService.render_desktop_shell()` (l.1393-2823) | the served inline `<style>` f-string: `{css_vars}` -> `{accent_rgb_css}` -> `{a11y_fontscale}` -> inline shell CSS -> `{_CSS_SLIDE_IN}` -> `{_CSS_FADE_OUT}` -> `{_CSS_PULSE}` -> `{_CSS_ANIMATIONS\|_CSS_NO_ANIMATIONS}` -> `{_CSS_DESIGN_SYSTEM}` -> `{_CSS_HERO}` -> `{_CSS_DESKTOP}` -> `{_CSS_LIVING_GLASS}` -> `{_CSS_POTATO_OVERRIDE}` |
| B | `integrations/agent_engine/static/hartHome.css` (790 lines) | Aura home surface: earnings hero, Netflix rows, image cards, Aura Motion System, top-bar additions |
| C | `integrations/agent_engine/static/hartResponsive.css` (624 lines) | loads LAST, wins on source order: responsive overrides, CINEMATIC v3 overhaul, software-render floor (#137) |

---

## What this ledger is

This ledger is the **EXHAUSTIVE parity contract** for `NATIVE_SHELL_PARITY_PROGRAM.md`.
Every CSS surface, effect, state, and animation that the HTML shell paints today is
enumerated here and mapped to a native SceneNode. **Nothing in the HTML shell may be
dropped.** Steward mandate, 2026-07-20: *"nothing shd be left"*.

Rules that follow from that mandate:

1. **No silent omissions.** If a rule is not portable it gets a row anyway, marked as a
   GAP or a DEAD rule, with the reason. Absence from the native shell must be a recorded
   decision, never an oversight.
2. **Cascade order is load-bearing.** Several selectors are declared 2-3 times and
   later-source-wins is a deliberate "re-skin without deleting" strategy, so a stale
   cached build still renders. A native port that de-duplicates naively ships the WRONG
   visual. Every confirmed supersession is recorded in the row for its component.
3. **Three parallel token systems coexist and must be reconciled, not merged:**
   `--hart-*` (theme, server-emitted, hot-swappable at runtime via
   `<style id="hart-theme-live">`), `--ds-*` (Material-3 design system, static),
   `--lg-*` (Living Glass, which imports `--hart-accent` / `--ds-ease-*` rather than
   redefining). Files B and C add `--hh-*` and `--hv-*` on top. Only `--hart-*` is
   live-swappable.
4. **Three independent motion kill-switches must all exist natively:** the
   `prefers-reduced-motion` media query (global 0.01ms clamp + `--t-*` -> 0ms), the
   `html.a11y-rmotion` class mirror (server-applied from `get_a11y_settings()`), and the
   potato tier (Python-side -- it strips animation strings before they are ever emitted).
5. **Two degradation floors on top of that:** `body.gpu-software` (no compositing budget)
   and `body.webkit-flat` (backdrop-filter will not paint at all). Degrade gracefully,
   never gut: one-time-raster depth (drop shadows, static glows, legibility scrims) is
   KEPT on the floor; only per-frame work (live blur, drift, spin, breathe) is shed.

## Legend -- native SceneNode kinds

| Kind | Meaning |
|------|---------|
| **Field** | full-bleed, non-interactive background strata: wallpaper, ambient/bloom, grain, vignette, snap-grid ghosts, marquee |
| **Orb** | the voice orb and its intrinsic bloom/state character |
| **Ring** | concentric presence/orbital geometry around the orb and around stateful controls (presence rings, comet, orbital dashes, ripples) |
| **Card** | a bounded elevated plate: panel/window, image card, app card, widget, dialog, toast, modal, tile |
| **Row** | a horizontally-scrolling or grid rail plus its head/see-all affordances; also grids of items (start grid, app grid) |
| **Text** | typographic runs: type scale, numerals, labels, headings, status lines |
| **Glyph** | icon-font ligature glyphs, emoji, dots, badges, plate-mounted symbols |
| **Chrome** | persistent OS furniture: top bar, taskbar, workspace pager, senses pod, scrollbars, focus rings, skip link |

## Legend -- milestones

| Milestone | Covers |
|-----------|--------|
| **M1** | Field + Chrome floor: token roots, fixed-canvas invariant, wallpaper/ambient/grain/vignette, glass ladder, top bar, taskbar, z-order |
| **M2** | Card + Row rails: home rows, image cards, marketplace cards, desktop icons, start grid, widgets, gallery tiles |
| **M3** | Orb + Ring presence: hero orb, orbital rings, presence rings, comet, ripple, Aura Motion layers 1-3 |
| **M4** | Text + Glyph system: DS type scale, icon font + ligatures, tabular numerals, labels, empty/offline states |
| **M5** | Windowing + overlays: panels, start menu, modals, context menu, toasts, lock screen, onboarding, dialogs, assistant chat |
| **M6** | State engine + degradation: `html[data-*]` visibility contract, a11y kill switches, potato / gpu-software / webkit-flat floors, live theme hot-swap |

---

# Part A -- inline shell CSS (`liquid_ui_service.py`)

## A1. Theme token root (`--hart-*`) -- server-emitted, live-swappable

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `:root` from `ThemeService.get_css_variables` | full `--hart-*` set; `--hart-glass-bg` composed as `rgba(var(--hart-glass-rgb),var(--hart-panel-opacity))`; ambient quad `--hart-amb-N-rgb` is the de-monochrome MOOD channel written by `hartPersonalize.paintPalette` while accent stays teal on functional signifiers | theme-swap; a11y font-scale override (later source wins over `css_vars`) | none | **Chrome** token store -- M1 (definition) / M6 (hot-swap) |
| hardcoded fallback set (l.1406) when `ThemeService` import fails | `--hart-background:#0F0E17`, `--hart-accent:#00D4AA`, `--hart-on-accent:#0F0E17`, `--hart-active:#00e676`, `--hart-text:#e0e0e0`, `--hart-glass-bg:rgba(15,14,23,.65)`, `--hart-glass-border:rgba(0,212,170,.18)`, `--hart-muted:#78909c`, `--hart-surface:#1a1a2e`, `--hart-blur:20px`, `--hart-saturation:180%`, `--hart-radius:16px`, `--hart-panel-opacity:.65`, `--hart-topbar-height:40px`, `--hart-icon-size:20px`, `--hart-titlebar-height:32px`, `--hart-font-family:"JetBrains Mono"`, `--hart-font-size:13px`, `--hart-heading-size:18px`, `--hart-font-weight:400`, `--hart-heading-weight:600`, `--hart-anim-speed:200ms`, `--hart-error:#FF6B6B`, `--hart-caution:#ffab40`, `--hart-heading:#00D4AA`, `--hart-surface-hover:#252540` | import failure | none | **Chrome** compiled-in default table -- M1 |
| `:root{--hart-accent-rgb:R,G,B}` (`accent_rgb_css`, l.1533) | numeric triple derived from accent, so every `rgba(var(--hart-accent-rgb),A)` glow retints | theme-swap | none | **Chrome** derived token -- M1 |
| `:root{--hart-font-size/--hart-heading-size/--hart-icon-size}` (`a11y_fontscale`, l.1513) | emitted only when `abs(font_scale-1)>0.01`, clamped 0.8..2.0 | font_scale != 1.0 | none | **Text** metric override -- M4/M6 |
| `style#hart-theme-live` (runtime, l.5747) | `applyPreset()` hot-swaps ONLY the `:root` custom-property block -- no reload; reload is the fetch-failure fallback | live retint | none | **Chrome** live token channel -- M6 |

## A2. Design-System token root (`--ds-*`)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `:root` (`_CSS_DESIGN_SYSTEM` l.1598-1646) | typography `--ds-font-body:"Inter",-apple-system,"Segoe UI",Roboto,sans-serif` / `--ds-font-mono:"JetBrains Mono","Fira Code",monospace`; 4dp spacing grid 0/1/4/8/12/16/20/24/32/40/48/64px; 6-rung elevation ladder; motion 100/200/350/500ms; 4 easings; 6 surface tones; 4 state layers; 6 radii; 5 icon sizes | none | none | **Chrome** static design-token table -- M1 |
| `html,body` | `font-family:var(--ds-font-body);line-height:1.5` | none | none | **Text** root metrics -- M4 |

## A3. Living-Glass token root (`--lg-*`, `--t-*`)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `:root` (`_CSS_LIVING_GLASS` l.2318-2363) | accent triad `--lg-accent`/`-rgb` + 3 glow stops; **deterministic state hues** listen `0,224,194` / think `108,99,255` / speak `25,227,125` / vision `52,176,255` / blind `120,120,132` / alert `255,92,122`; ink ladder `#F4F6FF/#E4E7F2/#9AA2B8/#646B82`; **4-rung glass depth ladder** (bg/blur/border: `.42`/14px/`.07`, `.56`/20px/`.10`, `.70`/26px/`.13`, `.82`/34px/`.16`, `--lg-sat:1.4`); specular `inset 0 1px 0 0 rgba(255,255,255,.14)` + 4 shadow rungs; **4 presence rings** as 3-stop layered box-shadows; type `--lg-num:"tnum" 1` + 3 letter-spacings; 5 motion-role easings; 5 duration roles + `--lg-stagger:28ms`; geometry `--lg-grid:92px`, `--lg-pad:24px`, `--lg-snap-widget:24px` (mirrors `hartDesktop.js` GRID/PAD) | none | none | **Ring** + **Card** + **Chrome** token table -- M1/M3 |
| `@media(prefers-reduced-motion:reduce) :root` (l.2364) | zeroes ALL duration roles `--t-micro..--t-ceremony:0ms` -- a GLOBAL motion kill-switch | reduced-motion | none | **Chrome** motion gate -- M6 |

## A4. Global reset / document / selection

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `*` | `margin:0;padding:0;box-sizing:border-box` | none | none | **Chrome** layout base -- M1 |
| `::selection` | `background:var(--hart-accent);color:#fff` | selection | none | **Text** -- M4 |
| `html,body` | `width/height:100%;overflow:hidden` (**FIXED-CANVAS INVARIANT -- no page scroll**), theme font family/size/weight, `color:var(--hart-text)`; hero layer adds `overscroll-behavior:none`, `-webkit-font-smoothing:antialiased`, `-moz-osx-font-smoothing:grayscale`, `text-rendering:optimizeLegibility`, `-webkit-tap-highlight-color:transparent` | none | none | **Field** root canvas + **Text** -- M1 |
| `img` | `-webkit-user-drag:none;user-select:none` | none | none | **Card** art -- M2 |
| cursor group (`.top-bar,.taskbar,.start-menu,.panel-titlebar,.start-item,.taskbar-chip,.ds-btn,.hart-hero-chip,.hart-hero-status,.hart-hero-brand`) | `cursor:default` -- kills the web pointer tell | none | none | **Chrome** cursor policy -- M1 |

## A5. Wallpaper layer (z0)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.wallpaper` | `position:fixed;inset:0;z-index:0;background:{wp_css}`. Default (l.1519) = `radial-gradient(120% 120% at 18% 0%,rgba(0,212,170,.07),transparent 50%)`, `radial-gradient(100% 100% at 100% 100%,rgba(22,33,62,.55),transparent 60%)`, `linear-gradient(135deg,#0F0E17 0%,#1a1a2e 50%,#16213e 100%)`. `wallpaper.type=='solid'` substitutes a flat colour (server-substituted, not a var) | theme wallpaper type | none | **Field** base layer -- M1 |

## A6. Ambient aurora + runtime bloom canvas (z0/z1)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-ambient` | `position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.9`; 4 stacked radial blobs `46%x52%@30%,30%` amb-1 `.20` / `40%x46%@82%,26%` amb-2 `.16` / `42%x48%@24%,84%` amb-3 `.12` / `30%x36%@82%,82%` amb-4 `.10`. **NO `filter:blur`** -- the live 64px blur was deliberately REMOVED (steward 2026-07-19); these gradients are the pre-JS/canvas-unavailable FALLBACK only. Emission gate (l.1489): `emit_ambient = (not is_potato) or gpu_mode=='software'` | thinking / voice / speaking; reduced-motion; potato (element omitted) | `@keyframes hart-ambient-drift` is DEFINED (l.1996) but **NEVER APPLIED** -- dead rule; reduced-motion still targets `.hart-ambient{animation:none}` | **Field** aurora -- M1 |
| `canvas.hart-bloom-canvas` | `position:fixed;inset:0;z-index:1;pointer-events:none;100%x100%;display:block`. Gaussian blur composed ONCE per compose (`composeHartBloom`/`hartBloom.js`), reused every frame | compose / recompose | see B17 | **Field** composited bloom texture -- M1 |
| state tint rules | filter-only, one-shot: `data-thinking` -> `saturate(150%) brightness(1.06)`; `data-voice` -> `saturate(162%) brightness(1.09)`; `data-speaking` -> `saturate(156%) brightness(1.05)`; each `transition:filter var(--t-reveal)` | `data-thinking\|voice\|speaking=1` | `transition:filter var(--t-reveal)` | **Field** state tint -- M6 |

## A7. Film grain overlay (z2)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-grain` | `position:fixed;inset:0;z-index:2;pointer-events:none;opacity:.045`; **`mix-blend-mode:overlay` -- the ONLY blend mode in the whole sheet**; background = inline SVG data-URI `feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='3'` on a 120x120 tile. Emitted only when NOT potato (l.2831) | potato -> not emitted; `body.gpu-software` -> `display:none` (file C) | none | **Field** noise overlay (needs a real screen/overlay blend in the compositor) -- M1 |

## A8. Vignette (z2)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-vignette` | `position:fixed;inset:0;z-index:2;pointer-events:none;background:radial-gradient(120% 120% at 50% 38%,transparent 56%,rgba(0,0,0,.30) 100%)`. Always emitted (no potato gate) | none | none | **Field** framing -- M1 |

## A9. Glass mixin (`.glass`) -- legacy chrome surface

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.glass` | `background:var(--hart-glass-bg)`; `backdrop-filter:blur(var(--hart-blur)) saturate(var(--hart-saturation))` + `-webkit-` -- **CONDITIONAL**: replaced by the comment `/* blur disabled for performance */` when potato; `border:1px solid var(--hart-glass-border)` with `border-top-color:rgba(255,255,255,.16)` top-light rim; dual inset `inset 0 1px 0 0 rgba(255,255,255,.08), inset 0 -1px 0 0 rgba(0,0,0,.18)`; `border-radius:var(--hart-radius)`. Applied to `.top-bar`, `.taskbar`, `.start-menu`, `.agent-pill`, `.assistant-chat`, `.ctx-menu`, `.hart-ws-switcher`, `.hart-hero-bar` | potato (no blur); `html.a11y-contrast` -> `background:#0a0a12;border-width:2px`; `body.webkit-flat` (blur will not composite -> solidify, floor in file C) | none | **Card**/**Chrome** glass material -- M1 |

## A10. Living-Glass elevation ladder (`.lg-1..lg-4`)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.lg-1,.lg-2,.lg-3,.lg-4` | base: `border:1px solid var(--lg-1-bd)`, `box-shadow:var(--lg-spec),var(--lg-sh-1)`, `background:var(--lg-1-bg)`, `backdrop-filter:blur(var(--lg-1-blur)) saturate(var(--lg-sat))`; rungs 2/3/4 raise bg+border+shadow and blur to 20/26/34px | -- | none | **Card** depth ladder -- M1 |
| `.lg-num` | `font-variant-numeric:tabular-nums;font-feature-settings:var(--lg-num)` | -- | none | **Text** numerals -- M4 |
| potato append (l.2523-2530) | `backdrop-filter:none` on all four; bg raised to `.92/.94/.95/.96`; `.lg-senses-ghost` and `.hart-desktop::before` `display:none` | potato | none | **Card** floor -- M6 |
| consumers in markup | `.hart-senses-cluster.lg-1`, `.hart-ws-switcher` (lg-2 per comment) | -- | -- | -- |

## A11. Top bar (z1000)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.top-bar` (+`.glass`) | `position:fixed;top/left/right:0;height:var(--hart-topbar-height);z-index:1000;display:flex;align-items:center;padding:0 12px;gap:8px;border-radius:0;border-bottom:1px solid var(--hart-glass-border);border-top:0` | -- | none | **Chrome** top bar -- M1 |
| `.start-btn` / `.start-logo` | `flex;gap:6px;padding:4px 12px;radius 8px;font-weight:var(--hart-heading-weight);13px;user-select:none;transition:background var(--hart-anim-speed)`; hover `background:var(--hart-surface-hover)`; `.mi{20px;color:var(--hart-accent)}`; logo 20x20, hover `filter:drop-shadow(0 0 8px var(--hart-accent))` | hover, focus-visible (2px accent, offset -2px) | `transition:background var(--hart-anim-speed)` | **Chrome**+**Glyph** -- M1 |
| `.top-bar-center` / `.agent-chip` / `.dot` | center `flex:1;gap:6px;padding:0 12px;12px;color:var(--hart-muted);overflow:hidden`; chip `inline-flex;gap:4px;padding:2px 8px;radius 10px;background:var(--hart-surface);11px`; dot `6x6;50%;background:var(--hart-active)` | `data-agents=1` -> dot `box-shadow:0 0 0 3px rgba(0,230,118,.22)`; `data-agents=0` -> center `opacity:.72`; `data-online=0` -> `opacity:.5;filter:grayscale(.7)` with `transition:opacity/filter var(--t-reveal)` | `transition:opacity/filter var(--t-reveal)` | **Chrome**+**Glyph** status -- M1/M6 |
| `.tray-btn` / `.clock` / `.badge` | tray `32x32;radius 8px;flex center;position:relative;transition:background var(--hart-anim-speed)`, hover surface-hover, `.mi{var(--hart-icon-size);color:var(--hart-muted)}`; clock `12px;500;padding:0 8px` + living-glass `tabular-nums;font-feature-settings:var(--lg-num)`; badge `absolute top/right 2px;8x8;50%;background:var(--hart-error)` | hover; badge visible/hidden (inline `display:none`) | hero spring `transform .18s cubic-bezier(.175,.885,.32,1.275)`, hover `translateY(-1px) scale(1.05)` -- **WINS over** the microanim block's `.16s cubic-bezier(.22,1,.36,1)` + `scale(1.08)` | **Chrome**+**Text**+**Glyph** -- M1 |

## A12. UNSTYLED top-bar markup (native-parity GAP)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-wordmark`, `.top-bar-nav`, `.tb-tab`, `.tb-tab.tb-active`, `.top-bar-omni`, `.tbo-kbd`, `.top-bar-orb`, `.top-bar-avatar` | **8 classes emitted in the markup (l.2864-2880) with ZERO rules in any inline `<style>`.** Their styling lives in the EXTERNAL `/shell/static/hartHome.css` (linked l.2963) -- see B20-B23. Only inline styling present: `.hart-wordmark` children carry `style="color:var(--hart-accent,#00E6C3);font-weight:800"` (HART) and `style="color:var(--hart-a2,#9B5CFF);font-weight:700"` (OS) | `tb-active` (class applied, no inline rule) | none | **Chrome**+**Orb**+**Text** -- M1 (resolve against B20-B23; do NOT port the markup without them) |

## A13. Panel container + floating window (`.panel`)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.panel-container` / `>*` | `position:fixed;top:var(--hart-topbar-height);left/right:0;bottom:44px;z-index:100;pointer-events:none`; children `pointer-events:auto` | -- | none | **Chrome** window host -- M5 |
| `.panel` | `position:absolute;flex column;min 320x240;overflow:hidden`; shadow (non-potato) `inset 0 1px 0 0 rgba(255,255,255,.08),0 1px 1px rgba(0,0,0,.22),0 8px 32px rgba(0,0,0,.38)`, `transition:box-shadow var(--hart-anim-speed)`; potato `0 2px 8px rgba(0,0,0,.3)` + `transition:none` | focused -> `inset 0 1px 0 0 rgba(255,255,255,.10),0 2px 4px rgba(0,0,0,.28),0 16px 56px rgba(0,0,0,.48);z-index:999` (potato `0 3px 12px rgba(0,0,0,.4)`); closing; minimizing; potato; reduced-motion / a11y-rmotion -> `animation:none` | **THREE competing open animations**: `hart-fade-in .18s ease` (l.2779) -> `fadeIn var(--hart-anim-speed) ease-out` (l.1585) -> `hart-panel-in .3s cubic-bezier(.175,.885,.32,1.275)` (l.2224). **LAST WINS -- the native target is `hart-panel-in` .3s spring.** `.closing{opacity:0;scale(.95);transition .2s}`; `.minimizing{opacity:0;scale(.8) translateY(20px);transition .15s}` | **Card** window -- M5 |
| `.panel-titlebar` (+`.mi`,`.title`,`.ctrl`) | `height:var(--hart-titlebar-height);flex;padding:0 8px;gap:6px;cursor:grab;user-select:none;border-bottom:1px solid var(--hart-glass-border)`; `:active{cursor:grabbing}`; `.mi{16px;color:var(--hart-accent)}`; `.title{flex:1;12px;500;ellipsis}`; ctrl span `24x24;radius 6px;14px;color:var(--hart-text);background:rgba(255,255,255,.06);transition:background/color var(--hart-anim-speed)`, hover `.16`, `.close:hover{background:var(--hart-error);color:#fff}` | grab/grabbing; ctrl hover; close hover (red + white glyph) | `transition:background/color var(--hart-anim-speed)` | **Chrome**+**Glyph** titlebar -- M5 |
| `.panel-body` / `iframe` / `.native-content` / `.panel-resize` | body `flex:1;overflow:hidden;position:relative`; iframe `100%;border:none;background:transparent`; native-content `padding:16px;overflow-y:auto;height:100%;13px`; resize grip `absolute right/bottom 0;16x16;cursor:nwse-resize` | internal scroll only | none | **Card** content + **Chrome** grip -- M5 |

## A14. Start menu (z2000)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.start-menu` (+`.glass`) | `position:fixed;bottom:calc(var(--hart-topbar-height));left:8px;width:720px;max-height:calc(100vh - var(--hart-topbar-height) - 24px);z-index:2000;padding:16px;display:none;flex column;overflow:hidden` | `.open{display:flex}`; JS focus-trap surface (l.5761) | `transform:translateY(20px);opacity:0` -> `.open{translateY(0);opacity:1}`, `transition:transform .2s ease-out,opacity .15s ease-out` | **Card** launcher -- M5 |
| `.start-search` / `.start-scroll` | search `100%;padding:8px 12px;radius 10px;border:1px solid var(--hart-glass-border);background:var(--hart-surface);color:var(--hart-text);font-family:var(--ds-font-body);13px;outline:none;margin-bottom:12px`, `:focus{border-color:var(--hart-accent)}`; scroll `flex:1;overflow-y:auto;overflow-x:hidden;scrollbar-width:thin;scrollbar-color:var(--hart-muted) transparent` | focus; internal scroll | none | **Chrome** input + scroller -- M5 |
| `.start-group-label` / `.start-grid` / `.start-item` | label `10px;uppercase;letter-spacing 1.5px;color:var(--hart-muted);600`; grid `repeat(4,1fr);gap:4px`; item `flex column center;padding:10px 4px;radius 10px;gap:4px;text-align:center;user-select:none`, `.mi{24px;color:var(--hart-accent)}`, `.label{11px;line-height 1.2;opacity .85}` | hover; focus-visible (outline 2px accent, offset -2px); reduced-motion -> `transition:none` | transition declared 3x -- `background var(--hart-anim-speed)` (l.2651) -> microanim `.16s cubic-bezier(.22,1,.36,1)` (l.2771) -> **hero `.18s cubic-bezier(.175,.885,.32,1.275)` WINS**; hover `translateY(-2px)` -> **`translateY(-2px) scale(1.02)` WINS** | **Row**/grid + **Glyph** -- M2/M5 |
| `.start-divider` / `.start-footer` / `.power-btn` | divider `border-top:1px solid var(--hart-glass-border);margin:8px 0`; footer `flex center;gap:16px;padding-top:8px;border-top:1px solid var(--hart-glass-border)`; power `flex;gap:4px;padding:6px 12px;radius 8px;12px;transition:background var(--hart-anim-speed)`, hover surface-hover + `scale(1.08)`, `.mi{16px}` | hover; focus-visible | `transition:background var(--hart-anim-speed)` | **Chrome** footer -- M5 |

## A15. Taskbar (z8000)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.taskbar` (+`.glass`) | `position:fixed;bottom/left/right:0;height:44px;z-index:8000;flex;gap:2px;padding:0 8px;align-items:center;border-radius:0;border-top:1px solid var(--hart-glass-border)` | -- | none | **Chrome** taskbar -- M1 |
| `.taskbar-chip` | `height:34px;padding:0 12px;flex;gap:4px;radius 8px;12px;user-select:none;border:1px solid transparent;will-change:transform`; hover `background:var(--hart-surface-hover)` (+hero `translateY(-2px)`); `.active{border-bottom:2px solid var(--hart-accent);background:var(--hart-surface)}`; `.mi{16px;color:var(--hart-accent)}`; `.chip-label{max-width:100px;ellipsis}` | hover; active (open window); focus-visible; potato (`transition:none`); reduced-motion | transition declared 3x (`background .15s` -> microanim `.16s` -> **hero `.18s` spring WINS**) | **Chrome**+**Glyph**+**Text** -- M1 |

## A16. Agent pill (collapsed floating bubble, z1500)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.agent-pill` (+`.glass`) | `position:fixed;bottom:56px;right:16px;z-index:1500;flex;gap:8px;padding:8px 14px;max-width:360px;transition:all var(--hart-anim-speed)`; hover `translateY(-2px);box-shadow:0 8px 24px rgba(0,0,0,.3)`; `.expanded{max-width:400px;padding:12px}`; `.hidden{display:none}` (default markup ships `.hidden`); `.mi{20px;color:var(--hart-accent)}` | hover; expanded; hidden; focus-visible | `transition:all var(--hart-anim-speed)` | **Card** floating pill -- M5 |
| `input` / `.agent-response` | input `flex:1;transparent;border:none;color:var(--hart-text);font-family:var(--ds-font-body);13px;min-width:0`, placeholder `var(--hart-muted)`; response `12px;color:var(--hart-muted);padding-top:6px;border-top:1px solid var(--hart-glass-border);display:none`, `.visible{display:block}` | response visible | none | **Text** -- M4 |

## A17. Floating assistant chat panel (z1600)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.assistant-chat` (+`.glass`) | `position:fixed;bottom:56px;right:16px;z-index:1600;380x520;display:none;flex column;border-radius:var(--hart-radius);overflow:hidden;resize:both;min 320x400;max 600px/80vh`; `.open{display:flex}` | open; user-resizable | none | **Card** chat window -- M5 |
| `.ac-header` / `.ac-title` / `.ac-btn` | header `flex;gap:8px;padding:10px 14px;cursor:grab;user-select:none;border-bottom:1px solid var(--hart-glass-border)`, `:active{grabbing}`; title `flex:1;13px;500`; btn `none;color:var(--hart-muted);18px`, hover `color:var(--hart-text)` | grab/grabbing (drag); btn hover | none | **Chrome** header -- M5 |
| `.ac-caps` / `.ac-cap` | caps `flex;gap:6px;padding:8px 14px;overflow-x:auto;border-bottom:1px solid var(--hart-glass-border)`; cap `flex;gap:4px;padding:4px 10px;radius 12px;11px;nowrap;background:var(--hart-glass-bg);border:1px solid var(--hart-glass-border);transition:background 120ms`, hover `.1`, `.active{background/border:var(--hart-accent);color:#fff}`, `.mi{14px}` | hover; active | `transition:background 120ms` | **Row** capability rail -- M2 |
| `.ac-messages` / `.ac-msg` | list `flex:1;overflow-y:auto;padding:12px 14px;flex column;gap:8px`; msg `max-width:85%;padding:8px 12px;radius 12px;13px;line-height 1.4;word-break:break-word`; `.user{align-self:flex-end;background:var(--hart-accent);color:#fff;border-bottom-right-radius:4px}`; `.assistant{align-self:flex-start;background:var(--hart-glass-bg);border:1px solid var(--hart-glass-border);border-bottom-left-radius:4px}`; `.typing{opacity:.6;italic}` | user / assistant / typing | none | **Card**+**Text** bubbles -- M4/M5 |
| `.ac-input-row` / `.ac-input` / `.ac-send` | row `flex;gap:6px;padding:8px 10px;border-top:1px solid var(--hart-glass-border)`; input `flex:1;transparent;border:1px solid var(--hart-glass-border);radius 20px;padding:8px 14px;color:var(--hart-text);font-family:var(--ds-font-body);13px;resize:none`, `:focus{border-color:var(--hart-accent)}`; send `32x32;50%;background:var(--hart-accent);flex center;transition:opacity 120ms`, hover `.85`, `.mi{16px;color:#fff}` | input focus; send hover | `transition:opacity 120ms` | **Chrome** composer -- M5 |

## A18. Context menu (z3000)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.ctx-menu` (+`.glass`) | `position:fixed;z-index:3000;min-width:180px;padding:4px;box-shadow:0 8px 24px rgba(0,0,0,.5);12px` (markup ships `style="display:none"`) | shown/hidden | none | **Card** menu -- M5 |
| `.ctx-menu-item` / `.ctx-menu-sep` | item `flex;gap:8px;padding:6px 10px;radius 6px;transition:background 100ms`, hover `var(--hart-surface-hover)`, `.mi{16px;color:var(--hart-muted)}`; sep `border-top:1px solid var(--hart-glass-border);margin:4px 0` | hover; focus-visible; reduced-motion -> `transition:none` | `transition:background 100ms` + microanim `transform .16s cubic-bezier(.22,1,.36,1)` with hover `translateX(2px)` -- **a LATERAL nudge, unique among the hover lifts** | **Row**/list item -- M5 |

## A19. Lock screen (z9999)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.lock-screen` | `position:fixed;inset:0;z-index:9999;display:none;flex column center;gap:16px`. **TWO-LAYER opaque base (deliberate)**: `background:var(--hart-background,#0F0E17)` then `linear-gradient(rgba(7,6,15,.82),rgba(7,6,15,.82)),var(--hart-background)` + `backdrop-filter:blur(24px)`. POTATO uses `rgba(7,6,15,.97)` twice and NO backdrop-filter. **Opaque-first is mandatory** -- backdrop-filter is unreliable on kiosk WebKitGTK and the hero bar bled through | `.active{display:flex}` -- server SEEDS ` active` at first paint (`lock_boot_class`, l.2546-2555) when `shell_session.json` has `lock_pw_hash`, preventing FOUC/desktop leak; `.setup` hides clock+date; potato; JS focus-trap surface | none | **Field**+**Card** lock -- M5/M6 |
| `.lock-clock` / `.lock-date` / `.lock-input` / `.lock-status` / `.lock-brand` | clock `64px;300`; date `16px;color:var(--hart-muted)`; input `padding:10px 16px;radius 12px;border:1px solid var(--hart-glass-border);background:var(--hart-glass-bg);color:var(--hart-text);14px;font-family:var(--ds-font-body);width:280px;text-align:center`; status `12px;var(--hart-muted)`; brand `flex;gap:10px;margin-bottom:8px;opacity:.92`, img `30x30;filter:drop-shadow(0 2px 10px rgba(0,212,170,.4))`, span `13px;letter-spacing 2.5px;600;opacity:.8` | setup variant | none | **Text**+**Glyph** -- M4 |

## A20. Desktop widgets (clock + system, z30)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-widgets` | `position:fixed;top:calc(var(--hart-topbar-height,40px) + 18px);right:16px;z-index:30;flex column;gap:12px;width:222px` | -- | none | **Chrome** widget column -- M2 |
| `.hart-widget` | `background:var(--hart-glass-bg);border:1px solid var(--hart-glass-border);radius 16px;padding:14px 16px;box-shadow:0 8px 30px rgba(0,0,0,.28)`; `backdrop-filter:blur(var(--hart-blur)) saturate(var(--hart-saturation))` ONLY when not potato | hover -> `translateY(-2px);box-shadow:0 14px 40px rgba(0,0,0,.34)`; potato; reduced-motion | `transition:transform .2s cubic-bezier(.22,1,.36,1),box-shadow .2s ease` | **Card** widget -- M2 |
| `.hw-clock-time` / `.hw-clock-date` / `.hw-title` / `.hw-row` / `.hw-val` / `.hw-bar>i` | time `30px;300;letter-spacing .5px;color:var(--hart-text);tabular-nums`; date `12px;var(--hart-muted)`; title `11px;uppercase;letter-spacing 1.5px;var(--hart-muted)`; row `flex between;12px;var(--hart-text);margin-top 7px`; val `var(--hart-accent);tabular-nums`; bar `height 5px;radius 3px;background:var(--hart-surface);overflow:hidden`, fill `100% height;background:var(--hart-accent);radius 3px;transition:width .5s` | meter fill | `transition:width .5s` | **Text**+**Glyph** meters -- M4 |

## A21. Hero shell / spine (`.hart-hero`, z40)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-hero` / `>*` | `position:fixed;left:50%;top:46%;transform:translate(-50%,-50%);z-index:40;flex column center;gap:16px;text-align:center;width:min(660px,86vw);pointer-events:none`, children re-enable pointer-events | -- | `transition:opacity .55s cubic-bezier(.2,0,0,1),transform .55s cubic-bezier(.2,0,0,1),filter .55s` | **Chrome** hero spine -- M3 |
| `.hart-hero.hart-hero-dragging` | `transition:none` -- **REQUIRED for 1:1 pointer tracking**; without it the .55s transform transition lags the cursor (real-HW 2026-07-20 bug) | dragging | transition suppressed | **Chrome** drag channel -- M3 |
| `.hart-hero.dimmed` | `opacity:0;transform:translate(-50%,-56%) scale(.96);filter:blur(6px)`; `>*{pointer-events:none}` | dimmed | `.55s` group | **Field**/**Chrome** -- M6 |
| `.hart-hero-brand` | `flex;gap:9px;opacity:.92`; img `34x34;filter:drop-shadow(0 3px 12px rgba(0,212,170,.4))`; span `14px;600;letter-spacing 2.5px;opacity:.8` | -- | none | **Glyph**+**Text** -- M4 |
| `.hart-hero.ai-blind` | `#hart-voice-orb{opacity:.12;filter:grayscale(1) brightness(.4)}`; `.hart-hero-orbwrap{cursor:default;box-shadow:none}` -- senses cut by human | ai-blind | `transition:opacity .5s,filter .5s` | **Orb** blind state -- M3/M6 |

## A22. Hero ORB -- orbwrap, canvas, orbital rings, presence rings

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-hero-orbwrap` | `position:relative;300x300;flex center;cursor:pointer;border-radius:50%`; hover `scale(1.03)`; active `scale(.985)`; focus-visible `outline:none;box-shadow:0 0 0 3px var(--hart-accent)` | hover / active / focus-visible | transition declared TWICE -- hero `.25s cubic-bezier(.175,.885,.32,1.275)+box-shadow .25s`, then **living-glass `transform .25s var(--lg-spring),box-shadow var(--t-move) var(--lg-breathe)` WINS** | **Orb** hit target -- M3 |
| `#hart-voice-orb` | `300x300` (canvas attr 360x360); `background:transparent;pointer-events:none`; `filter:drop-shadow(0 0 46px rgba(0,230,195,.34)) drop-shadow(0 8px 64px rgba(155,92,255,.26))` -- **teal INNER glow + violet OUTER halo** (replaces deprecated indigo `#6C63FF`) | -- | see B18 (float+breathe) | **Orb** core -- M3 |
| `.hart-orb-orbit` / `.hart-orb-orbit2` | both `position:absolute;50%;pointer-events:none;left/top:50%;will-change:transform`; orbit1 `300x300;margin:-150px 0 0 -150px;border:1.5px dashed rgba(var(--hart-amb-2-rgb,0,221,249),.42)`; orbit2 `236x236;margin:-118px 0 0 -118px;border:1px dashed rgba(var(--hart-amb-1-rgb,177,130,255),.28)` -- **counter-rotating for depth** | reduced-motion -> `animation:none` | `hart-orbit-spin 26s linear infinite` / `38s linear infinite reverse` | **Ring** orbital -- M3 |
| `.hart-hero-orbwrap::after` (presence ring) | `content:'';absolute;inset:-6px;50%;pointer-events:none;opacity:0`; `[data-orb-state=listening]::after{opacity:1;box-shadow:var(--lg-ring-listen)}` + breathe; `speaking` -> `var(--lg-ring-speak)` (static); `thinking` -> `var(--lg-ring-think)` | `data-orb-state=idle` (seeded l.2842, ring hidden) / listening / speaking / thinking | `transition:opacity var(--t-reveal) var(--lg-enter),box-shadow var(--t-move)`; `lg-breathe-ring 2.2s var(--lg-breathe) infinite` (listening only) | **Ring** presence -- M3 |
| `[data-orb-state="thinking"]::before` (comet) | `inset:-6px;50%;background:conic-gradient(from 0deg,transparent 0 78%,rgba(var(--lg-think-rgb),.9) 90%,transparent 100%)`; `-webkit-mask/mask:radial-gradient(closest-side,transparent calc(100% - 5px),#000 calc(100% - 4px))` -- **the only conic-gradient and the only mask in the sheet** | thinking; a11y-rmotion -> `animation:none` | `lg-comet 1.4s linear infinite` | **Ring** masked sweep -- M3 |
| `.lg-orb-ripple` | `absolute;50%;pointer-events:none;background:radial-gradient(circle,rgba(var(--lg-accent-rgb),.35),transparent 70%)` | press | `lg-ripple .45s var(--lg-exit) forwards` | **Ring** press feedback -- M3 |
| LEGACY `.hart-hero-orbwrap.listening` | `box-shadow:0 0 0 5px rgba(255,107,107,.22),0 10px 44px rgba(255,107,107,.35)` + red orb drop-shadow -- **SUPERSEDED**: living-glass sets `box-shadow:none`. **MUST NOT SHIP RED** | listening (neutralised) | -- | **Ring** -- do NOT port |

## A23. Hero status line / command bar / go button

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-hero-status` | `13px;500;letter-spacing .3px;color:var(--hart-muted);min-height:18px;max-width:560px;ellipsis;nowrap`; `.thinking{color:var(--hart-accent)}` | thinking | `transition:color .3s` | **Text** status -- M4 |
| `.hart-hero-bar` (+`.glass`) | `flex;gap:10px;width:100%;padding:7px 8px 7px 18px;border-radius:var(--ds-radius-full)`; `:focus-within{box-shadow:0 0 0 2px var(--hart-accent),0 18px 50px rgba(0,0,0,.42)}` -- ring + deep drop | focus-within | `transition:box-shadow .25s,border-color .25s` | **Chrome** omnibox -- M1 |
| `.hart-hero-bar-ic` / `.hart-hero-input` | ic `21px;color:var(--hart-muted)`; input `flex:1;min-width:0;transparent;border/outline:none;color:var(--hart-text);font-family:var(--ds-font-body);15.5px;letter-spacing .2px`, placeholder `var(--hart-muted)` | -- | none | **Glyph**+**Text** -- M4 |
| `.hart-hero-go` | `42x42;50%;border:none;background:var(--hart-accent);color:var(--hart-on-accent);flex center`; hover `filter:brightness(1.12);scale(1.08)`; active `scale(.94)`; `.mi{21px}` | hover / active | `transition:transform .18s cubic-bezier(.175,.885,.32,1.275),filter .18s` | **Glyph** action -- M3 |

## A24. Hevolve live pip (speaking cue)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-hero-hevolve` | `flex;gap:7px;height:16px;opacity:0;11px;600;letter-spacing 1.2px;uppercase;color:var(--hart-muted)`; `.on{opacity:.9}` | on; `data-speaking=1` -> `opacity:.9` | `transition:opacity .3s` | **Text** cue -- M4 |
| `.hart-hero-hevolve .dot` | `7x7;50%;background:var(--hart-accent);box-shadow:0 0 10px var(--hart-accent)`; **speaking re-tint**: `background:rgb(var(--lg-speak-rgb));box-shadow:0 0 0 3px rgba(var(--lg-speak-rgb),.22)` -- the deterministic TTS-out cue | speaking; reduced-motion -> `animation:none` | `hart-hevolve-pulse 1s ease-in-out infinite` | **Glyph** pip -- M3/M6 |

## A25. Hero suggestion chips

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-hero-chips` | `flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-top:2px`; contextual hide on any of busy/panels/typing -> `opacity:0;transform:translateY(6px) scale(.98);pointer-events:none` | `data-busy\|panels\|typing=1` | `transition:opacity/transform var(--t-move) var(--lg-enter)` | **Row** chip rail -- M2/M6 |
| `.hart-hero-chip` | `padding:7px 15px;border-radius:var(--ds-radius-full);12px;500;background:var(--hart-glass-bg);border:1px solid var(--hart-glass-border);color:var(--hart-text);font-family:var(--ds-font-body)` | hover -> `translateY(-2px)` + **living-glass `box-shadow:var(--lg-sh-2)` WINS** over hero's `0 6px 18px rgba(0,0,0,.28)`; active -> **living-glass `scale(.97)` WINS** over hero's `translateY(0)`; reduced-motion | transition declared 3x -- **living-glass `background .18s,transform .18s var(--lg-spring),box-shadow .18s` WINS** | **Card**/chip -- M2 |

## A26. AI senses cluster / sensory pod (draggable, z8100)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-senses` | `position:fixed;left:14px;top:auto;bottom:54px;z-index:8100;touch-action:none`; JS writes inline left/top from localStorage. Visibility: `data-idle=1` -> `opacity:.55` (**NEVER hides -- safety control**); `data-voice\|blind=1` -> `opacity:1` | idle / voice / blind | `transition:opacity var(--t-reveal) var(--lg-glide)` | **Chrome** pod -- M1/M6 |
| `.hart-senses-cluster` (+`.lg-1`) | `flex;gap:8px -> 6px (living-glass);padding:6px;radius 999px;background:var(--hart-glass-bg);border:1px solid var(--hart-glass-border);backdrop-filter:blur(12px);box-shadow:inset 0 1px 0 0 rgba(255,255,255,.08),0 6px 22px rgba(0,0,0,.4)` | dragging -> hero `scale(1.03)+0 12px 36px` **SUPERSEDED by living-glass `scale(1.04);box-shadow:var(--lg-spec),var(--lg-sh-3)`**; settle | `transition:box-shadow .2s,transform .2s cubic-bezier(.175,.885,.32,1.275)`; `lg-settle .34s var(--lg-spring)` on `.settle` | **Card** pod plate -- M1 |
| `.hart-senses-grip` | `22x40;cursor:grab;color:var(--hart-muted);radius 8px;touch-action:none;opacity:0` -- **hidden at rest AND on hover; only `.dragging` reveals it** (`cursor:grabbing;opacity:1;color:var(--hart-text);background:rgba(255,255,255,.06)`); `.mi{20px;opacity:.85}` | dragging | `transition:opacity .18s` | **Glyph** grip -- M1 |
| `.hart-senses-btn` | `46x46;50%;border:1px solid var(--hart-glass-border);background:var(--hart-glass-bg);backdrop-filter:blur(10px);flex center;box-shadow:0 4px 16px rgba(0,0,0,.35)`; hover `scale(1.08)`; `.mi{24px;color:var(--hart-accent)}` | hover | `transition:transform .18s cubic-bezier(.175,.885,.32,1.275),background .2s,box-shadow .2s` | **Glyph** control -- M1 |
| EYE 3-state | `.off` legacy red `rgba(255,107,107,.18)`+`--hart-error` **SUPERSEDED by** `background:rgba(var(--lg-blind-rgb),.20);border-color:rgb(var(--lg-blind-rgb))` + grey glyph; `.is-sensing{background:rgba(var(--lg-vision-rgb),.16);border-color:rgb(var(--lg-vision-rgb));box-shadow:var(--lg-ring-vision)}`, glyph `color:rgb(var(--lg-vision-rgb))` | off (blind, grey) / is-sensing (vision blue); a11y-rmotion -> pulse off | `lg-pulse 2.4s var(--lg-breathe) infinite` on `.is-sensing .mi` | **Glyph**+**Ring** -- M3/M6 |
| `.hart-senses-mic.listening` | legacy red **SUPERSEDED by cyan** `background:rgba(var(--lg-listen-rgb),.18);border-color:rgb(var(--lg-listen-rgb));box-shadow:var(--lg-ring-listen)`, glyph `rgb(var(--lg-listen-rgb))` | listening | -- | **Glyph**+**Ring** -- M3 |
| `.lg-senses-ghost` | `position:fixed;inset:0;z-index:8090;pointer-events:none;opacity:0;background-image:radial-gradient(rgba(var(--lg-accent-rgb),.16) 1px,transparent 1px);background-size:24px 24px`; `.show{opacity:1}`; `display:none` under potato | show; potato | `transition:opacity var(--t-reveal)` | **Field** snap grid -- M1/M6 |
| `[data-edge~=b/t/r/l] .hart-senses-panel` | edge-aware popover anchoring: b -> `bottom:56px;top:auto`; t -> `top:56px;bottom:auto`; r -> `right:0;left:auto`; l -> `left:0;right:auto` (markup seeds `data-edge="b"`) | edge b/t/r/l | none | **Chrome** anchoring -- M6 |

## A27. Senses proof popover

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-senses-panel` | `absolute;left:0;bottom:56px;display:none;flex column;gap:6px;min-width:248px;max-width:300px;padding:12px 14px;radius 12px;background:var(--hart-glass-bg);border:1px solid var(--hart-glass-border);backdrop-filter:blur(14px);box-shadow:0 8px 28px rgba(0,0,0,.4)`; `.open{display:flex}` | open; edge-repositioned (A26) | none | **Card** popover -- M5 |
| `.hsp-title` / `.hsp-row` / `.hsp-name` / `.hsp-state` / `.hsp-detail` / `.hsp-foot` | title `12px;600;letter-spacing .5px;uppercase;var(--hart-muted)`; row `flex;gap:8px;12px`, `.mi{16px;var(--hart-muted)}`, name `flex:1`; state `10px;700;letter-spacing .6px;uppercase;padding:2px 7px;radius 8px`, `.on{color:var(--hart-active);background:rgba(0,230,118,.12)}`, `.off{color:var(--hart-error);background:rgba(255,107,107,.14)}`; detail/foot `10px;var(--hart-muted)` | state on/off | none | **Text**+**Glyph** proof rows -- M4 |

## A28. Desktop icon layer + icons (z20)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-desktop` | `position:fixed;left/right:0;top:var(--hart-topbar-height);bottom:44px;z-index:20;pointer-events:none` -- **transparent to pointer so empty-desktop right-click reaches the wallpaper menu** | -- | none | **Field** icon host -- M1 |
| `.hart-desktop::before` (snap grid) | `content:'';absolute;inset:0;opacity:0;pointer-events:none;background-image:radial-gradient(rgba(var(--lg-accent-rgb),.16) 1.5px,transparent 1.5px);background-size:var(--lg-grid) var(--lg-grid);background-position:var(--lg-pad) var(--lg-pad)`; `.arranging::before{opacity:1}`; `display:none` under potato | arranging; potato | `transition:opacity var(--t-fast)` | **Field** grid ghost -- M2/M6 |
| `.desktop-icon` | `absolute;width:84px;flex column;gap:6px;padding:8px 4px;radius 12px;cursor:default;pointer-events:auto;user-select:none;will-change:transform`; hover `background:rgba(255,255,255,.08)` | selected: legacy purple `rgba(108,99,255,.28)`+outline **SUPERSEDED by** `background:rgba(var(--lg-accent-rgb),.22);outline:1px solid rgba(var(--lg-accent-rgb),.55);box-shadow:0 8px 30px rgba(var(--lg-accent-rgb),.30)`; dragging: hero `z-index:60;transition:none;opacity:.92;cursor:grabbing` then living-glass `scale(1.06);box-shadow:var(--lg-sh-3);z-index:60`; focus-visible | `transition:background .15s,transform .12s cubic-bezier(.175,.885,.32,1.275)` | **Card** icon tile -- M2 |
| `.di-glyph` / `.mi` / `.di-emoji` / `.di-label` | glyph `52x52;radius 14px;flex center;background:var(--hart-glass-bg);border:1px solid var(--hart-glass-border);box-shadow:inset 0 1px 0 0 rgba(255,255,255,.08),0 4px 12px rgba(0,0,0,.28)`, hover `inset .12 + 0 8px 20px rgba(0,0,0,.36);translateY(-1px)`; `.mi{28px;color:var(--hart-accent)}` (JS `miStyle()` overrides per-app inline); emoji `"Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",system-ui;30px`; label `11px;line-height 1.25;center;max-width:80px;color:var(--hart-text);text-shadow:0 1px 3px rgba(0,0,0,.6);-webkit-line-clamp:2` | hover | -- | **Glyph**+**Text** -- M2/M4 |
| `.lg-drop-cell` / `.lg-marquee` | drop cell `absolute;84x84;radius 14px;pointer-events:none;border:2px dashed rgba(var(--lg-accent-rgb),.6);background:rgba(var(--lg-accent-rgb),.08)`; marquee `position:fixed;z-index:55;pointer-events:none;border:1px solid rgba(var(--lg-accent-rgb),.7);background:rgba(var(--lg-accent-rgb),.10);radius 6px` | drop-cell present; marquee active | `transition:left/top var(--t-fast) var(--lg-glide)` | **Field** drag affordances -- M2 |

## A29. Per-icon Customize dialog (z9000)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-icustom-backdrop` | `position:fixed;inset:0;z-index:9000;flex center;background:rgba(0,0,0,.42);backdrop-filter:blur(2px)` -- **NOT potato-gated (parity GAP: every other blur is)** | open | none | **Field** scrim -- M5/M6 |
| `.hart-icustom` | `340px;max-width:92vw;padding:18px;radius 16px;flex column;gap:12px;color:var(--hart-text);font-family:var(--ds-font-body,system-ui);box-shadow:0 18px 50px rgba(0,0,0,.5)`; head `flex;gap:14px` with a 52x52 r14 preview glyph mirroring `.di-glyph`; title `15px;600`; row `flex column;gap:5px;12px;var(--hart-muted)` | -- | none | **Card** dialog -- M5 |
| inputs / buttons | text input `padding:8px 10px;radius 9px;border:1px solid var(--hart-glass-border);background:var(--hart-surface);color:var(--hart-text);14px`, `:focus{outline:none;border-color:var(--hart-accent)}`; color input `40x30;border:1px solid var(--hart-glass-border);radius 8px;background:none`; btn `padding:7px 14px;radius 9px;border:1px solid var(--hart-glass-border);background:var(--hart-surface);13px`, hover `var(--hart-surface-hover);translateY(-1px)`, `.primary{background:var(--hart-accent);color:var(--hart-on-accent);border-color:transparent}`, `.ghost{background:transparent}` | hover; focus; focus-visible; primary/ghost | `transition:background .15s,transform .12s` | **Chrome** controls -- M5 |

## A30. Virtual-desktop switcher + segmented pager (z8050)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-ws-switcher` (+`.glass`) | `position:fixed;bottom:6px;left:50%;transform:translateX(-50%);z-index:8050;flex;gap:4px;padding:4px 6px;radius 999px`; living-glass overrides `padding:3px;gap:2px;height:30px`. Contextual hide `html[data-multiws="0"]` -> `opacity:0;transform:translate(-50%,8px);pointer-events:none` -- **`data-multiws="0"` is SEEDED on `<html>` at first paint (l.2558) so there is no reveal FOUC** | data-multiws 0/1 | `transition:opacity/transform var(--t-move) var(--lg-enter)` | **Chrome** pager -- M1/M6 |
| `.hart-pager-thumb` | `absolute;top:3px;left:3px;height:24px;border-radius:var(--ds-radius-full);background:rgba(var(--lg-accent-rgb),.92);box-shadow:0 2px 10px rgba(var(--lg-accent-rgb),.4);z-index:0` | reduced-motion -> `transition:none` | `transition:transform/width var(--t-move) var(--lg-spring)` (thumb slide) | **Chrome** selection thumb -- M1 |
| `.hart-pager-seg` (+`.hps-n`,`.hps-occ`,`i`) | `relative;z-index:1;min-width:34px;height:24px;flex column center;gap:2px;border:none;transparent;color:var(--lg-muted);border-radius:var(--ds-radius-full)`; `.hps-n{11px;700}`; `.hps-occ{flex;gap:2px;height:3px}`, `i{3x3;50%;background:currentColor;opacity:.7}`; `.empty .hps-occ{opacity:.35}`; hover `color:var(--lg-text)`; `.active{color:var(--hart-on-accent)}` | hover; active; empty | `transition:color var(--t-fast)` | **Glyph**+**Text** occupancy -- M4 |
| LEGACY `.hart-ws-dot` / `.hart-ws-square` | dot `26x20;radius 6px;11px;600;color:var(--hart-muted)`, hover surface-hover+`translateY(-1px)`, `.active{background:var(--hart-accent);color:var(--hart-on-accent)}`; square `aspect-ratio 16/9;radius 8px;background:#1a1a1a;border:2px solid transparent`, `.active{border-color/color:var(--hart-accent)}` -- **SUPERSEDED by the pager; the rules survive but `hartWorkspaces.js` builds the pager DOM** | hover; active | `transition:transform .15s cubic-bezier(.175,.885,.32,1.275)` (legacy) | **Chrome** -- do NOT port as primary |

## A31. Personalize -- theme/wallpaper gallery, orb cards, custom palette, media URL

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-gallery` / `.hart-tile` | gallery `grid;repeat(auto-fill,minmax(116px,1fr));gap:10px;padding:4px 0 8px`; tile `radius 10px`, hover `translateY(-3px)`, focus-visible `outline:2px solid var(--hart-accent);outline-offset:2px` | hover; focus-visible | `transition:transform .15s cubic-bezier(.175,.885,.32,1.275)` | **Row**/grid + **Card** -- M2 |
| `.htc-prev` / `.htc-dot` / `.htc-name` | prev `relative;aspect-ratio 16/10;radius 10px;overflow:hidden;border:1px solid var(--hart-glass-border);box-shadow:0 4px 12px rgba(0,0,0,.3)`, tile-hover -> `border-color:var(--hart-accent)`; dot `absolute left/bottom 8px;16x16;50%;box-shadow:0 0 8px currentColor,inset 0 0 0 2px rgba(255,255,255,.25)` -- **dual glow+inset ring driven by `currentColor`**; name `11px;var(--hart-text);center;margin-top 5px;ellipsis` | hover | -- | **Card** preview + **Glyph** dot -- M2 |
| `.hart-orb-card.active .htc-prev` | selection ring `border-color:var(--hart-accent);box-shadow:0 0 0 2px var(--hart-accent),0 4px 12px rgba(0,0,0,.3)` | active | -- | **Card** selection -- M2 |
| `.hart-custom-palette` / `.hart-cp-field` / `input[type=color]` / `.hart-media-url` | palette `flex;wrap;align-items:flex-end;gap:12px;padding:2px 0 12px`; field `flex column;gap:4px;11px;var(--hart-muted)`; color input `52x34;border:1px solid var(--hart-glass-border);radius 8px;background:transparent`; media-url `flex;wrap;gap:8px`, `.ds-input{flex:1;min-width:160px}`, `.ds-select{width:96px;flex:0 0 auto}` | -- | none | **Chrome** controls -- M5 |

## A32. Marketplace / App Store cards

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-mkt` / `.hart-mkt-head` / `.hart-mkt-search` | `padding:var(--ds-space-2) var(--ds-space-1) var(--ds-space-6)`; head `margin-bottom:var(--ds-space-5)`; search `flex;gap:var(--ds-space-2);position:sticky;top:0;z-index:3;padding-bottom:var(--ds-space-2);background:linear-gradient(to bottom,var(--hart-surface) 70%,transparent)` -- **sticky fade-out header** | sticky | none | **Row** header -- M2 |
| `.hart-app-grid` | `grid;repeat(auto-fill,minmax(248px,1fr));gap:var(--ds-space-3);margin-top:var(--ds-space-3)` | -- | none | **Row**/grid -- M2 |
| `.hart-app-card` (+`::before`) | `relative;flex column;gap:var(--ds-space-3);padding:var(--ds-space-4);border-radius:var(--ds-radius-lg);overflow:hidden;background:var(--hart-glass-bg);border:1px solid var(--hart-glass-border);backdrop-filter:blur(12px) saturate(1.2);box-shadow:inset 0 1px 0 0 rgba(255,255,255,.06),0 2px 10px rgba(0,0,0,.22)`; `::before` top-light wash `radial-gradient(120% 90% at 0% 0%,rgba(255,255,255,.06),transparent 60%);opacity:.9` | hover -> `translateY(-3px);border-color:var(--hart-accent);box-shadow:inset .10 + 0 12px 30px rgba(0,0,0,.34)` (see C16 for the CINEMATIC override) | `transition:transform .18s cubic-bezier(.175,.885,.32,1.275),box-shadow .18s,border-color .18s` | **Card** app card -- M2 |
| `.hac-ic` / `.hac-name` / `.hac-desc` / `.hac-cat` / `.ds-btn.is-installed` | ic `52x52;border-radius:var(--ds-radius-md);flex center;background:linear-gradient(150deg,rgba(255,255,255,.10),rgba(255,255,255,.03));border:1px solid var(--hart-glass-border);box-shadow:inset 0 1px 0 rgba(255,255,255,.10)`, `.mi{28px;color:var(--hart-accent)}`; name `14px/18px;600;color:var(--hart-heading);ellipsis`; desc `12px/16px;var(--hart-muted);line-clamp:2`; cat `10px;600;letter-spacing .6px;uppercase;var(--hart-accent);opacity .8`; installed `background:rgba(0,230,118,.14);color:var(--hart-active);border:1px solid rgba(0,230,118,.35);cursor:default`, `:hover{transform:none;filter:none}` -- **calm done-state, not a CTA** | is-installed | -- | **Glyph**+**Text** -- M4 |

## A33. First-run "Light Your HART" onboarding ceremony (z12000)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-onboarding` | `position:fixed;inset:0;z-index:12000;display:none;flex column center;gap:26px;text-align:center;padding:48px;background:radial-gradient(circle at 50% 38%,#16142e,#07060f 72%)`; `.open{display:flex}` | open | none | **Field** ceremony backdrop -- M5 |
| `.hob-orb` | `150x150;50%;background:radial-gradient(circle at 50% 40%,rgba(0,230,195,.95),rgba(0,230,195,.34) 45%,transparent 70%);box-shadow:0 0 70px rgba(0,230,195,.5),0 0 150px rgba(155,92,255,.24)` -- **teal core + violet outer halo (duotone rule; NOT indigo `#6C63FF`)** | -- | `hob-breathe 3.2s ease-in-out infinite` | **Orb** ceremony orb -- M3/M5 |
| `.hob-name` / `.hob-narr` / `.hob-line` | name `34px;600;letter-spacing 1px;#fff;opacity:0;translateY(8px)`, `.show{opacity:1;transform:none;text-shadow:0 0 30px rgba(0,230,195,.55)}`; narr `max-width:640px;min-height:84px;flex column;gap:10px`; line `20px;line-height 1.5;#e9f7f3;font-family:var(--ds-font-body);opacity:0;translateY(6px)`, `.in{opacity:1;transform:none}` | name show; line in | `transition:opacity .6s,transform .6s` | **Text** reveal -- M4/M5 |
| `.hob-opts` / `.hob-opt` / `.hob-skip` | opts `flex;wrap;gap:10px;center;max-width:700px`; opt `padding:12px 22px;radius 999px;border:1px solid rgba(0,230,195,.4);background:rgba(0,230,195,.12);#fff;15px;font-family:var(--ds-font-body)`, hover `background:rgba(0,230,195,.22);translateY(-2px);box-shadow:0 8px 24px rgba(155,92,255,.35)` -- **teal fill, VIOLET hover glow**; skip `position:fixed;bottom:20px;12px;rgba(255,255,255,.4)` | opt hover | `transition:background .18s,transform .18s cubic-bezier(.175,.885,.32,1.275),box-shadow .18s` | **Card**/chip -- M5 |
| **PARITY NOTE** | all ceremony colours are HARD-CODED hex/rgba (teal `0,230,195` + violet `155,92,255`), NOT theme vars -- **they do not retint with the accent** | -- | -- | GAP: decide retint vs freeze before M5 |

## A34. Toasts -- legacy `.toast` + DS toast

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.toast-container` | `position:fixed;top:calc(var(--hart-topbar-height) + 12px);right:16px;flex column;gap:8px;z-index:9500;pointer-events:none` | -- | none | **Chrome** toast host -- M5 |
| `.toast` | `padding:12px 16px;radius 12px;pointer-events:auto;max-width:340px;12px`; hover `opacity:1!important` | potato -> **animation string EMPTY** | `slideInRight .3s ease-out, fadeOutToast .3s ease-in 4.7s forwards` | **Card** legacy toast -- M5 |
| `.ds-toast` (+icon/content/title/message) | `flex;align-items:flex-start;gap:var(--ds-space-3);padding:var(--ds-space-4);border-radius:var(--ds-radius-md);background:var(--hart-glass-bg);border:1px solid var(--hart-glass-border);box-shadow:var(--ds-elevation-3);max-width:380px;overflow:hidden;backdrop-filter:blur(16px) saturate(150%)`; icon `20px`; title `14px/20px;500`; message `12px/16px;var(--hart-muted)` | potato -> `backdrop-filter:none;background:var(--hart-surface)` | `ds-toast-in var(--ds-duration-long) var(--ds-ease-spring)`; `.ds-toast-exit{ds-toast-out var(--ds-duration-medium) var(--ds-ease-accelerate) forwards}` | **Card** toast -- M5 |
| `.ds-toast-progress` | `absolute;bottom/left:0;height:2px;background:var(--hart-accent)` | potato -> `animation:none` | `ds-toast-countdown 5s linear forwards` | **Glyph** countdown -- M5 |

## A35. DS Buttons + ripple

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.ds-btn` | `inline-flex center;gap:var(--ds-space-2);padding:10px var(--ds-space-6);border-radius:var(--ds-radius-full);font-family:var(--ds-font-body);14px;500;letter-spacing .1px;line-height 20px;border/outline:none;position:relative;overflow:hidden;user-select:none;-webkit-tap-highlight-color:transparent`; `.mi{18px}` | focus-visible `outline:2px solid var(--hart-accent);outline-offset:2px`; disabled `opacity:.38;pointer-events:none` | `transition:box-shadow var(--ds-duration-medium) var(--ds-ease-standard), background var(--ds-duration-short) var(--ds-ease-standard), filter var(--ds-duration-short) var(--ds-ease-standard)` | **Chrome** button -- M1 |
| variants | primary `background:var(--hart-accent);color:var(--hart-on-accent)`, hover `box-shadow:var(--ds-elevation-1);filter:brightness(1.1)`, active `brightness(.9)`; secondary `transparent;color:var(--hart-accent);border:1px solid var(--hart-glass-border)`, hover `var(--ds-state-hover)`, active `var(--ds-state-pressed)`; text `transparent;var(--hart-accent);padding:10px var(--ds-space-3)`; tonal `var(--ds-surface-3)`, hover `elevation-1 + var(--ds-surface-4)`; danger `var(--hart-error);#fff`; icon `padding:var(--ds-space-2);radius full;min 40x40`; sm `padding:6px var(--ds-space-4);12px/16px` | hover / active / focus-visible / disabled | as above | **Chrome** -- M1 |
| `.ds-ripple` | `absolute;50%;background:rgba(255,255,255,.2);transform:scale(0);pointer-events:none` (JS-spawned element) | ripple | `ds-ripple-anim 500ms ease-out forwards` | **Ring** press feedback -- M3 |

## A36. DS form controls -- input, select, slider, switch

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.ds-input` (+label/help/error) | `100%;padding:var(--ds-space-3) var(--ds-space-4);border-radius:var(--ds-radius-sm);border:1px solid var(--hart-glass-border);background:var(--ds-surface-1);color:var(--hart-text);font-family:var(--ds-font-body);14px/20px;outline:none`; label `12px;500;letter-spacing .5px;var(--hart-muted);uppercase`; help `12px;var(--hart-muted)` | `:focus{border-color:var(--hart-accent);box-shadow:0 0 0 2px rgba(0,212,170,.25)}` -- **HARD-CODED teal ring, does NOT retint (GAP)**; `.ds-input-error{border-color:var(--hart-error)}`, error focus `0 0 0 2px rgba(255,107,107,.2)` (also hard-coded) | `transition:border-color/box-shadow var(--ds-duration-medium) var(--ds-ease-standard)` | **Chrome** input -- M1 |
| `.ds-select` | as input + `padding-right:var(--ds-space-8);appearance:none;background-image:url(inline SVG caret, fill %2378909c);background-position:right 8px center`; `option{background:var(--hart-surface);color:var(--hart-text)}` | focus -> `border-color:var(--hart-accent)` | `transition:border-color var(--ds-duration-medium) var(--ds-ease-standard)` | **Chrome** select + **Glyph** caret -- M1 |
| `.ds-slider` (+thumbs) | track `-webkit-appearance:none;100%;height:4px;background:var(--ds-surface-3);border-radius:var(--ds-radius-full)`; thumb `20x20;50%;background:var(--hart-accent);box-shadow:var(--ds-elevation-1)`, hover `elevation-2;scale(1.15)`, active `elevation-3;scale(1.25)`; `-moz-range-thumb` mirror (no transition) | hover / active | `transition:box-shadow var(--ds-duration-short) var(--ds-ease-standard),transform var(--ds-duration-short) var(--ds-ease-spring)` | **Chrome** slider -- M1 |
| `.ds-switch` | `inline-flex;cursor:pointer`; `input{absolute;1x1;opacity:0;margin:0}` -- **visually hidden but FOCUSABLE (not `display:none`, preserves tab order)**; slider `38x22;radius 999px;background:var(--ds-surface-3)`; `::before{absolute;top/left 2px;18x18;50%;#fff;box-shadow:var(--ds-elevation-1)}`; checked -> slider `var(--hart-accent)` + knob `translateX(16px)`; focus-visible -> `outline:2px solid var(--hart-accent);outline-offset:2px` | checked; focus-visible | `transition:background var(--ds-duration-short) var(--ds-ease-standard)`; knob `transform var(--ds-duration-short) var(--ds-ease-spring)` | **Chrome** switch -- M1 |

## A37. DS card / chip / progress / skeleton

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.ds-card` (+elevated/interactive) | `background:var(--hart-surface);border-radius:var(--ds-radius-md);padding:var(--ds-space-4);border:1px solid var(--hart-glass-border)`; elevated `box-shadow:var(--ds-elevation-1)`; interactive hover `elevation-2;translateY(-1px)`, active `translateY(0);elevation-1` | hover / active | `transition:box-shadow/transform var(--ds-duration-medium) var(--ds-ease-standard)` | **Card** -- M2 |
| `.ds-chip` (+dot variants) | `inline-flex;gap:var(--ds-space-1);padding:var(--ds-space-1) var(--ds-space-3);border-radius:var(--ds-radius-full);12px;500;letter-spacing .5px;line-height 16px;border:1px solid var(--hart-glass-border);background:var(--ds-surface-1)`; dot `6x6;50%`; success `var(--hart-active)`, warning `var(--hart-caution)`, error `var(--hart-error)` | success / warning / error | none | **Glyph**+**Text** -- M4 |
| `.ds-progress` / `-fill` | track `height:6px;background:var(--ds-surface-3);border-radius:var(--ds-radius-full);overflow:hidden`; fill `100% height;radius full` | -- | `transition:width var(--ds-duration-long) var(--ds-ease-decelerate)` | **Glyph** meter -- M4 |
| `.ds-skeleton` (+text/title/circle/bar/card) | `linear-gradient(90deg,var(--ds-surface-2) 25%,var(--ds-surface-4) 50%,var(--ds-surface-2) 75%);background-size:200% 100%;border-radius:var(--ds-radius-sm)`; text `14px;margin-bottom:var(--ds-space-2);radius xs`; title `22px;width:50%`; circle `50%`; bar `6px;radius full`; card `64px;radius md` | potato -> `animation:none;background:var(--ds-surface-2)` | `ds-shimmer 1.5s ease-in-out infinite` | **Card** placeholder -- M2/M6 |

## A38. DS modal

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.ds-modal-overlay` | `position:fixed;inset:0;z-index:10000;flex center;background:rgba(0,0,0,.6);opacity:0;visibility:hidden`; `.ds-open{opacity:1;visibility:visible}` | ds-open | `transition:opacity var(--ds-duration-medium) var(--ds-ease-standard),visibility var(--ds-duration-medium)` | **Field** scrim -- M5 |
| `.ds-modal` | `background:var(--hart-glass-bg);border:1px solid var(--hart-glass-border);border-radius:var(--ds-radius-lg);padding:var(--ds-space-6);max-width:480px;width:calc(100% - var(--ds-space-8));box-shadow:var(--ds-elevation-5);backdrop-filter:blur(20px) saturate(180%);transform:scale(.92) translateY(20px);opacity:0`; open -> `scale(1) translateY(0);opacity:1`. JS focus-trap surface (l.5761) | ds-open; potato -> `backdrop-filter:none;background:var(--hart-surface)` | `transition:transform var(--ds-duration-long) var(--ds-ease-spring),opacity var(--ds-duration-medium) var(--ds-ease-decelerate)` | **Card** modal -- M5 |
| `.ds-modal-title` / `-body` / `-actions` | title `22px/28px;500;margin-bottom:var(--ds-space-4)`; body `14px/20px;var(--hart-muted);margin-bottom:var(--ds-space-6)`; actions `flex;justify-content:flex-end;gap:var(--ds-space-2)` | -- | none | **Text** -- M4 |

## A39. DS layout -- panel grid, list item, metric, dividers, type scale, elevation, utilities

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| type scale (`.ds-display-lg/md/sm`, `.ds-headline-*`, `.ds-title-*`, `.ds-body-*`, `.ds-label-*`, `.ds-mono`) | display 57/64/400/-0.25px, 45/52/400, 36/44/400; headline 32/40/600, 28/36/600, 24/32/600; title 22/28/500, 16/24/500/.15px, 14/20/500/.1px; body 16/24/400/.5px, 14/20/400/.25px, 12/16/400/.4px; label 14/20/500/.1px, 12/16/500/.5px, 11/16/500/.5px; mono `var(--ds-font-mono)` | -- | none | **Text** type scale -- M4 |
| `.ds-elevation-0..5` | `box-shadow:var(--ds-elevation-N)`; **potato -> `box-shadow:none`** | potato | none | **Card** depth -- M1/M6 |
| `.ds-panel-grid` / `-header` / `-title` / `-subtitle` / `.ds-section-label` | grid `gap:var(--ds-space-3)`; header `flex between;margin-bottom:var(--ds-space-2)`; title `22/28/500;var(--hart-heading)`; subtitle `14px;var(--hart-muted)`; section-label `11px;600;uppercase;letter-spacing 1.5px;var(--hart-muted);padding:var(--ds-space-2) 0` | -- | none | **Row**+**Text** -- M2/M4 |
| `.ds-list-item` (+interactive/icon/content/primary/secondary/trailing) | `flex;gap:var(--ds-space-3);padding:var(--ds-space-3);border-radius:var(--ds-radius-sm);background:var(--hart-surface)`; interactive hover `background:var(--hart-surface-hover);translateY(-1px)`; icon `var(--ds-icon-sm)`; primary 14/20; secondary 12/16 muted; trailing 12px | hover | `transition:background/transform var(--ds-duration-short) var(--ds-ease-standard)` | **Row** list item -- M2 |
| `.ds-metric` / `.ds-dot` / `.ds-divider` | metric `center;padding:var(--ds-space-4)`, value `32px/600/40`, label `12px muted`, icon `var(--ds-icon-xl)`; dot `8x8;50%`; divider `border-top:1px solid var(--hart-glass-border);margin:var(--ds-space-3) 0` | -- | none | **Text**+**Glyph** -- M4 |
| `.ds-fade-in` / `.ds-stagger>*` | entrance both-fill | stagger index 1-8 / n+9 | `ds-content-enter var(--ds-duration-medium) var(--ds-ease-decelerate) both`; delays 0/30/40/50/60/70/80/90ms for children 1-8, 100ms for n+9 | **Row** entrance -- M2 |
| utilities | `.ds-flex/.ds-flex-col/.ds-flex-center/.ds-flex-between/.ds-flex-wrap/.ds-gap-1..4/.ds-flex-1`; text colours `.ds-text-accent/-active/-error/-caution/-muted/-heading` | -- | none | **Chrome** layout helpers -- M1 |

## A40. Designed empty / offline states

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.lg-empty` | `flex column center;text-align:center;gap:var(--ds-space-3);padding:var(--ds-space-12) var(--ds-space-6);min-height:240px`. Built by `hartStates.js` as `.lg-empty-{loading,offline,empty}` | loading / offline / empty | `lg-empty-in var(--t-reveal) var(--lg-enter)` | **Card** state block -- M2 |
| `.lg-empty-disc` (+`.mi`) | `56x56;border-radius:var(--ds-radius-lg);flex center;background:var(--lg-1-bg);border:1px solid var(--lg-1-bd);box-shadow:var(--lg-spec)`; `.mi{28px;color:var(--lg-muted)}`; offline variant `.mi{color:rgb(var(--lg-blind-rgb))}` | offline; a11y-rmotion -> breathe off | `lg-empty-breathe 3s var(--lg-breathe) infinite` (offline only) | **Glyph** state disc -- M3/M4 |
| `.lg-empty-title` / `-msg` / `-retry` | title `15px;600;color:var(--lg-heading);letter-spacing -.1px`; msg `13px/1.5;var(--lg-muted);max-width:340px`; retry `margin-top:var(--ds-space-1)` | -- | none | **Text** -- M4 |

## A41. Scrollbars

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `::-webkit-scrollbar` / `-track` / `-thumb` / `-thumb:hover` | `width:6px`; track `transparent`; thumb `background:var(--hart-muted);border-radius:3px`, hover `var(--hart-accent)` | hover | none | **Chrome** scrollbar -- M1 |
| `.start-scroll` (Firefox) | `scrollbar-width:thin;scrollbar-color:var(--hart-muted) transparent` -- the ONLY Firefox-syntax scrollbar rule | -- | none | **Chrome** -- M1 |

## A42. Legacy voice-recording mic button

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.mic-btn` / `.mic-btn.recording` | `cursor:pointer`; recording `color:var(--hart-error)!important` | recording; potato (**animation string EMPTY and `@keyframes pulse` not emitted at all**) | `pulse 1s infinite` | **Glyph** legacy mic -- M3/M6 |

## A43. Deterministic visibility engine -- `html[data-*]` contract

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `html[data-busy\|panels\|typing\|idle\|voice\|blind\|thinking\|speaking\|agents\|online\|multiws]`, `#hart-hero-orbwrap[data-orb-state]`, `#hart-senses[data-edge]` | **Sole writer is `hartVisibility.js`** (`data-multiws` owned by `hartWorkspaces.js`; `data-orb-state` by `hartHero.js`; `data-edge` by `hartSenses.js`). All show/hide is DECLARATIVE CSS on EXISTING markup -- **no JS style mutation**. Complete consumer list: busy\|panels\|typing -> hide `.hart-hero-chips`; idle -> `.hart-senses{opacity:.55}`; voice\|blind -> `.hart-senses{opacity:1}`; thinking\|voice\|speaking -> `.hart-ambient` saturate/brightness; speaking -> `.hart-hero-hevolve{opacity:.9}` + green dot; agents=1 -> chip dot glow; agents=0 -> `.top-bar-center{opacity:.72}`; online=0 -> `.top-bar-center{opacity:.5;grayscale(.7)}`; multiws=0 -> hide `.hart-ws-switcher` | 14 documented states (busy, panels, typing, idle, voice, blind, thinking, speaking, agents 0/1, online 0, multiws 0, orb-state idle/listening/thinking/speaking, edge b/t/r/l) | `transition:opacity/transform var(--t-move) var(--lg-enter)`; `transition:opacity var(--t-reveal) var(--lg-glide)`; `transition:filter var(--t-reveal)` | **Chrome** state engine -- **M6 (the single most important native contract; port as a declarative state table, not imperative show/hide)** |
| first-paint seeding | `<html class="{a11y_cls}" data-multiws="0">` (l.2558); `data-orb-state="idle"` on `#hart-hero-orbwrap`; `data-edge="b"` on `#hart-senses` -- **an ABSENT attribute would not match, causing reveal FOUC** | boot | none | **Chrome** -- M6 |

## A44. Accessibility layer

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `:focus-visible` | global keyboard ring `outline:2px solid var(--hart-accent);outline-offset:2px` -- **keyboard-only; a mouse click draws none** | focus-visible | none | **Chrome** focus ring -- M1 |
| chrome inset ring group (`.start-btn`,`.tray-btn`,`.start-item`,`.power-btn`,`.taskbar-chip`,`.agent-pill`,`.ctx-menu-item`) | same ring with `outline-offset:-2px` (INSET) | focus-visible | none | **Chrome** -- M1 |
| `.skip-link` | `position:fixed;top:-200px;left:8px;z-index:100000;padding:8px 16px;background:var(--hart-accent);color:var(--hart-on-accent);radius 8px;600;text-decoration:none`; `:focus{top:8px}` | focus | `transition:top .2s` | **Chrome** skip link -- M1 |
| `@media(prefers-reduced-motion:reduce) *,*::before,*::after` | `animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important` AND `:root` motion tokens -> 0ms | reduced-motion | all forced to 0.01ms / 1 iteration | **Chrome** motion gate -- M6 |
| `html.a11y-contrast` | token remap, not a separate skin: `--hart-muted:#e8eef2;--hart-glass-bg:#0a0a12;--hart-glass-border:#ffffff;--hart-text:#ffffff`; `.glass{background:#0a0a12;border-width:2px}` | a11y-contrast | none | **Chrome** contrast mode -- M6 |
| `html.a11y-rmotion` | mirrors the media query at class level; per-component kills: `.hart-ambient`, `.hart-hero-hevolve .dot`, `.hart-orb-orbit`, `.hart-orb-orbit2`, `.panel`, orb comet `::before`, orb listening `::after`, `.hart-senses-btn.is-sensing .mi`, `.lg-empty-offline .lg-empty-disc .mi` | a11y-rmotion | animation:none | **Chrome** motion gate -- M6 |
| `:root` font scale | server-emitted, clamped 0.8-2.0: `--hart-font-size/--hart-heading-size/--hart-icon-size` computed from theme font/shell sizes x scale; classes from `get_a11y_settings()`: `a11y-contrast` and/or `a11y-rmotion` on `<html>` | font_scale != 1.0 | none | **Text** metrics -- M4/M6 |

## A45. Icon font (`@font-face`) + glyph rendering

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `@font-face 'Material Symbols Rounded'` | `font-style:normal;font-weight:400;font-display:block;src:url('/shell/static/MaterialSymbolsRounded.woff2') format('woff2')` -- **BUNDLED, ~440KB, 6.5k glyphs, authoritative offline** | offline (bundled) / online (CDN variant) | none | **Glyph** font source -- M4 |
| `.mi, .material-icons-round` | family fallback chain `'Material Symbols Rounded','Material Icons Round','Material Icons','Material Symbols Outlined'`; `font-weight/style:normal;line-height:1;letter-spacing:normal;text-transform:none;white-space:nowrap;word-wrap:normal;direction:ltr;display:inline-block;font-feature-settings:'liga'` (+`-webkit-`); `-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility`. **`'liga'` turns the ligature name (e.g. `smart_toy`) into the glyph -- native parity needs a ligature-capable text shaper or an explicit codepoint map** | -- | none | **Glyph** shaping -- **M4 (highest-risk native item)** |
| CDN `<link>` (l.2562) | Material Icons Round, JS-injected ONLY when `navigator.onLine` -- progressive enhancement only | online | none | **Glyph** -- M4 |

## A46. Boot splash + JS error banner (inline `style` attributes, not `<style>`)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `#hart-boot` | `position:fixed;inset:0;z-index:99999;flex center;background:#0F0E17` -- highest z in the shell; `hartBootSplash.js` drives the fade | boot visible -> faded out | `transition:opacity .6s ease` | **Field** boot splash -- M1 |
| `#hart-boot-lottie` | `width:min(46vw,360px);height:min(64vw,497px)` (Lottie brand animation, aspect ~0.724) | -- | Lottie (JS) | **Field**/**Orb** brand animation -- M1 |
| `#_js_err` (created by `window.onerror`, l.3105) | `position:fixed;bottom/left/right:0;background:#c00;color:#fff;font:13px monospace;padding:8px 12px;z-index:99999;white-space:pre-wrap;max-height:40vh;overflow-y:auto` -- kiosk debug surface | JS error present | none | **Chrome** debug banner -- M6 |

## A47. Degradation tiers -- potato / webkit-flat / gpu body class

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| Python gates (l.1443-1489) | `webkit_compositing = LIQUID_UI_PREFER_HW_GL=='1' OR read_shell_render_mode() in ('vulkan','webkit-cairo')`; `gpu_mode='hardware'` only if gpu probe==hardware AND `webkit_compositing`; body class `gpu-software\|gpu-hardware`; `flat_body_class=' webkit-flat'` when compositing off; `is_potato = theme disable_blur OR gpu_mode=='software'`; `emit_ambient = (not is_potato) or gpu_mode=='software'` | is_potato / gpu-software / gpu-hardware / webkit-flat | none | **Chrome** capability verdict -- **M6 (single source of truth; the GTK4 host reads the same `/run/hart/gpu-render`)** |
| POTATO strip list (complete, in order) | `.glass` backdrop-filter; `.panel`/`.panel.focused` rich shadows -> flat; `.panel transition:none`; `.hart-widget` backdrop-filter; `.taskbar-chip transition:none`; `.toast` animation (**and `@keyframes slideInRight/fadeOutToast/pulse` are not emitted at all**); `.mic-btn.recording` animation; `_CSS_ANIMATIONS` replaced by `.panel{animation:none}`; `.lock-screen` blur -> opaque `.97`; `.ds-modal`/`.ds-toast` backdrop-filter + opaque bg; `.ds-skeleton` shimmer; `.ds-toast-progress` countdown; `.ds-elevation-1..5` -> `box-shadow:none`; `.lg-1..4` backdrop-filter + raised opacity; `.lg-senses-ghost` + `.hart-desktop::before` -> `display:none`; `.hart-grain` element not emitted | is_potato | animations removed at emit time | **Chrome** floor -- M6 |
| `body.webkit-flat` | backdrop-filter will NOT paint -- the CSS floor that solidifies glass lives in the EXTERNAL `hartResponsive.css` (see Part C) | webkit-flat | none | **Chrome** floor -- M6 |

## A-notes -- cross-cutting inventories for Part A

**Confirmed supersessions (later source wins -- do NOT naively de-duplicate):**

| # | Superseded | Effective |
|---|---|---|
| 1 | `.panel` open: `hart-fade-in .18s` -> `fadeIn` | **`hart-panel-in .3s` spring** |
| 2 | `.hart-hero-orbwrap.listening` RED glow | `box-shadow:none`; live cue is `[data-orb-state=listening]::after` with `--lg-ring-listen` cyan |
| 3 | `.hart-senses-mic.listening` red | cyan `rgba(var(--lg-listen-rgb),…)` |
| 4 | `.hart-senses-btn.off` red | grey `--lg-blind-rgb` |
| 5 | `.desktop-icon.selected` flat purple `rgba(108,99,255)` | `rgba(var(--lg-accent-rgb),.22)` + outline + glow |
| 6 | `.hart-ws-dot` legacy pills | `.hart-pager-thumb`/`.hart-pager-seg` segmented pager |
| 7 | `.start-item`/`.taskbar-chip`/`.tray-btn` transitions (3x) | `_CSS_HERO` spring `cubic-bezier(.175,.885,.32,1.275)` |
| 8 | `.hart-hero-chip` hover/active | living-glass `--lg-sh-2` + `scale(.97)` |
| 9 | `.hart-hero-orbwrap` transition | living-glass `var(--lg-spring)` / `var(--t-move) var(--lg-breathe)` |
| 10 | `.hart-senses-cluster` gap 8px | 6px |

**Dead / gap items:** `@keyframes hart-ambient-drift` defined, applied to nothing. `.hart-icustom-backdrop` blur(2px) is NOT potato-gated. `.ds-input:focus` / `.ds-input-error:focus` rings are hard-coded `rgba(0,212,170,.25)` / `rgba(255,107,107,.2)` and do NOT retint. The onboarding ceremony palette is hard-coded teal `0,230,195` / violet `155,92,255`. Eight markup classes have ZERO inline rules (A12) -- styled by the external `hartHome.css`; `hartResponsive.css` owns the `body.gpu-software` / `body.webkit-flat` floors.

**Blur inventory (compositor budget):** `.glass` `blur(var(--hart-blur)) saturate(var(--hart-saturation))`; `.lg-1..4` 14/20/26/34px + `saturate(1.4)`; `.hart-app-card` 12px + 1.2; `.hart-senses-cluster` 12px; `.hart-senses-btn` 10px; `.hart-senses-panel` 14px; `.ds-modal` 20px + 180%; `.ds-toast` 16px + 150%; `.hart-widget` `var(--hart-blur)`; `.hart-icustom-backdrop` 2px; `.lock-screen` 24px. Non-backdrop filters: `.hart-hero.dimmed` blur(6px); `.hart-hero.ai-blind` grayscale(1) brightness(.4); `.hart-ambient` state tints; `html[data-online=0]` grayscale(.7); orb two-layer drop-shadow (teal 46px + violet 64px). **Exactly ONE `mix-blend-mode`** (`.hart-grain` overlay), **one conic-gradient** and **one mask** (both the thinking comet).

**Z-index ladder (complete):** wallpaper 0 / `.hart-ambient` 0 / bloom canvas 1 / grain 2 / vignette 2 / `.hart-desktop` 20 / `.hart-widgets` 30 / `.hart-hero` 40 / `.lg-marquee` 55 / `.desktop-icon.dragging` 60 / `.panel-container` 100 / `.panel.focused` 999 / `.top-bar` 1000 / `.agent-pill` 1500 / `.assistant-chat` 1600 / `.start-menu` 2000 / `.ctx-menu` 3000 / `.taskbar` 8000 / `.hart-ws-switcher` 8050 / `.lg-senses-ghost` 8090 / `.hart-senses` 8100 / `.hart-icustom-backdrop` 9000 / `.toast-container` 9500 / `.lock-screen` 9999 / `.ds-modal-overlay` 10000 / `.hart-onboarding` 12000 / `#hart-boot` + `#_js_err` 99999 / `.skip-link` 100000. **Note the inversion: `.taskbar` (8000) sits BELOW `.hart-ws-switcher` (8050) and `.hart-senses` (8100).**

**Fixed-canvas invariant:** `html,body{width:100%;height:100%;overflow:hidden}` + `overscroll-behavior:none`. Only `.start-scroll`, `.ac-messages`, `.panel-body .native-content` and `.hart-mkt` scroll internally. Every top-level surface is `position:fixed`.

---

# Part B -- `hartHome.css` (Aura home surface)

## B1. ROOT TOKENS -- brand palette (theme-tracked, `:root` L25-52)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `:root` | `--hh-teal:var(--hart-accent,#00E6C3)` [FUNCTIONAL accent]; `--hh-cyan:var(--hart-amb-3,#29C5FF)` [themable MOOD slot 3]; `--hh-blue:#3B82F6` [**HARD-CODED**]; `--hh-violet:var(--hart-a2,#9B5CFF)`; `--hh-magenta:var(--hart-amb-4,#FF2E9A)` [themable MOOD slot 4]; `--hh-amber:#FFC83D` [**HARD-CODED**]; `--hh-ink:var(--hart-text,#F3F6FB)`; `--hh-dim:var(--hart-muted,#9AA7B6)`; `--hh-bord:var(--hart-glass-border,rgba(255,255,255,.09))`; `--hh-top-safe:70px`; `--hh-bottom-safe:56px`; `--hh-gutter:60px` (-> 40px at `max-width:1400px`) | -- | none | **Chrome** token store -- M1 |
| `--hh-acc` (runtime-only) | NOT declared in `:root`; set per-row by `.hh-accent-*` and consumed with fallback `var(--hh-acc,var(--hh-teal))` / `var(--hh-acc,rgba(0,230,195,.7))` | per-row accent | none | **Row** accent channel -- M2 |
| **PARITY NOTE** | every fallback IS the exact default `theme_service` emits, so `hart-default` and every preset render pixel-identically; only a theme that sets the token retints | -- | -- | -- |

## B2. ROOT TOKENS -- Aura Motion System (`:root` L53-86)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| master + layer gates | `--hart-motion-speed:1` [**unitless multiplier applied to EVERY duration via `calc(base * speed)`**]; LAYER 1 `--hart-motion-ambient:running`; LAYER 2 `--hart-motion-orb:running`; LAYER 3 `--hart-motion-rings:running`; LAYER 4 `--hart-motion-detail:running`. Each layer var is an `animation-play-state` value -> **ONE Personalize write toggles a whole group** | running / paused | vocabulary declaration only | **Chrome** motion control plane -- **M3/M6 (expose all five as live-writable)** |
| amplitude + duration tokens | `--hart-anim-blob-speed:18s`; `-hue-speed:22s`; `-hue-amt:28deg` (read INSIDE `@keyframes vHue`); `-breathe-speed:4s`; `-breathe-amt:1.08` (inside `vBreathe`); `-float-speed:9s`; `-float-amt:-10px` (inside `vFloat`); `-spin-speed:60s`; `-spin-rev-speed:90s` **[DEAD]**; `-spinc-speed:26s` **[DEAD]**; `-blink-speed:1.05s` **[DEAD]**; `-wave-speed:1.1s` **[DEAD]**; `-dash-speed:4s` **[DEAD]**; `-joint-speed:2.4s` **[DEAD]** | -- | -- | **Chrome** -- M3 (amplitude-inside-keyframe needs a parameterised native animation) |

## B3. FIXED CANVAS -- `.hart-home` shell root (L88-121)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-home` | `position:fixed;inset:0` [**ONE screen, NEVER page-scrolls**]; `z-index:30` (above wallpaper + desktop-icon layer, below the floating orb z1450 and app windows z100+); `flex column`; `padding:var(--hh-top-safe) 0 var(--hh-bottom-safe) 0`; `color:var(--hh-ink)`; `font-family:var(--hart-font-display),var(--hart-font-family),"Segoe UI",Inter,Roboto,system-ui,…` (Aura display face = Space Grotesk); `overflow:hidden`. **NO blur, NO backdrop-filter, NO blend mode on the canvas itself** | `.hh-ready` (opacity 0 -> 1); `.hh-hidden{display:none}` (fullscreen app / other workspace takeover) | `transition:opacity 360ms ease` -- the ONLY transition on the root; killed by reduced-motion | **Field** home canvas -- M1 |
| pointer policy | `pointer-events:none` on the container AND every direct child; **only** `button`, `a`, `.hh-cards`, `.hh-card`, `.hh-see-all` opt back in with `pointer-events:auto`, so empty areas reach the wallpaper context menu + desktop icons | -- | -- | **Field** hit-test policy -- M1 |

## B4. EARNINGS HERO -- container + eyebrow (L123-137)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hh-hero` | `flex:0 0 auto;padding:6px var(--hh-gutter) 0;max-width:980px` -- left-aligned, value-first; the orb floats to its right in `hartHero.js` home-mode | -- | none | **Chrome** hero block -- M2 |
| `.hh-eyebrow` | `color:var(--hh-teal);16px;700;letter-spacing 3px;uppercase` | -- | none | **Text** -- M4 |

## B5. EARNINGS HERO -- the BIG NUMBER (L138-165)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hh-amount-row` / `.hh-amount` | row `flex;align-items:baseline;gap:12px;margin:10px 0 2px`; amount `88px;line-height .98;800;letter-spacing -2px;tabular-nums`; `color:` **SOLID `var(--hh-teal)`**. **EXPLICITLY NOT a teal->cyan->blue gradient-clip** -- glyphs sat in the cyan/blue half and read "blue", and `-webkit-background-clip:text` is fragile in the old cage WebKit (transparent => invisible). **PARITY: native must use a solid fill, not a text gradient.** `text-shadow:0 0 24px rgba(0,230,195,.35)` [STATIC teal glow, kept on the software floor] | software floor (text-shadow only); `body.gpu-hardware` adds `filter:drop-shadow(0 0 26px rgba(0,230,195,.38))` [second, softer bloom layered over the text-shadow] | count-up is JS-driven (`hartHome.js`), **not CSS** | **Text** hero numeral -- M4 |
| `.hh-amount-unit` | "Spark": `26px;700;color:var(--hh-dim);letter-spacing .5px` -- calm sibling so only the VALUE counts | -- | none | **Text** -- M4 |
| responsive | 88px -> 70px @`max-width:1400px` -> 58px @`max-height:820px`; unit 26px -> 22px @1400px | breakpoints | -- | **Text** -- M4 |
| **history** | this block REPLACED the old "Your hive earned X while you slept" sentence + paragraph subtitle (the text-wall regression) | -- | -- | -- |

## B6. EARNINGS HERO -- settlement sparkline (L167-174)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hh-spark` / `svg` | `margin:2px 0 4px;height:44px;width:232px;opacity:.95`; svg `display:block;100%x100%`; height 44 -> 34px @`max-height:820px` | breakpoint | none | **Glyph** sparkline (vector path) -- M4 |

## B7. EARNINGS HERO -- honest meta strip (L176-220)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hh-hero-meta` | `flex;align-items:center;wrap;gap:10px 16px;margin-top:12px;15px;color:#C3CDD9` [**HARD-CODED slate**] | -- | none | **Row**+**Text** -- M4 |
| `.hh-pill` / `.hh-pill-dot` | payout-pending pill `inline-flex;gap:7px;padding:5px 12px;radius 30px;13px;700;color:var(--hh-amber);background:rgba(255,200,61,.12);border:1px solid rgba(255,200,61,.30)`; dot `7x7;50%;background:var(--hh-amber)` | pending; software floor (solid dot); reduced-motion | GPU-only `hhLiveDot calc(1.8s * var(--hart-motion-speed,1)) ease-in-out infinite`, `animation-play-state:var(--hart-motion-detail,running)` -- amber breathe = honest state reads as live | **Glyph** live dot -- M3 |
| `.hh-usd` / `.hh-stat` / `.hh-stat b` / `.hh-local-mini` / `.hh-shield` | usd `var(--hh-dim);600`; stat `#C3CDD9` [HARD-CODED], `b{color:var(--hh-ink);800}`; local badge `inline-flex;gap:6px;color:var(--hh-teal);700`; shield `8x8;50%;background:var(--hh-teal)` -- **a DOT, not a shield glyph** | -- | none | **Text**+**Glyph** -- M4 |

## B8. HERO CTA BUTTONS (L222-252, GPU L499-504)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hh-cta` / `.hh-btn` | cta `flex;gap:14px;margin-top:26px` (-> 16px @`max-height:820px`); btn `inline-flex;gap:10px;padding:15px 26px;radius 14px;18px;700;color:var(--hh-ink);background:rgba(255,255,255,.08)`. **DECLARATION-ORDER TRAP: `border:none` is declared then OVERRIDDEN by `border:1px solid var(--hh-bord)` two lines later -- the border WINS.** `.mi{20px}` | hover (GPU only: `translateY(-2px)`; **software floor has NO hover feedback on buttons**); reduced-motion | GPU-only `transition:transform 160ms ease,box-shadow 160ms ease` | **Chrome** CTA -- M2 |
| `.hh-btn-primary` | `color:#04140F` (near-black ink on bright teal); `background:linear-gradient(135deg,#5CFFD9,var(--hh-teal))` -- **EXPLICITLY NOT teal->cyan** (cyan `#29C5FF` dominated the small button and read "blue"); `box-shadow:0 12px 30px rgba(0,230,195,.30)` [STATIC glow; one-time raster so the SOFTWARE floor keeps the lit Resume button] | primary | -- | **Chrome** -- M2 |
| GPU-only | `-webkit-backdrop-filter`/`backdrop-filter:blur(10px)` -- **the ONLY backdrop-filter in the whole file** | gpu-hardware | -- | **Chrome** -- M6 |

## B9. ROWS REGION -- container, head, see-all, scroller (L254-307)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hh-rows` | `flex:1 1 auto;min-height:0;flex column;justify-content:FLEX-END;gap:18px;padding:18px 0 4px var(--hh-gutter);overflow:hidden` -- **the region NEVER vertically scrolls**; the agent composes the 2-3 rows that fit, deeper categories open as panels via See all | -- | none | **Row** region -- M2 |
| `.hh-row-head` / `.hh-row-title` / `.hh-row-note` / `.hh-see-all` | head `flex;align-items:baseline;gap:14px;margin-bottom:12px;padding-right:var(--hh-gutter)`; title `23px;700`; note `15px;600` -- declared `var(--hh-teal)` at L282 then RE-DECLARED at L492 as `var(--hh-acc,var(--hh-teal))` (**later wins -> per-row accent tint**); see-all `margin-left:auto;15px;600;color:var(--hh-dim);background:none;border:none`, hover `color:var(--hh-ink)` (**instant, NO transition declared -- software AND gpu**) | see-all hover | none | **Row** head -- M2 |
| `.hh-cards` | `flex;gap:18px;overflow-x:auto;overflow-y:hidden;padding:6px 60px 8px 0` (**60px HARD-CODED right pad, NOT `--hh-gutter`**); `scroll-snap-type:x proximity`. Scrollbar fully hidden: `scrollbar-width:none` + `-ms-overflow-style:none` + `::-webkit-scrollbar{display:none}`. **HORIZONTAL scroll only -- Netflix rail behaviour** | scroll-snap proximity | none | **Row** rail -- M2 |

## B10. IMAGE CARD -- base + format variants (L309-333, GPU L506-519)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hh-card` | `relative;flex:0 0 auto;258x150` (landscape default); `border-radius:var(--hart-radius,16px)` [theme-driven; Aura = 22px, fallback 16px == today's look]; `overflow:hidden;border:1px solid var(--hh-bord);background:#0E1320` [HARD-CODED plate]; `scroll-snap-align:start`; `box-shadow:0 16px 38px rgba(0,0,0,.46)` [**STATIC; rasters ONCE so the SOFTWARE floor KEEPS the depth -- degrade gracefully, not gut. Without it the software home read as flat rectangles**]. Height 150 -> 132px @`max-height:820px` | hh-wide 360px / hh-portrait 180x252 / hh-square 180x180 / hh-ranked / empty; reduced-motion | GPU-only `transition:transform 200ms ease,box-shadow 200ms ease`; `will-change:transform` | **Card** -- M2 |
| GPU-only hover | `transform:scale(1.07)` (Netflix hover-expand); `z-index:5`; **TRIPLE shadow stack** `0 30px 70px rgba(0,0,0,.6)` + `0 0 0 2px var(--hh-acc,rgba(0,230,195,.7))` [accent ring] + `0 0 46px rgba(0,230,195,.32)` [teal bloom]. **Software floor has NO card hover.** | hover (gpu-hardware) | as above | **Card** -- M2/M6 |

## B11. IMAGE CARD -- art layer + lazy-load + scrim (L335-359, GPU L520-522)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hh-card-art` / `img` | art `absolute;inset:0;background-size:cover;background-position:center;background-repeat:no-repeat` [real photo when the payload supplies one, else a brand-spectrum static gradient seeded per card]; img `100%;object-fit:cover;display:block;opacity:0` | `.hh-loaded{opacity:1}` -- JS adds the class on `img.onload`; **native must reproduce the fade-in-on-decode** | `transition:opacity 300ms ease` (**NOT gpu-gated -- runs on the software floor too**) | **Card** art -- M2 |
| `.hh-card-scrim` | software floor `linear-gradient(transparent 32%,rgba(4,7,13,.78) 100%)` [text-over-art legibility; static, NO blur]; **GPU override** `linear-gradient(transparent 28%,rgba(4,7,13,.72) 100%)` [starts higher, ends LIGHTER -- more photo shows through when the GPU can carry the hover machinery] | software / gpu-hardware | none | **Card** scrim -- M2/M6 |

## B12. IMAGE CARD -- body text (L361-381)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hh-card-body` / `.hh-card-title` / `.hh-card-meta` | body `absolute;left/right/bottom:0;padding:14px;flex column;gap:4px` [sits OVER the art at the bottom]; title `18px;700;text-shadow:0 1px 6px rgba(0,0,0,.55)` [legibility shadow, kept on software]; meta `13px;color:#CFE` (**HARD-CODED 3-digit hex == `#CCFFEE`, pale mint**); `opacity:.85` | -- | none | **Text** -- M4 |

## B13. IMAGE CARD -- chips, badge, progress, live tag (L383-445, GPU L531-538)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hh-card-ic` | glyph chip, top-LEFT: `absolute;top:12px;left:14px` (**asymmetric vs the 12px top**); `34x34;radius 10px;flex center;20px;background:rgba(8,12,20,.55);border:1px solid var(--hh-bord);color:var(--hh-ink)`; `.mi{20px}` | -- | none | **Glyph** -- M4 |
| `.hh-card-badge` | top-RIGHT ("Replay"/rank/"New"): `absolute;top:12px;right:12px;12px;700;color:#04140F;background:var(--hh-teal);padding:3px 8px;radius 8px`. **COLLISION NOTE: shares the exact slot with `.hh-card-live` -- mutually exclusive per card** | badge present | none | **Glyph**+**Text** -- M4 |
| `.hh-card-prog` | Netflix continue-watching bar: `absolute;left/bottom:0;height:5px;border-radius:0 3px 0 0` (top-right corner only); background declared `var(--hh-teal)` L420 then RE-DECLARED L493 as `var(--hh-acc,var(--hh-teal))` (**later wins -> per-row accent**). Width set inline by JS | progress present | none | **Glyph** meter -- M4 |
| `.hh-card-live` / `.hh-dot` | running-agent tag: `absolute;top:12px;right:12px;inline-flex;gap:6px;12px;600;color:var(--hh-ink);background:rgba(8,12,20,.72);border:1px solid var(--hh-bord);padding:4px 9px;radius 30px`; dot `8x8;50%;background:var(--hh-magenta)` -- **MAGENTA, not teal: live == magenta in the spectrum** | live; software (solid); reduced-motion; `--hart-motion-detail:paused` | GPU-only `hhLiveDot calc(1.6s * var(--hart-motion-speed,1)) ease-in-out infinite`, play-state `var(--hart-motion-detail)` [LAYER 4] | **Glyph** live dot -- M3 |

## B14. IMAGE CARD -- ranked "Top N" leaderboard variant (L447-469)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hh-card.hh-ranked` | `background:transparent;border:NONE;overflow:VISIBLE` -- strips the base card plate so the giant numeral can bleed outside the box | hh-ranked; still inherits the GPU card hover-expand (`scale(1.07)`) since it IS a `.hh-card` | -- | **Card** -- M2 |
| `.hh-rank-num` | `absolute;left:-6px;bottom:-14px` (**NEGATIVE offsets -- bleeds out**); `116px;900;line-height .8;color:TRANSPARENT;-webkit-text-stroke:3px rgba(255,255,255,.30)` -- **OUTLINE-ONLY numeral; native must STROKE text, not fill**; `pointer-events:none` | -- | none | **Text** stroked numeral -- **M4 (needs a stroke-text primitive)** |
| `.hh-rank-inner` | `absolute;right/top/bottom:0;width:174px;border-radius:16px` (**HARD-CODED 16px, does NOT follow `--hart-radius` unlike the base card**); `overflow:hidden;border:1px solid var(--hh-bord)` | -- | none | **Card** -- M2 |

## B15. IMAGE CARD -- empty-state placeholder (L471-482)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hh-card-empty` | `flex center;color:var(--hh-dim);14px;text-align:center;padding:0 16px`; `background:rgba(255,255,255,.03)` [overrides the base `#0E1320` plate]; `cursor:default` [**overrides the base `cursor:pointer` -- explicitly NOT clickable**]. Inherits the base border, radius, and the static `0 16px 38px rgba(0,0,0,.46)` shadow | empty (graceful fallback when a row has no data) | none | **Card** empty -- M2 |

## B16. PER-CATEGORY ACCENT TINTS (spectrum, L484-493)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hh-accent-teal\|cyan\|blue\|violet\|magenta\|amber` | six row-level classes each set ONE var: `--hh-acc: var(--hh-teal)\|var(--hh-cyan)\|var(--hh-blue)\|var(--hh-violet)\|var(--hh-magenta)\|var(--hh-amber)`. Applied on the ROW **so the home is never green-only**; cascades into the row's cards | 6 variants + unset (falls back to teal) | none | **Row** accent channel -- M2 |
| consumers of `--hh-acc` (**exactly 3**) | `.hh-row-note` color; `.hh-card-prog` background; `body.gpu-hardware .hh-card:hover` second shadow ring `0 0 0 2px var(--hh-acc,rgba(0,230,195,.7))`. Also documented as seeding the gradient-art fallback + chips -- **that gradient is generated by `hartHome.js`, NOT by CSS** | -- | -- | **Row**/**Card**/**Text** -- M2 |

## B17. AMBIENT / BLOOM FIELD -- Motion Layer 1 (L596-616)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `body.gpu-hardware .hart-bloom-canvas` | PRIMARY: the runtime-composed bloom bitmap (`hartBloom.js`) gets TWO simultaneous animations -- `vBlob1` (transform drift) + `vHue` (hue-rotate filter). **PERF CONTRACT: the bloom is ALREADY pre-blurred into the bitmap, so animating it is a cheap GPU transform/filter, NEVER a per-frame re-blur. Native parity must animate a composited texture, not re-render the blur.** `vBlob1` keeps scale >= translate so the full-bleed canvas never reveals an edge | gpu-hardware; `--hart-motion-ambient:paused`; **NOT covered by the reduced-motion block (GAP)** | `vBlob1 calc(var(--hart-anim-blob-speed,18s) * var(--hart-motion-speed,1)) ease-in-out infinite, vHue calc(var(--hart-anim-hue-speed,22s) * speed) ease-in-out infinite`; `animation-play-state:var(--hart-motion-ambient)` | **Field** bloom texture -- M1/M3 |
| `body.gpu-hardware .hart-ambient` | FALLBACK: the CSS gradient field (hidden by `hartBloom.js` once the canvas composes) gets `vBlob1` ONLY (no hue) so a no-JS field is not dead. **Neither selector matches without `body.gpu-hardware` -> the bloom is STATIC on the calm floor.** Base styling for both lives elsewhere; only the animation is declared here | gpu-hardware / software floor | `vBlob1` only | **Field** -- M1 |

## B18. VOICE ORB (big, floating) -- Motion Layer 2 (L618-625)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `body.gpu-hardware #hart-voice-orb` | **TRANSFORM-ONLY by design**, so the orb's drop-shadow bloom AND the listening filter state (owned by `hartHero.js` / `voiceOrbViz.js`) are UNTOUCHED and can compose independently. This is the "living, breathing orb" pillar as a calm CSS float layered ON TOP of the canvas visualiser. Orb sits at `z-index:1450` (above `.hart-home` z30). Software floor: no float, no breathe | gpu-hardware; `--hart-motion-orb:paused`; reduced-motion (`animation:none !important`) | `vFloat calc(9s * speed) ease-in-out infinite, vBreathe calc(4s * speed) ease-in-out infinite`; play-state `var(--hart-motion-orb)` | **Orb** transform layer -- M3 |
| **GAP** | the orb's OWN geometry/gradient/shadow and its listening / thinking / speaking / idle character states are **NOT in this file** -- they live in `hartHero.js` / `voiceOrbViz.js`. This file contributes ONLY float+breathe and the reduced-motion kill | -- | -- | **Orb** -- M3 (source intrinsics from the JS) |

## B19. ORB AURA / ORBITAL RINGS -- Motion Layer 3 (L627-633)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `body.gpu-hardware .hart-hero-aura` | applies to the ring CONTAINER only. `hartHero.js` animates the ring CHILDREN, so this container spin is **ADDITIVE + conflict-free** (two independent transform owners at different DOM depths). Software floor: no spin | gpu-hardware; `--hart-motion-rings:paused`; reduced-motion | `vSpin calc(var(--hart-anim-spin-speed,60s) * speed) LINEAR infinite` [linear, not ease -- a true continuous orbit]; play-state `var(--hart-motion-rings)` | **Ring** container spin -- M3 |
| unapplied siblings | `vSpinRev` (90s) and `vSpinC` (26s) complete the mock's "spin trio" as VOCABULARY but have NO application rule here -- a native rewrite must source their targets from `hartHero.js` or leave the trio at one member | -- | dead | **Ring** -- M3 GAP |

## B20. TOP BAR -- nav tabs (L635-662)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| bar layout contract | brand \| nav tabs \| flexible agent-status \| omnibox pill \| orb-sm \| avatar \| trays. Existing `.top-bar` / `#agent-status` / `.top-bar-right` are KEPT (JS consumers depend on them); only NEW elements are styled here. **Old-WebKit-safe: NO conic-gradient anywhere in the bar** | -- | -- | **Chrome** -- M1 |
| `.top-bar-nav` / `.tb-tab` | nav `flex;align-items:center;gap:2px`; tab `inline-flex;height:30px;padding:0 13px;border:none;transparent;color:var(--hh-dim);13px;600;letter-spacing .2px;radius 9px;nowrap`; hover `color:var(--hh-ink);background:rgba(255,255,255,.06)`; `.tb-active{color:var(--hh-ink);background:rgba(0,230,195,.14);box-shadow:INSET 0 0 0 1px rgba(0,230,195,.34)}` -- **inset ring, not a border, so layout is identical between states** | hover; tb-active; `<=1100px` padding `0 9px`; `<=880px` tabs `[data-tab=earn]` + `[data-tab=hive]` `display:none` | GPU-only shared rule (L735-739) `transition:transform 140ms ease,box-shadow 140ms,background 140ms,color 140ms`. **NOT killed by the reduced-motion block (GAP)** | **Chrome** tabs -- M1 |

## B21. TOP BAR -- omnibox pill (L664-692)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.top-bar-omni` | **CONTRACT: it is a BUTTON, not a second input** -- it focuses the canonical hero command bar (rule e2: ONE omnibox, no parallel search). **Native parity must not create a second text field.** `inline-flex;gap:9px;height:34px;min-width:220px;max-width:360px;padding:0 14px;radius 30px;border:1px solid var(--hh-bord);background:rgba(255,255,255,.05);color:var(--hh-dim);13px;500;cursor:TEXT` (fakes an input affordance); hover `background .08;color:var(--hh-ink)`; `.mi{18px}` | hover; `<=1100px` min-width 120px | **NO transition declared even on gpu-hardware** (the L735 shared rule covers only orb/tab/avatar) -- hover is INSTANT | **Chrome** omnibox proxy -- M1 |
| `.tbo-kbd` | `margin-left:auto;11px;700;color:var(--hh-dim);border:1px solid var(--hh-bord);radius 6px;padding:1px 6px`; `display:none` @`<=1100px` | breakpoint | none | **Glyph**+**Text** -- M4 |

## B22. TOP BAR -- docked small orb (orb-sm, c7) (L694-710, 730-739)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.top-bar-orb` | `30x30;flex:0 0 auto;50%;border:none;padding:0`. **LAYERED BACKGROUND (2 stacked images, top layer first):** `radial-gradient(circle at 34% 30%,rgba(255,255,255,.60),transparent 45%)` [specular highlight] OVER `linear-gradient(135deg,var(--hh-teal),var(--hh-cyan) 40%,var(--hh-violet) 76%,var(--hh-magenta))` [**4-stop FULL-SPECTRUM sweep -- explicitly spectrum, not monochrome**]; `box-shadow:0 0 0 1px rgba(255,255,255,.10),0 2px 10px rgba(0,230,195,.30)` [hairline ring + teal glow]. **NO mic glyph inside (rule c5) -- the orb IS the control; clicking toggles voice, the SAME control as the big orb** | hover `scale(1.08)` (**declared UNGATED, so it fires on software too, but the smoothing transition is gpu-only -> software SNAPS, GPU eases**); gpu-hardware (breathing); reduced-motion | GPU-only `tbOrbBreathe 4s ease-in-out infinite`. **IMPORTANT: `tbOrbBreathe` uses a LITERAL 4s -- the ONE animation in the file NOT scaled by `--hart-motion-speed` and NOT gated by any `--hart-motion-*` layer var (a documented inconsistency vs the Aura Motion System)**; plus GPU-only `transition:transform/box-shadow/background/color 140ms ease` | **Orb** docked -- M3 |

## B23. TOP BAR -- user avatar (L712-728, 735-739)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.top-bar-avatar` | `30x30;flex:0 0 auto;50%;border:1px solid var(--hh-bord)`; `inline-flex center` [holds the user's INITIAL]; `color:var(--hh-ink);13px;800`; `background:linear-gradient(150deg,rgba(155,92,255,.55),rgba(41,197,255,.45))` [violet->cyan; **HARD-CODED rgba, does NOT track `--hh-violet`/`--hh-cyan` -- a themability GAP**]; hover `box-shadow:0 0 0 2px rgba(41,197,255,.4)` [cyan focus ring] | hover; **NOT killed by the reduced-motion block (GAP)** | GPU-only `transition:transform/box-shadow/background/color 140ms ease` | **Glyph**+**Text** avatar -- M4 |

## B24. DESKTOP ICONS -- image plate variant (L752-776)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.di-glyph.di-image` / `.di-photo` | **OWNERSHIP NOTE:** these live in `hartHome.css` (W1-owned) only because `hartResponsive.css` is owned elsewhere. Base `.desktop-icon` / `.di-glyph` / `.di-label` styling is NOT here. `hartDesktop.js` swaps `.di-glyph` to a photo plate when the manifest entry carries an `image`; falls back to the glyph automatically on load error; the app NAME (`.di-label`) is untouched. `.di-image{padding:0;overflow:hidden;background:#0E1320}` [same plate colour as `.hh-card`]; `.di-photo{100%;object-fit:cover;display:block;border-radius:INHERIT}` | default (glyph) / di-image (photo) / load-error fallback; **transitions NOT killed by the reduced-motion block (GAP)** | GPU-only `transition:transform 160ms ease,box-shadow 160ms ease`; GPU-only parent hover `translateY(-2px) scale(1.04);box-shadow:0 16px 34px rgba(0,0,0,.5)`. Software floor: plate renders, NO hover lift | **Card**+**Glyph** icon plate -- M2 |

## B25. A11Y -- `prefers-reduced-motion` kill switch (L545-560)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `@media(prefers-reduced-motion:reduce)` over 10 selectors | `transition:none !important;animation:none !important` on `.hart-home`, `body.gpu-hardware .hh-card`, `.hh-btn`, `.hh-card-live .hh-dot`, `.hh-pill-dot`, `.top-bar-orb`, `.hart-ambient`, `.hart-ambient::after`, `#hart-voice-orb`, `.hart-hero-aura`. Contract: **reduced-motion users NEVER animate regardless of GPU verdict** | reduce | kills all listed | **Chrome** motion gate -- M6 |
| **COVERAGE GAPS** | NOT listed: `body.gpu-hardware .hart-bloom-canvas` (**the PRIMARY aurora animator -- only the `.hart-ambient` FALLBACK is killed, so a reduced-motion user on GPU may still see canvas drift + hue-cycle**); `.hh-card-art img` opacity 300ms; `.tb-tab` / `.top-bar-avatar` / `.desktop-icon .di-image` transitions | -- | -- | **M6 -- fix in native, do not replicate the gap** |
| **absent a11y affordances** | NO `:focus`/`:focus-visible` rule anywhere; NO `forced-colors`; NO `prefers-contrast`; NO reduced-transparency query; NO outline styling; NO sr-only/visually-hidden helper. **Native parity must source keyboard-focus rings elsewhere (A44)** | -- | -- | **Chrome** -- M1 |

## B26. RESPONSIVE / BREAKPOINT MATRIX (L741-750, 778-789)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| FOUR breakpoints, TWO axes (no min-width, no container queries) | `@max-width:1400px` -> `.hh-amount` 88->70px, unit 26->22px, `:root{--hh-gutter:60px -> 40px}` (**a `:root` override inside a media query -- re-cascades to `.hh-hero` padding, `.hh-rows` padding-left, `.hh-row-head` padding-right**); `@max-width:1100px` -> omni min-width 220->120px, `.tbo-kbd` hidden, `.tb-tab` padding `0 13px -> 0 9px`; `@max-width:880px` -> `[data-tab=earn]` + `[data-tab=hive]` hidden; `@max-height:820px` -> `.hh-amount` 58px, `.hh-spark` 44->34px, `.hh-cta` margin-top 26->16px, `.hh-card` height 150->132px | <=1400 / <=1100 / <=880 / height <=820 | none | **Chrome** layout policy -- M1 |
| **CASCADE ORDER TRAP** | the 1400px block (L779) appears AFTER the 1100/880px blocks (L742/747); the height query (L784) is LAST, so `.hh-amount` 58px wins over 70px when both match | -- | -- | -- |
| INTENT | shrink the hero so the rows still fit ONE screen -- **the fixed-canvas rule is never traded away for scroll** | -- | -- | -- |

## B27. GPU-GATING CONTRACT (`body.gpu-hardware` / `body.gpu-software`)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| verdict source | the server stamps `body.gpu-software` / `body.gpu-hardware` from `/run/hart/gpu-render` (`liquid_ui_service.py:1927`) -- **the SAME verdict the GTK4 host reads for its GSK choice. Native parity needs the identical single source of truth** | gpu-hardware / gpu-software | -- | **Chrome** -- M6 |
| SOFTWARE IS THE SAFE DEFAULT (#137) | every base rule is flat: solid brand colours + STATIC gradients, NO blur, NO continuous animation; images lazy-load | software | -- | **Chrome** -- M6 |
| KEPT ON SOFTWARE | `.hh-card 0 16px 38px rgba(0,0,0,.46)`; `.hh-btn-primary 0 12px 30px rgba(0,230,195,.30)`; `.hh-amount 0 0 24px rgba(0,230,195,.35)`; `.hh-card-title` / `.hh-card-scrim` legibility gradients; `.hh-card-art img` 300ms opacity fade; `.top-bar-orb:hover scale(1.08)`; ALL hover COLOUR changes (`.tb-tab`, `.top-bar-omni`, `.hh-see-all`) | software | -- | **Card**/**Text** -- M6 |
| GPU-ONLY ADDITIONS (complete, 18 blocks) | `.hh-btn` backdrop blur(10px)+transition; `.hh-btn:hover translateY(-2px)`; `.hh-card` transition+will-change; `.hh-card:hover scale(1.07)`+triple shadow+z5; `.hh-card-scrim` lighter gradient; `.hh-amount` drop-shadow filter; `.hh-card-live .hh-dot` pulse; `.hh-pill-dot` pulse; `.hart-bloom-canvas vBlob1+vHue`; `.hart-ambient vBlob1`; `#hart-voice-orb vFloat+vBreathe`; `.hart-hero-aura vSpin`; `.top-bar-orb tbOrbBreathe`; `.top-bar-orb`/`.tb-tab`/`.top-bar-avatar` 140ms transitions; `.desktop-icon .di-image` transition; `.desktop-icon:hover .di-image` lift | gpu-hardware | -- | **M6** |
| file-wide effect budget | **the ONLY `backdrop-filter` in the entire file is `.hh-btn blur(10px)`**; the ONLY `filter` uses are `.hh-amount` drop-shadow and `@keyframes vHue` hue-rotate; **ZERO `mix-blend-mode` / `background-blend-mode` / `clip-path` / `mask`** | -- | -- | -- |

## B28. REQUESTED GROUPS WITH ZERO RULES IN `hartHome.css` (parity gaps to source elsewhere)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| TASKBAR | no selector; only reserved as `--hh-bottom-safe:56px`. Styling lives outside this file (A15) | -- | -- | **Chrome** -- M1 via A15 |
| START MENU | no selector, no reference of any kind | -- | -- | **Card** -- M5 via A14/C17 |
| PANELS | no selector; referenced only in comments ("deeper categories open as panels via See all") -- `.hh-see-all` is the trigger, the panel surface is styled elsewhere | -- | -- | **Card** -- M5 via A13/C6 |
| TOASTS / NOTIFICATIONS | no selector, no reference | -- | -- | **Card** -- M5 via A34 |
| SENSES CLUSTER | no selector, no reference. The mock's `vScan` / `vWave` / `vDash` / `vJoint` exist here as UNAPPLIED vocabulary explicitly "exposed for orb-character surfaces that mount those elements" -- **those are the likely senses-cluster hooks** | -- | dead vocabulary | **Chrome**/**Glyph** -- M1 via A26 |
| ONBOARDING | no selector, no reference | -- | -- | **Field** -- M5 via A33 |
| VIGNETTE / GRAIN / NOISE | no selector, no gradient, no noise texture, no radial vignette anywhere in this file | -- | -- | **Field** -- M1 via A7/A8/C3/C4 |
| PERSONALIZE PANEL | no selector. Present only as the CONTROL SURFACE contract -- "Personalize > Aura Motion" writes `--hart-motion-speed` and the four layer play-state vars via the same `:root`-var + `HartSession` mechanism the palette/feel controls already use. **Native parity must expose those five vars as live-writable** | -- | -- | **Chrome** -- M6 |
| ORB INTRINSICS | geometry, gradient, bloom, and the listening / thinking / speaking / idle character states are NOT here (`hartHero.js` + `voiceOrbViz.js` own them) | -- | -- | **Orb** -- M3 |
| INTERACTIVE STATES ABSENT ENTIRELY | `:focus`, `:focus-visible`, `:active`, `:disabled`, drag/dragover/dragging, docked, compact, merged, listening, thinking, speaking. **The ONLY interactive pseudo-class used is `:hover` (7 rules)**, plus `::-webkit-scrollbar` and `::after` | -- | -- | **M6 -- author fresh** |
| CLASS-BASED STATES PRESENT (complete, 12) | `.hh-ready`, `.hh-hidden`, `.hh-loaded`, `.hh-wide`, `.hh-portrait`, `.hh-square`, `.hh-ranked`, `.hh-card-empty`, `.tb-active`, `.di-image`, `.hh-accent-*` (6 variants), `body.gpu-hardware` | -- | -- | **M2/M6** |

## B-notes -- cross-cutting inventories for Part B

**Totals over the 790-line file:** 17 `@keyframes` (`hhLiveDot`, `tbOrbBreathe`, and the 15 mock-ported `v*` set) of which **9 have application rules and 8 are declared-but-unapplied vocabulary**; 4 `@media` queries (3 max-width, 1 max-height) plus 1 `prefers-reduced-motion`; 18 `body.gpu-hardware` rule blocks; 7 `:hover` rules; 2 pseudo-elements; 1 attribute-selector pair; 0 `@supports`, 0 `@import`, 0 `@font-face`.

**Transition inventory (complete, 8):** `.hart-home` opacity 360ms ease (ungated) \| `.hh-card-art img` opacity 300ms ease (ungated) \| `.hh-btn` transform+box-shadow 160ms (GPU) \| `.hh-card` transform+box-shadow 200ms (GPU) \| `.top-bar-orb`/`.tb-tab`/`.top-bar-avatar` transform+box-shadow+background+color 140ms (GPU) \| `.desktop-icon .di-image` transform+box-shadow 160ms (GPU). **EVERY easing in the file is `ease`, `ease-in-out`, or `linear` -- there is NOT ONE `cubic-bezier()` or custom spring curve. A native rewrite has no bespoke easing to port from this file.**

**Shadow inventory (12 box-shadows + 2 text-shadows + 1 filter drop-shadow):** card static `0 16px 38px rgba(0,0,0,.46)` \| card hover triple-stack `0 30px 70px rgba(0,0,0,.6)` + `0 0 0 2px var(--hh-acc)` + `0 0 46px rgba(0,230,195,.32)` \| btn-primary `0 12px 30px rgba(0,230,195,.30)` \| tb-active INSET `0 0 0 1px rgba(0,230,195,.34)` \| top-bar-orb `0 0 0 1px rgba(255,255,255,.10)` + `0 2px 10px rgba(0,230,195,.30)` \| tbOrbBreathe 50% `0 0 0 1px rgba(255,255,255,.16)` + `0 3px 16px rgba(41,197,255,.50)` \| avatar hover `0 0 0 2px rgba(41,197,255,.4)` \| desktop-icon hover `0 16px 34px rgba(0,0,0,.5)` \| text: `.hh-amount 0 0 24px rgba(0,230,195,.35)`, `.hh-card-title 0 1px 6px rgba(0,0,0,.55)` \| filter: `.hh-amount drop-shadow(0 0 26px rgba(0,230,195,.38))`.

**Gradient inventory (6):** `.hh-btn-primary` linear 135deg `#5CFFD9 -> var(--hh-teal)` \| `.hh-card-scrim` linear `transparent 32% -> rgba(4,7,13,.78)` \| GPU scrim override `transparent 28% -> rgba(4,7,13,.72)` \| `.top-bar-orb` radial `circle at 34% 30% rgba(255,255,255,.60) -> transparent 45%` \| `.top-bar-orb` linear 135deg 4-stop teal/cyan40%/violet76%/magenta \| `.top-bar-avatar` linear 150deg `rgba(155,92,255,.55) -> rgba(41,197,255,.45)`. **NO conic-gradient anywhere (deliberate: old-cage-WebKit safety). NO `background-clip:text` anywhere (deliberately removed -- fragile in old cage WebKit, rendered the number invisible).**

**Border-radius inventory:** `50%` (5 circles: `.hh-pill-dot`, `.hh-shield`, `.hh-card-live .hh-dot`, `.top-bar-orb`, `.top-bar-avatar`) \| `30px` pills (`.hh-pill`, `.hh-card-live`, `.top-bar-omni`) \| `var(--hart-radius,16px)` theme-driven (**`.hh-card` ONLY**) \| `16px` hard-coded (`.hh-rank-inner` -- does NOT follow the theme radius, an inconsistency vs `.hh-card`) \| `14px` (`.hh-btn`) \| `10px` (`.hh-card-ic`) \| `9px` (`.tb-tab`) \| `8px` (`.hh-card-badge`) \| `6px` (`.tbo-kbd`) \| `0 3px 0 0` (`.hh-card-prog`, top-right only) \| `inherit` (`.di-photo`).

**Hard-coded colours that bypass the theme (parity + themability gaps):** `#3B82F6` (`--hh-blue`), `#FFC83D` (`--hh-amber`), `#0E1320` (card + desktop-icon plate, x2), `#04140F` (ink on teal, x2), `#5CFFD9` (CTA gradient head), `#C3CDD9` (`.hh-hero-meta` + `.hh-stat`), `#CFE` (`.hh-card-meta`), `rgba(255,200,61,.12/.30)` (pill fill+border, an un-vared `--hh-amber`), `rgba(0,230,195,*)` teal literals in 6 shadow/glow declarations, `rgba(155,92,255,.55)`+`rgba(41,197,255,.45)` (avatar gradient), `rgba(41,197,255,.4/.50)`, `rgba(255,255,255,.03/.05/.06/.08/.10/.16/.30/.60)`, `rgba(8,12,20,.55/.72)`, `rgba(4,7,13,.72/.78)`, `rgba(0,0,0,.46/.5/.55/.6)`.

**Declaration-order traps:** (1) `.hh-btn` `border:none` then `border:1px solid var(--hh-bord)` -- the border wins. (2) `.hh-row-note` colour declared twice -- the accent version wins. (3) `.hh-card-prog` background declared twice -- the accent version wins. (4) `.hh-card-badge` and `.hh-card-live` share `top:12px right:12px` -- mutually exclusive per card. (5) `@max-height:820px` is LAST, so `.hh-amount` 58px beats the 1400px 70px.

**Parity risks flagged:** the reduced-motion block omits `.hart-bloom-canvas`, `.hh-card-art img`, `.tb-tab`, `.top-bar-avatar`, `.desktop-icon .di-image`. `tbOrbBreathe` ignores both `--hart-motion-speed` and every layer gate, so Personalize cannot slow or pause it. `--hart-anim-spin-rev-speed` / `-spinc` / `-blink` / `-wave` / `-dash` / `-joint` are dead tokens here. There is NO focus-visible styling anywhere.

---

# Part C -- `hartResponsive.css` (CINEMATIC v3 + software floor)

**Layering:** loads LAST, after the inline shell `<style>` (`_CSS_LIVING_GLASS`) and
`hartHome.css`/`hartHero.css`, **so it wins on source order.** Three strata:
(A) l.12-78 responsive media-query overrides only; (B) l.80-436 "DESIGN OVERHAUL v3
CINEMATIC (2026-06-28)" -- tokens, glass, Netflix hover-expand, entrance, reduced-motion;
(C) l.438-624 "SOFTWARE-RENDER PERFORMANCE FLOOR (#137)" -- `body.gpu-software` /
`body.webkit-flat` degradation. **Desktop >1024px baseline is intentionally untouched by
stratum A.**

## C1. Design tokens (`:root` palette + motion grammar)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `:root` typography + surface | `--hart-font-family: system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif` **`!important` (forced over the theme emitter)**; `--hart-font-mono:"JetBrains Mono",ui-monospace,"SFMono-Regular",monospace`; `--hart-background:#05060C` (deep blue-black; mockup canvas `#05070d`; supersedes `#07070B`); `--hart-text:#ECF1F4`; `--hart-muted:#8A93A6`; `--hart-surface:#14141F`; `--hart-surface-hover:#20212F` | static | none | **Chrome** token override -- M1/M6 |
| `:root` glass + geometry | `--hart-glass-rgb:18,19,28` (base triple, **ONE writer**); `--hart-glass-bg:rgba(var(--hart-glass-rgb,18,19,28),var(--hart-panel-opacity,0.56))` -- composed fill, **the opacity slider is the only multiplier**; `--hart-glass-border:rgba(255,255,255,.09)`; `--hart-blur:30px`; `--hart-saturation:165%`; `--hart-radius:20px` (drives the Radius slider) | static | none | **Card** glass material -- M1 |
| `:root` brand | `--hart-accent-rgb:0,230,195`; `--hart-a2:#9B5CFF` / `--hart-a2-rgb:155,92,255` (**teal LEADS, violet ACCENTS**); **HEVOLVE BRAND SPECTRUM, single source (6 hues x hex+rgb):** `--hv-teal #00E6C3 / 0,230,195`; `--hv-cyan #29C5FF / 41,197,255`; `--hv-blue #3B82F6 / 59,130,246`; `--hv-violet #9B5CFF / 155,92,255`; `--hv-magenta #FF2E9A / 255,46,154`; `--hv-amber #FFC83D / 255,200,61` | static | none | **Chrome** spectrum -- M1 |
| `:root` motion grammar | `--hv-focus: cubic-bezier(.2,.8,.2,1)` -- **the signature Netflix focus easing, used by EVERY transition in this file**; `--hv-lift:380ms` -- cinematic lift/reveal duration | static | none | **Chrome** motion tokens -- M1/M3 |

## C2. Ambient / bloom wallpaper (cinematic base field)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.wallpaper` (HARDWARE) | **7-layer stacked background, painted in order:** (1) `radial-gradient(46% 40% at 12% 6%,rgba(var(--hart-amb-1-rgb,0,230,195),.13),transparent 62%)` teal top-left; (2) `40% 38% at 88% 10%` amb-3 `41,197,255` `.11` cyan top-right; (3) `52% 50% at 94% 94%` amb-2 `155,92,255` `.15` violet bottom-right; (4) `44% 42% at 8% 92%` amb-4 `255,46,154` `.10` magenta bottom-left; (5) `34% 30% at 50% 40%` `--hv-blue-rgb 59,130,246` `.07` centre; (6) `130% 100% at 50% 128%` `rgba(10,12,26,.62)` bottom floor darkener; (7) `linear-gradient(158deg,#06070D 0%,#04050A 52%,#070610 100%)` base. Ambient hues are MOOD-RETINTABLE via `--hart-amb-{1..4}-rgb`; inline fallbacks reproduce teal/cyan/violet/magenta 1:1 when unset | default (hardware) | none | **Field** -- M1 |
| `body.gpu-software .wallpaper` | collapsed to a cheap **3-layer static bloom** (rasters once, no drift, no live blur): `radial-gradient(60% 50% at 14% 4%,rgba(var(--hart-amb-1-rgb,0,230,195),.10),transparent 60%)` + `radial-gradient(60% 55% at 92% 96%,rgba(var(--hart-amb-2-rgb,155,92,255),.12),transparent 62%)` + `linear-gradient(158deg,#06070D 0%,#04050A 60%,#070610 100%)` | gpu-software | none | **Field** floor -- M6 |
| **RULE** | **never a single-hue teal wash -- the spectrum must survive on both floors** | -- | -- | -- |

## C3. Vignette (`.wallpaper::after`)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.wallpaper::after` | `content:'';position:fixed;inset:0;z-index:1;pointer-events:none` (never blocks the desktop; sits above `.wallpaper` z0); `background:radial-gradient(135% 120% at 50% 34%,transparent 52%,rgba(0,0,0,.22) 82%,rgba(0,0,0,.46) 100%)` -- frames the screen, content-forward | `body.gpu-software` -> `display:none` (vignette dropped entirely) | none | **Field** -- M1/M6 |

## C4. Grain overlay

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `body.gpu-software .hart-grain` | the only rule here: `display:none` on the software floor -- the grain uses `mix-blend-mode:overlay`, a **PER-FRAME composite** (unlike the ambient gradients, which raster once), so it is the one ambient layer gutted on CPU. **The base rule (noise texture, blend mode, opacity) lives OUTSIDE this file (A7)** | gpu-software; hardware untouched here | none | **Field** -- M6 |

## C5. Frosted glass base (shared chrome mixin)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.glass,.panel,.top-bar,.hart-hero-bar,.start-menu,.ctx-menu,.agent-pill` | `border:1px solid var(--hart-glass-border)` hairline; `box-shadow:0 26px 76px rgba(0,0,0,.52)` [soft outer depth] + `inset 0 1px 0 rgba(255,255,255,.06)` [inner top highlight]; `backdrop-filter:blur(var(--hart-blur)) saturate(var(--hart-saturation))` = blur(30px) saturate(165%) + `-webkit-` (WebKitGTK prefix required) | see C-floor | none | **Card**/**Chrome** material -- M1 |
| radius group | `border-radius:var(--hart-radius,20px)` on `.glass/.panel/.start-menu/.ctx-menu/.agent-pill` ONLY. **RADIUS EXCLUSIONS (deliberate):** `.top-bar` excluded (fixed full-width banner, sets `border-radius:0` in the inline sheet; rounding would notch screen corners) and `.hart-hero-bar` excluded (keeps its own 32px pill) | -- | -- | **Chrome** -- M1 |

## C6. Panels / windows (open app windows)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.panel` | inherits the C5 glass mixin (hairline border, `0 26px 76px` + inset highlight, blur30/sat165, radius 20px) | -- | open animation is `hart-panel-in` (spring) defined INLINE in the shell, not here (A13) | **Card** -- M5 |
| `.panel:focus-within` | HARDWARE: `border-color:rgba(var(--hv-cyan-rgb),.30)`; `box-shadow:0 30px 86px rgba(0,0,0,.58), inset 0 1px 0 rgba(255,255,255,.07), 0 0 36px rgba(var(--hv-cyan-rgb),.12)`. **EXPLICIT NON-EFFECT: panels NEVER transform on focus -- they are user-dragged, so only shadow/border "breathe"**. SOFTWARE floor: flattened to a brand ring `border-color:rgba(var(--hv-cyan-rgb),.34);box-shadow:0 2px 12px rgba(0,0,0,.40)` (big blurred glow removed) | focus-within (hardware / software) | none | **Card** focus -- M5/M6 |
| responsive | `<=1024px` `max-width:100vw` (keeps inline-pixel windows from overflowing); `<=720px` fullscreen sheet, **all `!important` to beat `openPanel`'s inline left/top/width/height**: `left:0;top:var(--hart-topbar-height,40px);width:100vw;height:calc(100vh - var(--hart-topbar-height,40px) - 44px);min-width:0;border-radius:0`; `.panel-resize{display:none!important}`. **Shrinking the panel container on phone also trips the file-explorer's own ResizeObserver into its narrow drawer layout (a behavioural side-effect of the CSS)** | <=1024px / <=720px | none | **Card** -- M5 |

## C7. Desktop icon -- wrapper (app rail tile)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.desktop-icon` | `transition:background .18s var(--hv-focus)` -- **background ONLY**; `will-change:transform` (GPU-promote for a composited drag). **CRITICAL CHANNEL RULE: the wrapper's transform channel is OWNED by the drag handler** (`hartDesktop.js` writes `el.style.transform=translate(dx,dy)` every frame, clears on drop). A `transition:transform` here would interpolate every drag frame (lag) and overshoot on release -- **the visible LIFT is therefore animated on the inner `.di-glyph` instead** | hover/focus-within -> `z-index:40`; dragging -> `z-index:60` (drag always wins over hover lift) | `hv-rise` entrance via `.hart-desktop .desktop-icon` | **Card** tile -- M2 |
| `.desktop-icon.selected` | overrides an off-brand flat-purple selection, all `!important`: `background:rgba(var(--hart-accent-rgb),.16);outline:1px solid rgba(var(--hart-accent-rgb),.50);box-shadow:0 10px 32px rgba(var(--hart-accent-rgb),.28)` | selected | -- | **Card** -- M2 |
| floors + phone | `body.gpu-software`/`body.webkit-flat` -> `will-change:auto` (de-promote; the wrapper still carries NO transition so the inline drag translate stays 1:1); `<=720px` -> `position:static!important;left/top:auto!important;flex:0 0 auto;width:66px` (free-floating icons become dock items) | gpu-software / webkit-flat / <=720px | -- | **Card** -- M6 |

## C8. Desktop icon -- glyph plate (`.di-glyph`) + spectrum sheen

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.di-glyph` | `position:relative;border:1px solid var(--hart-glass-border);box-shadow:inset 0 1px 0 rgba(255,255,255,.12) + 0 10px 24px rgba(0,0,0,.34);border-radius:16px` (**LITERAL, not `--hart-radius`**); `overflow:hidden` | hover/focus-within lift | `transition:transform var(--hv-lift) var(--hv-focus),box-shadow var(--hv-lift) var(--hv-focus)` -> 380ms both | **Card** plate -- M2 |
| `.di-glyph::after` (sheen) | `content:'';absolute;inset:0;pointer-events:none;z-index:0;border-radius:inherit;mix-blend-mode:SCREEN;opacity:.55`. Layers a brand-hue wash ON TOP of the per-app `style.background` `hartDesktop.js` writes -- **the app's manifest colour stays visible underneath** | hover/focus -> opacity `.55 -> .85` | `transition:opacity var(--hv-lift) var(--hv-focus)` | **Field**/**Card** sheen -- M2 |
| stacking + lift | `.mi` and `.di-emoji` get `position:relative;z-index:1` so glyph + label ride ABOVE the sheen. HOVER/FOCUS LIFT: `transform:translateY(-5px) scale(1.05)` | hover / focus-within | 380ms | **Glyph** -- M2 |
| **6-HUE CYCLE** | `nth-child(6n+N) .di-glyph::after background = linear-gradient(150deg,rgba(<hue>,.40\|.42),transparent 72%)`: 6n+1 teal .40 \| 6n+2 cyan .40 \| 6n+3 blue .40 \| 6n+4 violet .42 \| 6n+5 magenta .40 \| 6n+6 amber .40. **Matching hover/focus glow** `box-shadow:inset 0 1px 0 rgba(255,255,255,.14),0 14px 34px rgba(<hue>,A)`: teal .36 \| cyan .36 \| blue .36 \| violet .40 \| magenta .38 \| amber .36 | nth-child(6n+1..6) | -- | **Card**/**Field** hue rotation -- M2 |
| `.di-glyph .mi` | `filter:drop-shadow(0 0 9px rgba(var(--hart-accent-rgb),.40))` | software/webkit-flat -> `filter:none` (per-element drop-shadow is a real per-frame filter cost) | -- | **Glyph** -- M2/M6 |
| floors + reduced-motion | software/webkit-flat: plate + sheen transition forced to `transform 140ms var(--hv-focus),opacity 140ms var(--hv-focus)!important`; `will-change:auto` -- **the LIFT SURVIVES on CPU, just short and transform/opacity only; the box-shadow transition is dropped so the coloured glow SNAPS on in a single raster**. reduced-motion / `html.a11y-rmotion` -> `transform:none` | gpu-software / webkit-flat / reduce | 140ms floor | **Card** -- M6 |

## C9. Desktop container / app dock (`.hart-desktop`)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `@media(max-width:720px) .hart-desktop` | free-floating desktop icons become a scrollable bottom app dock (no hero overlap): `pointer-events:auto;top:auto!important;bottom:44px;height:90px;flex row;align-items:center;gap:6px;padding:4px 10px;overflow-x:auto;overflow-y:hidden;z-index:25`. Desktop (>720px) layout is free-positioned/absolute and lives in the inline shell style -- untouched here | <=720px dock mode | `hv-rise` staggered entrance on child `.desktop-icon` | **Row** dock -- M2 |

## C10. Hero / command center (orb wrapper + status)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `@media(max-width:720px)` | `.hart-hero{top:38%;width:94vw}` (hero lifted, near-full width); `.hart-hero-orbwrap{184x184}` (smaller orb); `.hart-hero-status{font-size:13px}` | <=720px | none | **Chrome**+**Orb** -- M3 |
| `@media(min-width:2200px)` | `.hart-hero{top:44%}` -- stops the command center stranding dead-centre; `.hart-hero-orbwrap{340x340}` large orb | >=2200px | none | **Chrome**+**Orb** -- M3 |
| `.hart-hero-title` | joins the display-font group (C-typography) | -- | none | **Text** -- M4 |
| **NOTE** | desktop/default hero geometry, the orb canvas render, and the orb's listening/thinking/speaking states are NOT in this file | -- | -- | -- |

## C11. Voice orb + hero aura (motion-shed only)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `body.gpu-software\|.webkit-flat #hart-voice-orb`, `… .hart-hero-aura` | `animation:none !important` -- the AURA MOTION SYSTEM layers (orb breathe/float, orbital ring spin, hue drift) are shed wherever compositing is off. **Explicit design note in-file:** the orb CANVAS is NOT animated by CSS at all (Stream B / `hartHero.js` owns it); this file only makes the surrounding ambient/space cinematic. **Colour + depth (bloom) already raster once and are KEPT -- only per-frame drift/spin/breathe is dropped** | gpu-software / webkit-flat | all inherited orb/aura keyframes forcibly disabled | **Orb**+**Ring** -- M6 |
| **PARITY WARNING** | every POSITIVE orb rule (breathe keyframes, float, orbital ring, bloom, listening/thinking/speaking state classes) lives in `hartHome.css` / `hartHero.js` / `voiceOrbViz.js` -- **NOT here. Native parity for the orb cannot be derived from this file** | -- | -- | **Orb** -- M3 |

## C12. Ambient layer (`.hart-ambient`) -- degrade-gracefully floor

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `body.gpu-software .hart-ambient` | `animation:none!important;filter:blur(28px) saturate(135%);opacity:.42` -- **KEEPS the ambient glow** (multi-hue radials raster once and composite cheaply) but drops the drift animation and shrinks the live blur from 64px to a one-time 28px. A second, broader rule covers `body.webkit-flat` too | gpu-software (static, blur28/sat135/opacity .42); webkit-flat (animation killed) | drift disabled | **Field** -- M6 |
| `… .hart-ambient::after` (hue ghost) | `animation:none!important` AND `display:none` -- removed outright on both floors | both floors | -- | **Field** -- M6 |
| rationale + warning | recorded in-file: the 3 ambient cinematic glows are the biggest "looks rich vs looks cheap" lever, so they are made STATIC rather than gutted. **The ambient's own gradients, 64px blur, opacity and drift keyframes are defined in `hartHome.css`, not here** | -- | -- | -- |

## C13. Command bar / omnibox (`.hart-hero-bar`)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-hero-bar` | part of the C5 glass mixin group (hairline border + blur30/sat165 + inset highlight) but **EXCLUDED from the `--hart-radius` group**: `border-radius:32px` -- its own premium frosted PILL; `box-shadow:0 20px 56px rgba(0,0,0,.46), inset 0 1px 0 rgba(255,255,255,.07)` | -- | none | **Chrome** omnibox -- M1 |
| `:focus-within` (hardware) | `border-color:rgba(var(--hart-accent-rgb),.55)`; `box-shadow:0 0 0 1px rgba(var(--hart-accent-rgb),.40)` [1px accent ring] + `0 0 30px rgba(var(--hart-accent-rgb),.26)` [accent glow] + `0 20px 56px rgba(0,0,0,.46)` [depth retained] | focus-within | none | **Chrome** -- M1 |
| software floor | **keystroke hot path** -- a blurred backdrop re-composites under the caret on every character: `backdrop-filter:none!important;box-shadow:none`; opaque brand-gradient fill (see the glass-kill block); focus-within flattened to `border-color:rgba(var(--hart-accent-rgb),.55);box-shadow:0 0 0 1px rgba(var(--hart-accent-rgb),.45)` -- static ring only, no glow blur, no animation. **Target: keystroke-to-glyph < 1 frame** | gpu-software / webkit-flat | none | **Chrome** -- M6 |
| responsive | `<=720px` -> `width:100%` | <=720px | none | **Chrome** -- M1 |

## C14. Send button (`.hart-hero-go`) -- living accent

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-hero-go` | `background:linear-gradient(135deg,var(--hv-teal),var(--hv-cyan))!important` -- spectrum sheen; `box-shadow:0 0 20px rgba(var(--hart-accent-rgb),.48)` -- static accent glow; hover `filter:brightness(1.12) saturate(1.1)` | hover; `prefers-reduced-motion` and `html.a11y-rmotion` -> `animation:none`; `body.gpu-software` -> `animation:none!important` (continuous repaint forever on CPU) | `hv-accent-breathe 3.6s var(--lg-breathe,ease-in-out) infinite` -- **the LIVING pulse (filter/opacity/shadow only; never the orb)** | **Glyph** action -- M3 |

## C15. Brand wordmark (`.hart-hero-brand img`)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-hero-brand img` | `filter:drop-shadow(0 3px 14px rgba(var(--hv-cyan-rgb),.42))` -- spectrum-tinted logo glow | static | none | **Glyph** -- M4 |

## C16. Marketplace app cards (`.hart-app-card`)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-app-card` | `transition:transform var(--hv-lift) var(--hv-focus),box-shadow var(--hv-lift) var(--hv-focus),border-color .2s var(--hv-focus)` -> 380/380/200ms; `will-change:transform` | -- | 380ms `cubic-bezier(.2,.8,.2,1)` | **Card** -- M2 |
| `:hover, :focus-within` | **Netflix focus expand: `transform:translateY(-8px) scale(1.025)` -- the LARGEST lift in the file**; `border-color:rgba(var(--hv-cyan-rgb),.55)`; `box-shadow:inset 0 1px 0 0 rgba(255,255,255,.12), 0 22px 54px rgba(0,0,0,.46), 0 0 40px rgba(var(--hv-cyan-rgb),.18)`; `z-index:2` | hover / focus-within | as above | **Card** -- M2 |
| floors + reduced-motion | software/webkit-flat: transition forced to `transform 140ms + opacity 140ms var(--hv-focus)!important`; `will-change:auto` -- **lift KEPT, box-shadow transition dropped (glow snaps)**. reduced-motion (both triggers): `transform:none` on hover/focus-within -- collapses to a still highlight, border/shadow still apply | gpu-software / webkit-flat / reduce / a11y-rmotion | 140ms floor | **Card** -- M6 |

## C17. Start menu + start items

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.start-menu` | in BOTH the C5 glass mixin group AND the `--hart-radius` group (`border-radius:var(--hart-radius,20px)`) | `<=1024px` `width:min(720px,94vw)!important`; `<=720px` near-fullscreen launcher `width:96vw!important;left:2vw!important;max-height:calc(100vh - var(--hart-topbar-height,40px) - 60px)!important`; software floor -> opaque brand-gradient glass-kill | none | **Card** -- M5 |
| `.start-item` | `transition:background var(--t-fast,.18s) var(--hv-focus),transform var(--hv-lift) var(--hv-focus),box-shadow var(--hv-lift) var(--hv-focus)`; hover/focus-within `translateY(-3px) scale(1.04)`; `box-shadow:0 12px 30px rgba(0,0,0,.40), 0 0 22px rgba(var(--hv-violet-rgb),.20)` -- **VIOLET glow, deliberately distinct from the cards' cyan** | hover / focus-within; software floor `animation:none!important` + transition forced to transform/opacity 140ms + `will-change:auto`; reduced-motion -> `animation:none` and hover `transform:none` | `hv-rise var(--t-reveal,320ms) var(--hv-focus) backwards` with a **stagger cascade 20/44/68/92/116ms for children 1-5 and 140ms for `nth-child(n+6)`** | **Row**/grid item -- M2/M5 |
| `.start-btn span` | `font-weight:600;letter-spacing:.02em` -- brand text in the UI font; the logo keeps its accent glow from the inline base | -- | none | **Text** -- M4 |

## C18. Gallery tiles (`.hart-tile`) -- wallpaper / theme picker (Personalize)

| CSS surface | effects | states | animations | native SceneNode mapping |
|---|---|---|---|---|
| `.hart-tile:hover` (+ Personalize picker surfaces) | the CINEMATIC layer re-skins the Personalize gallery tiles on top of the inline base (A31): the same `--hv-focus` / `--hv-lift` grammar as the app cards and start items -- hover lift + spectrum-tinted ring, dropped to a 140ms transform/opacity transition on `body.gpu-software` / `body.webkit-flat`, and `transform:none` under `prefers-reduced-motion` / `html.a11y-rmotion`. **INVENTORY TRUNCATION NOTICE: the source inventory for this component was cut mid-record. Before M2 sign-off this component MUST be re-inventoried directly from `hartResponsive.css` (the `.hart-tile` block onward through EOF, l.~436-624) and this row replaced with the exhaustive listing.** No other component in this ledger carries an incomplete record | hover; gpu-software; webkit-flat; reduced-motion | `--hv-lift` 380ms `--hv-focus` (hardware) / 140ms (floor) | **Card** tile -- M2 (**RE-INVENTORY REQUIRED**) |

## C-notes -- cross-cutting rules for Part C

- **Source order is the mechanism.** This file wins by loading last. A native port must
  fold C's overrides into the base values rather than keeping both, and must record which
  base value each override replaced (`--hart-background` `#07070B` -> `#05060C`,
  `--hart-blur` 20px -> 30px, `--hart-radius` 16px -> 20px, `--hart-saturation` 180% ->
  165%, `--hart-font-family` monospace -> system-ui `!important`).
- **One easing to rule them all:** `--hv-focus: cubic-bezier(.2,.8,.2,1)` plus
  `--hv-lift: 380ms`. Every cinematic transition in stratum B uses that pair; the software
  floor collapses all of them to 140ms transform/opacity.
- **Lift-channel discipline:** wrapper elements that a JS drag handler writes
  `transform` on must NOT carry `transition:transform`; animate an inner plate instead
  (C7/C8). This is a hard native rule, not a CSS quirk.
- **Floor policy:** keep one-time rasters (static shadows, static glows, legibility
  gradients, lazy-load fades, hover COLOUR changes); shed per-frame work (live blur,
  drift, spin, breathe, blend modes, box-shadow transitions).

---

# Appendix K -- keyframes -> native animation primitive

Every `@keyframes` in all three sources, including the ones that are declared but never
applied. **A dead keyframe still gets a row** -- the native side decides explicitly to
implement or drop it; it may not simply go missing.

Native primitive vocabulary: **T-track** = transform track (translate / scale / rotate),
**O-track** = opacity track, **F-track** = filter track, **S-track** = shadow/ring morph
track, **W-track** = size/width track, **UV-track** = texture-offset (background-position)
track, **D-track** = stroke-dashoffset track.

## K.A -- inline shell (`liquid_ui_service.py`), 23 keyframes

| # | `@keyframes` | definition | applied to | native primitive |
|---|---|---|---|---|
| K1 | `slideInRight` (l.1580) | `from{translateX(100%);opacity:0} to{translateX(0);opacity:1}` | `.toast` `.3s ease-out`; **NOT emitted under potato** | T-track + O-track, enter |
| K2 | `fadeOutToast` (l.1581) | `to{opacity:0;transform:translateX(30px)}` | `.toast` `.3s ease-in 4.7s forwards`; **NOT emitted under potato** | T-track + O-track, exit + delay |
| K3 | `pulse` (l.1582) | `0%,100%{opacity:1} 50%{opacity:.5}` | `.mic-btn.recording 1s infinite`; **NOT emitted under potato** | O-track, ping-pong |
| K4 | `fadeIn` (l.1584) | `from{opacity:0;scale(.95) translateY(10px)} to{opacity:1;scale(1) translateY(0)}` | `.panel var(--hart-anim-speed) ease-out` -- **OVERRIDDEN by `hart-panel-in`** | T+O enter (dead in practice) |
| K5 | `ds-ripple-anim` (l.1716) | `to{transform:scale(2.5);opacity:0}` | `.ds-ripple 500ms ease-out forwards` | T-track scale + O-track, one-shot |
| K6 | `ds-shimmer` (l.1809) | `0%{background-position:200% 0} 100%{-200% 0}` | `.ds-skeleton 1.5s ease-in-out infinite`; potato -> none | **UV-track** on a 200%-wide gradient texture |
| K7 | `ds-toast-in` (l.1843) | `from{translateX(100%) scale(.95);opacity:0} to{translateX(0) scale(1);opacity:1}` | `.ds-toast var(--ds-duration-long) var(--ds-ease-spring)` | T+O enter, spring |
| K8 | `ds-toast-out` (l.1844) | `to{translateX(30px);opacity:0}` | `.ds-toast-exit var(--ds-duration-medium) var(--ds-ease-accelerate) forwards` | T+O exit |
| K9 | `ds-toast-countdown` (l.1845) | `from{width:100%} to{width:0%}` | `.ds-toast-progress 5s linear forwards`; potato -> none | **W-track**, linear |
| K10 | `ds-content-enter` (l.1893) | `from{opacity:0;translateY(8px)} to{opacity:1;translateY(0)}` | `.ds-fade-in` + `.ds-stagger>*` `var(--ds-duration-medium) var(--ds-ease-decelerate)` with a 0-100ms delay ladder | T+O enter + **stagger index** |
| K11 | `hart-ambient-drift` (l.1996) | `0%{translate3d(0,0,0) scale(1)} 50%{translate3d(2.4%,-2.2%,0) scale(1.08)} 100%{translate3d(-2.4%,2.2%,0) scale(1.05)}` | **DEFINED BUT NEVER APPLIED** -- the live-drift ambient was removed 2026-07-19 | T-track -- **DEAD; do not implement without a steward decision** |
| K12 | `hart-orbit-spin` (l.2050) | `from{rotate(0)} to{rotate(360deg)}` | `.hart-orb-orbit 26s linear infinite`; `.hart-orb-orbit2 38s linear infinite reverse` | T-track rotation, linear, two instances with opposite direction |
| K13 | `hart-hevolve-pulse` (l.2074) | `0%,100%{scale(.7);opacity:.5} 50%{scale(1.15);opacity:1}` | `.hart-hero-hevolve .dot 1s ease-in-out infinite` | T+O ping-pong |
| K14 | `hart-panel-in` (l.2223) | `from{opacity:0;scale(.92) translateY(14px)} to{opacity:1;scale(1) translateY(0)}` | `.panel .3s cubic-bezier(.175,.885,.32,1.275)` -- **THE EFFECTIVE window-open animation** | T+O enter, spring |
| K15 | `hob-breathe` (l.2291) | `0%,100%{scale(1);opacity:.85} 50%{scale(1.08);opacity:1}` | onboarding `.hob-orb 3.2s ease-in-out infinite` | T+O breathe |
| K16 | `lg-breathe-ring` (l.2384) | `0%,100%{scale(1);opacity:.85} 50%{scale(1.03);opacity:1}` | `orbwrap[data-orb-state=listening]::after 2.2s var(--lg-breathe) infinite` | **Ring** T+O breathe |
| K17 | `lg-comet` (l.2393) | `to{rotate(360deg)}` | `orbwrap[data-orb-state=thinking]::before 1.4s linear infinite` (masked conic sweep) | T-track rotation on a **masked conic ring node** |
| K18 | `lg-ripple` (l.2396) | `from{scale(.2);opacity:.7} to{scale(2.4);opacity:0}` | `.lg-orb-ripple .45s var(--lg-exit) forwards` | T+O one-shot ripple |
| K19 | `lg-settle` (l.2407) | `0%{scale(1.06)} 100%{scale(1)}` | `.hart-senses.settle .hart-senses-cluster .34s var(--lg-spring)` | T-track settle, spring |
| K20 | `lg-pulse` (l.2421) | `0%,100%{opacity:.7} 50%{opacity:1}` | `.hart-senses-btn.is-sensing .mi 2.4s var(--lg-breathe) infinite` | O-track breathe |
| K21 | `lg-empty-in` (l.2501) | `from{opacity:0;translateY(8px)} to{opacity:1;transform:none}` | `.lg-empty var(--t-reveal) var(--lg-enter)` | T+O enter |
| K22 | `lg-empty-breathe` (l.2505) | `0%,100%{opacity:.6;scale(1)} 50%{opacity:1;scale(1.06)}` | `.lg-empty-offline .lg-empty-disc .mi 3s var(--lg-breathe) infinite` | T+O breathe |
| K23 | `hart-fade-in` (l.2778) | `from{opacity:0} to{opacity:1}` | `.panel .18s ease` -- **OVERRIDDEN by `hart-panel-in`** | O-track (dead in practice) |

## K.B -- `hartHome.css`, 17 keyframes (9 applied, 8 dead vocabulary)

| # | `@keyframes` | definition | applied to | native primitive |
|---|---|---|---|---|
| K24 | `hhLiveDot` (L527) | `0%,100%{opacity:1} 50%{opacity:.4}` | `body.gpu-hardware .hh-card-live .hh-dot` @ `calc(1.6s * speed)`; `.hh-pill-dot` @ `calc(1.8s * speed)`; both play-state-gated by `--hart-motion-detail`. **Base durations are LITERALS, not `--hart-anim-*` vars** | O-track breathe, LAYER-4 gated |
| K25 | `vBlob1` (L575) | `0%,100%{translate(0,0) scale(1)} 50%{translate(60px,-40px) scale(1.15)}` | `.hart-bloom-canvas` + `.hart-ambient` @ `calc(18s * speed)` ease-in-out infinite, LAYER 1. **Scale >= translate so the full-bleed canvas never reveals an edge** | T-track on a **composited texture** (never a re-blur) |
| K26 | `vBlob2` (L576) | `50%{translate(-70px,50px) scale(1.1)}` | **DECLARED, NOT APPLIED** | T-track -- dead vocabulary |
| K27 | `vBlob3` (L577) | `50%{translate(40px,60px) scale(1.2)}` | **DECLARED, NOT APPLIED** | T-track -- dead vocabulary |
| K28 | `vHue` (L578) | `0%,100%{filter:hue-rotate(0deg)} 50%{filter:hue-rotate(var(--hart-anim-hue-amt,28deg))}` | `.hart-bloom-canvas` @ `calc(22s * speed)`, LAYER 1. **Amplitude read from a var INSIDE the keyframe** | **F-track** hue-rotate, parameterised amplitude |
| K29 | `vBreathe` (L579) | `50%{transform:scale(var(--hart-anim-breathe-amt,1.08))}` | `#hart-voice-orb` @ `calc(4s * speed)`, LAYER 2 | T-track scale, parameterised amplitude |
| K30 | `vFloat` (L580) | `50%{transform:translateY(var(--hart-anim-float-amt,-10px))}` | `#hart-voice-orb` @ `calc(9s * speed)`, LAYER 2. **Runs SIMULTANEOUSLY with `vBreathe` on the same element -- two transform animations composed by the engine** | T-track translate; native needs **additive transform composition** |
| K31 | `vRing` (L581) | `0%{translate(-50%,-50%) scale(.55);opacity:.8} 100%{translate(-50%,-50%) scale(1.7);opacity:0}` | **DECLARED, NOT APPLIED**; exposed for orb-character surfaces (ping/pulse ring) | **Ring** T+O -- dead vocabulary |
| K32 | `vScan` (L582) | `0%{translateY(-20%);opacity:0} 12%{opacity:.9} 88%{opacity:.9} 100%{translateY(430%);opacity:0}` | **DECLARED, NOT APPLIED**; 4-stop scanline sweep with fade shoulders | T+O multi-stop -- dead vocabulary |
| K33 | `vSpin` (L583) | `to{transform:rotate(360deg)}` | `.hart-hero-aura` @ `calc(60s * speed)` **LINEAR** infinite, LAYER 3 | T-track rotation, continuous orbit |
| K34 | `vSpinRev` (L584) | `to{transform:rotate(-360deg)}` | **DECLARED with `--hart-anim-spin-rev-speed:90s`, NOT APPLIED** | T-track -- dead vocabulary |
| K35 | `vSpinC` (L585) | `to{transform:translate(-50%,-50%) rotate(360deg)}` | centre-anchored spin; **DECLARED with `--hart-anim-spinc-speed:26s`, NOT APPLIED** | T-track with origin offset -- dead |
| K36 | `vBlink` (L586) | `0%,48%{opacity:1} 50%,100%{opacity:0}` | hard-edged square-wave blink (no easing between stops); **NOT APPLIED**, LAYER-4 vocabulary | O-track step -- dead |
| K37 | `vWave` (L587) | `0%,100%{scaleY(.25)} 50%{scaleY(1)}` | audio-bar wave; **NOT APPLIED**, LAYER-4 vocabulary | T-track scaleY -- dead |
| K38 | `vDash` (L588) | `to{stroke-dashoffset:-72}` | SVG marching-ants; **NOT APPLIED**, LAYER-4 vocabulary | **D-track** -- needs an SVG stroke-dasharray host |
| K39 | `vJoint` (L589) | `0%,100%{opacity:.5} 50%{opacity:1}` | **NOT APPLIED**, LAYER-4 vocabulary | O-track -- dead |
| K40 | `tbOrbBreathe` (L730) | `0%,100%{box-shadow:0 0 0 1px rgba(255,255,255,.10),0 2px 10px rgba(0,230,195,.30)} 50%{0 0 0 1px rgba(255,255,255,.16),0 3px 16px rgba(41,197,255,.50)}` | `body.gpu-hardware .top-bar-orb` @ `4s ease-in-out infinite`. **BOX-SHADOW animation (not transform), morphing a teal->cyan glow and widening the hairline ring. THE ONE ANIMATION NOT SCALED BY `--hart-motion-speed` AND NOT GATED BY ANY LAYER VAR** | **S-track** ring/glow morph -- native must interpolate two shadow stacks |

## K.C -- `hartResponsive.css`, 2 keyframes

| # | `@keyframes` | definition | applied to | native primitive |
|---|---|---|---|---|
| K41 | `hv-rise` | entrance rise (opacity + translateY), `backwards` fill | `.hart-desktop .desktop-icon`; `.start-menu .start-item` @ `var(--t-reveal,320ms) var(--hv-focus) backwards`, **stagger 20/44/68/92/116ms (children 1-5) and 140ms (`nth-child(n+6)`)** | T+O enter + stagger index, `--hv-focus` easing |
| K42 | `hv-accent-breathe` | living accent pulse (filter / opacity / shadow only -- **never the orb**) | `.hart-hero-go` @ `3.6s var(--lg-breathe,ease-in-out) infinite`; killed by reduced-motion, `html.a11y-rmotion`, and `body.gpu-software` | F/O/S-track breathe |

**Keyframe totals:** 23 (A) + 17 (B) + 2 (C) = **42 keyframes**.

- **Applied: 31** -- 22 in A, 7 in B (`hhLiveDot`, `vBlob1`, `vHue`, `vBreathe`, `vFloat`,
  `vSpin`, `tbOrbBreathe`), 2 in C.
- **Declared but never applied: 11** -- `hart-ambient-drift` (A, leftover from the removed
  live-drift ambient) plus the ten file-B vocabulary frames `vBlob2`, `vBlob3`, `vRing`,
  `vScan`, `vSpinRev`, `vSpinC`, `vBlink`, `vWave`, `vDash`, `vJoint`. (The file-B header
  comment says "8 of 16" -- the per-rule audit shows **10**, because `vRing` and `vScan`
  are also unapplied. Trust the per-rule audit.)
- **Applied but always overridden: 2** -- `fadeIn` and `hart-fade-in`, both beaten by
  `hart-panel-in` on `.panel`.
- **Suppressed at emit time under potato: 3** -- `slideInRight`, `fadeOutToast`, `pulse`
  are not written into the served sheet at all.

---

# Appendix P -- palette / token vars -> theme JSON key

Six token families across the three sources. **Only family P1+P2 (`--hart-*`) is
theme-driven and live-swappable**; P3 (`--ds-*`) and P4 (`--lg-*`/`--t-*`) are static in
the served HTML; P5 (`--hh-*`) and P6 (`--hv-*`) are file-local aliases that mostly
*re-export* P1 values with hard-coded fallbacks.

> **Verification note on the "theme JSON key" column.** The keys below are the
> name-derived mapping implied by `ThemeService.get_css_variables()` (token
> `--hart-<section>-<name>` <- theme dict `<section>.<name>`). Before any native code
> generation reads a theme preset, this column MUST be diffed against
> `theme_service.py` / the shipped preset JSON. Rows marked *(derived)* are inferred from
> the token name, not read out of the emitter.

## P1 -- `--hart-*` theme tokens (42) -- LIVE-SWAPPABLE

| var | fallback (l.1406) / file-C override | theme JSON key | notes |
|---|---|---|---|
| `--hart-background` | `#0F0E17` -> C: `#05060C` | `colors.background` *(derived)* | lock screen base, document canvas |
| `--hart-accent` | `#00D4AA` | `colors.accent` | THE functional signifier colour |
| `--hart-accent-rgb` | `0,212,170` -> C: `0,230,195` | derived from `colors.accent` by `accent_rgb_css` (l.1533) | numeric triple for every `rgba(var(--hart-accent-rgb),A)` |
| `--hart-on-accent` | `#0F0E17` | `colors.on_accent` *(derived)* | ink on accent fills |
| `--hart-active` | `#00e676` | `colors.active` *(derived)* | live/success signifier |
| `--hart-text` | `#e0e0e0` -> C: `#ECF1F4` | `colors.text` | body ink |
| `--hart-heading` | `#00D4AA` | `colors.heading` *(derived)* | headings, card names |
| `--hart-muted` | `#78909c` -> C: `#8A93A6`; a11y-contrast `#e8eef2` | `colors.muted` | secondary ink, scrollbar thumb |
| `--hart-error` | `#FF6B6B` | `colors.error` *(derived)* | close hover, danger btn, off-state |
| `--hart-caution` | `#ffab40` | `colors.caution` *(derived)* | warning chip dot |
| `--hart-surface` | `#1a1a2e` -> C: `#14141F` | `colors.surface` | opaque plates, list items, potato fallbacks |
| `--hart-surface-hover` | `#252540` -> C: `#20212F` | `colors.surface_hover` *(derived)* | hover fill for chrome controls |
| `--hart-glass-bg` | `rgba(15,14,23,.65)` -> C: `rgba(var(--hart-glass-rgb),var(--hart-panel-opacity))` | composed, not authored | **one composed writer -- do not author a second** |
| `--hart-glass-rgb` | C: `18,19,28` | `glass.rgb` *(derived)* | base triple for the composed fill |
| `--hart-glass-border` | `rgba(0,212,170,.18)` -> C: `rgba(255,255,255,.09)`; a11y-contrast `#ffffff` | `glass.border` *(derived)* | every hairline in the shell |
| `--hart-panel-opacity` | `0.65` -> C default `0.56` | `glass.panel_opacity` *(derived)* | **the only multiplier on the glass fill** (Personalize slider) |
| `--hart-blur` | `20px` -> C: `30px` | `effects.blur` *(derived)* | `.glass` + `.hart-widget` backdrop radius |
| `--hart-saturation` | `180%` -> C: `165%` | `effects.saturation` *(derived)* | `.glass` backdrop saturate |
| `--hart-radius` | `16px` -> C: `20px` | `layout.radius` *(derived)* | glass group + `.hh-card` (Radius slider) |
| `--hart-topbar-height` | `40px` | `layout.topbar_height` *(derived)* | top bar height + every `calc()` that offsets below it |
| `--hart-titlebar-height` | `32px` | `layout.titlebar_height` *(derived)* | panel titlebar |
| `--hart-icon-size` | `20px` | `layout.icon_size` *(derived)*; overridden by a11y font scale | tray glyph size |
| `--hart-font-family` | `"JetBrains Mono"` -> C: `system-ui,…` **`!important`** | `typography.font_family` | C forcibly overrides the theme emitter |
| `--hart-font-display` | (live emitter only) | `typography.font_display` *(derived)* | Aura display face (Space Grotesk) on `.hart-home` |
| `--hart-font-mono` | C: `"JetBrains Mono",ui-monospace,…` | `typography.font_mono` *(derived)* | declared in C; `--ds-font-mono` is the DS twin |
| `--hart-font-size` | `13px` | `typography.font_size` x a11y scale (clamped 0.8-2.0) | body metric |
| `--hart-heading-size` | `18px` | `typography.heading_size` x a11y scale | heading metric |
| `--hart-font-weight` | `400` | `typography.font_weight` *(derived)* | body weight |
| `--hart-heading-weight` | `600` | `typography.heading_weight` *(derived)* | start button, headings |
| `--hart-anim-speed` | `200ms` | `motion.anim_speed` *(derived)* | legacy transition duration across chrome |
| `--hart-a2` | `#9B5CFF` | `colors.a2` *(derived)* | secondary duotone accent (violet); wordmark "OS" |
| `--hart-a2-rgb` | `155,92,255` | derived from `colors.a2` | violet glows |
| `--hart-amb-1` | (live emitter) | `ambient.1` *(derived)* | MOOD quad slot 1 |
| `--hart-amb-2` | (live emitter) | `ambient.2` *(derived)* | MOOD quad slot 2 |
| `--hart-amb-3` | (live emitter) | `ambient.3` *(derived)* | MOOD quad slot 3 (= `--hh-cyan` source) |
| `--hart-amb-4` | (live emitter) | `ambient.4` *(derived)* | MOOD quad slot 4 (= `--hh-magenta` source) |
| `--hart-amb-1-rgb` | fallback `177,130,255` (A) / `0,230,195` (C) | derived from `ambient.1` | ambient blob 1, orbit2 dash |
| `--hart-amb-2-rgb` | fallback `0,221,249` (A) / `155,92,255` (C) | derived from `ambient.2` | ambient blob 2, orbit1 dash |
| `--hart-amb-3-rgb` | fallback `251,102,182` (A) / `41,197,255` (C) | derived from `ambient.3` | ambient blob 3 |
| `--hart-amb-4-rgb` | fallback `255,179,48` (A) / `255,46,154` (C) | derived from `ambient.4` | ambient blob 4 |
| `--hart-glow` | `40` | `effects.glow` *(derived)* | live-emitter only; glow intensity scalar |
| `--hart-density` | `1` | `layout.density` *(derived)* | live-emitter only; spacing scalar |

**Written by `hartPersonalize.paintPalette`:** the ambient quad is the de-monochrome MOOD
channel; the accent stays teal on functional signifiers. **Hot-swap path:** `applyPreset()`
rewrites ONLY this `:root` block into `<style id="hart-theme-live">`; no reload.

## P2 -- Aura Motion System (`--hart-motion-*`, `--hart-anim-*`) (19)

| var | default | theme JSON key | notes |
|---|---|---|---|
| `--hart-motion-speed` | `1` | `motion.aura.speed` *(derived; Personalize-written)* | **unitless master multiplier: every duration is `calc(base * speed)`** |
| `--hart-motion-ambient` | `running` | `motion.aura.ambient` *(derived)* | LAYER 1 play-state gate (bloom drift + hue) |
| `--hart-motion-orb` | `running` | `motion.aura.orb` *(derived)* | LAYER 2 gate (orb breathe + float) |
| `--hart-motion-rings` | `running` | `motion.aura.rings` *(derived)* | LAYER 3 gate (orbital spin) |
| `--hart-motion-detail` | `running` | `motion.aura.detail` *(derived)* | LAYER 4 gate (live dots; blink/wave/dash/joint vocabulary) |
| `--hart-anim-blob-speed` | `18s` | `motion.aura.blob_speed` *(derived)* | `vBlob1` base duration |
| `--hart-anim-hue-speed` | `22s` | `motion.aura.hue_speed` *(derived)* | `vHue` base duration |
| `--hart-anim-hue-amt` | `28deg` | `motion.aura.hue_amt` *(derived)* | **read INSIDE `@keyframes vHue`** |
| `--hart-anim-breathe-speed` | `4s` | `motion.aura.breathe_speed` *(derived)* | `vBreathe` duration |
| `--hart-anim-breathe-amt` | `1.08` | `motion.aura.breathe_amt` *(derived)* | **read INSIDE `@keyframes vBreathe`** |
| `--hart-anim-float-speed` | `9s` | `motion.aura.float_speed` *(derived)* | `vFloat` duration |
| `--hart-anim-float-amt` | `-10px` | `motion.aura.float_amt` *(derived)* | **read INSIDE `@keyframes vFloat`** |
| `--hart-anim-spin-speed` | `60s` | `motion.aura.spin_speed` *(derived)* | `vSpin` on `.hart-hero-aura` |
| `--hart-anim-spin-rev-speed` | `90s` | *(derived)* | **DEAD -- `vSpinRev` has no application rule** |
| `--hart-anim-spinc-speed` | `26s` | *(derived)* | **DEAD -- `vSpinC` unapplied** |
| `--hart-anim-blink-speed` | `1.05s` | *(derived)* | **DEAD -- `vBlink` unapplied** |
| `--hart-anim-wave-speed` | `1.1s` | *(derived)* | **DEAD -- `vWave` unapplied** |
| `--hart-anim-dash-speed` | `4s` | *(derived)* | **DEAD -- `vDash` unapplied** |
| `--hart-anim-joint-speed` | `2.4s` | *(derived)* | **DEAD -- `vJoint` unapplied** |

## P3 -- `--ds-*` Material-3 design system (49) -- STATIC

| group | vars | values |
|---|---|---|
| typography (2) | `--ds-font-body`, `--ds-font-mono` | `"Inter",-apple-system,"Segoe UI",Roboto,sans-serif`; `"JetBrains Mono","Fira Code",monospace` |
| spacing, 4dp grid (12) | `--ds-space-0`, `-px`, `-1`, `-2`, `-3`, `-4`, `-5`, `-6`, `-8`, `-10`, `-12`, `-16` | `0px,1px,4,8,12,16,20,24,32,40,48,64px` |
| elevation (6) | `--ds-elevation-0..5` | `none`; `0 1px 3px 1px rgba(0,0,0,.15),0 1px 2px rgba(0,0,0,.3)`; `0 2px 6px 2px…`; `0 4px 8px 3px…,0 1px 3px`; `0 6px 10px 4px…,0 2px 3px`; `0 8px 12px 6px…,0 4px 4px` -- **all zeroed under potato** |
| duration (4) | `--ds-duration-short`, `-medium`, `-long`, `-extra-long` | `100ms, 200ms, 350ms, 500ms` |
| easing (4) | `--ds-ease-standard`, `-decelerate`, `-accelerate`, `-spring` | `cubic-bezier(.2,0,0,1)`; `(0,0,0,1)`; `(.3,0,1,1)`; `(.175,.885,.32,1.275)` |
| surface tones (6) | `--ds-surface-dim`, `--ds-surface-1..5` | `rgba(15,14,23,.85)`; `rgba(255,255,255,.05/.08/.11/.12/.14)` |
| state layers (4) | `--ds-state-hover`, `-focus`, `-pressed`, `-dragged` | `rgba(255,255,255,.08/.12/.16/.16)` |
| radius (6) | `--ds-radius-xs`, `-sm`, `-md`, `-lg`, `-xl`, `-full` | `4, 8, 12, 16, 24, 9999px` |
| icon (5) | `--ds-icon-xs`, `-sm`, `-md`, `-lg`, `-xl` | `16, 20, 24, 32, 48px` |

**Theme JSON key:** none -- `--ds-*` is compile-time constant. Native parity ships it as a
frozen table, not a themable surface.

## P4 -- `--lg-*` Living Glass (50) + `--t-*` duration roles (5) = 55 -- STATIC

| group | vars | values / notes |
|---|---|---|
| accent triad (5) | `--lg-accent`, `--lg-accent-rgb`, `--lg-glow-0`, `--lg-glow-1`, `--lg-glow-2` | `var(--hart-accent)`; `var(--hart-accent-rgb,0,212,170)`; `rgba(var(--lg-accent-rgb),.55/.26/.12)` -- **imports P1, does not redefine it** |
| deterministic STATE hues (6) | `--lg-listen-rgb`, `--lg-think-rgb`, `--lg-speak-rgb`, `--lg-vision-rgb`, `--lg-blind-rgb`, `--lg-alert-rgb` | `0,224,194` / `108,99,255` / `25,227,125` / `52,176,255` / `120,120,132` / `255,92,122` -- **one hue per machine signal; these replace every legacy red state** |
| ink ladder (4) | `--lg-heading`, `--lg-text`, `--lg-muted`, `--lg-faint` | `#F4F6FF` / `#E4E7F2` / `#9AA2B8` / `#646B82` |
| glass depth ladder (12) | `--lg-{1,2,3,4}-bg`, `-blur`, `-bd` | bg `rgba(20,19,33,.42)`, `rgba(18,17,30,.56)`, `rgba(15,14,26,.70)`, `rgba(12,11,22,.82)`; blur `14/20/26/34px`; border `rgba(255,255,255,.07/.10/.13/.16)`. **Potato raises bg to `.92/.94/.95/.96` and drops blur** |
| saturation (1) | `--lg-sat` | `1.4` |
| specular + shadows (5) | `--lg-spec`, `--lg-sh-1..4` | `inset 0 1px 0 0 rgba(255,255,255,.14)`; `0 2px 10px rgba(0,0,0,.30)`; `0 8px 26px rgba(0,0,0,.42)`; `0 18px 50px rgba(0,0,0,.52)`; `0 30px 72px rgba(0,0,0,.58)` |
| presence rings (4) | `--lg-ring-listen`, `-think`, `-speak`, `-vision` | 3-stop layered box-shadows: listen `2px/.85, 7px/.20, 0 8px 30px/.38`; think `2px/.85, 8px/.18, 0 8px 30px/.36`; speak `2px/.85, 7px/.18, 0 8px 28px/.34`; vision `2px/.80, 6px/.18, 0 8px 26px/.32` |
| type (4) | `--lg-num`, `--lg-ls-display`, `--lg-ls-title`, `--lg-ls-micro` | `"tnum" 1`; `-.4px`; `-.1px`; `.6px` |
| motion roles (5) | `--lg-spring`, `--lg-glide`, `--lg-enter`, `--lg-exit`, `--lg-breathe` | `var(--ds-ease-spring)`; `var(--ds-ease-standard)`; `cubic-bezier(.16,1,.3,1)`; `cubic-bezier(.4,0,1,1)`; `cubic-bezier(.37,0,.63,1)` |
| stagger (1) | `--lg-stagger` | `28ms` |
| geometry (3) | `--lg-grid`, `--lg-pad`, `--lg-snap-widget` | `92px`, `24px`, `24px` -- **mirrors `hartDesktop.js` GRID/PAD; the two must not drift** |
| duration roles (5) | `--t-micro`, `--t-fast`, `--t-move`, `--t-reveal`, `--t-ceremony` | `140ms, 180ms, 220ms, 320ms, 560ms` -- **ALL forced to `0ms` by `@media(prefers-reduced-motion:reduce)`: the global motion kill-switch** |

**Theme JSON key:** none -- static in the served HTML. `--lg-accent*` and the motion roles
transitively track P1/P3, so retinting P1 retints Living Glass without touching P4.

## P5 -- `--hh-*` home aliases (13) -- file-local, theme-tracking with hard-coded fallbacks

| var | value | tracks | notes |
|---|---|---|---|
| `--hh-teal` | `var(--hart-accent,#00E6C3)` | P1 accent | **FUNCTIONAL accent** |
| `--hh-cyan` | `var(--hart-amb-3,#29C5FF)` | P1 ambient 3 | themable MOOD slot |
| `--hh-blue` | `#3B82F6` | -- | **HARD-CODED, not themable (GAP)** |
| `--hh-violet` | `var(--hart-a2,#9B5CFF)` | P1 a2 | secondary brand |
| `--hh-magenta` | `var(--hart-amb-4,#FF2E9A)` | P1 ambient 4 | themable MOOD slot |
| `--hh-amber` | `#FFC83D` | -- | **HARD-CODED, not themable (GAP)** |
| `--hh-ink` | `var(--hart-text,#F3F6FB)` | P1 text | body ink |
| `--hh-dim` | `var(--hart-muted,#9AA7B6)` | P1 muted | secondary ink |
| `--hh-bord` | `var(--hart-glass-border,rgba(255,255,255,.09))` | P1 glass border | hairlines |
| `--hh-top-safe` | `70px` | -- | reserved top-bar safe area |
| `--hh-bottom-safe` | `56px` | -- | reserved taskbar safe area (**the only taskbar reference in file B**) |
| `--hh-gutter` | `60px` (-> `40px` @`max-width:1400px`) | -- | `:root` override inside a media query |
| `--hh-acc` | **not declared in `:root`** -- set per-row by `.hh-accent-*` | P5 hues | consumed with fallback `var(--hh-acc,var(--hh-teal))`; **exactly 3 consumers** |

**PARITY NOTE:** every fallback IS the exact default `theme_service` emits, so
`hart-default` and every preset render pixel-identically; only a theme that sets the token
retints.

## P6 -- `--hv-*` Hevolve brand spectrum + cinematic motion (14) -- STATIC, single source

| var | value | notes |
|---|---|---|
| `--hv-teal` / `--hv-teal-rgb` | `#00E6C3` / `0,230,195` | primary spectrum hue |
| `--hv-cyan` / `--hv-cyan-rgb` | `#29C5FF` / `41,197,255` | panel focus glow, card hover ring, avatar ring, wordmark glow |
| `--hv-blue` / `--hv-blue-rgb` | `#3B82F6` / `59,130,246` | wallpaper centre blob, icon hue 6n+3 |
| `--hv-violet` / `--hv-violet-rgb` | `#9B5CFF` / `155,92,255` | start-item hover glow, icon hue 6n+4 |
| `--hv-magenta` / `--hv-magenta-rgb` | `#FF2E9A` / `255,46,154` | icon hue 6n+5 |
| `--hv-amber` / `--hv-amber-rgb` | `#FFC83D` / `255,200,61` | icon hue 6n+6 |
| `--hv-focus` | `cubic-bezier(.2,.8,.2,1)` | **the signature Netflix focus easing -- EVERY transition in file C uses it** |
| `--hv-lift` | `380ms` | cinematic lift/reveal duration (floors collapse to 140ms) |

**Theme JSON key:** none -- the brand spectrum is a fixed identity, not a themable surface.
The MOOD retint path is P1's ambient quad, not P6.

---

# Coverage

**93 components / 42 keyframes / 192 vars.**

Breakdown: components 47 (A, inline shell) + 28 (B, `hartHome.css`) + 18 (C,
`hartResponsive.css`). Keyframes 23 (A) + 17 (B) + 2 (C); 31 applied, 11 declared-but-dead,
2 applied-but-overridden, 3 potato-suppressed. Vars 42 (`--hart-*` theme) + 19 (Aura
motion) + 49 (`--ds-*`) + 55 (`--lg-*` + `--t-*`) + 13 (`--hh-*`) + 14 (`--hv-*`).

**Open items that must close before this ledger is signed off as complete:**

1. **C18 re-inventory** -- the `.hart-tile` / Personalize block onward in
   `hartResponsive.css` (l.~436-624) was truncated in the source inventory. Re-read and
   replace that row with the exhaustive listing.
2. **Appendix P theme-key column** -- diff every *(derived)* row against
   `theme_service.py` before generating native theme-loading code.
3. **External sheets not in scope here** -- `hartHero.js` / `voiceOrbViz.js` own the orb
   intrinsics (geometry, gradient, bloom, listening/thinking/speaking character);
   `hartBloom.js` owns the bloom compose; `hartDesktop.js` owns per-app icon colour and
   the drag transform channel. Native orb + icon parity CANNOT be derived from CSS alone.
4. **Recorded defects to fix rather than replicate** -- the reduced-motion coverage gaps
   (B25), `tbOrbBreathe` escaping the motion gates (B22), the un-potato-gated
   `.hart-icustom-backdrop` blur (A29), the non-retinting `.ds-input` focus rings (A36),
   and the hard-coded onboarding palette (A33).

