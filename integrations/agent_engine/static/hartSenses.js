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
    foot.textContent = 'Live OS state, polled - the AI cannot override this.';
    box.appendChild(foot);
  }

  function apply(st) {
    var d = (st && st.disabled) || {};
    var p = (st && st.proof) || {};
    cut = !!(d.mic || d.camera || d.screen);
    setBlind(cut);                                       // existing: paints '.off' (slate "shut")
    renderProof(st);
    // DETERMINISTIC eye lamp. The eye is the kill-switch for ALL the AI's senses
    // (mic + camera + screen), so its "active" lamp must reflect ANY live sense,
    // not the camera alone — otherwise the common case (hearing/screen ON, camera
    // service idle) shows a NEUTRAL eye that reads as "nothing happening" while the
    // AI is fully able to hear. Light '.is-sensing' when senses aren't cut AND at
    // least one channel is genuinely live: hearing ungated (!mic), screen ungated
    // (!screen), or the camera service actually running (the un-fakeable proof).
    // Still never lit on a guess/timer — every term is live OS state. '.is-sensing'
    // is styled in _CSS_LIVING_GLASS.
    var anySensing = (d.mic !== true) || (d.screen !== true) || (p.camera_service_running === true);
    var eye = document.getElementById('hart-senses-btn');
    if (eye) eye.classList.toggle('is-sensing', !cut && anySensing);
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
          cut ? 'Shut - eyes & ears closed' : 'Awake', cut ? 'warning' : 'success');
        var panel = document.getElementById('hart-senses-panel');
        if (panel) panel.classList.toggle('open', cut);   // reveal the proof when cut
      }).catch(function () {});
  }

  // ─────────────────────────────────────────────────────────────────────────
  // Drag + grid-snap + edge-magnetism + persistence — the missing #104 piece.
  //
  // hartSenses.js had ZERO pointer handlers (the CSS '.dragging'/grip scaffolding
  // existed but nothing moved the pod). This mirrors the PROVEN drag grammar from
  // hartDesktop.js (pointer-capture, rAF-batched transform, snap on drop) so there
  // is no parallel drag impl. The whole #hart-senses is picked up from its body
  // (or the grip); the eye/mic buttons still ACT (they're excluded from drag).
  // Position snaps to a 24px lattice, magnetises to the nearest corner, clamps to
  // the viewport, persists '{x,y,edge}' through the single HartSession writer, and
  // stamps data-edge so the proof popover opens AWAY from the screen edge.
  // ─────────────────────────────────────────────────────────────────────────
  var POD = null, SNAP = 24, EDGE = 44;
  var drag = false, sx = 0, sy = 0, ox = 0, oy = 0, raf = 0, ghost = null, moved = false;

  function place(x, y) {
    POD.style.left = x + 'px'; POD.style.top = y + 'px';
    POD.style.right = 'auto'; POD.style.bottom = 'auto';
  }
  function snap(v) { return Math.round(v / SNAP) * SNAP; }
  function clampSnap(x, y) {
    var r = POD.getBoundingClientRect(), w = window.innerWidth, h = window.innerHeight, edge = '';
    x = snap(Math.max(12, Math.min(x, w - r.width - 12)));
    y = snap(Math.max(12, Math.min(y, h - r.height - 12)));
    if (x < EDGE) { x = 12; edge += 'l'; } else if (x > w - r.width - EDGE) { x = w - r.width - 12; edge += 'r'; }
    if (y < EDGE) { y = 12; edge += 't'; } else if (y > h - r.height - EDGE) { y = h - r.height - 12; edge += 'b'; }
    POD.dataset.edge = edge || 'b';
    return { x: x, y: y, edge: POD.dataset.edge };
  }
  function mkGhost() {
    var g = document.createElement('div'); g.className = 'lg-senses-ghost';
    document.body.appendChild(g); return g;
  }
  function onDown(e) {
    if (e.target.closest('.hart-senses-btn')) return;    // buttons act, never drag
    if (e.button !== undefined && e.button !== 0) return;
    var r = POD.getBoundingClientRect(); ox = r.left; oy = r.top; place(ox, oy);
    sx = e.clientX; sy = e.clientY; drag = true; moved = false;
    POD.classList.add('dragging');
    // Suppress the browser's native text/element selection for the whole drag — without
    // this, dragging the pod by its body/grip rubber-bands a selection across the desktop
    // icons + labels underneath (the real-HW 2026-06-25 "auto-selecting text and visual
    // elements while dragging"). preventDefault stops selection STARTING; user-select:none
    // on the root stops any in-flight selection painting. Restored in onUp.
    e.preventDefault();
    var de = document.documentElement;
    de.style.userSelect = 'none'; de.style.webkitUserSelect = 'none';
    (ghost = ghost || mkGhost()).classList.add('show');
    try { POD.setPointerCapture(e.pointerId); } catch (_) {}
  }
  function onMove(e) {
    if (!drag) return;
    var dx = e.clientX - sx, dy = e.clientY - sy;
    if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
    if (raf) return;
    raf = requestAnimationFrame(function () { raf = 0; place(ox + dx, oy + dy); });
  }
  function onUp(e) {
    if (!drag) return;
    drag = false; POD.classList.remove('dragging');
    var de = document.documentElement;                   // restore native selection (suppressed in onDown)
    de.style.userSelect = ''; de.style.webkitUserSelect = '';
    if (ghost) ghost.classList.remove('show');
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    var s = clampSnap(parseInt(POD.style.left, 10) || 0, parseInt(POD.style.top, 10) || 0);
    place(s.x, s.y);
    if (moved && window.HartSession) window.HartSession.set('senses_pos', s);
    POD.classList.add('settle');
    setTimeout(function () { POD.classList.remove('settle'); }, 340);
    try { POD.releasePointerCapture(e.pointerId); } catch (_) {}
  }
  function restore() {
    var p = window.HartSession && window.HartSession.get('senses_pos');
    if (p && typeof p.x === 'number') {
      // Mirror onUp(): clampSnap RE-derives a viewport-clamped {x,y,edge} for the
      // CURRENT viewport, then we place THOSE coords (not the raw saved ones). A pod
      // saved at the bottom-right of a 1920-wide screen must not restore off-screen
      // when the shell later boots at 1280 / the window shrank — place the clamped
      // result so it lands on-screen, not just fix data-edge.
      var s = clampSnap(p.x, p.y);
      place(s.x, s.y);
    }
    // else: leave the CSS default (bottom-left, :1327).
  }
  function initDrag() {
    POD = document.getElementById('hart-senses');
    if (!POD) { return setTimeout(initDrag, 400); }
    POD.addEventListener('pointerdown', onDown);
    POD.addEventListener('pointermove', onMove);
    POD.addEventListener('pointerup', onUp);
    POD.addEventListener('pointercancel', onUp);
    window.addEventListener('resize', function () {
      if (POD.style.left) clampSnap(parseInt(POD.style.left, 10), parseInt(POD.style.top, 10));
    });
    if (window.HartSession) window.HartSession.ready(restore); else restore();
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
    initDrag();                                          // floating-draggable widget (#104)
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
