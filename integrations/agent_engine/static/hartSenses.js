/*
 * hartSenses.js — AI sensory kill-switch (human hard cut + live proof).
 *
 * A floating "eye" button: tap to SHUT the AI's senses (mic/camera/screen) and
 * tap to wake. It drives the backend gate (/api/shell/ai-sensing) which refuses
 * mic ingestion and stops the vision service, then POLLS the LIVE status so the
 * proof cannot be faked. When senses are cut, the orb "closes its eyes"
 * (darkens). The AI has no path to flip this — only this human button does.
 * Plain classic script.
 */
(function () {
  'use strict';
  var API = '/api/shell/ai-sensing';
  var cut = false, timer = null;

  function ts(ms) { return window.HartTimeoutSignal ? window.HartTimeoutSignal(ms) : null; }

  function setBlind(on) {
    var hero = document.getElementById('hart-hero');
    if (hero) hero.classList.toggle('ai-blind', !!on);
    var btn = document.getElementById('hart-senses-btn');
    if (btn) {
      btn.classList.toggle('off', !!on);
      var ic = btn.querySelector('.mi');
      if (ic) ic.textContent = on ? 'visibility_off' : 'visibility';
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
  }

  function row(label, off, detail) {
    var d = document.createElement('div');
    d.className = 'hsp-row';
    d.innerHTML = '<span class="mi material-icons-round" aria-hidden="true">' +
      (off ? 'lock' : 'lock_open') + '</span>';
    var n = document.createElement('span'); n.className = 'hsp-name'; n.textContent = label;
    var s = document.createElement('span'); s.className = 'hsp-state ' + (off ? 'off' : 'on');
    s.textContent = off ? 'SHUT' : 'sensing';
    d.appendChild(n); d.appendChild(s);
    if (detail) { var x = document.createElement('span'); x.className = 'hsp-detail'; x.textContent = detail; d.appendChild(x); }
    return d;
  }

  function renderProof(st) {
    var box = document.getElementById('hart-senses-proof');
    if (!box || !st) return;
    var d = st.disabled || {}, p = st.proof || {};
    box.innerHTML = '';
    box.appendChild(row('Hearing (mic)', !!d.mic, d.mic ? 'transcription refused' : ''));
    box.appendChild(row('Sight (camera)', !!d.camera, p.camera_service_running === false ? 'service stopped' : ''));
    box.appendChild(row('Screen', !!d.screen, ''));
    var foot = document.createElement('div');
    foot.className = 'hsp-foot';
    foot.textContent = 'Live OS state, polled — the AI cannot override this.';
    box.appendChild(foot);
  }

  function apply(st) {
    var d = (st && st.disabled) || {};
    cut = !!(d.mic || d.camera || d.screen);
    setBlind(cut);
    renderProof(st);
  }

  function refresh() {
    fetch(API, { signal: ts(4000) }).then(function (r) { return r.json(); })
      .then(apply).catch(function () {});
  }

  function toggle() {
    fetch(API, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: cut ? 'on' : 'off' }), signal: ts(4000) })
      .then(function (r) { return r.json(); }).then(function (st) {
        apply(st);
        if (window.showToast) window.showToast('AI senses',
          cut ? 'Shut — eyes & ears closed' : 'Awake', cut ? 'warning' : 'success');
        var panel = document.getElementById('hart-senses-panel');
        if (panel) panel.classList.toggle('open', cut);   // reveal the proof when cut
      }).catch(function () {});
  }

  function init() {
    var btn = document.getElementById('hart-senses-btn');
    if (!btn) { return setTimeout(init, 400); }
    btn.addEventListener('click', toggle);
    btn.addEventListener('contextmenu', function (e) {   // right-click = peek at proof
      e.preventDefault();
      var p = document.getElementById('hart-senses-panel'); if (p) p.classList.toggle('open');
    });
    refresh();
    timer = setInterval(refresh, 4000);                  // keep the proof live
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
