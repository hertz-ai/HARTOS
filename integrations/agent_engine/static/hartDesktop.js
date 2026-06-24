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
  var layer = null;  // positions persist via the shared window.HartSession (one
                     // writer per key, so the wallpaper module can't clobber us)

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

    el.querySelector('.di-label').textContent = eff.label;          // textContent = no HTML injection
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

    el.addEventListener('dblclick', function () { launch(id); }); // desktop (mouse): double-click opens
    el.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); launch(id); }
    });

    el.addEventListener('pointerdown', function (e) {
      if (e.button !== 0) return;
      dragging = true; moved = false;
      sx = e.clientX; sy = e.clientY;
      ox = parseInt(el.style.left, 10) || 0; oy = parseInt(el.style.top, 10) || 0;
      el.classList.add('dragging');
      if (layer) layer.classList.add('arranging');   // show the snap-grid overlay while dragging
      try { el.setPointerCapture(e.pointerId); } catch (_) {}
    });
    el.addEventListener('pointermove', function (e) {
      if (!dragging) return;
      dx = e.clientX - sx; dy = e.clientY - sy;
      if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
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
      if (layer) layer.classList.remove('arranging');
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
      try { el.releasePointerCapture(e.pointerId); } catch (_) {}
      el.style.transform = '';
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
      } else if (e && e.pointerType === 'touch') {    // touch surface: a single tap opens
        launch(id);
      } else {                                        // desktop mouse: single click selects
        selectIcon(el);
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
        window.ctxItem('tune', 'Customize…', "window.hartCustomizeIcon&&hartCustomizeIcon('" + id + "')"),
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

  function init() {
    layer = document.getElementById('hart-desktop');
    if (!layer || !window.MANIFEST || !window.HartSession) { return setTimeout(init, 300); }
    initMarquee();
    window.HartSession.ready(function () {
      var icons = window.HartSession.get('desktop_icons');
      render((icons && icons.length) ? icons : defaults());
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
