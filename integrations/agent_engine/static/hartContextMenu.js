/*
 * hartContextMenu.js — self-contained, glassy, keyboard-navigable context menu
 * for the HART OS desktop. Injected at runtime by hartDesktop.js (it adds the
 * <script src="/shell/static/hartContextMenu.js">), so NO other shell file needs
 * editing and this carries its OWN <style> (does not depend on the shell's
 * #ctx-menu / ctxItem renderer).
 *
 * Public API (the ONLY surface other modules use):
 *   window.HartCtxMenu.open(items, x, y)   open a floating menu at viewport x,y
 *   window.HartCtxMenu.close()             dismiss any open menu
 *   window.HartCtxMenu.isOpen()            is a menu currently shown
 *
 * 'items' is an array of:
 *   { label, icon?, onClick, disabled?, danger? }   a row
 *   { sep: true }                                    a divider
 *
 * Design constraints honoured (OLD WebKitGTK / cage shell runtime):
 *   - classic script: var/function + string concat, NO template literals,
 *     NO optional chaining, NO nullish coalescing. Arrow funcs are OK but
 *     avoided here for consistency with the rest of the shell static JS.
 *   - GPU-friendly motion only (transform + opacity), respects
 *     prefers-reduced-motion.
 *   - never renders off-screen (flips/clamps near every edge).
 *   - closes on outside pointerdown / Escape / scroll / blur / resize.
 *   - keyboard navigable: Up/Down move, Enter/Space activate, Escape closes,
 *     Home/End jump, with a roving focus + ARIA menu roles.
 *   - NO em dashes in user-visible text (caller supplies labels).
 */
