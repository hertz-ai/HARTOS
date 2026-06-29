/*
 * hartBrandArt.js - the ONE source of the HART brand-spectrum art language.
 *
 * Both the assembled HOME (hartHome.js, card art) and the desktop icon layer
 * (hartDesktop.js, icon art tiles) paint "a brand-spectrum hue darkened toward
 * ink" gradients and render glyphs (Material ligature vs emoji). Those were two
 * drifting copies (different ink, hue order, second-hue pick, darkening factor,
 * and two isMaterialName / glyph renderers). This module collapses them to one
 * canonical helper so desktop icons and home cards read as the SAME spectrum.
 *
 * Public API (window.HartBrandArt):
 *   .spectrum        ['teal','cyan','blue','violet','magenta','amber']
 *   .spectrumHex     { teal:'#00E6C3', ... }   the 6 brand hues
 *   .ink             [9,13,22]                  the deep ink tiles darken toward
 *   .hexAt(i)        spectrum hex by index (wraps)
 *   .isMaterialName(g)  true if g looks like a Material ligature name
 *   .esc(s)          HTML-escape a string
 *   .gradient(baseHex, seed)  cinematic DARK brand gradient (the art tile).
 *                    With an explicit baseHex it darkens that single hue; with
 *                    none it folds in a neighbour hue (seed-stable iridescence).
 *   .glyphTint(seed) a bright on-brand glyph colour for a seed (de-monochrome
 *                    glyph fill when no explicit colour is set).
 *   .glyphHTML(icon, color)  the glyph span markup. Material name -> the icon
 *                    font; anything else (emoji) -> a plain .di-emoji span (the
 *                    Material font would mangle an emoji), with the glyph text
 *                    embedded (escaped) and an optional inline colour.
 *
 * Plain classic script (old cage WebKit): no template literals, no optional
 * chaining / nullish coalescing, no arrow funcs - matches the rest of the
 * shell static JS. Loaded BEFORE hartHome.js / hartDesktop.js.
 */
(function () {
  'use strict';

  var SPECTRUM = ['teal', 'cyan', 'blue', 'violet', 'magenta', 'amber'];
  var SPECTRUM_HEX = {
    teal: '#00E6C3', cyan: '#29C5FF', blue: '#3B82F6',
    violet: '#9B5CFF', magenta: '#FF2E9A', amber: '#FFC83D'
  };
  // The canonical deep ink the tiles darken toward (keeps glyph/text legible).
  var INK = [9, 13, 22];

  function mod(n, m) { return ((n % m) + m) % m; }
  function hexAt(i) { return SPECTRUM_HEX[SPECTRUM[mod(i | 0, SPECTRUM.length)]]; }

  function rgbOf(hex) {
    var m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || '');
    return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : null;
  }
  function blend(a, b, t) {
    return [Math.round(a[0] + (b[0] - a[0]) * t),
            Math.round(a[1] + (b[1] - a[1]) * t),
            Math.round(a[2] + (b[2] - a[2]) * t)];
  }
  function rgbCss(c) { return 'rgb(' + c[0] + ',' + c[1] + ',' + c[2] + ')'; }
  // Tile base hue: an explicit per-app/user colour wins; else a deterministic
  // pick from the brand spectrum so a row/grid spans the full palette.
  function baseRgb(hex, seed) { return rgbOf(hex) || rgbOf(hexAt(seed)); }

  function isMaterialName(g) { return /^[a-z0-9_]+$/.test(g || ''); }

  function esc(s) {
    if (s == null) return '';
    try {
      var d = document.createElement('div');
      d.textContent = String(s);
      return d.innerHTML;
    } catch (e) {
      return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }
  }

  // The cinematic DARK brand gradient (the art tile). One source for home cards
  // AND desktop icons. With an explicit colour it darkens that single hue; with
  // none it folds in a faint neighbour hue for a subtle orb-like iridescence.
  function gradient(baseHex, seed) {
    seed = seed | 0;
    var base = baseRgb(baseHex, seed);
    var second = baseHex ? base : (rgbOf(hexAt(seed + 2)) || base);
    var dark = blend(base, INK, 0.72);     // primary (darker) stop
    var light = blend(second, INK, 0.52);  // neighbour (lighter) stop
    var ang = [135, 150, 165][mod(seed, 3)];
    return 'linear-gradient(' + ang + 'deg,' + rgbCss(light) + ',' + rgbCss(dark) + ')';
  }

  // A bright on-brand glyph fill for a seed (the de-monochrome default colour).
  function glyphTint(seed) {
    return rgbCss(blend(baseRgb('', seed | 0), [255, 255, 255], 0.6));
  }

  // The glyph span markup. A Material ligature name rides the icon font; an
  // emoji rides a plain .di-emoji span (the Material font would mangle it). The
  // glyph text is embedded (escaped); color (optional) is applied inline.
  function glyphHTML(icon, color) {
    var g = icon || 'apps';
    var style = color ? ' style="color:' + color + '"' : '';
    if (isMaterialName(g)) {
      return '<span class="mi material-icons-round"' + style + ' aria-hidden="true">' + esc(g) + '</span>';
    }
    return '<span class="mi di-emoji"' + style + ' aria-hidden="true">' + esc(g) + '</span>';
  }

  window.HartBrandArt = {
    spectrum: SPECTRUM,
    spectrumHex: SPECTRUM_HEX,
    ink: INK,
    hexAt: hexAt,
    isMaterialName: isMaterialName,
    esc: esc,
    gradient: gradient,
    glyphTint: glyphTint,
    glyphHTML: glyphHTML
  };
})();
