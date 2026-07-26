/*
 * hartBloom.js -- the HART OS cosmic-bloom backdrop, COMPOSED + PRE-BLURRED AT
 * RUNTIME (steward 2026-07-19).
 *
 * WHY this exists (and why it is NOT a shipped image, NOT a live blur):
 *   The Aura desktop wants a soft violet/cyan/pink aurora behind the shell. Two
 *   wrong ways to get it, both rejected:
 *     - LIVE blur (CSS filter:blur / backdrop-filter): the cairo software floor
 *       re-rasterises the blur EVERY frame -> the ~500ms lag we fight. Banned.
 *     - BUILD-TIME baked asset (a .webp in the ISO): frozen -- it cannot track the
 *       palette the agentic Liquid-UI / local LLM composes at runtime, and it bloats
 *       the image. Rejected by the steward: "pre blur not at build time, at compose
 *       time during OS runtime."
 *   The right way (this file): the shell COMPOSES the bloom ONCE into a <canvas>,
 *   Gaussian-blurring the blobs in a single canvas pass (ctx.filter='blur(...)') at
 *   COMPOSE time -- on first paint, and again only when the mood/palette changes
 *   (HartHome.compose) or the viewport resizes. The blur cost is paid once per
 *   compose; every subsequent frame just shows the finished bitmap (the canvas is
 *   never cleared/redrawn per frame, no requestAnimationFrame). So it is rich AND
 *   costs zero per-frame, AND it re-tints to whatever palette the LLM composed.
 *
 * Palette source: the live --hart-amb-1..4-rgb CSS custom properties (the theme sets
 * them from aura.json ambient_1..4; a mood re-compose overwrites them). ONE palette
 * source -- no parallel colour table here (the orbit ring + gradient fallback read
 * the SAME vars). Reduced-motion / potato do not need special-casing: this paints a
 * STATIC image (no animation), so it is calm by construction.
 */
(function (global) {
  'use strict';

  var CANVAS_ID = 'hart-bloom-canvas';
  var _raf = 0;

  // Read a --hart-amb-N-rgb custom property (an "r,g,b" triple) with a fallback so a
  // theme that has not set it (or a pre-compose first paint) still gets the aura hue.
  function amb(cs, n, fallback) {
    try {
      var v = cs.getPropertyValue('--hart-amb-' + n + '-rgb');
      v = v ? v.trim() : '';
      return v || fallback;
    } catch (e) {
      return fallback;
    }
  }

  // Compose the bloom ONCE. Idempotent: safe to call on load, resize, and every mood
  // re-compose. Never throws out (a backdrop must never take the shell down).
  function composeHartBloom() {
    try {
      var c = (typeof document !== 'undefined') && document.getElementById(CANVAS_ID);
      if (!c || !c.getContext) return;
      // Cap the device-pixel-ratio: a full blur pass scales with pixel count, and the
      // bloom is soft so 1.25x is visually indistinguishable from 2x -- this keeps the
      // one-time compose cheap even on a HiDPI panel.
      var dpr = Math.min((global.devicePixelRatio || 1), 1.25);
      var vw = (global.innerWidth || 1280), vh = (global.innerHeight || 800);
      var w = Math.max(2, Math.round(vw * dpr));
      var h = Math.max(2, Math.round(vh * dpr));
      if (c.width !== w) c.width = w;
      if (c.height !== h) c.height = h;
      var ctx = c.getContext('2d');
      if (!ctx) return;
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, w, h);

      var cs = (global.getComputedStyle && document.documentElement)
        ? global.getComputedStyle(document.documentElement) : null;
      var A1 = amb(cs, 1, '177,130,255');  // violet  (aura ambient_1 B182FF)
      var A2 = amb(cs, 2, '0,221,249');    // cyan    (aura ambient_2 00DDF9)
      var A3 = amb(cs, 3, '251,102,182');  // pink    (aura ambient_3 FB66B6)
      var A4 = amb(cs, 4, '255,179,48');   // amber   (aura ambient_4 FFB330)

      // Blob field echoing the Aura mock: violet core-left, cyan upper-right, pink
      // lower-left, amber lower-right accent, a violet reinforce mid. Positions are
      // fractions of the viewport so it composes right at any aspect ratio.
      // Deep + calm like the mock: modest alphas so the near-black #04050b base
      // breathes BETWEEN the blooms (the mock is mostly dark with focused blooms, not
      // a full-screen wash). Tighter radii keep negative space around them.
      var blobs = [
        { x: 0.32, y: 0.40, r: 0.42, c: A1, a: 0.42 },
        { x: 0.84, y: 0.24, r: 0.34, c: A2, a: 0.30 },
        { x: 0.20, y: 0.84, r: 0.34, c: A3, a: 0.22 },
        { x: 0.86, y: 0.84, r: 0.25, c: A4, a: 0.18 },
        { x: 0.58, y: 0.62, r: 0.30, c: A1, a: 0.20 }
      ];

      // THE compose-time pre-blur: one Gaussian pass over the whole field. 'lighter'
      // makes the blobs read luminous (screen-add) like the mock's aurora.
      ctx.globalCompositeOperation = 'lighter';
      ctx.filter = 'blur(' + Math.round(Math.min(w, h) * 0.09) + 'px)';
      var maxR = Math.max(w, h);
      for (var i = 0; i < blobs.length; i++) {
        var b = blobs[i];
        var cx = b.x * w, cy = b.y * h, rr = b.r * maxR;
        var g = ctx.createRadialGradient(cx, cy, 0, cx, cy, rr);
        g.addColorStop(0, 'rgba(' + b.c + ',' + b.a + ')');
        g.addColorStop(1, 'rgba(' + b.c + ',0)');
        ctx.fillStyle = g;
        ctx.beginPath();
        ctx.arc(cx, cy, rr, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.filter = 'none';
      ctx.globalCompositeOperation = 'source-over';

      // Canvas is now the sole bloom -> retire the CSS radial-gradient FALLBACK so the
      // two do not stack into an over-bright double-bloom (the fallback exists only for
      // the pre-JS / canvas-unavailable case). Deep #04050b then shows between the
      // canvas blobs, giving the mock's calm cosmic depth. Kept as a soft fade so the
      // handoff is invisible.
      try {
        var fb = document.querySelector('.hart-ambient');
        if (fb) { fb.style.transition = 'opacity .6s ease'; fb.style.opacity = '0'; }
      } catch (_f) {}
    } catch (e) {
      // A backdrop must never break the shell; the CSS radial-gradient fallback
      // (.hart-ambient) still shows colour if this ever fails.
      try { console.debug('hartBloom: compose failed', e); } catch (_e) {}
    }
  }

  // Debounced recompose for resize (avoid re-blurring on every resize tick).
  function scheduleCompose() {
    if (_raf) return;
    _raf = global.requestAnimationFrame(function () {
      _raf = 0;
      composeHartBloom();
    });
  }

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', composeHartBloom);
    } else {
      composeHartBloom();
    }
    // Fonts/late layout can shift the viewport metrics; recompose on full load too.
    global.addEventListener('load', composeHartBloom);
    global.addEventListener('resize', scheduleCompose);
  }

  // Exposed so the ONE mood/palette path (hartHome.js HartHome.compose) can trigger a
  // re-compose after it retints --hart-amb-*-rgb -- no parallel palette plumbing.
  global.composeHartBloom = composeHartBloom;
})(typeof window !== 'undefined' ? window : this);
