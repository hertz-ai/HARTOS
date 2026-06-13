/*
 * hartDock.js — macOS-style dock magnification for the HART OS taskbar (Phase D).
 *
 * As the pointer moves along the taskbar, each chip scales by its proximity to
 * the cursor (neighbours swell too), giving the buttery "dock" feel. Pure
 * enhancement over the existing #taskbar chips — no change to how they're built.
 * Machine-native: GPU transforms only, one rAF-throttled handler, skipped on the
 * low-end (potato) tier. Plain classic script.
 */
(function () {
  'use strict';
  var MAX = 0.5;    // peak extra scale at the cursor
  var RANGE = 110;  // px falloff radius
  var bar = null, raf = 0;

  function chips() { return bar ? bar.querySelectorAll('.taskbar-chip') : []; }

  function reset() {
    Array.prototype.forEach.call(chips(), function (c) { c.style.transform = ''; });
  }

  function magnify(x) {
    Array.prototype.forEach.call(chips(), function (c) {
      var r = c.getBoundingClientRect();
      var dist = Math.abs(x - (r.left + r.width / 2));
      var f = Math.max(0, 1 - dist / RANGE);
      f = f * f;                                   // ease the falloff
      c.style.transformOrigin = 'center bottom';
      c.style.transform = 'scale(' + (1 + MAX * f).toFixed(3) + ') translateY(' + (-10 * f).toFixed(1) + 'px)';
    });
  }

  function potato() {
    try { return (typeof PERF !== 'undefined') && PERF.potato; } catch (e) { return false; }
  }

  function init() {
    bar = document.getElementById('taskbar');
    if (!bar) { return setTimeout(init, 400); }
    if (potato()) return;                          // no magnify on the low-end tier
    bar.addEventListener('pointermove', function (e) {
      if (raf) return;
      var x = e.clientX;
      raf = requestAnimationFrame(function () { raf = 0; magnify(x); });
    });
    bar.addEventListener('pointerleave', reset);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
