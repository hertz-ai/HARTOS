/*
 * hartOnboarding.js — first-run "Light Your HART" ceremony (web, in-shell).
 *
 * After OS install, when the user isn't onboarded yet, this runs the narrated
 * phased ceremony as a full-screen overlay INSIDE the glass shell, driving the
 * EXISTING backend state machine:
 *   GET  /api/onboarding/status  -> skip if already lit
 *   POST /api/onboarding/start   -> language prompt
 *   POST /api/onboarding/advance -> narration + options ... -> sealed name
 * No separate GTK4 process => no software-GL paint risk on the cage kiosk. The
 * PA "speaks" via the shell's existing window.speakText when available. Esc skips
 * (never traps). Plain classic script; WebKitGTK-safe (HartTimeoutSignal).
 */
(function () {
  'use strict';
  var USER = '1';
  function ts(ms) { return window.HartTimeoutSignal ? window.HartTimeoutSignal(ms) : null; }
  function $(id) { return document.getElementById(id); }

  function api(path, method, body) {
    return fetch(path, { method: method || 'GET', signal: ts(8000),
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined }).then(function (r) { return r.json(); });
  }

  var overlay, narr, opts, lang = 'en';

  function show() {
    if (overlay) overlay.classList.add('open');
    document.documentElement.classList.add('onboarding-active');
  }
  function finish() {
    if (overlay) overlay.classList.remove('open');
    document.documentElement.classList.remove('onboarding-active');
  }
  function speak(t) { try { if (window.speakText && t) window.speakText(t, 'onboarding'); } catch (e) {} }
  function clearOpts() { if (opts) opts.innerHTML = ''; }

  function button(label, cb) {
    var b = document.createElement('button');
    b.className = 'hob-opt'; b.type = 'button'; b.textContent = label;
    b.addEventListener('click', cb);
    opts.appendChild(b);
    return b;
  }

  // Render narration lines one at a time (the PA speaking), then call done().
  function typeLines(lines, done) {
    narr.innerHTML = '';
    var i = 0;
    (function next() {
      if (i >= lines.length) { if (done) done(); return; }
      var ln = lines[i++], p = document.createElement('div');
      p.className = 'hob-line'; p.textContent = ln.text || '';
      narr.appendChild(p);
      requestAnimationFrame(function () { p.classList.add('in'); });
      speak(ln.text || '');
      setTimeout(next, ln.pause_after_ms || 1800);
    })();
  }

  function revealName(resp) {
    var name = resp.hart_name || resp.name || (resp.generated_name && resp.generated_name.name) || '';
    var emoji = resp.emoji_combo || '';
    if (name) { var el = $('hart-onboarding-name'); if (el) { el.textContent = (emoji ? emoji + ' ' : '') + '@' + name; el.classList.add('show'); } }
  }

  function advance(action, data) {
    api('/api/onboarding/advance', 'POST', { user_id: USER, action: action, data: data || {} })
      .then(handle).catch(function () {});
  }

  function handle(resp) {
    if (!resp) return;
    if (resp.already_onboarded || resp.onboarded) { finish(); return; }
    if (resp.language) lang = resp.language;

    // Language phase (from /start): a button per language, shown in its own script.
    if (resp.phase === 'language' && resp.language_prompt) {
      narr.innerHTML = '';
      var head = document.createElement('div');
      head.className = 'hob-line in'; head.textContent = resp.language_prompt.en || 'What language feels like home?';
      narr.appendChild(head);
      clearOpts();
      Object.keys(resp.language_prompt).forEach(function (code) {
        button(resp.language_prompt[code], function () { clearOpts(); advance('select_language', { language: code, locale: code + '_US' }); });
      });
      return;
    }

    var lines = resp.pa_lines || [];
    var options = resp.options || [];
    // Nothing renderable and not a known interactive phase -> close instead of
    // sitting as an invisible click-blocker / looping on empty responses.
    if (!lines.length && !options.length && resp.phase !== 'reveal' && !resp.sealed) {
      finish(); return;
    }
    clearOpts();
    typeLines(lines, function () {
      if (resp.sealed) { revealName(resp); setTimeout(finish, 6000); return; }
      if (resp.phase === 'reveal') {
        revealName(resp);
        button('Accept this name', function () { clearOpts(); advance('accept_name', {}); });
        button('Try another', function () { clearOpts(); advance('try_another', {}); });
        return;
      }
      if (options.length) {
        options.forEach(function (o) {
          var label = (o.labels && (o.labels[lang] || o.labels.en)) || o.label || o.text || o.key;
          button(label, function () { clearOpts(); advance('answer', { key: o.key }); });
        });
        return;
      }
      // No options: keep the ceremony moving (the PA pauses then continues).
      setTimeout(function () { advance(null, {}); }, resp.auto_advance_ms || 3000);
    });
  }

  function start() {
    overlay = $('hart-onboarding'); narr = $('hart-onboarding-narr'); opts = $('hart-onboarding-opts');
    if (!overlay || !narr) { return setTimeout(start, 400); }
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && overlay.classList.contains('open')) finish();   // never trap
    });
    api('/api/onboarding/status?user_id=' + USER).then(function (st) {
      if (st && st.onboarded) return;             // already lit — no ceremony
      // Reveal the (full-screen, z-12000 modal) overlay ONLY once /start returns
      // content to render. Otherwise a backend hiccup leaves an INVISIBLE
      // full-screen overlay over the desktop that silently eats EVERY click
      // ("no button works"). If start fails or is empty, never show it — the
      // desktop stays fully interactive.
      api('/api/onboarding/start', 'POST', { user_id: USER }).then(function (resp) {
        if (!resp || resp.already_onboarded || resp.onboarded) return;
        show();
        handle(resp);
      }).catch(function () { /* start failed -> never block the desktop */ });
    }).catch(function () { /* status unreachable -> never block the desktop */ });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
