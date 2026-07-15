/*
 * hartBootSplash.js — Hevolve brand boot splash.
 *
 * Plays the Hevolve hourglass Lottie (#hart-boot-lottie) over a full-screen
 * overlay while the glass shell initialises, then fades to the desktop. Uses the
 * bundled offline lottie-web (no CDN — the ISO has no network guarantee).
 *
 * Robust by construction: the overlay ALWAYS fades — whichever is LATER of the
 * window 'load' event and a minimum brand-display time, with a hard ceiling so a
 * missing/failed player can never trap the desktop (mirrors the kiosk's
 * onboarding timeout philosophy). Honours prefers-reduced-motion (static first
 * frame + quick fade) and shows once per shell session (a soft reload won't
 * re-splash). Plain classic script, loaded before the other shell scripts.
 */
(function () {
  'use strict';
  var el = document.getElementById('hart-boot');
  var box = document.getElementById('hart-boot-lottie');
  if (!el) return;

  function remove() { if (el && el.parentNode) el.parentNode.removeChild(el); el = null; }

  // Show once per shell session — a soft reload shouldn't replay the splash.
  try {
    if (sessionStorage.getItem('hart_booted')) { remove(); return; }
    sessionStorage.setItem('hart_booted', '1');
  } catch (e) { console.debug('hartBootSplash: sessionStorage unavailable, showing splash', e); }

  var reduce = false;
  try { reduce = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); } catch (e) { console.debug('hartBootSplash: matchMedia reduced-motion probe failed', e); }
  var MIN_MS = reduce ? 600 : 2600;
  var t0 = Date.now();

  function fade() {
    if (!el) return;
    el.style.opacity = '0';
    el.style.pointerEvents = 'none';
    setTimeout(remove, 700);   // after the .6s CSS opacity transition
  }
  function finishWhenReady() {
    setTimeout(fade, Math.max(0, MIN_MS - (Date.now() - t0)));
  }

  try {
    if (box && window.lottie && typeof window.lottie.loadAnimation === 'function') {
      window.lottie.loadAnimation({
        container: box, renderer: 'svg',
        loop: !reduce, autoplay: !reduce,
        path: '/shell/static/hevolve-anim.json'
      });
    }
  } catch (e) { console.debug('hartBootSplash: lottie player failed to load (non-fatal)', e); }

  if (document.readyState === 'complete') finishWhenReady();
  else window.addEventListener('load', finishWhenReady);
  setTimeout(fade, 8000);   // hard ceiling — the splash never outlives the boot
})();
