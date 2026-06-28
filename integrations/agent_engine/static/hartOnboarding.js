/*
 * hartOnboarding.js — first-run "Light Your HART" ceremony (web, in-shell).
 *
 * After OS install, when the user isn't onboarded yet, this runs the narrated
 * phased ceremony as a full-screen overlay INSIDE the glass shell, driving the
 * EXISTING backend state machine:
 *   GET  /api/onboarding/status  -> skip if already lit
 *   POST /api/onboarding/start   -> language prompt
 *   POST /api/onboarding/advance -> narration + options ... -> sealed name
 *   POST /api/onboarding/advance (companion_progress) -> Nunba download bar
 * After the name seals, a final 'setup_companion' phase pre-fetches the Nunba
 * desktop companion in the background and shows a determinate progress bar
 * (skippable, retryable, graceful when offline). No separate GTK4 process => no
 * software-GL paint risk on the cage kiosk. The
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

  // ── Companion setup (final phase): a WebKit-safe progress bar that polls the
  // backend's background Nunba download. Built lazily, never traps the user.
  var compBar, compFill, compMsg, compPolls = 0;
  var COMP_MAX_POLLS = 600;   // ~12 min ceiling at the 1.2s cadence; then close

  function buildCompanion() {
    if (compBar) return;
    var wrap = document.createElement('div');
    wrap.className = 'hob-companion';
    wrap.style.cssText = 'width:min(420px,80vw);margin:18px auto 0;text-align:center';

    var msg = document.createElement('div');
    msg.className = 'hob-line in';
    msg.style.cssText = 'font-size:15px;color:#cfc9ff;margin-bottom:10px';
    msg.textContent = 'Setting up your companion...';

    var track = document.createElement('div');
    track.style.cssText = 'width:100%;height:8px;border-radius:999px;overflow:hidden;'
      + 'background:rgba(160,150,255,.18);border:1px solid rgba(160,150,255,.28)';

    var fill = document.createElement('div');
    fill.style.cssText = 'height:100%;width:0%;border-radius:999px;'
      + 'background:linear-gradient(90deg,#6c63ff,#a78bff);transition:width .4s ease';

    track.appendChild(fill);
    wrap.appendChild(msg);
    wrap.appendChild(track);
    // Sit above the options row so any Retry / Skip buttons render beneath.
    if (opts && opts.parentNode) opts.parentNode.insertBefore(wrap, opts);
    else if (overlay) overlay.appendChild(wrap);

    compBar = wrap; compFill = fill; compMsg = msg;
  }

  function startCompanion() {
    buildCompanion();
    compPolls = 0;
    clearOpts();
    pollCompanion();
  }

  function pollCompanion() { advance('companion_progress', {}); }

  function setFill(pct, indeterminate) {
    if (!compFill) return;
    if (indeterminate) {
      compFill.style.width = '100%';
      compFill.style.opacity = '0.45';
      return;
    }
    compFill.style.opacity = '1';
    var p = (typeof pct === 'number' && pct >= 0) ? pct : 0;
    if (p > 100) p = 100;
    compFill.style.width = p + '%';
  }

  function renderCompanion(c) {
    buildCompanion();
    var status = (c && c.status) || 'idle';
    var pct = c ? c.percent : null;
    var hasPct = (typeof pct === 'number');

    if (status === 'downloading' || status === 'starting' || status === 'idle') {
      if (compMsg) {
        compMsg.textContent = hasPct
          ? ('Setting up your companion... ' + pct + '%')
          : 'Setting up your companion...';
      }
      setFill(hasPct ? pct : 0, !hasPct);
      compPolls++;
      if (compPolls < COMP_MAX_POLLS) setTimeout(pollCompanion, 1200);
      else finish();          // safety: never poll forever
      return;
    }
    if (status === 'done') {
      if (compMsg) compMsg.textContent = (c && c.message) || 'Your companion is ready.';
      setFill(100, false);
      clearOpts();
      setTimeout(finish, 2600);
      return;
    }
    if (status === 'skipped') {
      if (compMsg) compMsg.textContent = (c && c.message) || 'Companion will be ready when you open it.';
      setFill(100, false);
      clearOpts();
      setTimeout(finish, 2200);
      return;
    }
    // error / offline: stop polling, offer Retry + Skip (never trap the user).
    if (compMsg) compMsg.textContent = (c && c.message) || 'Could not set up the companion right now.';
    setFill(0, false);
    clearOpts();
    button('Retry', function () { clearOpts(); compPolls = 0; advance('retry_companion', {}); });
    button('Skip for now', function () { clearOpts(); advance('skip_companion', {}); });
  }

  function handle(resp) {
    if (!resp) return;
    if (resp.already_onboarded || resp.onboarded) { finish(); return; }
    if (resp.language) lang = resp.language;

    // Companion setup phase (post-seal Nunba pre-fetch): a progress poll carries
    // a companion object. Render the bar and keep/stop polling. Never re-run
    // the ceremony narration for these.
    if (resp.companion) { renderCompanion(resp.companion); return; }

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
      // Name just sealed: reveal it, then move into the companion setup step
      // (download a progress bar drives, not the sealed auto-finish).
      if (resp.begin_companion) { revealName(resp); startCompanion(); return; }
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
