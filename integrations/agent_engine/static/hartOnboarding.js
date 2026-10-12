/*
 * hartOnboarding.js: first-run "Light Your HART", hosted by Nunba.
 *
 * Since 2026-10-12 the OS asks nothing of its own at first boot. Nunba ships
 * preinstalled (hart-nunba.service), and its /local page runs the ceremony
 * whenever the person has not sealed a name (Nunba's Agent.js gates on the
 * localStorage flag 'hart_sealed'). The shell serves Nunba through its own
 * proxy, so Nunba's origin IS the shell's: this driver reads the SAME flag
 * Nunba's gate reads, hosts Nunba's page full-screen in #hart-onboarding while
 * the flag is unset, and closes when the flag flips (the browser's 'storage'
 * event from Nunba's frame) or when Nunba says so ('nunba:onboarding', posted
 * by its utils/shellBridge when the ceremony seals). The duplicate OS ceremony
 * that lived here (language, passion, escape, name reveal, companion download)
 * is gone; the backend state machine stays for Nunba's own /api/hart/advance.
 *
 * Before any of that, /api/onboarding/status says whether the Nunba daemon is
 * up (companion_ready) and whether the desktop owner's seal is already on
 * record (onboarded): a page that cannot load is never shown full-screen, and
 * a sealed owner never sees the ceremony again.
 *
 * Esc and the Skip control still finish (never trap). 'onboarding-active' on
 * <html> keeps its meaning for hartSessionUI.js (the lock-password offer).
 * Plain classic script; WebKitGTK-safe (HartTimeoutSignal, no template
 * literals).
 */
