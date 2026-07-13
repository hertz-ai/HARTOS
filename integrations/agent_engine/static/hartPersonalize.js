/*
 * hartPersonalize.js — Customization hub (Personalize panel).
 *
 * The ONE customization surface. It renders (and applies live) five sections,
 * all EXTENDING the shell's own primitives so there is a single path per concern:
 *
 *   1. Palette   — brand accent DUOTONE (lead accent + secondary + bg). Applying a
 *                  palette sets the brand CSS vars CLIENT-SIDE instantly (no reload),
 *                  persists via HartSession, and best-effort extends
 *                  /api/appearance/apply (secondary_accent + custom) so the choice
 *                  survives a hard reload / propagates to other surfaces. Ships
 *                  "Vibrant" (teal + violet, the b1.2 default), "Monotone Teal"
 *                  (original single-hue), and a CUSTOM colour picker.  (#161)
 *   2. Orb       — switchable orb VARIETY, wired to voiceOrbViz.js's style registry
 *                  (window.HartVoiceOrbViz.STYLES). Applies live + persists.  (#140)
 *   3. Theme     — the 8 server-side theme presets (window.applyPreset, unchanged).
 *   4. Wallpaper — the built-in CSS gradients / solids (live, persisted).
 *   5. Backgrounds — video / lottie / gif wallpapers. BEST-EFFORT: on the software
 *                  floor (body.gpu-software / potato) video + lottie DEGRADE to a
 *                  static poster frame or the gradient (never play on a potato);
 *                  gif is cheap and always renders.  (#162)
 *   6. Images    — image wallpapers from the local Pictures collection.
 *
 * Reuses window.applyPreset (POST /api/appearance/apply), the /api/shell/wallpaper
 * routes, and window.HartSession for persistence (one shared session blob). The
 * .wallpaper div IS the visible desktop background on the cage kiosk, so CSS +
 * media paint it directly for instant, no-rebuild feedback.
 *
 * Consult docs/design/HOME_DESKTOP_DESIGN_CHECKLIST.md before changing this file
 * (BINDING desktop-design rule). Old-WebKit-safe: string concat, try/catch, no
 * optional chaining / template literals / nullish coalescing.
 */
