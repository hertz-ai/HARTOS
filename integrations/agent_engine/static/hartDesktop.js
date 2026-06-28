/*
 * hartDesktop.js — HART OS desktop layer: drag-drop app icons.
 *
 * Pin apps to the desktop, drag to arrange (grid-snapped + persisted),
 * double-click / Enter to launch, right-click for Open / Customize / Remove,
 * and an "Add app to desktop" picker. Reuses the shell's own primitives so
 * there is no parallel path:
 *   MANIFEST          — app id -> {title, icon}
 *   openPanel(id)     — launch
 *   ctxItem/ctxSep + #ctx-menu — the shell's context-menu renderer
 *   /api/shell/session-state   — single JSON blob (read-modify-write so we
 *                                never clobber other shell state)
 *
 * Per-icon customization (glyph / label / color, macOS-/Windows-style) travels
 * INSIDE the same 'desktop_icons' entries — readPositions() is the single
 * serializer (it reads the override back off each icon's data-attrs) and
 * persist() the single writer, so there is no parallel override store.
 *
 * Machine-native performance: drag uses a GPU-composited transform (no layout
 * thrash) committed to left/top only on drop; moves are rAF-batched; backend
 * writes are debounced. Plain classic script, loaded after the inline shell JS.
 */
(function () {
  'use strict';
  var GRID = 92;     // snap cell (px)
  var PAD = 24;      // desktop margin
  var TAP_MS = 300;  // pointerup within this of pointerdown ...
  var TAP_PX = 8;    // ... and within this distance == a TAP (single-activate)
  var layer = null;  // positions persist via the shared window.HartSession (one
                     // writer per key, so the wallpaper module can't clobber us)

  // Pull in the self-contained context-menu module (own file, own glass style)
  // by injecting its <script> from here, so no shell file needs a new tag. Guard
  // against a double-inject if init() ever re-runs.
  (function injectCtxMenu() {
    if (window.HartCtxMenu || document.getElementById('hart-ctxmenu-js')) return;
    var s = document.createElement('script');
    s.id = 'hart-ctxmenu-js';
    s.src = '/shell/static/hartContextMenu.js';
    s.defer = true;
    (document.head || document.documentElement).appendChild(s);
  })();

  function M() { return window.MANIFEST || {}; }

  // A glyph is rendered with the Material Symbols icon font ONLY when it looks
  // like a ligature name (lowercase snake_case ASCII, e.g. "open_in_new").
  // Anything else (emoji, unicode) is rendered as plain text — putting an emoji
  // inside .material-icons-round makes the icon font mangle it. Single source
  // of "is this a Material name" so makeIcon + the dialog agree.
  function isMaterialName(g) { return /^[a-z0-9_]+$/.test(g || ''); }

  // Inner HTML for an icon's glyph. color (optional) is applied inline so it
  // overrides the stylesheet's '.di-glyph .mi{color:var(--hart-accent)}'.
  function glyphSpan(glyph, color) {
    var g = glyph || 'apps';
    var style = color ? ' style="color:' + color + '"' : '';
    if (isMaterialName(g)) {
      return '<span class="mi material-icons-round"' + style + ' aria-hidden="true"></span>';
    }
    // Emoji / unicode: plain span (no icon font). textContent set by caller.
    return '<span class="mi di-emoji"' + style + ' aria-hidden="true"></span>';
  }

  // The effective override for an icon = explicit override fields, falling back
  // to the MANIFEST default. Used to seed the dialog and to render.
  function effective(id, ov) {
    var def = M()[id] || {};
    ov = ov || {};
    return {
      glyph: (ov.glyph != null && ov.glyph !== '') ? ov.glyph : (def.icon || 'apps'),
      label: (ov.label != null && ov.label !== '') ? ov.label : (def.title || id),
      color: ov.color || ''   // '' = no user override (manifest default applied at render)
    };
  }

  // Pure DOM apply of an override onto an existing .desktop-icon element. Reused
  // by makeIcon (initial render) AND the customize dialog (immediate apply), so
  // glyph/label/color rendering lives in exactly one place. Stashes the raw
  // override back onto data-attrs so readPositions() can serialize it.
  function applyIconVisual(el, ov) {
    ov = ov || {};
    var id = el.getAttribute('data-id');
    var eff = effective(id, ov);
    // De-monochrome default: with NO user colour override, tint the glyph with
    // the per-app colour stamped on the manifest (single source = shell_manifest.
    // with_icon_colors) instead of the single --hart-accent wash. A real user
    // override (eff.color) still wins; this default is render-only and is NOT
    // persisted (readPositions serializes data-ov-color, untouched below), so
    // the desktop blob stays lean and the dialog's "Theme default" stays honest.
    var renderColor = eff.color || ((M()[id] || {}).color) || '';

    var glyphBox = el.querySelector('.di-glyph');
    glyphBox.innerHTML = glyphSpan(eff.glyph, renderColor);
    var span = glyphBox.querySelector('.mi');
    if (!isMaterialName(eff.glyph)) span.textContent = eff.glyph;   // emoji -> text
    else span.textContent = eff.glyph;                              // ligature name
    // Tint the glyph plate to match (lighter when it's the manifest default so a
    // user-chosen colour still reads as "customized" vs the default vibrancy).
    glyphBox.style.background = renderColor ? _tint(renderColor, eff.color ? 0.22 : 0.15) : '';
    glyphBox.style.borderColor = renderColor ? _tint(renderColor, eff.color ? 0.55 : 0.40) : '';

    // The customize-dialog PREVIEW (.hic-prev) is a glyph plate with NO .di-label
    // node, so guard it — an unconditional el.querySelector('.di-label').textContent
    // is null.textContent in a real browser, which threw mid-setup and left the
    // dialog as a dead, undismissable modal (the shim's querySelector hid it).
    var lblEl = el.querySelector('.di-label');
    if (lblEl) lblEl.textContent = eff.label;                       // textContent = no HTML injection
    el.setAttribute('aria-label', eff.label);

    // Persist-source: only stash fields that actually override the default, so
    // an un-customized icon serializes to a plain {id,x,y} (no override noise).
    _setOv(el, 'glyph', ov.glyph);
    _setOv(el, 'label', ov.label);
    _setOv(el, 'color', ov.color);
  }

  function _setOv(el, k, v) {
    if (v != null && v !== '') el.setAttribute('data-ov-' + k, v);
    else el.removeAttribute('data-ov-' + k);
  }
  // Read the override an icon currently carries (mirror of _setOv).
  function _getOv(el) {
    return { glyph: el.getAttribute('data-ov-glyph') || '',
             label: el.getAttribute('data-ov-label') || '',
             color: el.getAttribute('data-ov-color') || '' };
  }
  // Low-alpha tint of a #rrggbb color for the glyph plate background.
  function _tint(hex, a) {
    var m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
    if (!m) return '';
    var n = parseInt(m[1], 16);
    return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + (a == null ? 0.22 : a) + ')';
  }

  function readPositions() {
    return Array.prototype.map.call(layer.querySelectorAll('.desktop-icon'), function (el) {
      var ov = _getOv(el);
      var rec = { id: el.getAttribute('data-id'),
                  x: parseInt(el.style.left, 10) || 0,
                  y: parseInt(el.style.top, 10) || 0 };
      // Only attach override fields that are actually set — keeps the blob lean
      // and keeps un-customized icons byte-identical to the old {id,x,y} shape.
      if (ov.glyph) rec.glyph = ov.glyph;
      if (ov.label) rec.label = ov.label;
      if (ov.color) rec.color = ov.color;
      return rec;
    });
  }

  function persist() {
    // HartSession.set merges by key into the single blob and debounce-saves the
    // whole thing, so other modules' keys (wallpaper, …) are never clobbered.
    if (window.HartSession) window.HartSession.set('desktop_icons', readPositions());
  }

  function launch(id) { if (typeof window.openPanel === 'function') window.openPanel(id); }

  function selectIcon(el) {                            // desktop single-click = select (deselect siblings)
    if (layer) Array.prototype.forEach.call(layer.querySelectorAll('.desktop-icon.selected'),
      function (n) { n.classList.remove('selected'); });
    if (el) el.classList.add('selected');
  }

  function snap(v) { return Math.max(0, Math.round((v - PAD) / GRID) * GRID + PAD); }

  function bindIcon(el) {
    var id = el.getAttribute('data-id');
    var dragging = false, moved = false, sx = 0, sy = 0, ox = 0, oy = 0, dx = 0, dy = 0, raf = 0;
    var downAt = 0, lpTimer = 0;

    // A real tap on a touchscreen is a quick, near-stationary press+release. The
    // old code only opened on touch via the 'moved' flag (a 3px jitter would
    // flip it to a no-op move) and the mouse needed a DOUBLE click — so a single
    // tap on the device never launched. We now treat ANY pointer (touch OR
    // mouse) as single-activate: a press that ends within TAP_MS and < TAP_PX
    // LAUNCHES. Mouse dblclick is kept harmless (re-launch is idempotent: an
    // already-open panel just gets raised by openPanel).
    el.style.touchAction = 'none';            // a drag must not scroll/select on touch
    el.style.webkitUserSelect = 'none';
    el.style.userSelect = 'none';

    el.addEventListener('dblclick', function (e) { e.preventDefault(); launch(id); });
    el.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); launch(id); }
    });

    function clearLongPress() { if (lpTimer) { clearTimeout(lpTimer); lpTimer = 0; } }

    el.addEventListener('pointerdown', function (e) {
      if (e.button !== 0) return;
      dragging = true; moved = false;
      downAt = (e.timeStamp || Date.now());
      sx = e.clientX; sy = e.clientY; dx = 0; dy = 0;
      ox = parseInt(el.style.left, 10) || 0; oy = parseInt(el.style.top, 10) || 0;
      el.classList.add('dragging');
      if (layer) layer.classList.add('arranging');   // show the snap-grid overlay while dragging
      try { el.setPointerCapture(e.pointerId); } catch (_) {}
      e.preventDefault();                            // stop native text/image selection on the press
      // Touch long-press == right-click: open the icon menu after 500ms if the
      // finger has not moved (cancelled by move past threshold / up / cancel).
      clearLongPress();
      if (e.pointerType === 'touch') {
        var lx = e.clientX, ly = e.clientY;
        lpTimer = setTimeout(function () {
          lpTimer = 0;
          if (dragging && !moved) {
            dragging = false; el.classList.remove('dragging');
            if (layer) layer.classList.remove('arranging');
            try { el.releasePointerCapture(e.pointerId); } catch (_) {}
            el.style.transform = '';
            openIconMenu(id, lx, ly);
          }
        }, 500);
      }
    });
    el.addEventListener('pointermove', function (e) {
      if (!dragging) return;
      dx = e.clientX - sx; dy = e.clientY - sy;
      if (Math.abs(dx) + Math.abs(dy) > TAP_PX) { moved = true; clearLongPress(); }
      if (!moved) return;                            // below threshold: still a candidate tap, no drag yet
      if (raf) return;
      raf = requestAnimationFrame(function () {       // GPU transform, no layout
        raf = 0;
        el.style.transform = 'translate(' + dx + 'px,' + dy + 'px)';
        // If this icon is part of a multi-selection, the whole group follows live
        // (same GPU-composited transform applied to each selected sibling).
        if (el.classList.contains('selected') && layer) {
          var sel = layer.querySelectorAll('.desktop-icon.selected');
          if (sel.length > 1) Array.prototype.forEach.call(sel, function (n) { n.style.transform = el.style.transform; });
        }
      });
    });
    function endDrag(e) {
      if (!dragging) return;
      dragging = false; el.classList.remove('dragging');
      clearLongPress();
      if (layer) layer.classList.remove('arranging');
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
      try { el.releasePointerCapture(e.pointerId); } catch (_) {}
      el.style.transform = '';
      var dt = (e && e.timeStamp ? e.timeStamp : Date.now()) - downAt;
      var isTap = !moved && (Math.abs(dx) + Math.abs(dy) <= TAP_PX) && dt <= TAP_MS;
      if (moved) {                                    // commit to grid + persist
        // GROUP MOVE: if this icon is part of a multi-selection (marquee), apply
        // the SAME snapped delta to every selected icon so the whole group moves
        // as one. Otherwise just this icon. One persist() at the end (single writer).
        var sel = layer ? layer.querySelectorAll('.desktop-icon.selected') : [];
        if (el.classList.contains('selected') && sel.length > 1) {
          var ddx = snap(ox + dx) - ox, ddy = snap(oy + dy) - oy;
          Array.prototype.forEach.call(sel, function (n) {
            n.style.transform = '';
            n.style.left = snap((parseInt(n.style.left, 10) || 0) + ddx) + 'px';
            n.style.top = snap((parseInt(n.style.top, 10) || 0) + ddy) + 'px';
          });
        } else {
          el.style.left = snap(ox + dx) + 'px';
          el.style.top = snap(oy + dy) + 'px';
        }
        persist();
      } else if (isTap) {                             // touch OR mouse: a single tap opens
        selectIcon(el);                               // reflect selection too (visual feedback)
        try { el.focus(); } catch (_) {}              // pointerdown.preventDefault() suppressed focus; restore it for keyboard
        launch(id);
      } else {                                        // a slow press that didn't move: just select
        selectIcon(el);
        try { el.focus(); } catch (_) {}
      }
    }
    el.addEventListener('pointerup', endDrag);
    el.addEventListener('pointercancel', function (e) { clearLongPress(); endDrag(e); });

    el.addEventListener('contextmenu', function (e) {
      e.preventDefault(); e.stopPropagation();
      openIconMenu(id, e.clientX, e.clientY);
    });
  }

  // Right-click / long-press menu for a desktop ICON. Self-contained menu
  // (HartCtxMenu) wired to the file's existing helpers — no parallel renderer.
  function openIconMenu(id, x, y) {
    if (!window.HartCtxMenu) return;
    var def = M()[id] || {};
    var items = [
      { icon: 'open_in_new', label: 'Open', onClick: function () { launch(id); } },
      { icon: 'drive_file_rename_outline', label: 'Rename', onClick: function () { renameIcon(id); } },
      { sep: true },
      { icon: 'tune', label: 'Properties', onClick: function () { window.hartCustomizeIcon && window.hartCustomizeIcon(id); } }
    ];
    // "Uninstall" for a real installed app (removes the app); otherwise the
    // desktop-only entry is "Remove from desktop" (unpins, keeps the app).
    if (def.installed) {
      items.push({ icon: 'delete_forever', label: 'Uninstall', danger: true, onClick: function () { uninstallApp(id); } });
    } else {
      items.push({ icon: 'delete', label: 'Remove from desktop', danger: true, onClick: function () { window.hartRemoveIcon && window.hartRemoveIcon(id); } });
    }
    window.HartCtxMenu.open(items, x, y);
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
    el.innerHTML = '<div class="di-glyph"></div><div class="di-label"></div>';
    // applyIconVisual fills glyph + label from the stored override (falling back
    // to MANIFEST) — the single render path, shared with the customize dialog.
    applyIconVisual(el, { glyph: item.glyph, label: item.label, color: item.color });
    bindIcon(el);
    return el;
  }

  function firstFreeRow() {
    var used = {};
    readPositions().forEach(function (p) { if (p.x < GRID + PAD) used[Math.round((p.y - PAD) / GRID)] = 1; });
    var row = 0; while (used[row]) row++;
    return row;
  }

  // ── Customize dialog (macOS-/Windows-style per-icon glyph/label/color) ──
  // Reuses the shell's glass tokens. Save -> applyIconVisual (immediate) +
  // persist (single writer). Reset clears the override back to the MANIFEST.
  function closeDialog() {
    var d = document.getElementById('hart-icon-customize');
    if (d && d.parentNode) d.parentNode.removeChild(d);
  }
  window.hartCustomizeIcon = function (id) {
    if (!layer) return;
    var el = layer.querySelector('.desktop-icon[data-id="' + id + '"]');
    if (!el) return;
    closeDialog();
    var eff = effective(id, _getOv(el));

    var ov = document.createElement('div');
    ov.id = 'hart-icon-customize';
    ov.className = 'hart-icustom-backdrop';
    ov.setAttribute('role', 'dialog');
    ov.setAttribute('aria-modal', 'true');
    ov.setAttribute('aria-label', 'Customize icon');
    ov.innerHTML =
      '<div class="hart-icustom glass" role="document">' +
        '<div class="hart-icustom-head">' +
          '<div class="hic-prev"><div class="di-glyph"></div></div>' +
          '<div class="hart-icustom-title">Customize icon</div>' +
        '</div>' +
        '<label class="hart-icustom-row">Glyph (Material Symbols name or emoji)' +
          '<input id="hic-glyph" type="text" autocomplete="off" spellcheck="false"></label>' +
        '<label class="hart-icustom-row">Label' +
          '<input id="hic-label" type="text" autocomplete="off"></label>' +
        '<label class="hart-icustom-row">Color' +
          '<span class="hic-color-wrap"><input id="hic-color" type="color">' +
          '<button type="button" id="hic-color-clear" class="hart-icustom-btn ghost">Theme default</button></span></label>' +
        '<div class="hart-icustom-actions">' +
          '<button type="button" id="hic-reset" class="hart-icustom-btn ghost">Reset</button>' +
          '<span style="flex:1"></span>' +
          '<button type="button" id="hic-cancel" class="hart-icustom-btn">Cancel</button>' +
          '<button type="button" id="hic-save" class="hart-icustom-btn primary">Save</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(ov);

    var $ = function (s) { return ov.querySelector(s); };
    var inGlyph = $('#hic-glyph'), inLabel = $('#hic-label'), inColor = $('#hic-color');
    var preview = $('.hic-prev');
    inGlyph.value = eff.glyph;
    inLabel.value = eff.label;
    inColor.value = /^#?[0-9a-f]{6}$/i.test(eff.color) ? eff.color : _accentHex();

    function current() {
      // '' for color means "no override" (theme default) — tracked separately so
      // clicking the swatch is distinct from leaving it on Theme default.
      return { glyph: inGlyph.value.trim(),
               label: inLabel.value.trim(),
               color: ov._colorOn ? inColor.value : '' };
    }
    function refreshPreview() {
      // Render the live preview through the SAME applyIconVisual path.
      preview.setAttribute('data-id', id);
      applyIconVisual(preview, current());
    }
    // Seed the swatch "on" only from a real per-icon override — NOT from a
    // manifest-default colour. The preview still shows the manifest colour
    // (current().color='' -> effective() falls back to def.color) while a plain
    // Save persists no colour, so the desktop blob stays lean.
    ov._colorOn = !!_getOv(el).color;
    refreshPreview();

    inGlyph.addEventListener('input', refreshPreview);
    inLabel.addEventListener('input', refreshPreview);
    inColor.addEventListener('input', function () { ov._colorOn = true; refreshPreview(); });
    $('#hic-color-clear').addEventListener('click', function () { ov._colorOn = false; refreshPreview(); });

    function commit(o) { applyIconVisual(el, o); persist(); closeDialog(); }
    $('#hic-save').addEventListener('click', function () { commit(current()); });
    $('#hic-reset').addEventListener('click', function () { commit({ glyph: '', label: '', color: '' }); });
    $('#hic-cancel').addEventListener('click', closeDialog);
    ov.addEventListener('mousedown', function (e) { if (e.target === ov) closeDialog(); });
    ov.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { e.preventDefault(); closeDialog(); }
      else if (e.key === 'Enter' && e.target.tagName === 'INPUT') { e.preventDefault(); commit(current()); }
    });
    inGlyph.focus(); inGlyph.select();
  };

  function _accentHex() {
    try {
      var c = getComputedStyle(document.documentElement).getPropertyValue('--hart-accent').trim();
      var m = /^#?([0-9a-f]{6})$/i.exec(c);
      if (m) return '#' + m[1];
    } catch (_) {}
    return '#6c63ff';
  }

  // ── Exposed actions (wired from the desktop context menu) ──
  window.hartRemoveIcon = function (id) {
    var el = layer && layer.querySelector('.desktop-icon[data-id="' + id + '"]');
    if (el) { el.parentNode.removeChild(el); persist(); }
  };
  // Inline rename: a tiny editable field over the icon's label. Writes through
  // the SAME canonical path as the Customize dialog (applyIconVisual stores the
  // 'label' override on data-attrs, persist() serializes it) so there is no
  // second rename store. Empty/blank == "reset to the manifest title".
  function renameIcon(id) {
    var el = layer && layer.querySelector('.desktop-icon[data-id="' + id + '"]');
    if (!el) return;
    var lbl = el.querySelector('.di-label');
    if (!lbl || el.querySelector('.di-rename')) return;
    var cur = effective(id, _getOv(el)).label;
    var inp = document.createElement('input');
    inp.type = 'text';
    inp.className = 'di-rename';
    inp.value = cur;
    inp.setAttribute('aria-label', 'Rename icon');
    // Minimal inline styling so it does not depend on shell CSS for this widget.
    inp.style.cssText = 'width:80px;font:11px system-ui;text-align:center;border-radius:6px;' +
      'border:1px solid var(--hart-accent,#8b80ff);background:rgba(0,0,0,.5);color:#fff;padding:1px 3px;outline:none';
    lbl.style.display = 'none';
    lbl.parentNode.insertBefore(inp, lbl.nextSibling);
    var done = false;
    function finish(commit) {
      if (done) return; done = true;
      if (commit) {
        var ov = _getOv(el);
        ov.label = inp.value.trim();                 // '' -> effective() falls back to manifest title
        applyIconVisual(el, ov);
        persist();
      }
      if (inp.parentNode) inp.parentNode.removeChild(inp);
      lbl.style.display = '';
    }
    inp.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); finish(true); }
      else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
      e.stopPropagation();                           // don't trigger the icon's Enter-to-launch
    });
    // A pointer interaction on the field must not start an icon drag.
    inp.addEventListener('pointerdown', function (e) { e.stopPropagation(); });
    inp.addEventListener('click', function (e) { e.stopPropagation(); });
    inp.addEventListener('blur', function () { finish(true); });
    inp.focus(); inp.select();
  }
  // Uninstall a real installed app, then drop its desktop icon. Reuses the
  // shell's installer route family (/api/apps/install has a sibling uninstall);
  // if that endpoint is unavailable we still unpin the icon (always-correct
  // local action) so the menu item is never a dead end. Confirms first.
  function uninstallApp(id) {
    var def = M()[id] || {};
    var name = def.title || id;
    function drop() {
      window.hartRemoveIcon && window.hartRemoveIcon(id);
      if (window.MANIFEST && window.MANIFEST[id]) { try { delete window.MANIFEST[id]; } catch (e) {} }
    }
    function go() {
      var base = (typeof window.SHELL === 'string' && window.SHELL) ? window.SHELL : '';
      try {
        fetch(base + '/api/apps/uninstall', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ app_id: id, platform: def.platform || '' })
        }).then(function () { drop(); }, function () { drop(); });
      } catch (e) { drop(); }
    }
    // Prefer the shell's themed confirm; fall back to the native one.
    if (typeof window.dsConfirm === 'function') {
      window.dsConfirm('Uninstall ' + name + '?',
        'This removes ' + name + ' from this device.',
        { okLabel: 'Uninstall', danger: true }).then(function (ok) { if (ok) go(); });
    } else if (window.confirm('Uninstall ' + name + '?')) {
      go();
    }
  }
  window.hartPinIcon = function (id) {
    if (!layer || !M()[id]) return;
    if (layer.querySelector('.desktop-icon[data-id="' + id + '"]')) return;
    layer.appendChild(makeIcon({ id: id, x: PAD, y: PAD + firstFreeRow() * GRID }));
    persist();
  };
  // Installed app -> live desktop icon (NixOS-style). The app-installer pushes
  // an 'app_installed' A2UI event; the shell's SSE consumer calls this. We
  // register the entry into window.MANIFEST (so render()/hartPinIcon accept it
  // AND a later refresh still finds it) then REUSE hartPinIcon to place + persist
  // the icon. openPanel launches it via its 'exec' (the gtk-launch path).
  window.hartInstallIcon = function (entry) {
    if (!entry || !entry.id) return;
    var id = String(entry.id);
    window.MANIFEST = window.MANIFEST || {};
    // Merge (don't clobber a richer existing definition; fill missing fields).
    var prev = window.MANIFEST[id] || {};
    window.MANIFEST[id] = {
      title: entry.title || prev.title || id,
      icon: entry.icon || prev.icon || 'apps',
      exec: entry.exec || prev.exec || id,
      group: entry.group || prev.group || 'Installed',
      color: entry.color || prev.color,
      installed: true
    };
    // Layer may still be initializing (init() polls); retry the pin briefly.
    if (!layer) { setTimeout(function () { window.hartPinIcon(id); }, 400); return; }
    window.hartPinIcon(id);
  };
  // How many icon rows fit in one screen column (accounts for the top bar).
  function rowsPerScreen() {
    var top = 40;
    try {
      var v = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--hart-topbar-height'), 10);
      if (v) top = v;
    } catch (_) {}
    return Math.max(1, Math.floor((window.innerHeight - top - 2 * PAD) / GRID));
  }

  // Real SORT + column-major reflow into the grid. 'by' ∈ name|type|color|null
  // (null = keep DOM order = "auto-arrange"). Staggered glide so it reads as a
  // deliberate tidy, then clears the per-icon transition + persists (single
  // writer). Generalizes the old one-column hartAutoArrange.
  window.hartArrange = function (by) {
    if (!layer) return;
    var M_ = M();
    var items = Array.prototype.slice.call(layer.querySelectorAll('.desktop-icon'));
    var key = {
      name: function (el) { return ((M_[el.dataset.id] || {}).title || el.dataset.id || '').toLowerCase(); },
      type: function (el) { return ((M_[el.dataset.id] || {}).group || 'zzz').toLowerCase(); },
      color: function (el) { return el.getAttribute('data-ov-color') || (M_[el.dataset.id] || {}).color || '~'; }
    }[by] || null;
    if (key) items.sort(function (a, b) { var ka = key(a), kb = key(b); return ka < kb ? -1 : ka > kb ? 1 : 0; });
    var rows = rowsPerScreen(), i = 0;
    items.forEach(function (el) {
      var col = Math.floor(i / rows), row = i % rows; i++;
      el.style.transition = 'left var(--t-reveal,320ms) var(--lg-glide,ease) ' + (i * 8) + 'ms,top var(--t-reveal,320ms) var(--lg-glide,ease) ' + (i * 8) + 'ms';
      el.style.left = (PAD + col * GRID) + 'px';
      el.style.top = (PAD + row * GRID) + 'px';
    });
    if (window.HartSession) window.HartSession.set('icon_sort', by || 'auto');
    setTimeout(function () { items.forEach(function (el) { el.style.transition = ''; }); persist(); }, 600);
  };
  // Back-compat alias — existing callers (context menu, etc.) still work.
  window.hartAutoArrange = function () { window.hartArrange(null); };
  // The list of apps not yet on the desktop, as HartCtxMenu items.
  function _addAppItems() {
    var have = {}; readPositions().forEach(function (p) { have[p.id] = 1; });
    var M_ = M();
    var ids = Object.keys(M_).filter(function (id) { return !have[id]; }).slice(0, 40);
    if (!ids.length) return [{ icon: 'info', label: 'Everything is already on the desktop', disabled: true }];
    return ids.map(function (id) {
      return { id: id, icon: (M_[id].icon || 'apps'), label: (M_[id].title || id),
               onClick: function () { window.hartPinIcon && window.hartPinIcon(id); } };
    });
  }
  // Add-app picker. Prefer the self-contained menu (works regardless of the
  // shell's #ctx-menu); fall back to the shell renderer if HartCtxMenu hasn't
  // loaded yet. Position near the pointer when invoked from a context menu.
  window.hartAddAppPicker = function (x, y) {
    var items = _addAppItems();
    if (window.HartCtxMenu) {
      var px = (typeof x === 'number') ? x : Math.round(window.innerWidth / 2);
      var py = (typeof y === 'number') ? y : Math.round(window.innerHeight / 3);
      setTimeout(function () { window.HartCtxMenu.open(items, px, py); }, 0);
      return;
    }
    var menu = document.getElementById('ctx-menu');
    if (!menu || typeof window.ctxItem !== 'function') return;
    var html = items.map(function (it) {
      return it.disabled ? window.ctxItem(it.icon, it.label, '')
        : window.ctxItem(it.icon, it.label, "window.hartPinIcon&&hartPinIcon('" + it.id + "')");
    }).join('');
    setTimeout(function () { menu.innerHTML = html; menu.style.display = 'block'; }, 0);
  };

  function defaults() {
    // Default desktop icons. render() only shows ids present in window.MANIFEST
    // (the panel manifest), so these MUST be real manifest keys — the old list
    // ('files'/'weather'/'terminal'/'app_store'/'security') were system-app ids
    // absent from the manifest, so only 'appearance' ever rendered.
    var want = ['appearance', 'feed', 'agents_browse', 'recipes', 'notifications', 'communities'];
    var M_ = M(), out = [], row = 0;
    want.forEach(function (id) { if (M_[id]) { out.push({ id: id, x: PAD, y: PAD + row * GRID }); row++; } });
    return out;
  }

  function render(list) {
    layer.innerHTML = '';
    list.forEach(function (it) { if (M()[it.id]) layer.appendChild(makeIcon(it)); });
  }

  // ── Marquee (rubber-band) multi-select over the empty desktop ──
  // #hart-desktop is pointer-events:none (so right-click reaches the wallpaper
  // menu) and only icons capture events — therefore an empty-area drag lands on
  // .wallpaper / <body>. We bind on document, start a band ONLY when the press
  // begins on empty desktop (not on an icon / panel / chrome), draw a fixed
  // .lg-marquee, and toggle .selected on every icon whose rect intersects. A
  // plain empty click (no drag) clears the selection. Group-move of the selection
  // is handled in endDrag (the shared delta path).
  function initMarquee() {
    var mq = null, mx = 0, my = 0, marquing = false;
    function onEmpty(t) {
      // Empty desktop = the wallpaper or the body, and NOT inside any icon/panel/menu.
      return t && (t.classList && t.classList.contains('wallpaper') || t === document.body) &&
        !(t.closest && t.closest('.desktop-icon,.panel,.start-menu,.ctx-menu,.taskbar,.top-bar,.hart-senses,.hart-hero,#hart-ws-switcher'));
    }
    document.addEventListener('pointerdown', function (e) {
      if (e.button !== 0 || !onEmpty(e.target)) return;
      marquing = true; mx = e.clientX; my = e.clientY;
      mq = document.createElement('div'); mq.className = 'lg-marquee';
      mq.style.cssText = 'left:' + mx + 'px;top:' + my + 'px;width:0;height:0';
      document.body.appendChild(mq);
    });
    document.addEventListener('pointermove', function (e) {
      if (!marquing || !mq) return;
      var x = Math.min(e.clientX, mx), y = Math.min(e.clientY, my),
          w = Math.abs(e.clientX - mx), h = Math.abs(e.clientY - my);
      mq.style.left = x + 'px'; mq.style.top = y + 'px'; mq.style.width = w + 'px'; mq.style.height = h + 'px';
      if (!layer) return;
      var bx = x, by = y, bw = w, bh = h;
      Array.prototype.forEach.call(layer.querySelectorAll('.desktop-icon'), function (el) {
        var r = el.getBoundingClientRect();
        var hits = !(r.right < bx || r.left > bx + bw || r.bottom < by || r.top > by + bh);
        el.classList.toggle('selected', hits);
      });
    });
    function endMarquee() {
      if (!marquing) return;
      marquing = false;
      if (mq && mq.parentNode) mq.parentNode.removeChild(mq);
      mq = null;
    }
    document.addEventListener('pointerup', function (e) {
      if (!marquing) return;
      // A plain click on empty space (no real drag) clears the selection.
      if (Math.abs(e.clientX - mx) + Math.abs(e.clientY - my) < 4 && layer) {
        Array.prototype.forEach.call(layer.querySelectorAll('.desktop-icon.selected'),
          function (n) { n.classList.remove('selected'); });
      }
      endMarquee();
    });
    document.addEventListener('pointercancel', endMarquee);
  }

  // ── Context menus for the DESKTOP background and a WINDOW titlebar ──
  // A self-contained, glassy menu (HartCtxMenu) reused for all three surfaces
  // (icon menu lives in openIconMenu). We attach the contextmenu listener in the
  // CAPTURE phase and stopImmediatePropagation so the shell's own bubble-phase
  // #ctx-menu handler does not ALSO fire — this cleanly supersedes it for the
  // surfaces we own without editing the shell file. Touch gets a 500ms long
  // press equivalent. Every action routes to an EXISTING shell/file helper
  // (openPanel / minimizePanel / toggleMax / closePanel / hart*), no fork.

  // Resolve the panel id for a window target, mirroring the shell's _pid().
  function panelIdOf(t) {
    var p = t && t.closest && t.closest('.panel[data-panel-id]');
    return p ? p.getAttribute('data-panel-id') : null;
  }
  // Right-click happened on a window's titlebar (or its controls) — not the body.
  function titlebarTarget(t) {
    if (!t || !t.closest) return null;
    if (!t.closest('.panel-titlebar')) return null;
    return panelIdOf(t);
  }
  function isDesktopBg(t) {
    return t && ((t.classList && t.classList.contains('wallpaper')) || t === document.body) &&
      !(t.closest && t.closest('.desktop-icon,.panel,.start-menu,.ctx-menu,.taskbar,.top-bar,.hart-senses,.hart-hero,#hart-ws-switcher'));
  }

  function openWindowMenu(pid, x, y) {
    if (!window.HartCtxMenu || !pid) return;
    // Bring the window forward first so a right-click also focuses it (OS feel),
    // reusing the shell's canonical raise (keeps every other window's state).
    if (typeof window.bringToFront === 'function') { try { window.bringToFront(pid); } catch (e) {} }
    // The shell adds a '.maximized' class to a panel element in applyMax (the
    // 'panels{}' map is a lexical global not exposed on window, so we read the
    // DOM class instead — robust + no cross-script coupling).
    var pel = document.getElementById('panel-' + pid);
    var maxed = !!(pel && pel.classList.contains('maximized'));
    var items = [
      { icon: 'minimize', label: 'Minimize', onClick: function () { window.minimizePanel && window.minimizePanel(pid); } },
      { icon: maxed ? 'fullscreen_exit' : 'crop_square', label: maxed ? 'Restore' : 'Maximize',
        onClick: function () { window.toggleMax && window.toggleMax(pid); } },
      { sep: true },
      { icon: 'close', label: 'Close', danger: true, onClick: function () { window.closePanel && window.closePanel(pid); } }
    ];
    window.HartCtxMenu.open(items, x, y);
  }

  function openDesktopMenu(x, y) {
    if (!window.HartCtxMenu) return;
    var items = [
      { icon: 'palette', label: 'Personalize', onClick: function () { window.openPanel && window.openPanel('wallpaper_manager'); } },
      { icon: 'wallpaper', label: 'Change wallpaper', onClick: function () { window.openPanel && window.openPanel('wallpaper_manager'); } },
      { sep: true },
      { icon: 'add_to_home_screen', label: 'Add app to desktop', onClick: function () { window.hartAddAppPicker && window.hartAddAppPicker(x, y); } },
      { icon: 'create_new_folder', label: 'New folder', onClick: newDesktopFolder },
      { icon: 'grid_view', label: 'Auto-arrange icons', onClick: function () { window.hartArrange && window.hartArrange(null); } },
      { sep: true },
      { icon: 'desktop_windows', label: 'Display settings', onClick: openDisplaySettings },
      { icon: 'refresh', label: 'Refresh', onClick: function () { location.reload(); } }
    ];
    window.HartCtxMenu.open(items, x, y);
  }

  // "New folder" routes to the file manager, which OWNS folder creation (one
  // creator, no fork). If neither the panel nor a direct creator exists we no-op
  // gracefully rather than invent a second folder store.
  function newDesktopFolder() {
    if (typeof window.hartNewDesktopFolder === 'function') { window.hartNewDesktopFolder(); return; }
    if (typeof window.openPanel === 'function') {
      window.openPanel('file_manager');               // the Files app is the canonical folder creator
    }
  }
  // "Display settings" opens the shell's existing Display system panel.
  function openDisplaySettings() {
    if (typeof window.openPanel === 'function') window.openPanel('display');
  }

  function initContextMenus() {
    // CAPTURE phase: we win over the shell's document-level contextmenu handler.
    document.addEventListener('contextmenu', function (e) {
      var t = e.target;
      // Icons own their own contextmenu handler (it stops propagation); leave them.
      if (t && t.closest && t.closest('.desktop-icon')) return;
      // Only SUPERSEDE the shell's right-click menu once our richer glass menu is
      // actually loaded. The HartCtxMenu <script> is injected async, so until it
      // resolves we must NOT swallow the event (preventDefault + stop) or an early
      // right-click would be a dead no-op; let the shell's own #ctx-menu handle it.
      if (!window.HartCtxMenu) return;
      var pid = titlebarTarget(t);
      if (pid) {
        e.preventDefault(); e.stopImmediatePropagation();
        openWindowMenu(pid, e.clientX, e.clientY);
        return;
      }
      if (isDesktopBg(t)) {
        e.preventDefault(); e.stopImmediatePropagation();
        openDesktopMenu(e.clientX, e.clientY);
        return;
      }
      // Anything else: let the shell decide (we don't take over panel bodies).
    }, true);

    // Touch long-press == right-click, for the desktop background and titlebars.
    // Icons handle their own long-press inside bindIcon. Cancelled by movement
    // past TAP_PX or an early release.
    var lp = 0, lx = 0, ly = 0, lpid = null, lpKind = null;
    function cancelLP() { if (lp) { clearTimeout(lp); lp = 0; } lpid = null; lpKind = null; }
    document.addEventListener('pointerdown', function (e) {
      if (e.pointerType !== 'touch' || e.button !== 0) return;
      var t = e.target;
      if (t && t.closest && t.closest('.desktop-icon')) return;   // icon owns it
      var pid = titlebarTarget(t);
      var kind = pid ? 'window' : (isDesktopBg(t) ? 'desktop' : null);
      if (!kind) return;
      lx = e.clientX; ly = e.clientY; lpid = pid; lpKind = kind;
      cancelLP();                                                 // clear any stale timer (keeps coords)
      lpid = pid; lpKind = kind;
      lp = setTimeout(function () {
        lp = 0;
        if (lpKind === 'window') openWindowMenu(lpid, lx, ly);
        else openDesktopMenu(lx, ly);
      }, 500);
    }, true);
    document.addEventListener('pointermove', function (e) {
      if (!lp) return;
      if (Math.abs(e.clientX - lx) + Math.abs(e.clientY - ly) > TAP_PX) cancelLP();
    }, true);
    document.addEventListener('pointerup', cancelLP, true);
    document.addEventListener('pointercancel', cancelLP, true);
  }

  // ── Touch dragging for WINDOW titlebars ──
  // The shell drags panels via mouse-only handlers (titlebar onmousedown ->
  // startDrag, document mousemove/mouseup). Those never fire continuously under
  // touch, so a finger can't move a window. We add a TOUCH-ONLY pointer drag
  // that moves the panel with a GPU transform and commits to left/top on
  // release, raising the window first (focus) and clamping the titlebar on
  // screen with the SAME margins the shell uses. Mouse keeps the shell's path
  // (no duplication for mouse); we only fill the touch gap. We never touch the
  // shell's 'panels{}' model (it re-reads offsetLeft/Top on its next drag).
  function initWindowTouchDrag() {
    var pid = null, pel = null, sx = 0, sy = 0, dx = 0, dy = 0, raf = 0, active = false;
    var KEEP = 80, TOP = 40, TASK = 44;
    function onDown(e) {
      if (e.pointerType !== 'touch' || e.button !== 0) return;
      var t = e.target;
      if (!t || !t.closest) return;
      // Don't start a drag from the titlebar control buttons (min/max/close).
      if (t.closest('.panel-titlebar .ctrl')) return;
      var bar = t.closest('.panel-titlebar');
      if (!bar) return;
      var panel = bar.closest('.panel[data-panel-id]');
      if (!panel) return;
      // Maximized windows don't drag (matches the shell's startDrag guard).
      if (panel.classList.contains('maximized')) return;
      pid = panel.getAttribute('data-panel-id');
      pel = panel;
      active = true; dx = 0; dy = 0;
      sx = e.clientX; sy = e.clientY;
      if (typeof window.bringToFront === 'function') { try { window.bringToFront(pid); } catch (_) {} }
      pel.style.willChange = 'transform';
      try { bar.setPointerCapture(e.pointerId); } catch (_) {}
    }
    function onMove(e) {
      if (!active || !pel) return;
      dx = e.clientX - sx; dy = e.clientY - sy;
      if (raf) return;
      raf = requestAnimationFrame(function () {
        raf = 0;
        pel.style.transform = 'translate(' + dx + 'px,' + dy + 'px)';
      });
    }
    function onUp(e) {
      if (!active || !pel) return;
      active = false;
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
      pel.style.transform = '';
      pel.style.willChange = '';
      // Commit, clamped on-screen (same rule as the shell's move handler).
      var nx = (pel.offsetLeft || 0) + dx, ny = (pel.offsetTop || 0) + dy;
      nx = Math.min(Math.max(nx, KEEP - pel.offsetWidth), window.innerWidth - KEEP);
      ny = Math.min(Math.max(ny, TOP), window.innerHeight - TASK - 28);
      pel.style.left = Math.round(nx) + 'px';
      pel.style.top = Math.round(ny) + 'px';
      pid = null; pel = null;
    }
    document.addEventListener('pointerdown', onDown, true);
    document.addEventListener('pointermove', onMove, true);
    document.addEventListener('pointerup', onUp, true);
    document.addEventListener('pointercancel', onUp, true);
  }

  function init() {
    layer = document.getElementById('hart-desktop');
    if (!layer || !window.MANIFEST || !window.HartSession) { return setTimeout(init, 300); }
    initMarquee();
    initContextMenus();
    initWindowTouchDrag();
    window.HartSession.ready(function () {
      var icons = window.HartSession.get('desktop_icons');
      render((icons && icons.length) ? icons : defaults());
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
