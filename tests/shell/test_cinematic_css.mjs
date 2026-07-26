/*
 * Structural / validator behavioural guard for the STREAM "Netflix Home"
 * cinematic CSS overhaul v3 (integrations/agent_engine/static/hartResponsive.css).
 *
 * Sibling test tests/unit/test_stream_cinematic_css.mjs already proves the
 * cascade OUTCOMES (which transform/z-index resolves for a hovered element).
 * This file owns a different contract: the *artifact health* of the stylesheet
 * the cage shell actually loads -
 *
 *   - the file is well-formed: braces balance, no unterminated comment/string;
 *   - the v3 cinematic layer is present (the --hv-* design tokens + the
 *     multi-radial cinematic .wallpaper bloom);
 *   - the steward's hard requirement is met: the brand SPECTRUM is woven, the
 *     shell is NOT a single teal wash (the specific regression this stream fixes);
 *   - a prefers-reduced-motion (and the live html.a11y-rmotion) escape hatch
 *     exists and actually stills motion;
 *   - no U+2014 em dash leaked into the product CSS (house style).
 *
 * It is NOT a grep test. It loads the REAL bytes, runs them through a real
 * comment/string-aware CSS reader (the unit under test = the validate() +
 * scanBraces() contract), and asserts on that function's OBSERVABLE OUTPUT.
 * Every check is then re-exercised against a CRAFTED boundary input where the
 * expected verdict is the OPPOSITE (empty, missing-file, unbalanced braces,
 * brace-inside-comment, single-teal-only, an injected em dash) so each assertion
 * is proven to discriminate rather than pass vacuously.
 *
 * Run:  node tests/shell/test_cinematic_css.mjs
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const CSS_PATH = join(HERE, '..', '..', 'integrations', 'agent_engine', 'static', 'hartResponsive.css');

let failures = 0;
let total = 0;
function ok(cond, msg) {
  total++;
  if (cond) { console.log('  OK   ' + msg); }
  else { failures++; console.log(' FAIL  ' + msg); }
}

// ─────────────────────────────────────────────────────────────────────────────
// The unit under test: a small, real CSS reader. No network, no DOM, no browser.
// ─────────────────────────────────────────────────────────────────────────────

// loadCss: graceful loader. Returns {ok, css, error}. NEVER throws (offline /
// missing-asset safety: a packaging slip-up must surface as a verdict, not a crash).
function loadCss(path) {
  try {
    return { ok: true, css: readFileSync(path, 'utf8'), error: null };
  } catch (e) {
    return { ok: false, css: '', error: (e && e.code) ? e.code : String(e) };
  }
}

// scanBraces: comment- AND string-aware brace balance. A `{` inside a /* */ comment
// or inside a '…' / "…" string MUST NOT count, or the balance check is a lie.
function scanBraces(src) {
  let depth = 0, maxDepth = 0, wentNegative = false;
  let inComment = false, inString = false, stringCh = '';
  for (let i = 0; i < src.length; i++) {
    const ch = src[i], nx = src[i + 1];
    if (inComment) {
      if (ch === '*' && nx === '/') { inComment = false; i++; }
      continue;
    }
    if (inString) {
      if (ch === '\\') { i++; continue; }      // skip escaped char
      if (ch === stringCh) { inString = false; }
      continue;
    }
    if (ch === '/' && nx === '*') { inComment = true; i++; continue; }
    if (ch === '"' || ch === "'") { inString = true; stringCh = ch; continue; }
    if (ch === '{') { depth++; if (depth > maxDepth) maxDepth = depth; }
    else if (ch === '}') { depth--; if (depth < 0) wentNegative = true; }
  }
  return {
    depthAtEnd: depth,
    maxDepth: maxDepth,
    wentNegative: wentNegative,
    unterminatedComment: inComment,
    unterminatedString: inString,
    balanced: depth === 0 && !wentNegative && !inComment && !inString
  };
}