(function () {
  'use strict';

  var EL = null;          // the live menu element (null when closed)
  var STYLE_ID = 'hart-ctxmenu-style';
  var MENU_ID = 'hart-ctxmenu';
  var reduce = false;
  try {
    reduce = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  } catch (e) {}

  // One-time injected glass styling. Tokens fall back to literals so the menu
  // still reads correctly even before the shell's CSS custom-props resolve.
  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var css =
      '#' + MENU_ID + '{position:fixed;z-index:12000;min-width:190px;max-width:300px;' +
        'padding:6px;border-radius:14px;' +
        'background:var(--hart-glass-bg,rgba(28,26,46,0.86));' +
        'border:1px solid var(--hart-glass-border,rgba(255,255,255,0.12));' +
        'box-shadow:0 12px 40px rgba(0,0,0,0.5),inset 0 1px 0 0 rgba(255,255,255,0.08);' +
        'backdrop-filter:blur(22px) saturate(1.3);-webkit-backdrop-filter:blur(22px) saturate(1.3);' +
        'font:13px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--hart-text,#e8e6f0);' +
        'user-select:none;-webkit-user-select:none;outline:none;' +
        'transform-origin:top left;will-change:transform,opacity}' +
      '#' + MENU_ID + '.hart-ctx-anim{animation:hartCtxIn 120ms cubic-bezier(.2,.9,.3,1)}' +
      '@keyframes hartCtxIn{from{opacity:0;transform:scale(.96) translateY(-4px)}' +
        'to{opacity:1;transform:scale(1) translateY(0)}}' +
      '@media(prefers-reduced-motion:reduce){#' + MENU_ID + '.hart-ctx-anim{animation:none}}' +
      '#' + MENU_ID + ' .hart-ctx-item{display:flex;align-items:center;gap:10px;' +
        'padding:8px 12px 8px 10px;border-radius:9px;cursor:default;white-space:nowrap;' +
        'color:inherit;transition:background .1s ease}' +
      '#' + MENU_ID + ' .hart-ctx-item .mi{font-size:18px;width:20px;text-align:center;' +
        'color:var(--hart-accent,#8b80ff);flex-shrink:0}' +
      '#' + MENU_ID + ' .hart-ctx-item.danger .mi{color:var(--hart-error,#ff5d6c)}' +
      '#' + MENU_ID + ' .hart-ctx-item.danger{color:var(--hart-error,#ff7a86)}' +
      '#' + MENU_ID + ' .hart-ctx-item:hover,#' + MENU_ID + ' .hart-ctx-item.active{' +
        'background:rgba(255,255,255,0.10)}' +
      '#' + MENU_ID + ' .hart-ctx-item.danger:hover,#' + MENU_ID + ' .hart-ctx-item.danger.active{' +
        'background:rgba(255,93,108,0.18)}' +
      '#' + MENU_ID + ' .hart-ctx-item.disabled{opacity:.4;pointer-events:none}' +
      '#' + MENU_ID + ' .hart-ctx-sep{height:1px;margin:5px 8px;' +
        'background:var(--hart-glass-border,rgba(255,255,255,0.12))}';
    var s = document.createElement('style');
    s.id = STYLE_ID;
    s.textContent = css;
    document.head.appendChild(s);
  }

  // The Material icon span (matches the shell's icon font usage). Plain text
  // glyphs (emoji) still render fine inside the span; we only add the font
  // class when the name looks like a Material ligature.
  function iconSpan(name) {
    if (!name) return '';
    var isLigature = /^[a-z0-9_]+$/.test(name);
    var cls = isLigature ? 'mi material-icons-round' : 'mi';
    var span = document.createElement('span');
    span.className = cls;
    span.setAttribute('aria-hidden', 'true');
    span.textContent = name;
    return span;
  }

  function rows() {
    if (!EL) return [];
    return Array.prototype.slice.call(EL.querySelectorAll('.hart-ctx-item:not(.disabled)'));
  }

  function setActive(idx) {
    var r = rows();
    if (!r.length) return;
    if (idx < 0) idx = r.length - 1;
    if (idx >= r.length) idx = 0;
    for (var i = 0; i < r.length; i++) r[i].classList.remove('active');
    r[idx].classList.add('active');
    r[idx].setAttribute('data-active', '1');
    try { r[idx].focus(); } catch (e) {}
    // Track the active index on the element for keyboard stepping.
    EL.setAttribute('data-active-idx', String(idx));
  }

  function activeIdx() {
    if (!EL) return -1;
    var v = parseInt(EL.getAttribute('data-active-idx'), 10);
    return isNaN(v) ? -1 : v;
  }

  function close() {
    if (!EL) return;
    var el = EL;
    EL = null;
    detach();
    if (el && el.parentNode) el.parentNode.removeChild(el);
  }

  // Build the menu DOM from the items spec. Each actionable row is a focusable
  // role="menuitem"; activation runs onClick then closes.
  function build(items) {
    var menu = document.createElement('div');
    menu.id = MENU_ID;
    menu.className = 'glass hart-ctx-anim';
    menu.setAttribute('role', 'menu');
    menu.setAttribute('tabindex', '-1');
    menu.setAttribute('aria-label', 'Context menu');

    items.forEach(function (it) {
      if (!it) return;
      if (it.sep) {
        var sep = document.createElement('div');
        sep.className = 'hart-ctx-sep';
        sep.setAttribute('role', 'separator');
        menu.appendChild(sep);
        return;
      }
      var row = document.createElement('div');
      row.className = 'hart-ctx-item' + (it.danger ? ' danger' : '') + (it.disabled ? ' disabled' : '');
      row.setAttribute('role', 'menuitem');
      if (!it.disabled) row.setAttribute('tabindex', '-1');
      row.setAttribute('aria-disabled', it.disabled ? 'true' : 'false');
      var ic = iconSpan(it.icon);
      if (ic) row.appendChild(ic);
      var lbl = document.createElement('span');
      lbl.className = 'hart-ctx-label';
      lbl.textContent = it.label || '';
      row.appendChild(lbl);
      if (!it.disabled && typeof it.onClick === 'function') {
        // Run on click; guard so a stray re-entrant call can't double-fire.
        row.addEventListener('click', function (e) {
          e.preventDefault();
          e.stopPropagation();
          var fn = it.onClick;
          close();
          try { fn(); } catch (err) {}
        });
        // Hovering a row makes it the active (roving) item for keyboard parity.
        row.addEventListener('mousemove', function () {
          var r = rows();
          var i = r.indexOf(row);
          if (i >= 0 && i !== activeIdx()) {
            for (var k = 0; k < r.length; k++) r[k].classList.remove('active');
            row.classList.add('active');
            EL.setAttribute('data-active-idx', String(i));
          }
        });
      }
      menu.appendChild(row);
    });
    return menu;
  }

  // Position the menu fully on-screen: flip to the left/up when it would
  // overflow the right/bottom edge, then hard-clamp with an 8px margin so it
  // is never partially off-screen on any axis (small viewports included).
  function place(x, y) {
    if (!EL) return;
    var pad = 8;
    var vw = window.innerWidth, vh = window.innerHeight;
    // Measure after it is in the DOM but before the reveal so there is no jump.
    var w = EL.offsetWidth, h = EL.offsetHeight;
    var nx = x, ny = y;
    if (nx + w + pad > vw) nx = x - w;          // flip horizontally near right edge
    if (nx < pad) nx = pad;
    if (nx + w + pad > vw) nx = Math.max(pad, vw - w - pad);
    if (ny + h + pad > vh) ny = y - h;          // flip vertically near bottom edge
    if (ny < pad) ny = pad;
    if (ny + h + pad > vh) ny = Math.max(pad, vh - h - pad);
    EL.style.left = Math.round(nx) + 'px';
    EL.style.top = Math.round(ny) + 'px';
  }

  // ── Global dismissers (attached only while a menu is open) ──
  function onDocPointerDown(e) {
    if (EL && !EL.contains(e.target)) close();
  }
  function onKeyDown(e) {
    if (!EL) return;
    var k = e.key;
    if (k === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); return; }
    if (k === 'ArrowDown') { e.preventDefault(); setActive(activeIdx() + 1); return; }
    if (k === 'ArrowUp') { e.preventDefault(); setActive(activeIdx() - 1); return; }
    if (k === 'Home') { e.preventDefault(); setActive(0); return; }
    if (k === 'End') { e.preventDefault(); setActive(rows().length - 1); return; }
    if (k === 'Enter' || k === ' ' || k === 'Spacebar') {
      e.preventDefault();
      var r = rows();
      var i = activeIdx();
      if (i >= 0 && i < r.length) r[i].click();
      return;
    }
    if (k === 'Tab') { e.preventDefault(); setActive(activeIdx() + (e.shiftKey ? -1 : 1)); return; }
  }
  function onScrollOrResize() { close(); }
  function onBlur() { close(); }

  function attach() {
    // pointerdown (capture) so we dismiss before any other click handler runs;
    // keydown (capture) so Escape wins over the shell's global key shortcuts.
    document.addEventListener('pointerdown', onDocPointerDown, true);
    document.addEventListener('keydown', onKeyDown, true);
    window.addEventListener('scroll', onScrollOrResize, true);
    window.addEventListener('resize', onScrollOrResize, true);
    window.addEventListener('blur', onBlur);
  }
  function detach() {
    document.removeEventListener('pointerdown', onDocPointerDown, true);
    document.removeEventListener('keydown', onKeyDown, true);
    window.removeEventListener('scroll', onScrollOrResize, true);
    window.removeEventListener('resize', onScrollOrResize, true);
    window.removeEventListener('blur', onBlur);
  }

  function open(items, x, y) {
    if (!items || !items.length) return;
    ensureStyle();
    close();                       // never stack two menus
    EL = build(items);
    if (reduce) EL.classList.remove('hart-ctx-anim');
    // Off-screen first so measuring offsetWidth/Height is accurate, then place.
    EL.style.left = '-9999px';
    EL.style.top = '-9999px';
    document.body.appendChild(EL);
    place(x, y);
    attach();
    EL.setAttribute('data-active-idx', '-1');
    // Focus the menu container so keyboard nav works immediately; the first
    // ArrowDown then lands on the first item (no pre-highlight, macOS-style).
    try { EL.focus(); } catch (e) {}
  }

  window.HartCtxMenu = {
    open: open,
    close: close,
    isOpen: function () { return !!EL; }
  };
})();
