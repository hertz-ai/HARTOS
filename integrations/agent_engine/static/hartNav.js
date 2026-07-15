/* ═══════════════════════════════════════════════════════════════════════════
 * hartNav.js — the unified shell-wide navigation framework (#169).
 *
 * HART OS opens every surface (agents, recipes, communities, system panels,
 * installed apps) as a floating panel through ONE canonical entry point:
 * openPanel(id, opts) + the single-instance panels registry. That registry
 * already REUSES by default — opening an already-open panel just brings it to
 * front. hartNav builds two things ON TOP of that one registry, without forking
 * it:
 *
 *   1. A shell-wide back/forward/breadcrumb HISTORY where the panel id IS the
 *      location — the exact generalisation of hartFiles.js's proven
 *      navigate/back/forward/up primitive, lifted from "folders in one window"
 *      to "panels across the whole shell".
 *
 *   2. An explicit reuse-vs-NEW-INSTANCE policy: reuse stays the default;
 *      openPanel(id, {newInstance:true}) opts into a second coexisting instance,
 *      for which hartNav mints a distinct instance id (base#N).
 *
 * The CORE (window.HartNavCore) is a pure, DOM-free module: a history stack + the
 * reuse-vs-new decision. It is unit-tested headless by tests/unit/test_hart_nav.mjs
 * (no jsdom needed). The WIRING layer (window.HartNav) connects that core to the
 * shell's openPanel/bringToFront/panels and renders the persistent nav chrome.
 *
 * Plain classic IIFE, loaded via <script defer> AFTER the inline shell script
 * (which defines openPanel/panels/bringToFront) and after hartFiles.js.
 * ═══════════════════════════════════════════════════════════════════════════ */