// parseRules: comment-strip then a brace-aware rule model with @-context nesting.
// (@keyframes stop-frames are skipped, not modelled — same convention as sibling.)
function parseRules(src) {
  src = src.replace(/\/\*[\s\S]*?\*\//g, '');   // CSS comments do not nest
  const rules = [];
  let i = 0;
  function skipBalanced() { let d = 1; while (i < src.length && d > 0) { const c = src[i++]; if (c === '{') d++; else if (c === '}') d--; } }
  function readDecls() {
    const decls = {}; let d = '';
    function commit(s) { const k = s.indexOf(':'); if (k < 0) return; const p = s.slice(0, k).trim().toLowerCase(); const v = s.slice(k + 1).trim(); if (p) decls[p] = v; }
    while (i < src.length) {
      const ch = src[i];
      if (ch === '}') { i++; break; }
      if (ch === ';') { commit(d); d = ''; i++; }
      else { d += ch; i++; }
    }
    if (d.trim()) commit(d);
    return decls;
  }
  function parseBlock(atStack) {
    let buf = '';
    while (i < src.length) {
      const ch = src[i];
      if (ch === '{') {
        const prelude = buf.trim(); buf = ''; i++;
        if (prelude[0] === '@') {
          if (/^@keyframes/i.test(prelude)) { skipBalanced(); }
          else { parseBlock(atStack.concat(prelude)); }
        } else {
          rules.push({ atStack: atStack.slice(), selectors: prelude.split(',').map(function (s) { return s.trim(); }), decls: readDecls() });
        }
      } else if (ch === '}') { i++; return; }
      else { buf += ch; i++; }
    }
  }
  parseBlock([]);
  return rules;
}

// validate: the high-level contract. Pure function of a CSS string -> a structured
// report the tests assert on. Purely offline / in-memory (no fs, no net).
function validate(css) {
  const braces = scanBraces(css);
  const rules = parseRules(css);

  // :root custom-property collection.
  const root = {};
  rules.forEach(function (r) {
    if (r.atStack.length === 0 && r.selectors.indexOf(':root') >= 0) {
      Object.keys(r.decls).forEach(function (k) { if (k.indexOf('--') === 0) root[k] = r.decls[k]; });
    }
  });

  // Brand spectrum hue families: --hv-<name> hex tokens (exclude -rgb / -focus / -lift).
  const hueHex = {};
  Object.keys(root).forEach(function (k) {
    const m = /^--hv-([a-z]+)$/.exec(k);
    if (m && /^#[0-9a-fA-F]{3,8}$/.test(root[k])) hueHex[m[1]] = root[k].toLowerCase();
  });
  const distinctHueValues = {};
  Object.keys(hueHex).forEach(function (n) { distinctHueValues[hueHex[n]] = true; });

  // Spectrum USAGE: which --hv-<name>-rgb tokens are referenced anywhere outside :root
  // (proves the spectrum is actually painted across the UI, not merely declared).
  const usedHues = {};
  rules.forEach(function (r) {
    const inRoot = r.atStack.length === 0 && r.selectors.indexOf(':root') >= 0;
    if (inRoot) return;
    Object.keys(r.decls).forEach(function (k) {
      const v = r.decls[k];
      let m; const re = /--hv-([a-z]+)-rgb/g;
      while ((m = re.exec(v)) !== null) { usedHues[m[1]] = true; }
    });
  });

  // .wallpaper cinematic bloom: count radial-gradient layers in its background.
  let wallpaperRadials = 0;
  rules.forEach(function (r) {
    if (r.atStack.length === 0 && r.selectors.indexOf('.wallpaper') >= 0 && r.decls.background) {
      wallpaperRadials = (r.decls.background.match(/radial-gradient/g) || []).length;
    }
  });

  // prefers-reduced-motion at-context that actually stills motion.
  let reducedStillsTransform = false, reducedStillsAnimation = false;
  rules.forEach(function (r) {
    const isReduced = r.atStack.some(function (a) { return /prefers-reduced-motion/.test(a); });
    if (!isReduced) return;
    if (r.decls.transform === 'none') reducedStillsTransform = true;
    if (r.decls.animation === 'none' || r.decls['animation-name'] === 'none') reducedStillsAnimation = true;
  });

  // Live a11y toggle (top-level html.a11y-rmotion selectors that still motion).
  let a11yToggleStills = false;
  rules.forEach(function (r) {
    if (r.atStack.length !== 0) return;
    const hasToggle = r.selectors.some(function (s) { return /^html\.a11y-rmotion\b/.test(s); });
    if (hasToggle && (r.decls.transform === 'none' || r.decls.animation === 'none')) a11yToggleStills = true;
  });

  // Em-dash policy. The house rule bans U+2014 in USER-VISIBLE PRODUCT TEXT.
  // For a stylesheet the rendered/delivered surface is selectors + declaration
  // values (notably `content:`); CSS comments are stripped and never reach the
  // browser. So scan the PARSED product surface for the hard rule, and report
  // comment-only occurrences separately (informational, non-fatal).
  let productText = '';
  rules.forEach(function (r) {
    productText += r.selectors.join(',');
    Object.keys(r.decls).forEach(function (k) { productText += k + ':' + r.decls[k] + ';'; });
  });
  const emDashInProduct = productText.indexOf('—') >= 0;
  const emDashAnywhere = css.indexOf('—') >= 0;

  return {
    braces: braces,
    ruleCount: rules.length,
    rootTokenCount: Object.keys(root).length,
    hasFocusEasing: typeof root['--hv-focus'] === 'string' && /cubic-bezier/.test(root['--hv-focus']),
    hasLiftDuration: typeof root['--hv-lift'] === 'string',
    hueFamilyCount: Object.keys(hueHex).length,
    distinctHueValueCount: Object.keys(distinctHueValues).length,
    usedHueCount: Object.keys(usedHues).length,
    usedHues: Object.keys(usedHues).sort(),
    wallpaperRadials: wallpaperRadials,
    reducedStillsTransform: reducedStillsTransform,
    reducedStillsAnimation: reducedStillsAnimation,
    a11yToggleStills: a11yToggleStills,
    emDashInProduct: emDashInProduct,
    emDashAnywhere: emDashAnywhere,
    emDashCommentsOnly: emDashAnywhere && !emDashInProduct
  };
}

// ════════════════════════════════════════════════════════════════════════════
// HAPPY PATH — the real artifact passes the full v3 cinematic contract.
// ════════════════════════════════════════════════════════════════════════════
console.log('[happy] real hartResponsive.css satisfies the v3 cinematic contract');
const loaded = loadCss(CSS_PATH);
ok(loaded.ok, 'the stylesheet loads from disk (' + (loaded.error || 'ok') + ')');
const V = validate(loaded.css);

// well-formed
ok(V.braces.balanced, 'braces balance: depthAtEnd=' + V.braces.depthAtEnd + ', neverNegative=' + (!V.braces.wentNegative) + ', no unterminated comment/string');
ok(V.ruleCount > 40, 'parses into a real rule model (' + V.ruleCount + ' rules)');

// v3 layer present
ok(V.rootTokenCount > 10, ':root declares the design-token layer (' + V.rootTokenCount + ' custom props)');
ok(V.hasFocusEasing, 'v3 marker present: --hv-focus is a cubic-bezier (the Netflix focus easing)');
ok(V.hasLiftDuration, 'v3 marker present: --hv-lift duration token defined');
ok(V.wallpaperRadials >= 4, '.wallpaper paints a multi-layer cinematic bloom (' + V.wallpaperRadials + ' radial-gradient layers)');

// spectrum, NOT single teal  (the specific regression this stream fixes)
ok(V.hueFamilyCount >= 5, 'brand SPECTRUM declared: ' + V.hueFamilyCount + ' --hv-* hue families (steward rejected single-hue teal)');
ok(V.distinctHueValueCount >= 5, 'the hues are genuinely DISTINCT colors (' + V.distinctHueValueCount + ' unique hex values, not one teal repeated)');
ok(V.usedHueCount >= 4, 'the spectrum is actually WOVEN across the UI: ' + V.usedHueCount + ' distinct hue-rgb tokens referenced [' + V.usedHues.join(', ') + ']');
ok(V.usedHues.indexOf('teal') >= 0 && V.usedHues.length > 1, 'teal is one of MANY accents, not the only one (regression: a single-teal wash)');

// reduced motion escape hatch
ok(V.reducedStillsTransform, '@media (prefers-reduced-motion) collapses a hover lift to transform:none');
ok(V.reducedStillsAnimation, '@media (prefers-reduced-motion) disables a cinematic entrance animation');
ok(V.a11yToggleStills, 'the live html.a11y-rmotion toggle also stills motion (no OS pref needed)');

// house style: no em dash in the rendered surface (selectors + decl values incl. content:)
ok(!V.emDashInProduct, 'no U+2014 em dash in the delivered/rendered surface (selectors + declaration values)');
if (V.emDashCommentsOnly) {
  console.log('  note  U+2014 appears ONLY inside stripped CSS comments (not user-visible product text); the rendered surface is clean');
}

// ════════════════════════════════════════════════════════════════════════════
// BOUNDARY — each check is proven to discriminate against a crafted opposite.
// ════════════════════════════════════════════════════════════════════════════
console.log('\n[boundary] empty input');
const E = validate('');
ok(E.braces.balanced && E.braces.depthAtEnd === 0, 'empty CSS: braces trivially balanced, depth 0');
ok(E.ruleCount === 0 && E.rootTokenCount === 0, 'empty CSS: zero rules, zero tokens (no false positives)');
ok(E.hueFamilyCount === 0 && E.usedHueCount === 0 && !E.hasFocusEasing, 'empty CSS: spectrum + v3-marker checks correctly report ABSENT');
ok(!E.emDashInProduct && !E.emDashAnywhere, 'empty CSS: no em dash anywhere');

console.log('\n[boundary] missing file is handled gracefully (offline / packaging-slip safety)');
const miss = loadCss(join(HERE, 'does-not-exist-9f3a.css'));
ok(!miss.ok && miss.error === 'ENOENT', 'loadCss(missing) returns {ok:false, error:ENOENT} and does NOT throw');
ok(validate(miss.css).ruleCount === 0, 'validate() on the empty fallback still runs (degrades, never crashes)');

console.log('\n[boundary] malformed: unbalanced braces are CAUGHT');
const bad = validate('.a { color: red; ');                 // missing closing brace
ok(!bad.braces.balanced && bad.braces.depthAtEnd === 1, 'unterminated block -> balanced:false, depthAtEnd:1 (the balance check is real)');
const extra = scanBraces('.a { color: red; } }');           // one extra close
ok(!extra.balanced && extra.wentNegative, 'stray closing brace -> wentNegative:true (not silently accepted)');

console.log('\n[boundary] a "{" inside a comment / string must NOT count');
const tricky = scanBraces("/* orphan { brace */ .a { content: '}'; color: red; }");
ok(tricky.balanced && tricky.depthAtEnd === 0, 'braces inside a comment and inside a string are ignored (balanced:true)');

console.log('\n[boundary] regression: a single-teal-only stylesheet FAILS the spectrum check');
const teal = validate(
  ':root { --hv-teal: #00E6C3; --hv-teal-rgb: 0,230,195; }\n' +
  '.a { box-shadow: 0 0 10px rgba(var(--hv-teal-rgb),0.4); }\n' +
  '.b { border-color: rgba(var(--hv-teal-rgb),0.5); }'
);
ok(teal.hueFamilyCount === 1 && teal.distinctHueValueCount === 1, 'single-teal CSS: only 1 hue family / 1 distinct value (would fail the >=5 happy gate)');
ok(teal.usedHueCount === 1 && teal.usedHues[0] === 'teal', 'single-teal CSS: only teal is referenced (the exact regression the stream guards against)');

console.log('\n[boundary] em-dash scoping: rendered surface vs stripped comment');
const cd = validate(".x::after { content: '—'; }");           // WOULD render
ok(cd.emDashInProduct, 'an em dash inside a content: value (which renders) is CAUGHT on the product surface');
const cm = validate('/* premium — cinematic */ .a { color: red; }');   // never renders
ok(!cm.emDashInProduct && cm.emDashAnywhere && cm.emDashCommentsOnly,
   'an em dash confined to a comment is NOT flagged as product text (comments are stripped, never rendered)');

console.log('\n[boundary] a v2-only stylesheet (no --hv-focus) reports the v3 layer ABSENT');
const v2 = validate(':root { --hart-accent-rgb: 0,230,195; } .a { color: red; }');
ok(!v2.hasFocusEasing && !v2.hasLiftDuration && v2.wallpaperRadials === 0, 'no v3 tokens / no cinematic bloom -> v3 markers correctly absent');

console.log('\n[boundary] reduced-motion check discriminates: a stylesheet WITHOUT the block reports it missing');
const noRm = validate('.a:hover { transform: translateY(-5px) scale(1.05); }');
ok(!noRm.reducedStillsTransform && !noRm.reducedStillsAnimation && !noRm.a11yToggleStills, 'no prefers-reduced-motion / a11y block -> all three reduced-motion flags false');

console.log(failures
  ? ('\nRESULT: ' + failures + ' FAILED of ' + total)
  : ('\nRESULT: ALL ' + total + ' PASS'));
process.exit(failures ? 1 : 0);
