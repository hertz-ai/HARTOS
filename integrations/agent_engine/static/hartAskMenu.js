/*
 * hartAskMenu.js — adds an "Ask <Product>" row to EVERY right-click menu in the
 * shell, branded to the installed product: "Ask HART" in OS mode, "Ask Nunba" in
 * the desktop companion (window.HART_PRODUCT, injected by render_desktop_shell from
 * core.port_registry.is_os_mode()). Selecting it opens the floating assistant chat
 * with the current text selection prefilled.
 *
 * DRY: reuses the ONE menu renderer (window.HartCtxMenu.open) and the ONE assistant
 * path (toggleAssistantChat + #ac-input, where acSend is the sole dispatcher) — no
 * parallel menu, no parallel chat. Classic script, WebKitGTK-safe (no optional
 * chaining, guarded throughout so a missing node is a no-op, never a throw).
 */
(function () {
  'use strict';

  function product() {
    // 'HART' (OS mode) or 'Nunba' (companion). Default HART if not injected.
    return (window.HART_PRODUCT === 'Nunba') ? 'Nunba' : 'HART';
  }

  function currentSelection() {
    try {
      var s = window.getSelection ? String(window.getSelection()) : '';
      s = (s || '').trim();
      return s.length > 400 ? s.slice(0, 400) : s;
    } catch (e) { return ''; }
  }

  // Open the floating assistant (reusing the canonical toggle) and prefill `text`.
  // toggleAssistantChat TOGGLES, so only call it while the chat is closed, else a
  // second right-click-Ask would close an already-open chat.
  function askOpen(text) {
    try {
      var chat = document.getElementById('assistant-chat');
      if (chat && chat.classList.contains('open')) {
        // already open — just (re)fill + focus below
      } else if (typeof window.toggleAssistantChat === 'function') {
        window.toggleAssistantChat();
      }
      setTimeout(function () {
        var i = document.getElementById('ac-input');
        if (i) {
          if (text) { i.value = text; }
          try { i.focus(); } catch (e) { /* dead pointer / test DOM */ }
        }
      }, 130);
    } catch (e) { console.debug('hartAskMenu: askOpen failed', e); }
  }

  // The menu row. Labelled per product; when text is selected, phrase it about the
  // selection. __ask flags it so the wrapper never double-adds it.
  function askItem() {
    var sel = currentSelection();
    var label = sel ? ('Ask ' + product() + ' about this') : ('Ask ' + product());
    return {
      icon: 'auto_awesome', label: label, __ask: true,
      onClick: function () { askOpen(sel); }
    };
  }

  // Prepend the Ask row to EVERY menu the single renderer opens (icons, wallpaper,
  // the empty-area fallback below) — deduped via __ask so it is never doubled.
  function installWrap() {
    var M = window.HartCtxMenu;
    if (!M || typeof M.open !== 'function') { return false; }
    if (M.__askWrapped) { return true; }
    var _open = M.open;
    M.open = function (items, x, y) {
      items = Array.isArray(items) ? items : [];
      var hasAsk = items.some(function (it) { return it && it.__ask; });
      var full = hasAsk ? items
        : (items.length ? [askItem(), { sep: true }].concat(items) : [askItem()]);
      return _open.call(M, full, x, y);
    };
    M.__askWrapped = true;
    return true;
  }

  // hartContextMenu.js is loaded dynamically (by hartDesktop.js), so retry until the
  // renderer exists, then wrap once. Bounded so it can never spin forever.
  (function waitForRenderer(tries) {
    if (installWrap() || tries <= 0) { return; }
    setTimeout(function () { waitForRenderer(tries - 1); }, 200);
  })(40);

  // Empty-area fallback: a right-click anywhere no specific handler claimed still
  // offers Ask. A specific handler (icon/wallpaper) calls preventDefault (and the
  // wrap already added Ask to its menu), so skip when defaultPrevented.
  document.addEventListener('contextmenu', function (e) {
    if (e.defaultPrevented) { return; }
    var M = window.HartCtxMenu;
    if (!M || typeof M.open !== 'function') { return; }
    e.preventDefault();
    M.open([askItem()], e.clientX, e.clientY);
  });
})();
