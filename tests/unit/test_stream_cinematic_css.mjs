/*
 * Behavioural guard for the STREAM "Netflix Home" cinematic CSS overhaul v3
 * (integrations/agent_engine/static/hartResponsive.css).
 *
 * There is no headless browser in this env, so this drives the REAL stylesheet
 * through a small but real CSS reader: it strips comments, parses every rule into
 * a {atStack, selectorList, declarations{}} model (respecting @media / @keyframes
 * nesting), and then RESOLVES the effective value of a property for a concrete
 * element + interaction state by walking the matching rules in source order
 * (the cascade). It asserts OBSERVABLE cascade outcomes, not source substrings —
 * e.g. "for a hovered .desktop-icon wrapper, the resolved `transform` is unset"
 * and "the dot's z-index resolves above the scrim's, which resolves above the
 * image's". (CLAUDE.md Gate 5 / feedback_no_grep_tests.md.)
 *
 * The intents it guards are the three drag/animation regressions the v3 pass
 * could introduce on the ONLY draggable / hover-lifted shell elements, plus the
 * image-card scrim layering contract:
 *
 *  R1  .desktop-icon (the drag wrapper, moved each frame via inline
 *      el.style.transform) must NOT transition `transform` — else every drag
 *      frame eases (lag) and the drop-clear overshoots. The lift lives on the
 *      inner .di-glyph (its own transform channel, never drag-driven).
 *  R2  The icon entrance animation must use `backwards` fill, not `both`/`forwards`
 *      (a held end-frame pins transform at animation-priority and breaks the drag
 *      inline transform), and must NOT be gated by a `.dragging { animation:none }`
 *      rule (toggling .dragging per tap would replay the entrance every click).
 *  R3  The .start-item entrance must likewise use `backwards` fill, else the held
 *      end-frame overrides the :hover lift (animations outrank author rules) and
 *      the lift is dead after the menu settles.
 *  R4  Image-card layering: dot/caption z resolves ABOVE the scrim z, which
 *      resolves ABOVE the poster <img> z (text-over-art stays readable).
 *  R5  Reduced motion (both the OS pref and html.a11y-rmotion) collapses the
 *      glyph lift to transform:none.
 *
 * Run:  node tests/unit/test_stream_cinematic_css.mjs
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const CSS_PATH = join(HERE, '..', '..', 'integrations', 'agent_engine', 'static', 'hartResponsive.css');
const css = readFileSync(CSS_PATH, 'utf8');

let failures = 0;
function ok(cond, msg) { if (cond) { console.log('  OK   ' + msg); } else { failures++; console.log(' FAIL  ' + msg); } }

// ── A small real CSS reader: comment-strip + brace-aware rule model ──────────
function parseRules(src) {
  src = src.replace(/\/\*[\s\S]*?\*\//g, '');   // strip comments
  const rules = [];
  let i = 0;
  function parseBlock(atStack) {
    let buf = '';
    while (i < src.length) {
      const ch = src[i];
      if (ch === '{') {
        const prelude = buf.trim(); buf = ''; i++;     // consume '{'
        if (prelude[0] === '@') {
          if (/^@keyframes/i.test(prelude)) { skipBalanced(); }   // don't model keyframe stops
          else { parseBlock(atStack.concat(prelude)); }
        } else {
          const decls = readDecls();
          rules.push({ atStack: atStack.slice(), selectors: prelude.split(',').map(function (s) { return s.trim(); }), decls: decls });
        }
      } else if (ch === '}') { i++; return; }
      else { buf += ch; i++; }
    }
  }
  function readDecls() {
    const decls = {}; let d = '';
    while (i < src.length) {
      const ch = src[i];
      if (ch === '}') { i++; break; }
      if (ch === ';') { commit(d, decls); d = ''; i++; }
      else { d += ch; i++; }
    }
    if (d.trim()) commit(d, decls);
    return decls;
  }
  function commit(d, decls) {
    const idx = d.indexOf(':'); if (idx < 0) return;
    const prop = d.slice(0, idx).trim().toLowerCase();
    const val = d.slice(idx + 1).trim();
    if (prop) decls[prop] = val;
  }
  function skipBalanced() { let depth = 1; while (i < src.length && depth > 0) { const ch = src[i++]; if (ch === '{') depth++; else if (ch === '}') depth--; } }
  parseBlock([]);
  return rules;
}

const RULES = parseRules(css);
ok(RULES.length > 40, 'the stylesheet parsed into a rule model (' + RULES.length + ' rules)');

// Resolve the effective value of `prop` for the EXACT compound selector `sel`,
// considering only rules whose at-context passes `atFilter`, in source order.
function resolve(sel, prop, atFilter) {
  atFilter = atFilter || function (st) { return st.length === 0; };
  let val;
  RULES.forEach(function (r) {
    if (!atFilter(r.atStack)) return;
    if (r.selectors.indexOf(sel) >= 0 && Object.prototype.hasOwnProperty.call(r.decls, prop)) val = r.decls[prop];
  });
  return val;
}
const TOP = function (st) { return st.length === 0; };
const REDUCED = function (st) { return st.some(function (p) { return /prefers-reduced-motion/.test(p); }); };
function z(sel) { const v = resolve(sel, 'z-index'); return v == null ? NaN : parseInt(v, 10); }

// ════════════════════════════════════════════════════════════════════════════
// R1 — the drag wrapper must not transition transform; the lift is on the glyph
// ════════════════════════════════════════════════════════════════════════════
(function r1() {
  console.log('\n[R1] .desktop-icon drag wrapper keeps its transform channel clean');
  const tr = resolve('.desktop-icon', 'transition') || '';
  ok(tr !== '', '.desktop-icon declares a transition (' + JSON.stringify(tr) + ')');
  ok(!/\btransform\b/.test(tr),
     '.desktop-icon transition does NOT animate `transform` (drag writes inline transform every frame — easing it would lag + overshoot)');

  // The wrapper itself must not carry a hover/focus transform (that is the drag channel).
  ok(resolve('.desktop-icon:hover', 'transform') == null && resolve('.desktop-icon:focus-within', 'transform') == null,
     '.desktop-icon:hover/:focus-within set NO transform on the wrapper');
  ok(z('.desktop-icon:hover') === 40, '.desktop-icon:hover still raises z-index (40) so it lifts above neighbours');

  // The visible lift moved to the inner glyph, which has its own (non-drag) transform transition.
  const glyphLift = resolve('.desktop-icon:hover .di-glyph', 'transform') || '';
  ok(/translateY\(-?\d/.test(glyphLift) && /scale\(/.test(glyphLift),
     'the Netflix lift lives on .desktop-icon:hover .di-glyph (' + JSON.stringify(glyphLift) + ')');
  const glyphTr = resolve('.desktop-icon .di-glyph', 'transition') || '';
  ok(/\btransform\b/.test(glyphTr), '.di-glyph DOES transition transform (the lift animates smoothly there)');
})();

// ════════════════════════════════════════════════════════════════════════════
// R2 — icon entrance: backwards fill, no .dragging replay guard
// ════════════════════════════════════════════════════════════════════════════
(function r2() {
  console.log('\n[R2] .desktop-icon entrance does not pin transform or replay on tap');
  const anim = resolve('.hart-desktop .desktop-icon', 'animation') || '';
  ok(/\bhv-rise\b/.test(anim), '.desktop-icon plays the hv-rise entrance (' + JSON.stringify(anim) + ')');
  ok(/\bbackwards\b/.test(anim) && !/\b(both|forwards)\b/.test(anim),
     'the entrance fill is `backwards` (self-clears; never pins transform over the inline drag transform)');

  // No rule may set animation:none on a .dragging icon (that toggle replays the entrance every tap).
  const draggingGuard = RULES.some(function (r) {
    return r.atStack.length === 0 &&
      r.selectors.some(function (s) { return /\.desktop-icon\.dragging\b/.test(s); }) &&
      (r.decls.animation === 'none' || r.decls['animation-name'] === 'none');
  });
  ok(!draggingGuard, 'NO `.desktop-icon.dragging { animation:none }` guard exists (it would restart the entrance on every pointerdown/up)');
})();

// ════════════════════════════════════════════════════════════════════════════
// R3 — start-item entrance backwards fill so the :hover lift survives
// ════════════════════════════════════════════════════════════════════════════
(function r3() {
  console.log('\n[R3] .start-item entrance does not suppress its own hover lift');
  const anim = resolve('.start-menu .start-item', 'animation') || '';
  ok(/\bhv-rise\b/.test(anim), '.start-item plays the hv-rise entrance');
  ok(/\bbackwards\b/.test(anim) && !/\b(both|forwards)\b/.test(anim),
     'the start-item entrance fill is `backwards` (a held end-frame would outrank + kill the :hover lift)');
  const lift = resolve('.start-item:hover', 'transform') || '';
  ok(/translateY\(/.test(lift) && /scale\(/.test(lift), '.start-item:hover declares its lift (' + JSON.stringify(lift) + ')');
})();

// ════════════════════════════════════════════════════════════════════════════
// R4 — image-card scrim layering: dot/caption above scrim above poster image
// ════════════════════════════════════════════════════════════════════════════
(function r4() {
  console.log('\n[R4] image-card scrim layering keeps text-over-art readable');
  const dot = z('.hart-tile .htc-prev > .htc-dot');
  const scrim = z('.hart-tile .htc-prev::after');
  const img = z('.hart-tile .htc-prev > img');
  ok(!isNaN(dot) && !isNaN(scrim) && !isNaN(img), 'dot/scrim/image all declare a z-index (' + dot + '/' + scrim + '/' + img + ')');
  ok(dot > scrim, 'the status dot / caption (z ' + dot + ') sits ABOVE the scrim (z ' + scrim + ')');
  ok(scrim > img, 'the scrim (z ' + scrim + ') sits ABOVE the poster image (z ' + img + ') so the gradient darkens the art');

  // The reusable text-over-art primitive keeps its caption above its own scrim too.
  const cap = z('.hv-art-cap');
  const artScrim = z('.hv-art::after');
  ok(!isNaN(cap) && !isNaN(artScrim) && cap > artScrim,
     '.hv-art-cap (z ' + cap + ') stays above the .hv-art scrim (z ' + artScrim + ')');
})();

// ════════════════════════════════════════════════════════════════════════════
// R5 — reduced motion collapses the glyph lift (OS pref AND the live a11y toggle)
// ════════════════════════════════════════════════════════════════════════════
(function r5() {
  console.log('\n[R5] reduced motion stills the glyph lift + entrance');
  ok(resolve('.desktop-icon:hover .di-glyph', 'transform', REDUCED) === 'none',
     '@media (prefers-reduced-motion) sets .desktop-icon:hover .di-glyph transform:none');
  ok(resolve('html.a11y-rmotion .desktop-icon:hover .di-glyph', 'transform', TOP) === 'none',
     'html.a11y-rmotion sets .desktop-icon:hover .di-glyph transform:none (live toggle)');
  ok(resolve('.hart-desktop .desktop-icon', 'animation', REDUCED) === 'none',
     '@media (prefers-reduced-motion) disables the icon entrance animation');
})();

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);
