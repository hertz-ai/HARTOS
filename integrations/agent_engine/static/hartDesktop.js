/*
 * hartDesktop.js — HART OS desktop layer: drag-drop app icons.
 *
 * Pin apps to the desktop, drag to arrange (grid-snapped + persisted),
 * double-click / Enter to launch, right-click for Open / Remove, and an
 * "Add app to desktop" picker. Reuses the shell's own primitives so there is
 * no parallel path:
 *   MANIFEST          — app id -> {title, icon}
 *   openPanel(id)     — launch
 *   ctxItem/ctxSep + #ctx-menu — the shell's context-menu renderer
 *   /api/shell/session-state   — single JSON blob (read-modify-write so we
 *                                never clobber other shell state)
 *
 * Machine-native performance: drag uses a GPU-composited transform (no layout
 * thrash) committed to left/top only on drop; moves are rAF-batched; backend
 * writes are debounced. Plain classic script, loaded after the inline shell JS.
 */
(function () {
  'use strict';
  var GRID = 92;     // snap cell (px)
  var PAD = 24;      // desktop margin
  var layer = null;  // positions persist via the shared window.HartSession (one
                     // writer per key, so the wallpaper module can't clobber us)

  function M() { return window.MANIFEST || {}; }

  function readPositions() {
    return Array.prototype.map.call(layer.querySelectorAll('.desktop-icon'), function (el) {
      return { id: el.getAttribute('data-id'),
               x: parseInt(el.style.left, 10) || 0,
               y: parseInt(el.style.top, 10) || 0 };
    });
  }

  function persist() {
    // HartSession.set merges by key into the single blob and debounce-saves the
    // whole thing, so other modules' keys (wallpaper, …) are never clobbered.
    if (window.HartSession) window.HartSession.set('desktop_icons', readPositions());
  }

  function launch(id) { if (typeof window.openPanel === 'function') window.openPanel(id); }

  function snap(v) { return Math.max(0, Math.round((v - PAD) / GRID) * GRID + PAD); }

  function bindIcon(el) {
    var id = el.getAttribute('data-id');
    var dragging = false, moved = false, sx = 0, sy = 0, ox = 0, oy = 0, dx = 0, dy = 0, raf = 0;

    el.addEventListener('dblclick', function () { launch(id); });
    el.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); launch(id); }
    });

    el.addEventListener('pointerdown', function (e) {
      if (e.button !== 0) return;
      dragging = true; moved = false;
      sx = e.clientX; sy = e.clientY;
      ox = parseInt(el.style.left, 10) || 0; oy = parseInt(el.style.top, 10) || 0;
      el.classList.add('dragging');
      try { el.setPointerCapture(e.pointerId); } catch (_) {}
    });
    el.addEventListener('pointermove', function (e) {
      if (!dragging) return;
      dx = e.clientX - sx; dy = e.clientY - sy;
      if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
      if (raf) return;
      raf = requestAnimationFrame(function () {       // GPU transform, no layout
        raf = 0; el.style.transform = 'translate(' + dx + 'px,' + dy + 'px)';
      });
    });
    function endDrag(e) {
      if (!dragging) return;
      dragging = false; el.classList.remove('dragging');
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
      try { el.releasePointerCapture(e.pointerId); } catch (_) {}
      el.style.transform = '';
      if (moved) {                                    // commit to grid + persist
        el.style.left = snap(ox + dx) + 'px';
        el.style.top = snap(oy + dy) + 'px';
        persist();
      }
    }
    el.addEventListener('pointerup', endDrag);
    el.addEventListener('pointercancel', endDrag);

    el.addEventListener('contextmenu', function (e) {
      e.preventDefault(); e.stopPropagation();
      var menu = document.getElementById('ctx-menu');
      if (!menu || typeof window.ctxItem !== 'function') return;
      menu.innerHTML = [
        window.ctxItem('open_in_new', 'Open', "window.openPanel&&openPanel('" + id + "')"),
        (window.ctxSep ? window.ctxSep() : ''),
        window.ctxItem('delete', 'Remove from desktop', "window.hartRemoveIcon&&hartRemoveIcon('" + id + "')")
      ].join('');
      menu.style.left = e.clientX + 'px';
      menu.style.top = e.clientY + 'px';
      menu.style.display = 'block';
    });
  }

  function makeIcon(item) {
    var def = M()[item.id] || {};
    var el = document.createElement('div');
    el.className = 'desktop-icon';
    el.setAttribute('data-id', item.id);
    el.setAttribute('role', 'button');
    el.setAttribute('tabindex', '0');
    el.setAttribute('aria-label', def.title || item.id);
    el.style.left = (item.x != null ? item.x : PAD) + 'px';
    el.style.top = (item.y != null ? item.y : PAD) + 'px';
    el.innerHTML =
      '<div class="di-glyph"><span class="mi material-icons-round" aria-hidden="true">' +
      (def.icon || 'apps') + '</span></div>' +
      '<div class="di-label"></div>';
    el.querySelector('.di-label').textContent = def.title || item.id; // textContent = no HTML injection
    bindIcon(el);
    return el;
  }

  function firstFreeRow() {
    var used = {};
    readPositions().forEach(function (p) { if (p.x < GRID + PAD) used[Math.round((p.y - PAD) / GRID)] = 1; });
    var row = 0; while (used[row]) row++;
    return row;
  }

  // ── Exposed actions (wired from the desktop context menu) ──
  window.hartRemoveIcon = function (id) {
    var el = layer && layer.querySelector('.desktop-icon[data-id="' + id + '"]');
    if (el) { el.parentNode.removeChild(el); persist(); }
  };
  window.hartPinIcon = function (id) {
    if (!layer || !M()[id]) return;
    if (layer.querySelector('.desktop-icon[data-id="' + id + '"]')) return;
    layer.appendChild(makeIcon({ id: id, x: PAD, y: PAD + firstFreeRow() * GRID }));
    persist();
  };
  window.hartAutoArrange = function () {
    if (!layer) return;
    var col = 0;
    Array.prototype.forEach.call(layer.querySelectorAll('.desktop-icon'), function (el) {
      el.style.left = PAD + 'px'; el.style.top = (PAD + col * GRID) + 'px'; col++;
    });
    persist();
  };
  window.hartAddAppPicker = function () {
    var menu = document.getElementById('ctx-menu');
    if (!menu || typeof window.ctxItem !== 'function') return;
    var have = {}; readPositions().forEach(function (p) { have[p.id] = 1; });
    var M_ = M();
    var ids = Object.keys(M_).filter(function (id) { return !have[id]; }).slice(0, 40);
    var html = ids.length
      ? ids.map(function (id) {
          return window.ctxItem(M_[id].icon || 'apps', M_[id].title || id,
            "window.hartPinIcon&&hartPinIcon('" + id + "')");
        }).join('')
      : window.ctxItem('info', 'Everything is already on the desktop', '');
    // Defer past THIS click's auto-close: ctxItem appends display='none' and the
    // global click handler also closes the menu, so re-render the picker on the
    // next tick to keep it open at the same position.
    setTimeout(function () { menu.innerHTML = html; menu.style.display = 'block'; }, 0);
  };

  function defaults() {
    var want = ['app_store', 'files', 'security', 'appearance', 'terminal', 'weather'];
    var M_ = M(), out = [], row = 0;
    want.forEach(function (id) { if (M_[id]) { out.push({ id: id, x: PAD, y: PAD + row * GRID }); row++; } });
    return out;
  }

  function render(list) {
    layer.innerHTML = '';
    list.forEach(function (it) { if (M()[it.id]) layer.appendChild(makeIcon(it)); });
  }

  function init() {
    layer = document.getElementById('hart-desktop');
    if (!layer || !window.MANIFEST || !window.HartSession) { return setTimeout(init, 300); }
    window.HartSession.ready(function () {
      var icons = window.HartSession.get('desktop_icons');
      render((icons && icons.length) ? icons : defaults());
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