(function (global) {
  'use strict';

  /* ── pure helper: mint the next free instance id for a base panel ──────────
   * openIds = the ids currently open. The plain base id is instance #1; the next
   * new instance is base#2, then base#3… If nothing for this base is open yet, a
   * "new instance" simply IS the plain base id (don't gratuitously suffix). */
  function nextInstance(base, openIds) {
    base = ('' + base).split('#')[0];
    openIds = openIds || [];
    var any = false, max = 1;
    for (var i = 0; i < openIds.length; i++) {
      var oid = '' + openIds[i];
      if (oid === base) { any = true; continue; }
      var m = oid.match(/^(.+)#(\d+)$/);
      if (m && m[1] === base) {
        any = true;
        var n = parseInt(m[2], 10);
        if (n > max) max = n;
      }
    }
    if (!any) return base;            // nothing open for this base -> the plain id
    return base + '#' + (max + 1);
  }

  /* ── the pure, DOM-free navigation core ────────────────────────────────────
   * A linear history of {id, title} with a pointer, exactly like a browser's
   * per-tab history. push() truncates any forward entries (a new navigation from
   * the middle discards the redo tail); back()/forward() move the pointer;
   * remove() drops a closed location and keeps the pointer valid. */
  function createNavCore() {
    var stack = [];   // [{id, title}]
    var idx = -1;     // pointer into stack (-1 == empty)

    function current() { return (idx >= 0 && idx < stack.length) ? stack[idx] : null; }

    return {
      stack: function () { return stack.slice(); },
      index: function () { return idx; },
      current: current,
      canBack: function () { return idx > 0; },
      canForward: function () { return idx >= 0 && idx < stack.length - 1; },

      /* Record a navigation to loc={id,title}. Navigating to the SAME id we're
       * already on is a no-op for the stack (reuse: re-focusing the current
       * panel doesn't add a duplicate history entry) — it only refreshes title. */
      push: function (loc) {
        if (!loc || !loc.id) return current();
        var cur = current();
        if (cur && cur.id === loc.id) {
          if (loc.title) cur.title = loc.title;
          return cur;
        }
        stack = stack.slice(0, idx + 1);              // drop the forward (redo) tail
        stack.push({ id: loc.id, title: loc.title || loc.id });
        idx = stack.length - 1;
        return current();
      },

      back: function () { if (idx > 0) idx--; return current(); },
      forward: function () { if (idx < stack.length - 1) idx++; return current(); },

      /* Remove EVERY entry for id (a closed panel) and keep the pointer valid. */
      remove: function (id) {
        for (var i = stack.length - 1; i >= 0; i--) {
          if (stack[i].id === id) {
            stack.splice(i, 1);
            if (i <= idx) idx--;
          }
        }
        if (stack.length === 0) idx = -1;
        else if (idx < 0) idx = 0;
        else if (idx >= stack.length) idx = stack.length - 1;
        return current();
      },

      /* Pure reuse-vs-new-instance decision. openIds = currently-open panel ids.
       *   - {newInstance:true}  -> always CREATE, on a freshly-minted instance id
       *   - id already open      -> FOCUS it (the default reuse policy)
       *   - else                 -> CREATE it (first open) */
      decideOpen: function (id, opts, openIds) {
        opts = opts || {};
        openIds = openIds || [];
        var base = ('' + id).split('#')[0];
        if (opts.newInstance) {
          return { instanceId: nextInstance(base, openIds), action: 'create', base: base };
        }
        if (openIds.indexOf(id) >= 0) return { instanceId: id, action: 'focus', base: base };
        return { instanceId: id, action: 'create', base: base };
      },

      nextInstance: function (base, openIds) { return nextInstance(base, openIds); }
    };
  }

  /* Export the pure core so a bundler / node --check / the .mjs unit test can
   * construct a fresh instance headlessly. */
  global.HartNavCore = createNavCore;
  global.HartNavNextInstance = nextInstance;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { createNavCore: createNavCore, nextInstance: nextInstance };
  }

  /* ── the DOM wiring layer ──────────────────────────────────────────────────
   * Below here we touch the DOM; guard everything so a headless load (the unit
   * test) that has no document still evaluates the pure core above cleanly. */
  if (typeof document === 'undefined') return;

  var core = createNavCore();
  var openSet = {};              // id -> true, the set of live panels hartNav knows
  var chrome = null;             // the #hart-nav chrome element (lazy)

  function openIds() { return Object.keys(openSet); }

  /* Focus a panel by id WITHOUT recording new history — used by back/forward so a
   * history move never pushes a fresh entry. Reopen only if it was closed. */
  function focusOnly(id) {
    if (!id) return;
    if (openSet[id] && typeof global.bringToFront === 'function') {
      // bringToFront already un-minimises (p.min) before raising — canonical.
      try { global.bringToFront(id); } catch (e) { console.error('hartNav: bringToFront failed for ' + id, e); }
    } else if (typeof global.openPanel === 'function') {
      try { global.openPanel(id); } catch (e) { console.error('hartNav: openPanel failed for ' + id, e); }
    }
  }

  var HartNav = {
    core: core,

    /* Called by openPanel AFTER a panel is opened OR re-focused: record the
     * location and remember it as open. Idempotent for a re-focus (reuse). */
    onOpen: function (id, title) {
      if (!id) return;
      openSet[id] = true;
      core.push({ id: id, title: title || id });
      render();
    },

    /* Called by closePanel: forget the panel + drop it from history. */
    onClose: function (id) {
      if (!id) return;
      delete openSet[id];
      core.remove(id);
      render();
    },

    /* Mint the next instance id for a base panel from the LIVE open set. */
    nextInstance: function (base) { return core.nextInstance(base, openIds()); },

    /* Pure reuse-vs-new decision against the live open set. */
    decide: function (id, opts) { return core.decideOpen(id, opts, openIds()); },

    /* Convenience: open a second coexisting instance of a panel. */
    openNew: function (id) {
      if (typeof global.openPanel === 'function') global.openPanel(id, { newInstance: true });
    },

    back: function () {
      if (!core.canBack()) return;
      var e = core.back();
      if (e) focusOnly(e.id);
      render();
    },
    forward: function () {
      if (!core.canForward()) return;
      var e = core.forward();
      if (e) focusOnly(e.id);
      render();
    },
    /* "Up" = surface the desktop: minimise the current panel so the home canvas
     * (the parent of any window) shows. Falls back to a no-op with nothing open. */
    up: function () {
      var e = core.current();
      if (e && openSet[e.id] && typeof global.minimizePanel === 'function') {
        try { global.minimizePanel(e.id); } catch (x) { console.error('hartNav: minimizePanel failed for ' + e.id, x); }
      }
    }
  };
  global.HartNav = HartNav;

  /* ── persistent nav chrome (back / forward / up / title + breadcrumb) ───────
   * A compact glass pill under the top bar. Hidden until there's something to
   * navigate (a panel has been opened this session), so the pristine desktop is
   * never cluttered. Self-contained styles (injected once) keep this one file
   * the sole owner of its chrome. */
  function injectStyle() {
    if (document.getElementById('hart-nav-style')) return;
    var s = document.createElement('style');
    s.id = 'hart-nav-style';
    s.textContent =
      '#hart-nav{position:fixed;top:calc(var(--hart-topbar-height,40px) + 8px);left:12px;' +
      'z-index:2500;display:none;align-items:center;gap:2px;padding:3px 6px;border-radius:12px;' +
      'font-family:var(--ds-font-body,inherit);font-size:12px;max-width:min(52vw,520px);' +
      'box-shadow:0 6px 22px rgba(0,0,0,.28)}' +
      '#hart-nav.show{display:flex}' +
      '#hart-nav .hn-btn{display:flex;align-items:center;justify-content:center;width:26px;height:26px;' +
      'border-radius:8px;cursor:pointer;color:var(--hart-text,#e0e0e0);background:transparent;border:0;padding:0}' +
      '#hart-nav .hn-btn:hover{background:var(--hart-surface-hover,rgba(255,255,255,.1))}' +
      '#hart-nav .hn-btn[disabled]{opacity:.32;cursor:default;pointer-events:none}' +
      '#hart-nav .hn-btn .mi{font-size:18px}' +
      '#hart-nav .hn-title{padding:0 8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;' +
      'font-weight:600;color:var(--hart-text,#e0e0e0);max-width:260px}' +
      '#hart-nav .hn-crumb{color:var(--hart-muted,#78909c);white-space:nowrap;overflow:hidden;' +
      'text-overflow:ellipsis}';
    (document.head || document.documentElement).appendChild(s);
  }

  function ensureChrome() {
    if (chrome) return chrome;
    injectStyle();
    chrome = document.createElement('div');
    chrome.id = 'hart-nav';
    chrome.className = 'glass';
    chrome.setAttribute('role', 'navigation');
    chrome.setAttribute('aria-label', 'Shell navigation');
    chrome.innerHTML =
      '<button class="hn-btn" id="hn-back" type="button" title="Back" aria-label="Back">' +
        '<span class="mi material-icons-round" aria-hidden="true">arrow_back</span></button>' +
      '<button class="hn-btn" id="hn-fwd" type="button" title="Forward" aria-label="Forward">' +
        '<span class="mi material-icons-round" aria-hidden="true">arrow_forward</span></button>' +
      '<button class="hn-btn" id="hn-up" type="button" title="Show desktop" aria-label="Up to desktop">' +
        '<span class="mi material-icons-round" aria-hidden="true">desktop_windows</span></button>' +
      '<span class="hn-title" id="hn-title"></span>' +
      '<span class="hn-crumb" id="hn-crumb"></span>';
    document.body.appendChild(chrome);
    chrome.querySelector('#hn-back').addEventListener('click', function () { HartNav.back(); });
    chrome.querySelector('#hn-fwd').addEventListener('click', function () { HartNav.forward(); });
    chrome.querySelector('#hn-up').addEventListener('click', function () { HartNav.up(); });
    return chrome;
  }

  function render() {
    try {
      var c = ensureChrome();
      var st = core.stack();
      var cur = core.current();
      c.classList.toggle('show', st.length > 0);
      var back = c.querySelector('#hn-back'), fwd = c.querySelector('#hn-fwd');
      if (core.canBack()) back.removeAttribute('disabled'); else back.setAttribute('disabled', '');
      if (core.canForward()) fwd.removeAttribute('disabled'); else fwd.setAttribute('disabled', '');
      c.querySelector('#hn-title').textContent = cur ? cur.title : '';
      // Breadcrumb: the trail behind the current location (most recent first),
      // capped so the pill never grows unbounded.
      var trail = st.slice(0, core.index()).map(function (e) { return e.title; });
      var crumb = trail.length ? '· ' + trail.slice(-3).reverse().join(' · ') : '';
      c.querySelector('#hn-crumb').textContent = crumb;
    } catch (e) { console.debug('hartNav: breadcrumb chrome update failed (best-effort)', e); }
  }
})(typeof window !== 'undefined' ? window : this);
