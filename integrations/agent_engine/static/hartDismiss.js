/*
 * hartDismiss.js - ONE dismissal set for every floating sheet in the shell.
 *
 * WHY THIS EXISTS (box, 2026-09-22): the Wi-Fi popover closed on a mousedown
 * captured in the shell document and the start menu on a bubbling click, so
 * both stayed open when the press landed somewhere the host document never
 * sees: another Wayland surface, or an iframed panel. The context menu already
 * carried the complete set (pointerdown capture, Escape, scroll, resize, window
 * blur). That set is extracted here, unchanged in behaviour, and every sheet
 * arms it: the context menu (hartContextMenu.js), the quick-settings popover
 * (hartConnectivity.js), the senses proof panel (hartSenses.js) and the start
 * menu (the inline shell script).
 *
 * Why blur is the iframe-aware half: a press inside an iframe moves focus into
 * the nested browsing context, and the host window fires 'blur'. A press on
 * another surface takes keyboard focus from the WebView, also 'blur'. Neither
 * delivers a pointer event to this document, so pointerdown alone can never
 * close a sheet in those two cases.
 *
 * Public API (the ONLY surface other modules use):
 *   var disarm = window.HartDismiss.arm({
 *     els:       [el | function () -> el, ...]   the sheet's own bounds (a press
 *                                              inside any of them is not a dismissal;
 *                                              include the button that toggles it so
 *                                              its own click is one toggle, not two)
 *     inside:    function (target) -> bool     alternative to els for odd shapes
 *     onDismiss: function (reason, event)      reason: pointer|escape|scroll|resize|blur
 *   });
 *   disarm();   remove the whole set (call from the sheet's close path; idempotent)
 *
 * The set fires ONCE: it disarms itself before calling onDismiss, so a close
 * path that calls disarm() again is a no-op and nothing leaks across cycles.
 * Escape is consumed (preventDefault + stopPropagation) so it wins over the
 * shell's global shortcut table, exactly as the context menu did; every other
 * key is left to the sheet (keyboard navigation stays where it was). A scroll
 * whose target is inside the sheet (its own list) is not a dismissal.
 *
 * Classic script for OLD WebKitGTK: var/function, no template literals, no
 * optional chaining. Loaded deferred before every consumer.
 */
(function () {
  'use strict';

  function makeInside(opts) {
    if (typeof opts.inside === 'function') return opts.inside;
    var els = opts.els || [];
    return function (target) {
      if (!target) return false;
      for (var i = 0; i < els.length; i++) {
        var el = (typeof els[i] === 'function') ? els[i]() : els[i];
        if (el && el.contains && el.contains(target)) return true;
      }
      return false;
    };
  }

  function arm(opts) {
    opts = opts || {};
    var inside = makeInside(opts);
    var armed = true;

    function fire(reason, e) {
      if (!armed) return;
      disarm();
      try { if (typeof opts.onDismiss === 'function') opts.onDismiss(reason, e); }
      catch (err) { console.error('hartDismiss: onDismiss threw', err); }
    }
    function onPointerDown(e) { if (!inside(e.target)) fire('pointer', e); }
    function onKeyDown(e) {
      if (e.key !== 'Escape' && e.key !== 'Esc') return;
      e.preventDefault();
      e.stopPropagation();
      fire('escape', e);
    }
    function onScroll(e) { if (e && inside(e.target)) return; fire('scroll', e); }
    function onResize(e) { fire('resize', e); }
    function onBlur(e) { fire('blur', e); }

    function disarm() {
      if (!armed) return;
      armed = false;
      document.removeEventListener('pointerdown', onPointerDown, true);
      document.removeEventListener('keydown', onKeyDown, true);
      window.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', onResize, true);
      window.removeEventListener('blur', onBlur);
    }

    // pointerdown (capture) so we dismiss before any other click handler runs;
    // keydown (capture) so Escape wins over the shell's global key shortcuts;
    // scroll/resize (capture) because scroll does not bubble; blur on window.
    document.addEventListener('pointerdown', onPointerDown, true);
    document.addEventListener('keydown', onKeyDown, true);
    window.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', onResize, true);
    window.addEventListener('blur', onBlur);
    return disarm;
  }

  window.HartDismiss = { arm: arm };
})();
