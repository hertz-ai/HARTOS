/*
 * hartVisibility.js — the contextual / deterministic visibility engine.
 *
 * ONE tiny engine that samples REAL machine signals on a 250ms tick and stamps
 * boolean attributes on <html data-*>. All show/hide is then DECLARATIVE CSS
 * (GPU-cheap, themeable) in _CSS_LIVING_GLASS — there are no per-element JS
 * visibility hacks (the parallel-path trap). Chrome appears only when its backing
 * state makes it actionable; nothing lights on a guess or a timer.
 *
 * Gate 4 (one writer): this module is the SOLE writer of <html data-*> EXCEPT
 * `data-multiws`, which hartWorkspaces.js owns (it alone knows desktop occupancy).
 * It never touches #panels, the taskbar, or openPanel — only chrome affordances
 * (hero chips, sensory pod dimming, pager, ambient wash). The "cut senses" control
 * is explicitly exempt from hiding (safety-critical) — the CSS keeps it visible.
 *
 * Reads the shell's OWN existing globals (no parallel state): isRecording
 * (listening), _acAudio (TTS out), window._hartThinking (the brain computing).
 * Plain classic script, loaded after the inline shell JS so those globals exist.
 */
(function () {
  'use strict';
  var R = document.documentElement, last = Date.now();

  // Track real human activity for the idle signal.
  ['pointermove', 'keydown', 'pointerdown', 'wheel'].forEach(function (ev) {
    window.addEventListener(ev, function () { last = Date.now(); }, { passive: true });
  });

  function tts() { try { return !!(_acAudio && !_acAudio.paused && !_acAudio.ended); } catch (e) { return false; } }
  function rec() { try { return typeof isRecording !== 'undefined' && !!isRecording; } catch (e) { return false; } }

  // Live agent count. window.__hartAgentCount is the canonical single source IF the
  // top bar sets it; until it does, derive from the rendered #agent-status chips
  // (the same DOM the bar paints) so this is self-contained and correct today.
  function agentCount() {
    if (typeof window.__hartAgentCount === 'number') return window.__hartAgentCount;
    var bar = document.getElementById('agent-status');
    return bar ? bar.querySelectorAll('.agent-chip').length : 0;
  }

  function blind() {
    var h = document.getElementById('hart-hero');
    return !!(h && h.classList && h.classList.contains('ai-blind'));
  }

  // Only write when the value changes (avoids needless attribute churn / restyle).
  function set(k, v) {
    var s = v ? '1' : '0';
    if (R.getAttribute('data-' + k) !== s) R.setAttribute('data-' + k, s);
  }

  function tick() {
    var listening = rec(), thinking = !!window._hartThinking, speaking = tts();
    set('voice', listening);
    set('thinking', thinking);
    set('speaking', speaking);
    set('blind', blind());
    set('panels', (document.getElementById('panels') || { children: [] }).children.length > 0);
    set('typing', /^(INPUT|TEXTAREA)$/.test((document.activeElement || {}).tagName || ''));
    set('idle', (Date.now() - last) > 6000);
    set('agents', agentCount() > 0);
    set('online', typeof navigator !== 'undefined' ? navigator.onLine !== false : true);
    set('busy', listening || thinking || speaking);
    // NOTE: data-multiws is owned by hartWorkspaces.js (occupancy), NOT here.
  }

  setInterval(tick, 250);
  tick();
})();
