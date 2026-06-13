/*
 * hartPersonalize.js — Themes & Wallpaper gallery (Phase B).
 *
 * Renders the Personalize panel (preset theme cards + wallpaper chooser) and
 * applies wallpaper live. Reuses the shell's own primitives — window.applyPreset
 * (POST /api/social/theme/apply), the /api/shell/wallpaper collection+set routes,
 * and window.HartSession for persistence (so it shares the one session blob).
 * The .wallpaper div IS the visible desktop background on the cage kiosk, so
 * CSS wallpapers paint it directly for instant, no-rebuild feedback.
 *
 * The panel HTML is built here (plain JS) rather than in the shell f-string, so
 * loadWallpaperPanel() stays a one-line delegate (no brace-escaping risk).
 */
(function () {
  'use strict';

  // Swatch palettes for the 8 server-side presets (applyPreset applies by id;
  // these are only the gallery card colours: accent a, bg b, deep c).
  var PRESETS = window.HART_THEME_PRESETS = [
    { id: 'hart-default', name: 'Aurora',    a: '#00D4AA', b: '#0F0E17', c: '#16213e' },
    { id: 'midnight',     name: 'Midnight',  a: '#5B8CFF', b: '#0a0e1f', c: '#10204a' },
    { id: 'cyberpunk',    name: 'Cyberpunk', a: '#FF2E97', b: '#0d0221', c: '#241734' },
    { id: 'forest',       name: 'Forest',    a: '#3FBF7F', b: '#0c160f', c: '#143024' },
    { id: 'sunset',       name: 'Sunset',    a: '#FF8A4C', b: '#1a0f14', c: '#3a1726' },
    { id: 'arctic',       name: 'Arctic',    a: '#3AA6FF', b: '#0c1622', c: '#16324a' },
    { id: 'minimal',      name: 'Minimal',   a: '#9aa3ad', b: '#121214', c: '#222226' },
    { id: 'potato',       name: 'Potato',    a: '#00D4AA', b: '#0c0c0c', c: '#161616' }
  ];

  // Built-in CSS wallpapers — animated multi-hue gradients + solids, theme
  // independent (de-monochrome), painted live + persisted.
  var WALLPAPERS = window.HART_WALLPAPERS = [
    { id: 'aurora', name: 'Aurora', live: true, css: 'radial-gradient(120% 120% at 18% 0%,rgba(0,212,170,0.12),transparent 50%),radial-gradient(100% 100% at 100% 100%,rgba(22,33,62,0.55),transparent 60%),linear-gradient(135deg,#0F0E17 0%,#1a1a2e 50%,#16213e 100%)' },
    { id: 'nebula', name: 'Nebula', live: true, css: 'radial-gradient(50% 60% at 25% 30%,rgba(108,99,255,0.22),transparent 60%),radial-gradient(50% 60% at 75% 70%,rgba(255,46,151,0.16),transparent 60%),linear-gradient(160deg,#0b0a1a,#16102b 60%,#0b0a1a)' },
    { id: 'ocean',  name: 'Ocean',  live: true, css: 'radial-gradient(60% 70% at 30% 20%,rgba(34,176,255,0.18),transparent 60%),linear-gradient(160deg,#06121f,#0a2236 60%,#06121f)' },
    { id: 'ember',  name: 'Ember',  live: true, css: 'radial-gradient(60% 70% at 70% 25%,rgba(255,138,76,0.18),transparent 60%),linear-gradient(160deg,#170d0a,#2a1410 60%,#170d0a)' },
    { id: 'ink',    name: 'Ink',    live: false, css: '#0b0b0f' },
    { id: 'slate',  name: 'Slate',  live: false, css: '#15171c' },
    { id: 'plum',   name: 'Plum',   live: false, css: '#1a1024' },
    { id: 'pine',   name: 'Pine',   live: false, css: '#0c1a14' }
  ];

  function applyCss(css) {
    var wp = document.querySelector('.wallpaper');
    if (wp && css) wp.style.background = css;
  }

  window.hartSetWallpaper = function (css) {
    applyCss(css);
    if (window.HartSession) window.HartSession.set('wallpaper_css', css);
  };
  window.hartSetWallpaperImage = function (url, path) {
    var css = "center/cover no-repeat url('" + url + "')";
    applyCss(css);
    if (window.HartSession) window.HartSession.set('wallpaper_css', css);
    if (path) {
      fetch('/api/shell/wallpaper/set', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: path }) }).catch(function () {});
    }
  };

  function card(extraClass, previewStyle, name, dotColor) {
    var el = document.createElement('div');
    el.className = 'hart-tile ' + extraClass;
    el.setAttribute('role', 'button'); el.setAttribute('tabindex', '0');
    var prev = document.createElement('div');
    prev.className = 'htc-prev';
    prev.setAttribute('style', previewStyle);
    if (dotColor) { var dot = document.createElement('span'); dot.className = 'htc-dot';
      dot.setAttribute('style', 'background:' + dotColor); prev.appendChild(dot); }
    var nm = document.createElement('div'); nm.className = 'htc-name'; nm.textContent = name;
    el.appendChild(prev); el.appendChild(nm);
    return el;
  }

  function toast(t, m) { if (typeof window.showToast === 'function') window.showToast(t, m, 'success'); }

  // Build the Personalize panel into el. Called by loadWallpaperPanel.
  window.hartRenderPersonalize = function (el) {
    if (!el) return;
    el.innerHTML = '';
    var grid = document.createElement('div'); grid.className = 'ds-panel-grid ds-fade-in';
    var title = document.createElement('div'); title.className = 'ds-panel-title'; title.textContent = 'Personalize';
    grid.appendChild(title);

    function section(label) {
      var s = document.createElement('div'); s.className = 'ds-section-label'; s.textContent = label; grid.appendChild(s);
      var g = document.createElement('div'); g.className = 'hart-gallery'; grid.appendChild(g); return g;
    }

    // Themes
    var tg = section('Theme');
    PRESETS.forEach(function (p) {
      var c = card('hart-theme-card', 'background:linear-gradient(135deg,' + p.b + ',' + p.c + ')', p.name, p.a);
      c.addEventListener('click', function () {
        if (typeof window.applyPreset === 'function')
          window.applyPreset(p.id, { set textContent(v) { toast('Theme', p.name); } });
      });
      tg.appendChild(c);
    });

    // CSS wallpapers
    var wg = section('Wallpaper');
    WALLPAPERS.forEach(function (w) {
      var c = card('hart-wall-card', 'background:' + w.css, w.name + (w.live ? ' · live' : ''), null);
      c.addEventListener('click', function () { window.hartSetWallpaper(w.css); toast('Wallpaper', w.name); });
      wg.appendChild(c);
    });

    // Image wallpapers from the existing collection (best effort)
    var ig = section('Images');
    var loading = document.createElement('div'); loading.className = 'ds-body-sm ds-text-muted';
    loading.textContent = 'Loading…'; ig.appendChild(loading);
    fetch('/api/shell/wallpaper/collection',
      { signal: window.HartTimeoutSignal ? window.HartTimeoutSignal(5000) : null })
      .then(function (r) { return r.json(); }).then(function (col) {
        ig.innerHTML = '';
        var imgs = (col.wallpapers || []).slice(0, 12);
        if (!imgs.length) { ig.appendChild(loading); loading.textContent = 'No image wallpapers found'; return; }
        imgs.forEach(function (w) {
          var url = '/api/shell/files/thumb?path=' + encodeURIComponent(w.path);
          var c = card('hart-wall-card', 'background:#1a1a1a;overflow:hidden', w.name || 'Image', null);
          var img = document.createElement('img');
          img.src = url; img.setAttribute('style', 'width:100%;height:100%;object-fit:cover');
          img.onerror = function () { img.style.display = 'none'; };
          c.querySelector('.htc-prev').appendChild(img);
          c.addEventListener('click', function () { window.hartSetWallpaperImage(url, w.path); toast('Wallpaper', w.name || 'Image'); });
          ig.appendChild(c);
        });
      }).catch(function () { ig.innerHTML = ''; var d = document.createElement('div');
        d.className = 'ds-body-sm ds-text-muted'; d.textContent = 'Images unavailable'; ig.appendChild(d); });

    el.appendChild(grid);
  };

  // Restore the saved wallpaper on boot.
  function restore() {
    if (!window.HartSession) { return setTimeout(restore, 300); }
    window.HartSession.ready(function () {
      var css = window.HartSession.get('wallpaper_css');
      if (css) applyCss(css);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', restore);
  else restore();
})();
