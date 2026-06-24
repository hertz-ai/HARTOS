/*
 * hartHero.js — HART OS voice-first hero.
 *
 * Promotes the shell's EXISTING voice orb (#hart-voice-orb, driven by
 * voiceOrbViz.js + initHartOrb) to the desktop centerpiece and fuses it with a
 * central command bar (Windows-search style, but voice-native). It reuses the
 * shell's own globals so there is ONE pipeline, never a parallel one:
 *   window.toggleVoice()        — start/stop the mic (push-to-talk)
 *   window.acSend() + #ac-input — dispatch a command into the assistant
 *   window.openPanel(id)        — launch an app/panel
 *   window.toggleAssistantChat()— surface the conversation
 *   isRecording (script global) — live listening flag
 *
 * Plain classic script: loaded AFTER the inline shell script, so all of the
 * above are already defined. No framework, no bundle — the native twin of the
 * web UI, matching voiceOrbViz.js's design intent.
 */
(function () {
  'use strict';
  var DEFAULT_HINT = 'Ask HART anything - say it or type it';

  function $(id) { return document.getElementById(id); }

  // Live listening flag. isRecording is a top-level let in the inline shell
  // script (shared global lexical scope); guard against the temporal-dead-zone
  // ReferenceError on the off-chance this runs before that script evaluated.
  function listening() {
    try { return (typeof isRecording !== 'undefined') && !!isRecording; }
    catch (e) { return false; }
  }

  function start() {
    var hero = $('hart-hero'), input = $('hart-hero-input'), go = $('hart-hero-go'),
        mic = $('hart-hero-mic'), status = $('hart-hero-status'),
        chips = $('hart-hero-chips'), hev = $('hart-hero-hevolve');
    if (!hero || !input) { return setTimeout(start, 300); }

    function setStatus(t, cls) {
      if (status) { status.textContent = t; status.className = 'hart-hero-status' + (cls ? ' ' + cls : ''); }
    }
    function thinking(on) { if (hev) hev.classList.toggle('on', !!on); }

    // DETERMINISTIC-FIRST app launch: an app/panel name ("firefox", "files",
    // "settings") should open INSTANTLY via the canonical openPanel(id), never
    // crawl through the slow brain. We match against the shell's OWN registries
    // (no parallel app list): window.MANIFEST (id+title, exposed by the inline
    // shell script) and — to also cover SYSTEM_PANELS, which is a bare const and
    // not on window — the already-rendered .start-item nodes (data-id/data-title,
    // built once from MANIFEST + SYSTEM_PANELS). Returns true if it launched.
    //
    // Match is case-insensitive on id AND title: exact or prefix wins outright;
    // 1-char queries never auto-launch (too noisy); substring only for >= 3 chars
    // (2 chars = exact/prefix only) so a stray short token can't hijack a question.
    function tryLaunch(text) {
      var q = (text || '').toLowerCase().trim();
      if (q.length < 2) return false;
      var allowSub = q.length >= 3;
      function hit(id, title) {
        id = (id || '').toLowerCase(); title = (title || '').toLowerCase();
        if (id === q || title === q) return true;            // exact
        if (id.indexOf(q) === 0 || title.indexOf(q) === 0) return true;  // prefix
        return allowSub && (id.indexOf(q) >= 0 || title.indexOf(q) >= 0); // substring
      }
      // 1) Canonical MANIFEST (the real registry) — launch via openPanel(id).
      var M = window.MANIFEST;
      if (M && typeof window.openPanel === 'function') {
        for (var id in M) {
          if (!Object.prototype.hasOwnProperty.call(M, id)) continue;
          if (hit(id, (M[id] && M[id].title) || '')) { window.openPanel(id); return true; }
        }
      }
      // 2) System panels (+ anything else in the start menu) — click the rendered
      //    .start-item, which invokes the same canonical openPanel(this.dataset.id).
      var items = document.querySelectorAll('.start-item');
      for (var i = 0; i < items.length; i++) {
        var el = items[i];
        if (hit(el.dataset && el.dataset.id, el.dataset && el.dataset.title)) { el.click(); return true; }
      }
      return false;
    }

    // Dispatch into the EXISTING assistant pipeline (open chat -> fill #ac-input
    // -> acSend). Mirrors how a typed command and a spoken command converge.
    // App names launch instantly (tryLaunch); only real questions/commands fall
    // through to the slow brain.
    function dispatch(text) {
      text = (text || '').trim();
      if (!text) return;
      // Deterministic fast-path: a known app/panel name launches now, no "Thinking…".
      if (tryLaunch(text)) { input.value = ''; setStatus(DEFAULT_HINT, ''); return; }
      var chat = $('assistant-chat'), aci = $('ac-input');
      if (chat && !chat.classList.contains('open') && typeof window.toggleAssistantChat === 'function') {
        window.toggleAssistantChat();
      }
      if (aci) { aci.value = text; if (typeof window.acSend === 'function') window.acSend(); }
      input.value = '';
      setStatus('Hevolve AI is thinking…', 'thinking');
      thinking(true);
      setTimeout(function () { thinking(false); if (!listening()) setStatus(DEFAULT_HINT, ''); }, 4500);
    }

    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); dispatch(input.value); }
    });
    if (go) go.addEventListener('click', function () { dispatch(input.value); });
    if (mic) mic.addEventListener('click', function () {
      if (typeof window.toggleVoice === 'function') window.toggleVoice();
    });

    // Quick-action chips. Reuse openPanel(id) with canonical MANIFEST keys; a
    // wrong/absent key is a harmless no-op, never a fork.
    var SUGGEST = [
      ['App Store', 'app_store'], ['Security', 'security'],
      ['Appearance', 'appearance'], ['Files', 'files'], ['Weather', 'weather']
    ];
    if (chips) {
      SUGGEST.forEach(function (s) {
        var c = document.createElement('button');
        c.className = 'hart-hero-chip';
        c.type = 'button';
        c.textContent = s[0];
        c.addEventListener('click', function () {
          if (typeof window.openPanel === 'function') window.openPanel(s[1]);
        });
        chips.appendChild(c);
      });
    }

    // Reflect listening state from the existing flag (no second AudioContext).
    var wasListen = false;
    setInterval(function () {
      var l = listening();
      if (mic) mic.classList.toggle('listening', l);
      if (l) { setStatus('Listening…', 'thinking'); wasListen = true; }
      else if (wasListen) { wasListen = false; setStatus(DEFAULT_HINT, ''); }
    }, 350);

    // Speech transcript -> hero bar: the "your words become the search" moment.
    // Called from the shell's /api/voice onstop handler (mirror, then acSend
    // still runs the existing dispatch).
    window.HartHeroShowTranscript = function (t) {
      if (input && t) { input.value = t; setStatus('Heard: “' + t + '”', 'thinking'); }
    };

    // The orb/command bar is the PRIMARY composition surface — never inert
    // wallpaper. When a panel opens it gracefully DOCKS (shrinks + steps aside
    // so foreground work has room) but stays fully visible, active, and
    // reachable: the brain can still push composed UI to it and the user can
    // still speak/type a command into the spine.
    //
    // We do NOT add the legacy `dimmed` class — that set opacity:0 +
    // pointer-events:none, turning the spine into dead wallpaper (the exact
    // launcher-is-the-spine symptom this removes). Instead we toggle `docked`
    // for stylesheet hooks AND apply a self-contained dock transform inline so
    // the behaviour holds even on a shell whose CSS predates this change:
    //   - keeps opacity ~1 (visible) and pointer-events auto (clickable)
    //   - scales down + parks the spine toward the screen edge (room for work)
    // Restoring to '' on close hands the surface back to the stylesheet.
    var pc = $('panels');
    if (pc && typeof MutationObserver === 'function') {
      var apply = function () {
        var open = pc.children.length > 0;
        // Defensively strip the inert state in case any prior build set it.
        hero.classList.remove('dimmed');
        hero.classList.toggle('docked', open);
        // Self-contained dock — orb stays active + reachable, never wallpaper.
        var s = hero.style;
        if (open) {
          s.opacity = '1';
          s.pointerEvents = 'auto';
          s.transform = 'translate(-50%,-50%) scale(.62)';
          s.top = '82%';
        } else {
          s.opacity = '';
          s.pointerEvents = '';
          s.transform = '';
          s.top = '';
        }
      };
      new MutationObserver(apply).observe(pc, { childList: true });
      apply();
    }

    // Super/Win + Space = push-to-talk from anywhere on the desktop.
    document.addEventListener('keydown', function (e) {
      if ((e.code === 'Space' || e.key === ' ') && e.getModifierState && e.getModifierState('Meta')) {
        e.preventDefault();
        if (typeof window.toggleVoice === 'function') window.toggleVoice();
      }
    });

    setStatus(DEFAULT_HINT, '');
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