(function () {
  'use strict';
  var SEAL_KEY = 'hart_sealed';          // Nunba's own gate (landing-page Agent.js), same origin
  var ROUTE = '/local';                  // Nunba's chat page; the ceremony is its first-run state
  var SEALED_WORD = 'nunba:onboarding';  // utils/shellBridge, posted by Nunba's Agent.js on seal
  var POLL_MS = 2000;
  var PROBE_EVERY_MS = 5000, PROBE_MAX = 90;   // ~7.5 min: a cold USB boot of backend + Nunba

  function ts(ms) { return window.HartTimeoutSignal ? window.HartTimeoutSignal(ms) : null; }
  function $(id) { return document.getElementById(id); }
  function api(path) {
    return fetch(path, { method: 'GET', signal: ts(8000) }).then(function (r) { return r.json(); });
  }
  // NUNBA_BASE is the inline shell's const (same origin, '' on the node).
  function base() { return (typeof NUNBA_BASE === 'string') ? NUNBA_BASE : ''; }
  function sealed() {
    try { return localStorage.getItem(SEAL_KEY) === 'true'; } catch (e) { return false; }
  }
  function focusEl(el) {
    if (el && typeof el.focus === 'function') { try { el.focus(); } catch (e) { console.debug('hartOnboarding: focusEl failed', e); } }
  }

  var overlay, frame = null, skipBtn = null, isOpen = false, poll = null;

  // The always-visible, keyboard-reachable Skip control + an "Esc to skip" hint
  // (#134: a dead pointer must never lock the user out). Built once; it reuses
  // the SAME finish() the Esc hatch calls, so there is ONE exit path.
  function ensureSkip() {
    if (skipBtn || !overlay) return skipBtn;
    var wrap = document.createElement('div');
    wrap.className = 'hob-skip-wrap';
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'hob-opt hob-skip-btn';
    b.textContent = 'Skip setup';
    if (typeof b.setAttribute === 'function') b.setAttribute('aria-label', 'Skip setup (or press Esc)');
    b.addEventListener('click', function () { finish(); });
    var hint = document.createElement('div');
    hint.className = 'hob-skip-hint';
    hint.textContent = 'Esc to skip';
    wrap.appendChild(b);
    wrap.appendChild(hint);
    overlay.appendChild(wrap);
    skipBtn = b;
    return b;
  }

  // Nunba's page, mounted only while the ceremony is due (a sealed node never
  // loads a second copy of the SPA at boot).
  function mount() {
    if (frame) return frame;
    var f = document.createElement('iframe');
    f.className = 'hob-frame';
    f.id = 'hart-onboarding-frame';
    f.title = 'Light Your HART';
    if (typeof f.setAttribute === 'function') f.setAttribute('allow', 'microphone; camera; autoplay');
    f.src = base() + ROUTE;
    overlay.appendChild(f);
    frame = f;
    return f;
  }
  function unmount() {
    if (frame && frame.parentNode && typeof frame.parentNode.removeChild === 'function') {
      frame.parentNode.removeChild(frame);
    }
    frame = null;
  }

  function show() {
    if (isOpen) return;
    isOpen = true;
    mount();
    overlay.classList.add('open');
    document.documentElement.classList.add('onboarding-active');
    ensureSkip();
    // Focus lands on the shell's own control; Tab moves into Nunba's page.
    requestAnimationFrame(function () { focusEl(skipBtn); });
    // The storage event is the signal; this poll is the floor under it (a
    // hydration that landed before the listener, a WebKit that drops the event).
    if (!poll) poll = setInterval(function () { if (sealed()) finish(); }, POLL_MS);
  }
  function finish() {
    isOpen = false;
    overlay.classList.remove('open');
    document.documentElement.classList.remove('onboarding-active');
    if (poll) { clearInterval(poll); poll = null; }
    // Drop Nunba's frame: from here the conversation lives in the assistant
    // panel; two live copies of the page would be two chats.
    unmount();
  }

  function start() {
    overlay = $('hart-onboarding');
    if (!overlay) { return setTimeout(start, 400); }
    ensureSkip();
    document.addEventListener('keydown', function (e) {
      if (!overlay.classList.contains('open')) return;
      if (e.key === 'Escape') { finish(); return; }
      if (e.key === 'Tab') {
        // The trap has two stops: the Skip control and Nunba's page (which runs
        // its own Tab order inside the frame). Never the hidden desktop behind.
        var next = (document.activeElement === skipBtn && frame) ? frame : skipBtn;
        if (!next) return;
        if (e.preventDefault) e.preventDefault();
        focusEl(next);
      }
    });
    // Nunba's frame wrote the seal (same origin, another window): close.
    window.addEventListener('storage', function (e) {
      if (!isOpen) return;
      if (e && e.key && e.key !== SEAL_KEY) return;
      if (sealed()) finish();
    });
    // Nunba says so itself (its Agent.js posts the seal through utils/shellBridge).
    window.addEventListener('message', function (e) {
      if (!isOpen || !e || e.origin !== location.origin) return;
      var d = e.data;
      if (!d || typeof d !== 'object' || d.type !== SEALED_WORD) return;
      if (frame && e.source && frame.contentWindow && e.source !== frame.contentWindow) return;
      if (d.state === 'sealed') finish();
    });
    probe(0);
  }

  // BOUNDED RETRY, NOT ONE-SHOT (2026-08-16, real-HW regression): the shell
  // paints in seconds; the backend behind the status route and the Nunba
  // daemon legitimately take minutes on slow media. Retry on the not-ready
  // answers only; never block the desktop; never open the overlay unless
  // Nunba is there to fill it.
  function reprobe(attempt, why) {
    if (attempt + 1 >= PROBE_MAX) {
      console.debug('hartOnboarding: Nunba never became ready; giving up', why);
      return;
    }
    setTimeout(function () { probe(attempt + 1); }, PROBE_EVERY_MS);
  }
  function probe(attempt) {
    if (sealed()) return;                      // Nunba's own gate says lit: no ceremony
    api('/api/onboarding/status').then(function (st) {
      if (!st) return reprobe(attempt, 'status empty');
      if (st.onboarded) return;                // the owner's seal is on record
      if (!st.companion_ready) return reprobe(attempt, 'nunba not up');
      if (sealed()) return;
      show();
    }).catch(function (e) {
      console.debug('hartOnboarding: /status unreachable (never block desktop)', e);
      reprobe(attempt, e);
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
