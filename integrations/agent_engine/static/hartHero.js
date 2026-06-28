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
    // The ORB ITSELF is the click-to-talk control (there is no centre mic glyph).
    // We hang the voice toggle + listening reflection on the orbwrap; the
    // #hart-voice-orb canvas stays pointer-events:none and visually untouched.
    var hero = $('hart-hero'), input = $('hart-hero-input'), go = $('hart-hero-go'),
        orb = $('hart-hero-orbwrap'), status = $('hart-hero-status'),
        chips = $('hart-hero-chips'), hev = $('hart-hero-hevolve');
    if (!hero || !input) { return setTimeout(start, 300); }

    function setStatus(t, cls) {
      if (status) { status.textContent = t; status.className = 'hart-hero-status' + (cls ? ' ' + cls : ''); }
    }
    // The hevolve dot MIRRORS the single global flag — it never owns it. acSend is
    // the CANONICAL writer of window._hartThinking (Gate 4 one-writer); the reflect
    // loop below reads that flag and lights/clears the dot, so a hero-local timer
    // can never race acSend's real terminal paths (the local 4B routinely runs
    // 64-600s — a fixed clear would make the "thinking" lamp lie). For the
    // deterministic fast-paths that DON'T call acSend (e.g. a known app launch we
    // could show a brief "Thinking…" for), the caller flips the flag and the same
    // reflect loop clears it; no second source of truth, no fixed-duration guess.
    function hevDot(on) { if (hev) hev.classList.toggle('on', !!on); }

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
      // acSend is the SOLE owner of window._hartThinking here: it sets it now and
      // clears it on its REAL terminal paths (response arrived / error / fast-path).
      // The hero must NOT arm a fixed-duration clear — the brain can run far longer
      // than any guess, and a premature flip would make the "thinking" lamp lie
      // (then snap off again when acSend resolves). We only paint our local status
      // text; the reflect loop below resets it off the real flag, never a timer.
      if (aci) { aci.value = text; if (typeof window.acSend === 'function') window.acSend(); }
      input.value = '';
      setStatus('Hevolve AI is thinking…', 'thinking');
    }

    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); dispatch(input.value); }
    });
    if (go) go.addEventListener('click', function () { dispatch(input.value); });
    // Clicking (or keyboard-activating) the orb toggles voice — the orb IS the
    // voice interface. role="button" + tabindex make it keyboard-reachable.
    function speak() { if (typeof window.toggleVoice === 'function') window.toggleVoice(); }
    if (orb) {
      orb.addEventListener('click', speak);
      orb.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ' || e.code === 'Space') { e.preventDefault(); speak(); }
      });
      // Press ripple from the click point — a tactile "you touched the orb" pulse.
      // Purely visual (pointer-events:none span removed after the animation); the
      // canvas + toggleVoice are untouched.
      orb.addEventListener('pointerdown', function (e) {
        var r = document.createElement('span'); r.className = 'lg-orb-ripple';
        var b = orb.getBoundingClientRect(), s = 120;
        r.style.cssText = 'left:' + (e.clientX - b.left - s / 2) + 'px;top:' + (e.clientY - b.top - s / 2) +
          'px;width:' + s + 'px;height:' + s + 'px';
        orb.appendChild(r);
        setTimeout(function () { if (r.parentNode) r.parentNode.removeChild(r); }, 480);
      });
    }

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

    // Reflect the orb's lit STATE from the real existing globals (no second
    // AudioContext, no second loop): isRecording (listening), _acAudio (TTS out),
    // window._hartThinking (the brain is computing — owned by acSend). The orb
    // becomes a state lamp via #hart-hero-orbwrap[data-orb-state] — the CSS in
    // _CSS_LIVING_GLASS draws accent/purple/green rings off that attr. We KEEP
    // toggling the legacy '.listening' class too so a stale build (whose CSS
    // predates data-orb-state) still shows a listening cue. The hevolve dot + the
    // hero status text are MIRRORS of the real flag here — never a fixed timer —
    // so the lamp can't lie while the brain is still in flight (64-600s on the
    // local 4B). Priority: thinking > speaking > listening > idle.
    var wasListen = false, wasThink = false;
    function ttsPlaying() {
      try { return !!(_acAudio && !_acAudio.paused && !_acAudio.ended); } catch (e) { return false; }
    }
    setInterval(function () {
      var l = listening(), think = !!window._hartThinking;
      var speak = ttsPlaying();
      var st = think ? 'thinking'
             : (speak ? 'speaking'
             : (l ? 'listening' : 'idle'));
      // Wake the orb to full presence whenever it is actually doing something
      // (voice in, TTS out, or thinking) — merge is for idle-only.
      if (l || speak || think) { try { wake(); } catch (e) {} }
      if (orb) {
        orb.setAttribute('data-orb-state', st);
        orb.classList.toggle('listening', l);   // legacy cue, superseded by data-orb-state CSS
      }
      hevDot(think);                              // dot mirrors the real flag (acSend owns it)
      // Status text follows the real state, NOT a guessed duration: listening wins,
      // else clear back to the hint once thinking truly ends (the job the removed
      // 4.5s timer used to do — now driven off acSend's actual terminal clear).
      if (l) { setStatus('Listening…', 'thinking'); wasListen = true; }
      else if (wasListen) { wasListen = false; setStatus(DEFAULT_HINT, ''); }
      else if (wasThink && !think) { setStatus(DEFAULT_HINT, ''); }
      wasThink = think;
    }, 350);

    // Speech transcript -> hero bar: the "your words become the search" moment.
    // Called from the shell's /api/voice onstop handler (mirror, then acSend
    // still runs the existing dispatch).
    window.HartHeroShowTranscript = function (t) {
      if (input && t) { input.value = t; setStatus('Heard: “' + t + '”', 'thinking'); }
    };

    // ═══════════════════════════════════════════════════════════════════════
    // ORB BEHAVIOUR — float / drag / minimize / merge / attach-to-chat.
    //
    // The orb (the whole #hart-hero spine) is a LIVING desktop object: it floats
    // above app windows, can be dragged anywhere, can shrink to a bubble, fades
    // toward the background when idle and returns on wake, and docks beside the
    // HART chat when chatting. All of this is BEST-EFFORT and never breaks the
    // existing hero/command-bar wiring (toggleVoice / acSend / openPanel).
    //
    // Gate 4 (one writer): there is exactly ONE function that writes
    // hero.style.transform / .top / .opacity — place(). It COMPOSES the four
    // independent behaviour flags below into a single transform so the dock
    // observer, the dragger, the minimiser and the idle-merge never fight over
    // the style (the prior build had the dock writing transform directly; that
    // would clobber a drag offset). Everyone else just flips a flag + calls
    // place(). The drag idiom mirrors hartSenses.js / hartDesktop.js (pointer-
    // capture, rAF-batched, drag-threshold) — no parallel drag implementation.
    // ═══════════════════════════════════════════════════════════════════════
    var B = {
      dragX: 0, dragY: 0,   // px offset from the CSS anchor (drag)
      placed: false,        // has the user dragged it (use absolute px) ?
      panelOpen: false,     // a panel/window is open -> dock aside
      chatOpen: false,      // the HART chat is open -> dock beside it
      compact: false,       // minimised to a floating bubble
      merged: false,        // faded toward the background (idle/unused)
      raf: 0
    };

    // Single source of truth for the spine's transform/opacity. It reads B and
    // paints once. translate(-50%,-50%) keeps the CSS centring contract; drag
    // adds a px delta on top; dock/chat/compact compose a scale + a nudge.
    function place() {
      var s = hero.style;
      var scale = 1, dx = B.dragX, dy = B.dragY;
      // Park toward the lower screen when a panel/chat is open so foreground
      // work has room — unless the user has dragged it somewhere on purpose.
      if (B.compact) {
        scale = 0.34;                       // small floating bubble
      } else if (B.chatOpen) {
        scale = 0.7;
        if (!B.placed) { dx += -Math.min(360, window.innerWidth * 0.26); }
      } else if (B.panelOpen) {
        scale = 0.62;
        if (!B.placed) { dy += Math.round(window.innerHeight * 0.32); }
      }
      s.transform = 'translate(-50%,-50%) translate(' + dx + 'px,' + dy + 'px) scale(' + scale + ')';
      // Merge/demerge: fade toward the background when idle + nothing open, snap
      // back to full presence on wake/interaction/voice. Never goes fully
      // invisible or pointer-dead (the spine must stay reachable — no wallpaper).
      var op = 1;
      if (B.merged && !B.compact && !B.panelOpen && !B.chatOpen) op = 0.34;
      s.opacity = String(op);
      s.pointerEvents = 'auto';
      // FLOAT OVER WINDOWS: the orb is the always-on-top brand centerpiece, so it
      // rides ABOVE app windows. The shell CSS rests #hart-hero at z-index:40, which
      // buries it BEHIND panels (they start at z 100 and a focused .panel reaches
      // z 999). We override that here so the orb floats over every app window, yet
      // stay BELOW the persistent system chrome that must remain reachable (assistant
      // chat z 1600, context menus z 3000, taskbar z 8000, lock screen z 9999,
      // onboarding z 12000) so the orb docks BESIDE the conversation, never on top of
      // it. Written here because place() is the ONE writer of hero.style (Gate 4), so
      // the float layer composes with drag/dock/merge instead of forking the style.
      s.zIndex = '1450';
      // Defensively strip the legacy inert state in case any prior build set it.
      hero.classList.remove('dimmed');
      hero.classList.toggle('docked', B.panelOpen || B.chatOpen);
      hero.classList.toggle('hart-hero-compact', B.compact);
      hero.classList.toggle('hart-hero-merged', op < 1);
    }
    function placeSoon() {
      if (B.raf) return;
      B.raf = requestAnimationFrame(function () { B.raf = 0; place(); });
    }
    // Expose so other shell modules / the brain can wake or reposition the orb
    // without reaching into our internals (read path for A2UI nudges).
    window.HartOrbWake = wake;     // hoisted below
    window.HartOrbPlace = place;

    // ── MERGE / DEMERGE: idle-fade after inactivity, full presence on wake ──
    var idleTimer = null;
    function wake() {
      if (B.merged) { B.merged = false; place(); }
      armIdle();
    }
    function armIdle() {
      if (idleTimer) clearTimeout(idleTimer);
      // Only fade when truly idle AND nothing is open AND not actively voicing.
      idleTimer = setTimeout(function () {
        if (B.panelOpen || B.chatOpen || B.compact) return;
        if (listening() || window._hartThinking) return;
        B.merged = true; place();
      }, 14000);
    }
    // Any real interaction with the spine wakes it (capture so child clicks count).
    hero.addEventListener('pointerdown', wake, true);
    hero.addEventListener('focusin', wake);
    ['mousemove', 'keydown', 'pointerdown'].forEach(function (ev) {
      document.addEventListener(ev, function () { if (B.merged) wake(); }, true);
    });

    // ── DRAG: pick the spine up from the orb (or empty hero chrome) and move it
    // anywhere. Buttons/inputs/chips still ACT (excluded from drag). A real drag
    // suppresses the orb's click-to-talk so moving it never fires the mic. ──
    var dg = { on: false, sx: 0, sy: 0, bx: 0, by: 0, moved: false, pid: null };
    function draggableTarget(t) {
      // Inputs, the send button, chips and the start chips must keep acting.
      if (!t || !t.closest) return false;
      if (t.closest('input,textarea,button,a,select,.hart-hero-chip,.hart-hero-go')) return false;
      return true;
    }
    function onDown(e) {
      if (e.button !== undefined && e.button !== 0) return;
      if (!draggableTarget(e.target)) return;
      dg.on = true; dg.moved = false;
      dg.sx = e.clientX; dg.sy = e.clientY;
      dg.bx = B.dragX; dg.by = B.dragY;
      dg.pid = e.pointerId;
      hero.classList.add('hart-hero-dragging');
      // Suppress native selection rubber-banding across the desktop while dragging.
      var de = document.documentElement;
      de.style.userSelect = 'none'; de.style.webkitUserSelect = 'none';
      try { hero.setPointerCapture(e.pointerId); } catch (_e) {}
    }
    function onMove(e) {
      if (!dg.on) return;
      var ddx = e.clientX - dg.sx, ddy = e.clientY - dg.sy;
      if (!dg.moved && Math.abs(ddx) + Math.abs(ddy) > 4) { dg.moved = true; B.placed = true; }
      if (!dg.moved) return;
      B.dragX = dg.bx + ddx; B.dragY = dg.by + ddy;
      clampDrag();
      placeSoon();
    }
    function onUp(e) {
      if (!dg.on) return;
      dg.on = false;
      hero.classList.remove('hart-hero-dragging');
      var de = document.documentElement;
      de.style.userSelect = ''; de.style.webkitUserSelect = '';
      try { hero.releasePointerCapture(e.pointerId); } catch (_e) {}
      if (dg.moved) {
        // Swallow the click the browser will synthesise after a drag so the orb
        // does NOT toggle voice when the user only meant to move it.
        var swallow = function (ev) { ev.stopPropagation(); ev.preventDefault(); hero.removeEventListener('click', swallow, true); };
        hero.addEventListener('click', swallow, true);
        setTimeout(function () { hero.removeEventListener('click', swallow, true); }, 0);
        if (window.HartSession) try { window.HartSession.set('orb_pos', { x: B.dragX, y: B.dragY }); } catch (_e2) {}
      }
      wake();
    }
    // Keep the spine on-screen (centre anchor: dragX/dragY are deltas from the
    // viewport centre, so the half-extents bound how far it can travel).
    function clampDrag() {
      var r = hero.getBoundingClientRect();
      var halfW = window.innerWidth / 2, halfH = window.innerHeight / 2;
      var maxX = halfW - 40, maxY = halfH - 40;
      // r.width already reflects the current scale; allow the centre to reach the
      // edges but keep ~40px of the spine visible.
      var slackX = Math.max(0, halfW - r.width / 2 + 80);
      var slackY = Math.max(0, halfH - r.height / 2 + 80);
      maxX = Math.min(maxX + 200, slackX); maxY = Math.min(maxY + 200, slackY);
      if (B.dragX > maxX) B.dragX = maxX; if (B.dragX < -maxX) B.dragX = -maxX;
      if (B.dragY > maxY) B.dragY = maxY; if (B.dragY < -maxY) B.dragY = -maxY;
    }
    hero.addEventListener('pointerdown', onDown);
    hero.addEventListener('pointermove', onMove);
    hero.addEventListener('pointerup', onUp);
    hero.addEventListener('pointercancel', onUp);
    window.addEventListener('resize', function () { clampDrag(); place(); });

    // Restore a previously dragged position (single HartSession reader).
    function restorePos() {
      var p = window.HartSession && window.HartSession.get('orb_pos');
      if (p && typeof p.x === 'number') { B.dragX = p.x; B.dragY = p.y; B.placed = true; clampDrag(); place(); }
    }
    if (window.HartSession && window.HartSession.ready) window.HartSession.ready(restorePos);
    else restorePos();

    // ── COMPACT / MINIMIZE / REAPPEAR: a tiny control that shrinks the spine to
    // a floating bubble (orb only) and expands it back, with a smooth transition
    // (CSS transition on transform is already on .hart-hero). Double-clicking the
    // orb also toggles compact (a natural "tuck it away" gesture) without
    // stealing the single-click voice toggle. ──
    function setCompact(on) {
      B.compact = !!on;
      if (B.compact) { B.merged = false; }  // a bubble is small but present
      // When compact, hide the bar/status/chips so the bubble is just the orb;
      // the class drives that in CSS, but we also flip aria-hidden for AT.
      var bar = hero.querySelector('.hart-hero-bar'),
          st = $('hart-hero-status'), ch = $('hart-hero-chips'),
          br = hero.querySelector('.hart-hero-brand');
      [bar, st, ch, br].forEach(function (el) {
        if (el) { el.style.display = B.compact ? 'none' : ''; }
      });
      if (minBtn) {
        minBtn.setAttribute('aria-label', B.compact ? 'Expand HART orb' : 'Minimize HART orb');
        var mi = minBtn.querySelector('.mi'); if (mi) mi.textContent = B.compact ? 'open_in_full' : 'close_fullscreen';
      }
      place();
      armIdle();
    }
    // A small, self-contained minimize affordance pinned to the orb. Built in JS
    // (we own only these files) so it works even on a shell whose CSS predates
    // this; pointer-events auto so it is clickable, excluded from drag via its
    // tag (button) in draggableTarget().
    var minBtn = null;
    if (orb) {
      minBtn = document.createElement('button');
      minBtn.type = 'button';
      minBtn.className = 'hart-hero-min';
      minBtn.setAttribute('aria-label', 'Minimize HART orb');
      minBtn.innerHTML = '<span class="mi material-icons-round" aria-hidden="true">close_fullscreen</span>';
      minBtn.style.cssText = 'position:absolute;top:6px;right:6px;width:28px;height:28px;border-radius:50%;' +
        'border:none;cursor:pointer;display:flex;align-items:center;justify-content:center;z-index:3;' +
        'background:rgba(20,22,40,.55);color:#cfe;opacity:0;transition:opacity .2s,transform .15s;' +
        '-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px)';
      var minIc = minBtn.querySelector('.mi'); if (minIc) minIc.style.fontSize = '17px';
      minBtn.addEventListener('click', function (e) { e.stopPropagation(); setCompact(!B.compact); });
      orb.appendChild(minBtn);
      // Reveal the control on hover/focus of the orb (kept subtle).
      var showMin = function () { minBtn.style.opacity = B.compact ? '1' : '0.85'; };
      var hideMin = function () { if (!B.compact) minBtn.style.opacity = '0'; };
      orb.addEventListener('pointerenter', showMin);
      orb.addEventListener('pointerleave', hideMin);
      orb.addEventListener('focusin', showMin);
      orb.addEventListener('focusout', hideMin);
      // Double-click the orb = tuck to a bubble / restore (single-click still talks).
      orb.addEventListener('dblclick', function (e) { e.preventDefault(); e.stopPropagation(); setCompact(!B.compact); });
    }

    // ── ATTACH / DETACH TO THE HART CHAT: when the conversation is open the orb
    // docks beside it (compose surface stays next to the thread); when the chat
    // closes it detaches and free-floats. Subtle, and it never traps focus (we
    // only read the chat's open state; we never focus() into it). ──
    var chatEl = $('assistant-chat');
    function syncChat() {
      var open = !!(chatEl && chatEl.classList.contains('open'));
      if (open !== B.chatOpen) { B.chatOpen = open; if (open) B.compact = false; place(); }
    }
    if (chatEl && typeof MutationObserver === 'function') {
      new MutationObserver(syncChat).observe(chatEl, { attributes: true, attributeFilter: ['class'] });
      syncChat();
    }

    // ── DOCK aside when ANY panel/window opens (the existing behaviour, now
    // routed through the single place() writer instead of a parallel transform).
    // The orb stays fully visible, active and reachable — never inert wallpaper.
    var pc = $('panels');
    if (pc && typeof MutationObserver === 'function') {
      var applyDock = function () {
        var open = pc.children.length > 0;
        if (open !== B.panelOpen) { B.panelOpen = open; if (open) B.merged = false; place(); }
      };
      new MutationObserver(applyDock).observe(pc, { childList: true });
      applyDock();
    }

    // Super/Win + Space = push-to-talk from anywhere on the desktop.
    document.addEventListener('keydown', function (e) {
      if ((e.code === 'Space' || e.key === ' ') && e.getModifierState && e.getModifierState('Meta')) {
        e.preventDefault();
        wake();
        if (typeof window.toggleVoice === 'function') window.toggleVoice();
      }
    });

    place();
    armIdle();
    setStatus(DEFAULT_HINT, '');
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
