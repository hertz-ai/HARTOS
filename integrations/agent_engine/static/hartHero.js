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
  var DEFAULT_HINT = 'Ask HART anything — say it or type it';

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

    // Dispatch into the EXISTING assistant pipeline (open chat -> fill #ac-input
    // -> acSend). Mirrors how a typed command and a spoken command converge.
    function dispatch(text) {
      text = (text || '').trim();
      if (!text) return;
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

    // The hero is the desktop backdrop: gracefully dim it when any window opens
    // so foreground work owns the screen (macOS-desktop behaviour).
    var pc = $('panels');
    if (pc && typeof MutationObserver === 'function') {
      var apply = function () { hero.classList.toggle('dimmed', pc.children.length > 0); };
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
