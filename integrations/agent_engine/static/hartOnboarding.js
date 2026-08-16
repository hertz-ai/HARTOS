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

  var overlay, narr, opts, skipBtn, lang = 'en';

  // ── Keyboard-reachable navigation (a dead pointer must NEVER lock the user out:
  // the real-HW symptom is the pointer frozen at 0,0). The whole flow is keyboard
  // navigable — Tab cycles the controls, Enter activates a native <button>, and the
  // always-visible Skip control + the Esc hatch both finish. Every focus call is
  // guarded so the dependency-free test DOM (no .focus) is a no-op, never a throw. ──
  function focusEl(el) {
    if (el && typeof el.focus === 'function') { try { el.focus(); } catch (e) { console.debug('hartOnboarding: focusEl failed', e); } }
  }

  // The focusable controls currently in the overlay, in Tab order: the rendered
  // option buttons first, then the always-present Skip control. Walks children so
  // it works in both the real DOM and the dependency-free test shim.
  function controls() {
    var list = [], kids = (opts && opts.children) || [], i;
    for (i = 0; i < kids.length; i++) {
      if (kids[i] && kids[i].tagName === 'BUTTON') list.push(kids[i]);
    }
    if (skipBtn) list.push(skipBtn);
    return list;
  }

  // Pull focus to the first actionable control of a screen (the first option, or
  // the Skip control on a narration-only screen) so the flow is drivable from the
  // keyboard the instant it renders — no pointer required.
  function focusFirst() {
    var c = controls();
    focusEl(c.length ? c[0] : null);
  }

  // The always-visible, keyboard-reachable Skip control + an "Esc to skip" hint.
  // Built once and appended to the overlay (hidden with it: a display:none ancestor
  // hides it), and it reuses the SAME finish() the Esc hatch calls — ONE exit path,
  // no parallel skip semantics. Created here (not in the server markup) because this
  // driver owns the overlay's interactive controls.
  function ensureSkip() {
    if (skipBtn || !overlay) return skipBtn;
    // The server ships a static "Press Esc to skip" text node (.hob-skip — a div,
    // not an actionable control). Hide it so there are not two skip affordances.
    // Guarded: the test DOM shim has no querySelector -> skipped silently.
    try {
      if (typeof overlay.querySelector === 'function') {
        var legacy = overlay.querySelector('.hob-skip');
        if (legacy && legacy.tagName !== 'BUTTON' && legacy.style) legacy.style.display = 'none';
      }
    } catch (e) { console.debug('hartOnboarding: legacy skip hide failed', e); }

    var wrap = document.createElement('div');
    wrap.className = 'hob-skip-wrap';
    wrap.style.cssText = 'position:fixed;left:0;right:0;bottom:18px;display:flex;'
      + 'flex-direction:column;align-items:center;gap:6px;z-index:1';

    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'hob-opt hob-skip-btn';
    b.textContent = 'Skip setup';
    if (typeof b.setAttribute === 'function') b.setAttribute('aria-label', 'Skip setup (or press Esc)');
    b.style.cssText = 'padding:8px 20px;font-size:13px';
    b.addEventListener('click', function () { finish(); });

    var hint = document.createElement('div');
    hint.className = 'hob-skip-hint';
    hint.textContent = 'Esc to skip';
    hint.style.cssText = 'font-size:12px;color:rgba(255,255,255,.45)';

    wrap.appendChild(b);
    wrap.appendChild(hint);
    overlay.appendChild(wrap);
    skipBtn = b;
    return b;
  }

  function show() {
    if (overlay) overlay.classList.add('open');
    document.documentElement.classList.add('onboarding-active');
    ensureSkip();
    // Pull focus INTO the modal so the keyboard can drive it even with a dead
    // pointer. A frame later so the just-shown overlay is focusable; rAF runs
    // synchronously in the test shim -> a no-op there, real focus on WebKit.
    requestAnimationFrame(function () { focusFirst(); });
  }
  function finish() {
    if (overlay) overlay.classList.remove('open');
    document.documentElement.classList.remove('onboarding-active');
  }
  // Respect the human's AI-sensing kill-switch. hartSenses.js paints the
  // canonical '.ai-blind' flag on #hart-hero when the human shuts the AI's senses
  // (that file is the ONE writer; we only READ the flag - no parallel state).
  // While senses are cut the ceremony still SHOWS and stays skippable, but the PA
  // stays SILENT (the AI must not speak when the human has shut it). Guarded so a
  // DOM without the node (or the dependency-free test shim) is a no-op, never a throw.
  function sensesCut() {
    try {
      var hero = document.getElementById('hart-hero');
      return !!(hero && hero.classList && hero.classList.contains('ai-blind'));
    } catch (e) { return false; }
  }
  // TTS the ceremony line through the shell's canonical voice path (window.speakText
  // -> POST /api/voice/speak -> the Model Bus TTS router). No second TTS path: this
  // is the same helper chat replies + the greeting use. No-op when TTS is
  // unreachable (potato / not yet loaded) or the senses are cut.
  function speak(t) {
    try { if (window.speakText && t && !sensesCut()) window.speakText(t, 'onboarding'); } catch (e) { console.debug('hartOnboarding: speakText failed', e); }
  }
  function clearOpts() { if (opts) opts.innerHTML = ''; }

  function button(label, cb) {
    var b = document.createElement('button');
    b.className = 'hob-opt'; b.type = 'button'; b.textContent = label;
    b.addEventListener('click', cb);
    opts.appendChild(b);
    // First option of a freshly-rendered screen (clearOpts wiped opts): pull focus
    // onto it so Tab/Enter can pick among the options without a pointer. rAF is
    // synchronous in the test shim (a guarded no-op), real on WebKit.
    if (opts.children && opts.children.length === 1) {
      requestAnimationFrame(function () { focusEl(b); });
    }
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
      .then(handle).catch(function (e) { console.error('hartOnboarding: advance POST failed', e); });
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
    msg.style.cssText = 'font-size:15px;color:#c9f5ee;margin-bottom:10px';
    msg.textContent = 'Setting up your companion...';

    var track = document.createElement('div');
    track.style.cssText = 'width:100%;height:8px;border-radius:999px;overflow:hidden;'
      + 'background:rgba(0,230,195,.16);border:1px solid rgba(0,230,195,.30)';

    // Brand duotone: teal LEADS, violet ACCENTS (b1.2). Replaces the old
    // indigo-to-light-indigo fill so the onboarding matches the OS brand, not the
    // deprecated indigo.
    var fill = document.createElement('div');
    fill.style.cssText = 'height:100%;width:0%;border-radius:999px;'
      + 'background:linear-gradient(90deg,#00E6C3,#9B5CFF);transition:width .4s ease';

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
    ensureSkip();   // the keyboard-reachable Skip control exists before the overlay opens
    // Keyboard navigation for the WHOLE flow: Esc finishes (the never-trap hatch),
    // and Tab / Shift+Tab CYCLE focus among the overlay's own controls (a focus
    // trap) so a dead pointer can drive every screen and can never tab into the
    // hidden desktop behind this z-12000 modal.
    document.addEventListener('keydown', function (e) {
      if (!overlay.classList.contains('open')) return;
      if (e.key === 'Escape') { finish(); return; }            // never trap (the Esc hatch)
      if (e.key === 'Tab') {
        var c = controls();
        if (!c.length) return;
        var active = document.activeElement, idx = -1, i;
        for (i = 0; i < c.length; i++) { if (c[i] === active) { idx = i; break; } }
        var dir = e.shiftKey ? -1 : 1;
        var next = idx < 0 ? 0 : (idx + dir + c.length) % c.length;
        if (e.preventDefault) e.preventDefault();
        focusEl(c[next]);
      }
    });
    probe(0);
  }

  // BOUNDED RETRY, NOT ONE-SHOT (2026-08-16 — real-HW regression).
  // The probe used to run exactly once at DOMContentLoaded and give up silently
  // on any failure. The shell paints in SECONDS; the backend behind these
  // endpoints imports the ML stack first and legitimately takes MINUTES on slow
  // media (hart-backend.nix pins TimeoutStartSec=600 and documents ~170s of
  // imports, "far slower on USB"). So on a real boot the ceremony asked before
  // the backend could answer, returned silently, and never asked again: no
  // "Light Your HART" screen, and therefore no first-run password setup after
  // it (hartSessionUI.js) and no lock password. Both screens vanished, on a
  // machine where everything was actually working — it was a RACE, not state.
  // Retry on the not-ready answers only, keeping every existing guarantee:
  // never block the desktop, and never open the overlay unless /start returned
  // real content to render.
  var PROBE_EVERY_MS = 5000, PROBE_MAX = 90;   // ~7.5 min, covers a cold USB boot

  function reprobe(attempt, why) {
    if (attempt + 1 >= PROBE_MAX) {
      console.debug('hartOnboarding: backend never became ready; giving up', why);
      return;
    }
    setTimeout(function () { probe(attempt + 1); }, PROBE_EVERY_MS);
  }

  function probe(attempt) {
    api('/api/onboarding/status?user_id=' + USER).then(function (st) {
      if (st && st.onboarded) return;             // already lit — no ceremony
      if (!st) return reprobe(attempt, 'status empty');
      // Reveal the (full-screen, z-12000 modal) overlay ONLY once /start returns
      // content to render. Otherwise a backend hiccup leaves an INVISIBLE
      // full-screen overlay over the desktop that silently eats EVERY click
      // ("no button works"). If start fails or is empty, never show it — the
      // desktop stays fully interactive.
      api('/api/onboarding/start', 'POST', { user_id: USER }).then(function (resp) {
        if (resp && (resp.already_onboarded || resp.onboarded)) return;
        if (!resp) return reprobe(attempt, 'start empty');
        show();
        handle(resp);
      }).catch(function (e) {
        console.debug('hartOnboarding: /start failed (never block desktop)', e);
        reprobe(attempt, e);
      });
    }).catch(function (e) {
      console.debug('hartOnboarding: /status unreachable (never block desktop)', e);
      reprobe(attempt, e);
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