(function () {
  'use strict';

  // Same base-resolution convention hartHome.js uses (same-origin fallback when
  // window.BACKEND is not exposed). One place, no parallel guess.
  function backendBase() {
    return (typeof window.BACKEND === 'string' && window.BACKEND) ? window.BACKEND : '';
  }

  // ── Brand PALETTES (#161). id/name + the ambient hue QUAD: lead accent / mood
  // hue #1 (a), secondary accent + ambient #2 (a2), ambient hues #3/#4 (a3/a4,
  // OPTIONAL), canvas background (b). An OPTIONAL `accent` pins the FUNCTIONAL
  // signifier (orb core / primary CTA / earnings) to a fixed hue while a..a4 drive
  // the AMBIENT/mood field (the steward hybrid: b1.2 teal stays on function even
  // when the mood goes violet-lead). 'vibrant' = the b1.2 teal+violet default;
  // 'monotone-teal' = the original single-hue. a3/a4 omitted -> --hart-amb-3/4 stay
  // at the theme default (existing entries render pixel-identical). The 6 Aura moods
  // (aura_template_full.html:625-632) carry the full quad + accent pinned teal.
  // The CUSTOM picker builds an ad-hoc palette from three colour inputs. Exposed for
  // reuse/tests (no parallel table).
  var PALETTES = window.HART_PALETTES = [
    { id: 'vibrant',       name: 'Vibrant',       a: '#00E6C3', a2: '#9B5CFF', b: '#05060C' },
    { id: 'monotone-teal', name: 'Monotone Teal', a: '#00D4AA', a2: '#00D4AA', b: '#0F0E17' },
    { id: 'aqua',          name: 'Aqua',          a: '#00E6A8', a2: '#29C5FF', b: '#04070E' },
    { id: 'neon',          name: 'Neon',          a: '#39FF14', a2: '#FF00E5', b: '#08010A' },
    { id: 'sunset',        name: 'Sunset',        a: '#FF8A4C', a2: '#FF2E9A', b: '#16090F' },
    { id: 'electric',      name: 'Electric',      a: '#00D9FF', a2: '#7C3AED', b: '#060814' },
    { id: 'ember',         name: 'Ember',         a: '#FF6B35', a2: '#FFC53F', b: '#140803' },
    { id: 'vapor',         name: 'Vapor',         a: '#FF71CE', a2: '#01CDFE', b: '#0A0618' },
    { id: 'ocean',         name: 'Ocean',         a: '#00C6FF', a2: '#0066FF', b: '#041018' },
    { id: 'coral',         name: 'Coral',         a: '#FF5E7E', a2: '#FFB84C', b: '#170A0D' },
    // ── Aura moods (aura_template_full.html:625-632). accent pinned teal = steward
    // hybrid (functional signifier stays teal; a..a4 = the mood quad drive ONLY the
    // ambient field). Quads = oklch_hex(SLOTS, hue table) — Aurora hand-verified, the
    // other five are the deterministic converter output; shared cosmic canvas bg.
    { id: 'aurora',  name: 'Aurora',  accent: '#00E6C3', a: '#B182FF', a2: '#00DDF9', a3: '#FB66B6', a4: '#FFB330', b: '#04050B' },
    { id: 'solar',   name: 'Solar',   accent: '#00E6C3', a: '#FF7600', a2: '#FF9B92', a3: '#DA74F1', a4: '#E5C226', b: '#04050B' },
    { id: 'oceanic', name: 'Oceanic', accent: '#00E6C3', a: '#00B7FF', a2: '#00E1E2', a3: '#00C877', a4: '#9CBDFF', b: '#04050B' },
    { id: 'nebula',  name: 'Nebula',  accent: '#00E6C3', a: '#EA6AE2', a2: '#B2B8FF', a3: '#00B0FF', a4: '#FF9699', b: '#04050B' },
    { id: 'verdant', name: 'Verdant', accent: '#00E6C3', a: '#00C756', a2: '#CACC4A', a3: '#00C6D5', a4: '#F3BA25', b: '#04050B' },
    { id: 'ember-aura', name: 'Ember Aura', accent: '#00E6C3', a: '#FF605D', a2: '#FFA85D', a3: '#F269CB', a4: '#D4AAFF', b: '#04050B' }
  ];

  // Swatch palettes for the 8 server-side presets (applyPreset applies by id; these
  // are only the gallery card colours). 'a2' (secondary accent) added to the preset
  // schema so a swatch reads its duotone and stays consistent with the palette layer.
  var PRESETS = window.HART_THEME_PRESETS = [
    { id: 'hart-default', name: 'Aurora',    a: '#00D4AA', a2: '#29C5FF', b: '#0F0E17', c: '#16213e' },
    { id: 'midnight',     name: 'Midnight',  a: '#5B8CFF', a2: '#9B5CFF', b: '#0a0e1f', c: '#10204a' },
    { id: 'cyberpunk',    name: 'Cyberpunk', a: '#FF2E97', a2: '#29C5FF', b: '#0d0221', c: '#241734' },
    { id: 'forest',       name: 'Forest',    a: '#3FBF7F', a2: '#00E6C3', b: '#0c160f', c: '#143024' },
    { id: 'sunset',       name: 'Sunset',    a: '#FF8A4C', a2: '#FF2E9A', b: '#1a0f14', c: '#3a1726' },
    { id: 'arctic',       name: 'Arctic',    a: '#3AA6FF', a2: '#29C5FF', b: '#0c1622', c: '#16324a' },
    { id: 'minimal',      name: 'Minimal',   a: '#9aa3ad', a2: '#c8ced6', b: '#121214', c: '#222226' },
    { id: 'potato',       name: 'Potato',    a: '#00D4AA', a2: '#00D4AA', b: '#0c0c0c', c: '#161616' }
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
  var DEFAULT_GRADIENT = WALLPAPERS[0].css;

  // Bundled, offline media backgrounds (#162). The Hevolve lottie ships with the
  // ISO (used by the boot splash), so a live lottie background works with NO
  // network. type is one of 'video' | 'lottie' | 'gif'.
  var MEDIA_BGS = window.HART_MEDIA_BGS = [
    { id: 'hevolve-lottie', name: 'Hevolve', type: 'lottie', url: '/shell/static/hevolve-anim.json' }
  ];

  // Presentational swatch hues for each orb style (the authoritative render palette
  // lives in voiceOrbViz.js STYLES; this is only the picker preview).
  var ORB_PREVIEW = {
    'vibrant':  'radial-gradient(circle at 50% 45%,#00E6C3,#0b1b2a 62%),radial-gradient(circle at 72% 72%,rgba(155,92,255,.55),transparent 55%)',
    'ring-orb': 'radial-gradient(circle at 50% 50%,transparent 40%,#29C5FF 47%,transparent 54%),radial-gradient(circle at 50% 50%,transparent 60%,rgba(41,197,255,.5) 66%,transparent 72%),#0a1622',
    'nebula':   'radial-gradient(circle at 42% 42%,#FF2E9A,transparent 55%),radial-gradient(circle at 66% 62%,#9B5CFF,transparent 55%),radial-gradient(circle at 55% 55%,rgba(41,197,255,.5),transparent 62%),#0b0a1a',
    'minimal':  'radial-gradient(circle at 50% 48%,#ECF1F4,#20222c 40%)',
    'pulse':    'radial-gradient(circle at 50% 48%,#8CFCE4,#00E6C3 24%,#06201b 70%)'
  };

  // ── DOM helpers ──────────────────────────────────────────────────────────────
  function wpHost() { return document.querySelector('.wallpaper'); }
  function applyCss(css) {
    var wp = wpHost();
    if (wp && css) wp.style.background = css;
  }
  function hexToRgb(hex) {
    hex = (hex || '').replace('#', '');
    if (hex.length === 3) hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
    var r = parseInt(hex.slice(0, 2), 16), g = parseInt(hex.slice(2, 4), 16), b = parseInt(hex.slice(4, 6), 16);
    if (isNaN(r) || isNaN(g) || isNaN(b)) return null;
    return r + ',' + g + ',' + b;
  }
  function toast(t, m) { if (typeof window.showToast === 'function') window.showToast(t, m, 'success'); }

  // ── PALETTE: paint brand vars client-side (instant), persist, extend the server
  // theme route. paintPalette is the pure client apply (no persistence) used on
  // restore; applyPalette is the full mutator (paint + HartSession + server). ─────
  function paintPalette(p) {
    var root = document.documentElement;
    if (!root || !root.style || !root.style.setProperty) return;
    // Functional accent = the OPTIONAL override when present (teal on moods), else the
    // lead hue. Drives --hart-accent (orb core / primary CTA / earnings / live status).
    var acc = p.accent || p.a;
    if (acc) {
      root.style.setProperty('--hart-accent', acc);
      var rgb = hexToRgb(acc); if (rgb) root.style.setProperty('--hart-accent-rgb', rgb);
    }
    if (p.a2) {
      root.style.setProperty('--hart-a2', p.a2);
      var rgb2 = hexToRgb(p.a2); if (rgb2) root.style.setProperty('--hart-a2-rgb', rgb2);
    }
    // Ambient quad — the MOOD. amb-1 = p.a (NOT acc), so a mood is violet-lead while
    // the functional accent stays teal. amb-3/4 OPTIONAL: when a palette omits them the
    // theme default stands (existing duotone palettes render pixel-identical). Reuses the
    // keystone --hart-amb-1..4 (+ -rgb) consumed by .hart-ambient / .wallpaper.
    var amb = [p.a, p.a2, p.a3, p.a4];
    for (var i = 0; i < 4; i++) {
      var h = amb[i]; if (!h) continue;
      var rg = hexToRgb(h);
      root.style.setProperty('--hart-amb-' + (i + 1), h);
      if (rg) root.style.setProperty('--hart-amb-' + (i + 1) + '-rgb', rg);
    }
    if (p.b) root.style.setProperty('--hart-background', p.b);
  }
  function applyPalette(p, opts) {
    paintPalette(p);
    // Persist the full quad + functional-accent override so restore() re-paints the
    // ambient mood (not just the duotone). One session blob, no parallel store.
    if (window.HartSession) window.HartSession.set('palette',
      { a: p.a, a2: p.a2, a3: p.a3, a4: p.a4, b: p.b, accent: p.accent });
    if (!(opts && opts.noServer)) {
      // Extend /api/appearance/apply (do NOT fork): carry the secondary accent + the
      // custom colours INCLUDING the ambient quad so the server persists them as
      // overrides (they flow through the SAME custom-overrides path into
      // get_css_variables -> --hart-amb-1..4). accent = the FUNCTIONAL accent (teal on
      // moods) so the persisted --hart-accent matches the client. Best-effort — the
      // client apply + HartSession are already the source of truth for instant +
      // restore; a failed post is non-fatal (offline-first).
      try {
        fetch(backendBase() + '/api/appearance/apply', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ secondary_accent: p.a2, custom: {
            accent: p.accent || p.a, secondary: p.a2, background: p.b,
            ambient_1: p.a, ambient_2: p.a2, ambient_3: p.a3, ambient_4: p.a4
          } }),
          signal: window.HartTimeoutSignal ? window.HartTimeoutSignal(5000) : null
        }).catch(function () {});
      } catch (e) {}
    }
  }
  // Resolve a mood/palette ID -> the PALETTES entry (or null on a miss). The
  // authoritative client-side vocabulary owner: the LLM-composed `mood` id from the
  // agentic home push (compose_home) is resolved HERE before paintPalette, so an
  // unknown id is a graceful no-op (never a broken paint). One lookup, no parallel list.
  function paletteById(id) {
    if (!id) return null;
    var want = String(id).toLowerCase();
    for (var i = 0; i < PALETTES.length; i++) {
      if (PALETTES[i] && String(PALETTES[i].id).toLowerCase() === want) return PALETTES[i];
    }
    return null;
  }
  window.HartPalette = { apply: applyPalette, paint: paintPalette, list: PALETTES, byId: paletteById };

  // ── On-desktop MOOD DOCK (plan step 3). Renders HART_PALETTES as named swatches
  // that call the reload-free applyPalette — the SAME palette store + apply path as
  // the hub (no second palette table, no reload). The swatch is the QUAD
  // conic-gradient (a..a4), mirroring aura_template_full.html:407-414. Mounted by the
  // shell into a fixed-canvas glass dock (liquid_ui_service) — chrome placement per
  // the BINDING HOME_DESKTOP_DESIGN_CHECKLIST (inside the fixed canvas, no scroll).
  window.hartRenderMoodDock = function (host) {
    if (!host) return;
    host.innerHTML = '';
    var lbl = document.createElement('span'); lbl.className = 'hart-mood-label'; lbl.textContent = 'MOOD';
    host.appendChild(lbl);
    PALETTES.forEach(function (p) {
      var sw = document.createElement('div'); sw.className = 'hart-mood-sw';
      sw.setAttribute('role', 'button'); sw.setAttribute('tabindex', '0'); sw.title = p.name;
      sw.style.background = 'conic-gradient(from 40deg,' + p.a + ',' + (p.a2 || p.a) + ',' +
        (p.a3 || p.a2 || p.a) + ',' + (p.a4 || p.a) + ',' + p.a + ')';
      activate(sw, function () { applyPalette(p); toast('Mood', p.name); });
      host.appendChild(sw);
    });
  };

  // ── ORB VARIETY (#140): the customization hub owns the persisted pref
  // (HartSession.orb_style) and drives the ONE live orb instance
  // (window._hartVoiceOrb.setStyle). Single writer, no parallel persistence. ──────
  function orbStyleList() {
    return (window.HartVoiceOrbViz && window.HartVoiceOrbViz.STYLES) ||
      [{ id: 'vibrant', name: 'Vibrant' }, { id: 'ring-orb', name: 'Ring Orb' },
       { id: 'nebula', name: 'Nebula' }, { id: 'minimal', name: 'Minimal' }, { id: 'pulse', name: 'Pulse' }];
  }
  function defaultOrbStyle() {
    return (window.HartVoiceOrbViz && window.HartVoiceOrbViz.DEFAULT_STYLE) || 'vibrant';
  }
  function getOrbStyle() {
    var v = window.HartSession && window.HartSession.get('orb_style');
    return v || defaultOrbStyle();
  }
  function applyOrbToCanvas(id) {
    try { if (window._hartVoiceOrb && window._hartVoiceOrb.setStyle) window._hartVoiceOrb.setStyle(id); } catch (e) {}
  }
  function setOrbStyle(id) {
    if (window.HartSession) window.HartSession.set('orb_style', id);
    applyOrbToCanvas(id);
  }
  window.HartOrbStyle = { get: getOrbStyle, set: setOrbStyle,
    apply: applyOrbToCanvas, restore: function () { applyOrbToCanvas(getOrbStyle()); } };

  // ── WALLPAPER media host: manage a single #hart-wp-media child in .wallpaper so
  // switching background types never leaves a stale <video>/lottie behind. ────────
  function clearWpMedia() {
    var wp = wpHost(); if (!wp) return;
    if (wp._lottieAnim) { try { wp._lottieAnim.destroy(); } catch (e) {} wp._lottieAnim = null; }
    var m = wp.querySelector('#hart-wp-media');
    if (m && m.parentNode) m.parentNode.removeChild(m);
  }
  // The genuinely per-frame-expensive floor: software render / potato. On it, video
  // + lottie must DEGRADE to a static frame so the hang-free baseline is preserved.
  function isSoftwareFloor() {
    try {
      if (window.HART_PERF && window.HART_PERF.potato) return true;
      if (document.body && document.body.classList && document.body.classList.contains('gpu-software')) return true;
    } catch (e) {}
    return false;
  }

  window.hartSetWallpaper = function (css) {
    clearWpMedia();
    applyCss(css);
    if (window.HartSession) { window.HartSession.set('wallpaper_css', css); window.HartSession.set('wallpaper_bg', null); }
  };
  window.hartSetWallpaperImage = function (url, path) {
    clearWpMedia();
    var css = "center/cover no-repeat url('" + url + "')";
    applyCss(css);
    if (window.HartSession) { window.HartSession.set('wallpaper_css', css); window.HartSession.set('wallpaper_bg', null); }
    if (path) {
      fetch('/api/shell/wallpaper/set', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: path }) }).catch(function () {});
    }
  };
  // video / lottie / gif backgrounds (#162). Returns true when it DEGRADED to a
  // static frame (used by tests + callers). Never throws; degrade-not-die.
  window.hartSetWallpaperMedia = function (type, url, poster) {
    var wp = wpHost();
    if (!wp || !url) return false;
    clearWpMedia();
    var degraded = false;
    var posterCss = poster ? ("center/cover no-repeat url('" + poster + "')") : DEFAULT_GRADIENT;
    if (type === 'gif') {
      // GIF animates natively + cheaply -> always renders, even on a potato.
      applyCss("center/cover no-repeat url('" + url + "')");
    } else if (type === 'video') {
      if (isSoftwareFloor()) { applyCss(posterCss); degraded = true; }
      else {
        applyCss(posterCss);   // shows under the video / if it fails to load
        var v = document.createElement('video');
        v.id = 'hart-wp-media'; v.src = url;
        v.autoplay = true; v.loop = true; v.muted = true;
        v.setAttribute('playsinline', ''); v.setAttribute('aria-hidden', 'true');
        v.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;object-fit:cover;pointer-events:none';
        wp.appendChild(v);
        try { var pr = v.play(); if (pr && pr.catch) pr.catch(function () {}); } catch (e) {}
      }
    } else if (type === 'lottie') {
      if (isSoftwareFloor() || !(window.lottie && typeof window.lottie.loadAnimation === 'function')) {
        applyCss(posterCss); degraded = true;
      } else {
        applyCss(posterCss);
        var box = document.createElement('div');
        box.id = 'hart-wp-media'; box.setAttribute('aria-hidden', 'true');
        box.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none';
        wp.appendChild(box);
        try { wp._lottieAnim = window.lottie.loadAnimation({ container: box, renderer: 'svg', loop: true, autoplay: true, path: url }); }
        catch (e) { clearWpMedia(); applyCss(posterCss); degraded = true; }
      }
    } else {
      applyCss("center/cover no-repeat url('" + url + "')");   // unknown -> treat as image
    }
    if (window.HartSession) {
      window.HartSession.set('wallpaper_bg', { type: type, url: url, poster: poster || '' });
      window.HartSession.set('wallpaper_css', '');
    }
    return degraded;
  };

  // ── Card builders ────────────────────────────────────────────────────────────
  function activate(el, fn) {
    el.addEventListener('click', fn);
    el.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ' || e.code === 'Space') { e.preventDefault(); fn(); }
    });
  }
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

    // 1) PALETTE — brand accent duotone (instant, persisted). Cards + custom picker.
    var pg = section('Palette');
    PALETTES.forEach(function (p) {
      var c = card('hart-palette-card', 'background:linear-gradient(135deg,' + p.a + ' 0%,' + p.a2 + ' 100%)', p.name, null);
      activate(c, function () { applyPalette(p); toast('Palette', p.name); });
      pg.appendChild(c);
    });
    grid.appendChild(buildCustomPicker());

    // 1b) FEEL — live Glow (accent bloom) + Density + Blur / Opacity / Radius sliders (#170).
    section('Feel');
    grid.appendChild(buildFeelControls());

    // 1c) FONT — display/heading typeface from the server font catalogue. Sets
    // --hart-font-display live (heading/title consumer in hartResponsive.css), persists
    // via HartSession, and rides the SAME /api/appearance/apply custom-overrides path
    // (custom.font.display). Body text stays system-ui (offline-safe). Best-effort: an
    // unreachable /api/appearance/fonts leaves the section empty, never throws.
    var fg = section('Font');
    fetch(backendBase() + '/api/appearance/fonts',
      { signal: window.HartTimeoutSignal ? window.HartTimeoutSignal(5000) : null })
      .then(function (r) { return r.json(); }).then(function (d) {
        (d && d.fonts || []).forEach(function (f) {
          if (!f || !f.family) return;
          var c = card('hart-font-card',
            'background:#12131c;font-family:\'' + f.family + '\',system-ui;display:flex;align-items:center;justify-content:center;font-size:22px;color:#ECF1F4',
            'Aa · ' + f.family, null);
          activate(c, function () {
            var root = document.documentElement;
            if (root && root.style && root.style.setProperty)
              root.style.setProperty('--hart-font-display', '"' + f.family + '"');
            if (window.HartSession) window.HartSession.set('font_display', f.family);
            try {
              fetch(backendBase() + '/api/appearance/apply', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ custom: { font: { display: f.family } } }),
                signal: window.HartTimeoutSignal ? window.HartTimeoutSignal(5000) : null
              }).catch(function () {});
            } catch (e) {}
            toast('Font', f.family);
          });
          fg.appendChild(c);
        });
      }).catch(function () {});

    // 2) ORB — switchable variety, applied live + persisted (#140).
    var og = section('Orb');
    var curOrb = getOrbStyle();
    orbStyleList().forEach(function (o) {
      var prev = 'background:' + (ORB_PREVIEW[o.id] || ORB_PREVIEW.vibrant);
      var c = card('hart-orb-card', prev, o.name, null);
      if (o.id === curOrb) c.classList.add('active');
      activate(c, function () {
        setOrbStyle(o.id); toast('Orb', o.name);
        var sibs = og.querySelectorAll('.hart-orb-card');
        for (var i = 0; i < sibs.length; i++) sibs[i].classList.remove('active');
        c.classList.add('active');
      });
      og.appendChild(c);
    });

    // 3) THEME — render from the SERVER preset list (/api/appearance/presets = the ONE
    // source, incl. Aura + high-contrast), falling back to the built-in PRESETS offline
    // so the picker never empties (zero regression). Kills the drifted parallel list.
    // applyPreset now LIVE-swaps (no reload). One lookup, one source.
    var tg = section('Theme');
    function _hx(v) { v = String(v || ''); return (!v || v.charAt(0) === '#') ? v : '#' + v; }
    function themeCard(p) {
      var c = card('hart-theme-card',
        'background:linear-gradient(135deg,' + (p.b || '#0F0E17') + ',' + (p.c || p.b || '#16213e') + ')',
        p.name || p.id, p.a);
      activate(c, function () {
        if (typeof window.applyPreset === 'function')
          window.applyPreset(p.id, { set textContent(v) { toast('Theme', p.name || p.id); } });
      });
      return c;
    }
    function renderThemes(list) {
      while (tg.firstChild) tg.removeChild(tg.firstChild);
      list.forEach(function (p) { tg.appendChild(themeCard(p)); });
    }
    renderThemes(PRESETS);            // instant offline floor (the built-in fallback)
    try {
      fetch(backendBase() + '/api/appearance/presets', {
        signal: window.HartTimeoutSignal ? window.HartTimeoutSignal(4000) : null
      }).then(function (r) { return r.json(); }).then(function (data) {
        var arr = (data && data.presets) || [];
        if (!Array.isArray(arr) || !arr.length) return;   // keep the fallback
        renderThemes(arr.map(function (s) {
          return { id: s.id, name: s.name, a: _hx(s.accent), a2: _hx(s.secondary),
                   b: _hx(s.background), c: _hx(s.surface) };
        }));
      }).catch(function () {});       // offline -> keep the fallback (zero regression)
    } catch (e) {}

    // 4) WALLPAPER — built-in CSS gradients / solids.
    var wg = section('Wallpaper');
    WALLPAPERS.forEach(function (w) {
      var c = card('hart-wall-card', 'background:' + w.css, w.name + (w.live ? ' · live' : ''), null);
      activate(c, function () { window.hartSetWallpaper(w.css); toast('Wallpaper', w.name); });
      wg.appendChild(c);
    });

    // 5) BACKGROUNDS — video / lottie / gif (best-effort; degrade on the floor).
    var bg = section('Backgrounds');
    MEDIA_BGS.forEach(function (m) {
      var c = card('hart-media-card', 'background:radial-gradient(circle at 50% 45%,rgba(0,230,195,.35),transparent 60%),#0b1220', m.name + ' · ' + m.type, null);
      activate(c, function () {
        var deg = window.hartSetWallpaperMedia(m.type, m.url, m.poster);
        toast('Background', m.name + (deg ? ' (static)' : ''));
      });
      bg.appendChild(c);
    });
    grid.appendChild(buildMediaUrl());

    // 6) IMAGES — image wallpapers from the local Pictures collection. Routed
    // through the shared designed-state loader (hartStates.js): loading skeleton
    // -> grid on success, breathing "offline" card + one-click retry + silent
    // auto-recover on failure. Falls back to the original inline flow if
    // hartStates.js isn't loaded yet, so this never breaks unwired.
    var ig = section('Images');
    function renderImages(host, col) {
      var imgs = (col.wallpapers || []).slice(0, 12);
      if (!imgs.length) {
        if (typeof window.hartEmptyState === 'function') {
          host.appendChild(window.hartEmptyState({ kind: 'empty', icon: 'wallpaper',
            title: 'No image wallpapers', msg: 'Add images to your Pictures folder to see them here.' }));
        } else {
          var e = document.createElement('div'); e.className = 'ds-body-sm ds-text-muted';
          e.textContent = 'No image wallpapers found'; host.appendChild(e);
        }
        return;
      }
      imgs.forEach(function (w) {
        var url = '/api/shell/files/thumb?path=' + encodeURIComponent(w.path);
        var c = card('hart-wall-card', 'background:#1a1a1a;overflow:hidden', w.name || 'Image', null);
        var img = document.createElement('img');
        img.src = url; img.setAttribute('style', 'width:100%;height:100%;object-fit:cover');
        img.onerror = function () { img.style.display = 'none'; };
        c.querySelector('.htc-prev').appendChild(img);
        activate(c, function () { window.hartSetWallpaperImage(url, w.path); toast('Wallpaper', w.name || 'Image'); });
        host.appendChild(c);
      });
    }
    if (typeof window.hartLoadInto === 'function') {
      window.hartLoadInto(ig, '/api/shell/wallpaper/collection', renderImages, {
        title: 'Photo wallpapers unavailable',
        msg: 'Your photo wallpapers could not load yet (they come from your local Pictures). The palette, orb, themes and gradient wallpapers above work offline; photos appear when the file service is back.'
      });
    } else {
      var loading = document.createElement('div'); loading.className = 'ds-body-sm ds-text-muted';
      loading.textContent = 'Loading…'; ig.appendChild(loading);
      fetch('/api/shell/wallpaper/collection',
        { signal: window.HartTimeoutSignal ? window.HartTimeoutSignal(5000) : null })
        .then(function (r) { return r.json(); }).then(function (col) { ig.innerHTML = ''; renderImages(ig, col); })
        .catch(function () { ig.innerHTML = ''; var d = document.createElement('div');
          d.className = 'ds-body-sm ds-text-muted'; d.textContent = 'Images unavailable'; ig.appendChild(d); });
    }

    el.appendChild(grid);
  };

  // Custom colour palette: accent + secondary + background -> applyPalette.
  // Each field is a native swatch picker PAIRED with a synced hex text box, so the
  // user can pick visually OR type an exact hex (both stay in lock-step); a live
  // preview strip shows the chosen trio before applying. "As customisable as possible".
  function buildCustomPicker() {
    var wrap = document.createElement('div'); wrap.className = 'hart-custom-palette';
    var prev;
    function paint() {
      if (!prev) return;
      prev.style.background = 'linear-gradient(90deg,' + a.value + ' 0%,' + a2.value + ' 100%)';
      prev.style.borderColor = b.value;
    }
    function field(label, val) {
      var row = document.createElement('label'); row.className = 'hart-cp-field';
      var span = document.createElement('span'); span.textContent = label;
      var inp = document.createElement('input'); inp.type = 'color'; inp.value = val;
      inp.setAttribute('aria-label', label + ' colour');
      var hex = document.createElement('input'); hex.type = 'text'; hex.className = 'hart-cp-hex ds-input';
      hex.value = val; hex.maxLength = 7; hex.spellcheck = false;
      hex.setAttribute('aria-label', label + ' hex'); hex.setAttribute('style', 'width:80px');
      inp.addEventListener('input', function () { hex.value = inp.value; paint(); });
      hex.addEventListener('input', function () {
        var v = hex.value.trim(); if (v && v.charAt(0) !== '#') v = '#' + v;
        if (/^#[0-9a-fA-F]{6}$/.test(v)) { inp.value = v; paint(); }
      });
      row.appendChild(span); row.appendChild(inp); row.appendChild(hex); wrap.appendChild(row);
      return inp;
    }
    var a = field('Accent', '#00E6C3'), a2 = field('Secondary', '#9B5CFF'), b = field('Background', '#05060C');
    prev = document.createElement('div'); prev.className = 'hart-cp-preview';
    prev.setAttribute('style', 'height:26px;border-radius:8px;border:2px solid #05060C;margin:8px 0');
    paint();
    var btn = document.createElement('button'); btn.type = 'button'; btn.className = 'hart-cp-apply ds-btn ds-btn-primary ds-btn-sm';
    btn.textContent = 'Apply custom';
    btn.addEventListener('click', function () {
      applyPalette({ id: 'custom', a: a.value, a2: a2.value, b: b.value }); toast('Palette', 'Custom');
    });
    wrap.appendChild(prev); wrap.appendChild(btn);
    return wrap;
  }

  // FEEL controls (#170): live Glow (accent bloom) + Density (spacing) + Blur / Opacity
  // / Radius sliders. Each sets --hart-<key> on :root (1-frame CSS-var swap) + persists
  // via HartSession. ONE spec table (FEEL_SLIDERS) + ONE transform (applyFeelVar) is the
  // single source shared by the hub builder AND the boot restore — no parallel path.
  //   scale:true  -> raw/100 (density=1, panel-opacity=0.65)
  //   blur/radius -> a 'px' unit is appended (they feed length CSS vars)
  // Glow/Density consume the injected <style> below; Blur/Opacity/Radius consume the
  // shell's own CSS (hartResponsive.css --hart-blur/--hart-panel-opacity/--hart-radius).
  var FEEL_SLIDERS = [
    { label: 'Glow',    key: 'glow',          min: 0,  max: 100, def: 40,  scale: false },
    { label: 'Density', key: 'density',       min: 85, max: 115, def: 100, scale: true  },
    { label: 'Blur',    key: 'blur',          min: 8,  max: 40,  def: 30,  scale: false },
    { label: 'Opacity', key: 'panel-opacity', min: 30, max: 90,  def: 65,  scale: true  },
    { label: 'Radius',  key: 'radius',        min: 8,  max: 28,  def: 20,  scale: false }
  ];
  function feelHasUnit(key) { return key === 'blur' || key === 'radius'; }
  function applyFeelVar(root, key, rawValue, scale) {
    var val = scale ? (Number(rawValue) / 100) : rawValue;
    if (feelHasUnit(key)) val = String(val) + 'px';
    if (root && root.style && root.style.setProperty) root.style.setProperty('--hart-' + key, String(val));
  }
  // The glow bloom is GATED OFF on body.gpu-software/.potato so the cairo software floor
  // stays hang-free (d8c1567). Consumer set widened beyond the orb/primary CTA to the
  // tiles-on-hover, panels and agent pills so the accent bloom reads across the shell.
  var _feelStyleInjected = false;
  function ensureFeelStyle() {
    if (_feelStyleInjected || !document.head) return; _feelStyleInjected = true;
    var st = document.createElement('style'); st.id = 'hart-feel-style';
    st.textContent = [
      'body:not(.gpu-software):not(.potato) .hart-hero-orb,',
      'body:not(.gpu-software):not(.potato) .ds-btn-primary,',
      'body:not(.gpu-software):not(.potato) .hart-tile:hover,',
      'body:not(.gpu-software):not(.potato) .panel,',
      'body:not(.gpu-software):not(.potato) .agent-pill {',
      '  box-shadow: 0 0 calc(var(--hart-glow,40) * 0.5px) rgba(var(--hart-accent-rgb,0,230,195), calc(var(--hart-glow,40)/150));',
      '}',
      '.hart-gallery { gap: calc(10px * var(--hart-density,1)); }',
      '.hart-personalize .ds-section-label { margin-top: calc(14px * var(--hart-density,1)); }',
      // G8: the density slider now scales the DESKTOP home row spacing too (was
      // panel-only, so the slider was nearly invisible). At density=1 this is the base
      // 18px -> pixel-identical (zero regression); the injected <style> is later in the
      // <head> than hartHome.css, so it overrides at equal specificity. Same var, no fork.
      '.hh-rows { gap: calc(18px * var(--hart-density,1)); }',
      // G7: the on-desktop MOOD DOCK swatches (hartRenderMoodDock). CSS co-located with
      // the personalize feel-style it belongs to; injected at boot via restore() (G9),
      // so the dock renders styled wherever it is mounted (an A2UI-mountable utility).
      '.hart-mood-label { font-size: 10px; font-weight: 600; letter-spacing: .12em; color: var(--hart-muted,#8a90a0); margin-right: 4px; align-self: center; }',
      '.hart-mood-sw { display: inline-block; width: 22px; height: 22px; border-radius: 50%; cursor: pointer; border: 1.5px solid rgba(255,255,255,.14); box-shadow: 0 1px 4px rgba(0,0,0,.35); transition: transform .12s ease, box-shadow .12s ease; }',
      '.hart-mood-sw:hover, .hart-mood-sw:focus { transform: scale(1.12); box-shadow: 0 0 0 2px rgba(var(--hart-accent-rgb,0,230,195),.5); outline: none; }'
    ].join('\n');
    document.head.appendChild(st);
  }
  function buildFeelControls() {
    ensureFeelStyle();
    var root = document.documentElement;
    var wrap = document.createElement('div'); wrap.className = 'hart-feel';
    var S = window.HartSession;
    FEEL_SLIDERS.forEach(function (fs) {
      var row = document.createElement('label'); row.className = 'hart-cp-field';
      var span = document.createElement('span'); span.textContent = fs.label;
      var inp = document.createElement('input'); inp.type = 'range';
      inp.min = String(fs.min); inp.max = String(fs.max);
      var saved = S ? S.get(fs.key) : null;
      var hasSaved = (saved !== null && saved !== undefined && saved !== '');
      inp.value = String(hasSaved ? saved : fs.def);
      inp.setAttribute('aria-label', fs.label);
      inp.addEventListener('input', function () {
        applyFeelVar(root, fs.key, inp.value, fs.scale);
        if (S) S.set(fs.key, inp.value);
      });
      // Reflect a persisted choice live; DON'T write the default (an untouched control
      // stays pixel-identical to the theme_service-emitted default — regression-safe).
      if (hasSaved) applyFeelVar(root, fs.key, saved, fs.scale);
      row.appendChild(span); row.appendChild(inp); wrap.appendChild(row);
    });
    return wrap;
  }

  // Add a video / gif / lottie background by URL (offline-first: any local path or
  // remote URL). Same degrade contract as the bundled media cards.
  function buildMediaUrl() {
    var wrap = document.createElement('div'); wrap.className = 'hart-media-url';
    var sel = document.createElement('select'); sel.className = 'ds-select';
    ['video', 'gif', 'lottie'].forEach(function (t) {
      var o = document.createElement('option'); o.value = t; o.textContent = t; sel.appendChild(o);
    });
    var inp = document.createElement('input'); inp.type = 'text'; inp.className = 'ds-input';
    inp.placeholder = 'Video / GIF / Lottie URL or path'; inp.setAttribute('aria-label', 'Background media URL');
    var btn = document.createElement('button'); btn.type = 'button'; btn.className = 'ds-btn ds-btn-primary ds-btn-sm';
    btn.textContent = 'Set';
    btn.addEventListener('click', function () {
      var u = (inp.value || '').trim(); if (!u) return;
      var deg = window.hartSetWallpaperMedia(sel.value, u);
      toast('Background', sel.value + (deg ? ' (static)' : ''));
    });
    wrap.appendChild(sel); wrap.appendChild(inp); wrap.appendChild(btn);
    return wrap;
  }

  // ── Restore persisted choices on boot ───────────────────────────────────────
  function restore() {
    if (!window.HartSession) { return setTimeout(restore, 300); }
    window.HartSession.ready(function () {
      // Palette (only if the user explicitly picked one; else the CSS defaults —
      // Vibrant teal accent + violet a2 — stand as the brand default).
      var p = window.HartSession.get('palette');
      if (p && (p.a || p.a2 || p.b)) paintPalette(p);

      // Wallpaper: a media background wins; else the legacy single-css key.
      var mbg = window.HartSession.get('wallpaper_bg');
      if (mbg && mbg.type && mbg.url) { window.hartSetWallpaperMedia(mbg.type, mbg.url, mbg.poster); }
      else { var css = window.HartSession.get('wallpaper_css'); if (css) applyCss(css); }

      // Orb variety — apply once the canvas instance exists (init may race us).
      var tries = 0;
      (function applyOrb() {
        if (window._hartVoiceOrb && window._hartVoiceOrb.setStyle) { applyOrbToCanvas(getOrbStyle()); return; }
        if (tries++ < 40) setTimeout(applyOrb, 300);
      })();

      // Feel sliders + display font: re-apply the persisted choice on boot so it
      // survives reload even before the Personalize hub is opened. Only EXPLICIT picks
      // are re-applied (unset -> the theme_service default stands, pixel-identical).
      // Same spec + transform the hub uses (FEEL_SLIDERS / applyFeelVar) — one path.
      var froot = document.documentElement;
      FEEL_SLIDERS.forEach(function (fs) {
        var sv = window.HartSession.get(fs.key);
        if (sv !== null && sv !== undefined && sv !== '') applyFeelVar(froot, fs.key, sv, fs.scale);
      });
      var fd = window.HartSession.get('font_display');
      if (fd && froot.style && froot.style.setProperty) froot.style.setProperty('--hart-font-display', '"' + fd + '"');

      // G9: inject the feel <style> at BOOT (idempotent) so the persisted --hart-glow
      // bloom (and density) apply immediately -- before, it was injected only when the
      // Personalize hub was first opened, so a saved glow produced no bloom until then.
      ensureFeelStyle();
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', restore);
  else restore();
})();
