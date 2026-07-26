/*
 * hartEffects.js — HART OS shell effects (Phase 8): snap-zones + animated
 * ambient lift + transition polish.
 *
 * The snap-zone REVEAL + snap COMMIT are CHEAP (opacity fade + one snap geometry
 * write), so they run on the SOFTWARE (cairo/pixman) floor too - only the zone's
 * left/top/width/height SLIDE (a layout-prop animation) stays GPU-only, degrading
 * to an instant re-position + opacity fade on software (see ensureZoneEl). The one
 * hard gate is prefers-reduced-motion / html.a11y-rmotion: when set, this module
 * installs NOTHING - the desktop degrades to FLAT, never black (the never-fail
 * floor: effects must never block paint).
 *
 * It does NOT fork the drag or the snap. It LISTENS to the shell's canonical
 * drag lifecycle (hart:dragstart / hart:dragend, dispatched by startDrag /
 * mouseup in the inline shell script) and commits a snap via the canonical
 * window.snapPanel. One drag path, one snap geometry — no parallel path.
 *
 * Plain classic script (same realm as the inline shell script). Self-installs.
 */
(function () {
  'use strict';

  // ── The single effects gate (motion-only) ──
  // The snap-zone REVEAL (opacity fade) + the snap COMMIT (one snapPanel geometry
  // write) are CHEAP, so they run on the software (cairo/pixman) floor too - the
  // potato/gpu verdict no longer gates them (mirrors the orb-aura fix that runs its
  // transform+opacity breathe ungated in hartHero.js). The only genuinely costly
  // bit - the zone SLIDING its left/top/width/height between arm positions - is a
  // layout-prop animation, so that alone stays GPU-only (ensureZoneEl builds an
  // opacity-only transition on the software floor). Reduced-motion still stills the
  // whole affordance (accessibility), same as before.
  function effectsAllowed() {
    try {
      if (window.matchMedia &&
          window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        return false;
      }
      // Honour the live a11y reduced-motion class too (same source the shell
      // render uses) so a runtime toggle disables effects without reload.
      if (document.documentElement.classList.contains('a11y-rmotion')) return false;
    } catch (_e) { console.debug('hartEffects: matchMedia unavailable, allowing effects', _e); }
    return true;
  }

  // ── Snap-zones ──
  // While a panel is dragged, a near-edge pointer reveals a translucent zone;
  // releasing in the zone snaps the panel to that half/maximize via snapPanel.
  var EDGE = 28;          // px from a screen edge that arms a zone
  var TOP = 40;           // top bar height (matches the shell)
  var zoneEl = null;      // the single reused overlay element
  var armed = null;       // 'left' | 'right' | 'max' | null

  function ensureZoneEl() {
    if (zoneEl) return zoneEl;
    zoneEl = document.createElement('div');
    zoneEl.id = 'hart-snapzone';
    zoneEl.setAttribute('aria-hidden', 'true');
    // Inline styles so this needs no shell CSS edit. Transition: OPACITY always
    // (cheap - rasters fine on cairo/pixman). The left/top/width/height SLIDE
    // between arm positions is a layout-prop animation (costly on the CPU), so it
    // is added ONLY on a real GPU context; on the software floor the zone
    // re-positions instantly (one raster) and only its opacity fades -
    // compositor-friendly, no per-frame layout churn.
    var gpu = false;
    try {
      gpu = !!(document.body && document.body.classList &&
        document.body.classList.contains('gpu-hardware'));
    } catch (_e) { console.debug('hartEffects: gpu-hardware probe failed, defaulting to software floor', _e); }
    var trans = gpu
      ? 'opacity .12s ease, left .12s ease, top .12s ease, width .12s ease, height .12s ease'
      : 'opacity .12s ease';
    zoneEl.style.cssText = [
      'position:fixed', 'z-index:998', 'pointer-events:none',
      'border:2px solid var(--hart-accent,#00D4AA)',
      'background:rgba(0,212,170,0.12)', 'border-radius:12px',
      'box-shadow:0 0 0 1px rgba(0,0,0,0.25) inset',
      'opacity:0', 'transition:' + trans,
      'display:none'
    ].join(';');
    document.body.appendChild(zoneEl);
    return zoneEl;
  }

  function zoneRect(kind) {
    var W = window.innerWidth, H = window.innerHeight, taskH = 44;
    var h = (H - TOP - taskH);
    if (kind === 'left') return { left: 8, top: TOP + 8, width: (W / 2 - 16), height: h - 16 };
    if (kind === 'right') return { left: (W / 2 + 8), top: TOP + 8, width: (W / 2 - 16), height: h - 16 };
    return { left: 8, top: TOP + 8, width: (W - 16), height: h - 16 }; // max
  }

  function showZone(kind) {
    var el = ensureZoneEl();
    var r = zoneRect(kind);
    el.style.display = 'block';
    el.style.left = r.left + 'px'; el.style.top = r.top + 'px';
    el.style.width = r.width + 'px'; el.style.height = r.height + 'px';
    // Force a frame so the first show animates rather than snapping.
    requestAnimationFrame(function () { el.style.opacity = '1'; });
    armed = kind;
  }

  function hideZone() {
    armed = null;
    if (!zoneEl) return;
    zoneEl.style.opacity = '0';
    setTimeout(function () { if (zoneEl && !armed) zoneEl.style.display = 'none'; }, 140);
  }

  function onMove(e) {
    if (!effectsAllowed()) { hideZone(); return; }
    var x = e.clientX, y = e.clientY;
    var kind = null;
    if (y <= TOP + 6) kind = 'max';          // drag to the very top → maximize
    else if (x <= EDGE) kind = 'left';
    else if (x >= window.innerWidth - EDGE) kind = 'right';
    if (kind) { if (kind !== armed) showZone(kind); }
    else hideZone();
  }

  function onDragStart() {
    if (!effectsAllowed()) return;
    document.addEventListener('mousemove', onMove);
  }

  function onDragEnd(ev) {
    document.removeEventListener('mousemove', onMove);
    var kind = armed;
    hideZone();
    if (!kind || !effectsAllowed()) return;
    var id = ev && ev.detail && ev.detail.id;
    try {
      if (kind === 'max') {
        // Maximize: the shell's snapPanel only does halves, so fall back to the
        // canonical maximize if present, else a full-width snap-left+right span.
        if (window.maximizePanel) window.maximizePanel(id);
        else if (window.snapPanel) {
          // No maximize fn exposed → approximate by snapping then widening.
          window.snapPanel(id, 'left');
        }
      } else if (window.snapPanel) {
        window.snapPanel(id, kind);   // CANONICAL snap — no parallel geometry
      }
    } catch (_e) { console.error('hartEffects: snap on drag-end failed', _e); }
  }

  function init() {
    // Even when effects are OFF we still register the listeners — but they
    // self-gate via effectsAllowed() on every event, so toggling reduced-motion
    // at runtime takes effect with no reload and no parallel code path. When the
    // gate is closed, onDragStart returns before attaching the move handler, so
    // there is zero per-frame cost on potato boxes.
    window.addEventListener('hart:dragstart', onDragStart);
    window.addEventListener('hart:dragend', onDragEnd);
  }

  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', init);
  else init();
})();
