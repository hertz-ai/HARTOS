/*
 * hartHome.js - HART OS assembled Netflix HOME (W1).
 *
 * The value-first cinematic home surface. It is COMPOSED by the local LLM via
 * the EXISTING A2UI / agent_ui_update transport: hartHome renders from a
 * composition PAYLOAD (hero + rows + cards as data), never a hardcoded page.
 * The agent decides which rows, what content, which formats; this module is the
 * renderer + the sensible offline fallback (so the desktop is instant and never
 * blank while the brain composes).
 *
 * Transport (no parallel path - reuses the wired channels):
 *   - window.HartHome.compose(payload)  <- the SSE overlay consumer routes a
 *       'home'/'home_compose' A2UI component here (the orchestrator adds that
 *       one branch in renderAgentOverlay; until then refresh() drives it).
 *   - HartHome.refresh()  <- pulls the existing data endpoints (wallet/earnings,
 *       dashboard/agents, recipes, social) and paints; each source falls back
 *       gracefully, so a 401 / offline box still shows a coherent home.
 *
 * Data sources (all best-effort, all degrade to sample data):
 *   earnings  -> BACKEND /api/social/resonance/wallet            (Spark balance)
 *   agents    -> BACKEND /api/social/dashboard/agents            (Continue + Your agents)
 *   recipes   -> BACKEND /api/recipes | /api/social/recipes      (Recipes row)
 *   hive      -> BACKEND /api/social/feed                        (From the hive row)
 *
 * PERF (#137): paints sample INSTANTLY (offline-safe, no network on the hot
 * path), then upgrades sections as fetches resolve. Heavy blur / hover-scale /
 * breathing live behind body.gpu-hardware in hartHome.css; this JS adds NO
 * continuous timers and lazy-loads every image.
 *
 * Plain classic script (old cage WebKit): no template literals, no optional
 * chaining / nullish coalescing. Loaded after the inline shell JS (BACKEND /
 * SHELL / MANIFEST / openPanel / acSend already defined).
 */
(function () {
  'use strict';

  // ── Brand spectrum + art language (full range; the home is never green-only) ──
  // SINGLE source: window.HartBrandArt (hartBrandArt.js, loaded first). The home
  // card art, the row accent palette and the glyph rendering all read from it so
  // home cards and desktop icons share ONE spectrum (no parallel palette/math).
  function BA() { return window.HartBrandArt; }

  function backend() {
    return (typeof window.BACKEND === 'string' && window.BACKEND) ? window.BACKEND : '';
  }
  function shell() {
    return (typeof window.SHELL === 'string' && window.SHELL) ? window.SHELL : '';
  }
  // Reuse the shell's own abort-signal helper when present (short timeouts so a
  // hung endpoint never stalls the home).
  function sig(ms) {
    try { if (typeof window._sig === 'function') return window._sig(ms); } catch (e) { console.debug('hartHome: window._sig probe failed', e); }
    try {
      if (typeof AbortController === 'function') {
        var ac = new AbortController();
        setTimeout(function () { try { ac.abort(); } catch (e2) { console.debug('hartHome: abort on timeout failed', e2); } }, ms || 4000);
        return ac.signal;
      }
    } catch (e3) { console.debug('hartHome: AbortController unavailable', e3); }
    return undefined;
  }
  function getJSON(url, ms) {
    var opt = {};
    var s = sig(ms || 4000);
    if (s) opt.signal = s;
    return fetch(url, opt).then(function (r) {
      if (!r.ok) throw new Error('http ' + r.status);
      return r.json();
    });
  }
  function esc(s) {
    if (s == null) return '';
    var d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
  }

  // ───────────────────────────────────────────────────────────────────────
  // SAMPLE / FALLBACK PAYLOAD
  // The offline-safe default the home paints instantly. The agent's compose()
  // payload replaces this; refresh() upgrades individual rows from live data.
  // Shape == the composition contract (hero + rows[] + cards[]).
  // ───────────────────────────────────────────────────────────────────────
  function samplePayload() {
    return {
      hero: {
        eyebrow: 'Earned on the hive',
        // ZERO, never a fabricated figure (2026-07-24). This is the FALLBACK the
        // home paints before/without live data, and fetchEarnings KEEPS it on
        // 401/offline -- so a fabricated number here is what a fresh or offline box
        // actually shows the user (the real-HW "2,140 Spark / 3 agents / 41 tasks"
        // on a just-installed machine). A number is still used so the hero's
        // count-up animation works; it just counts up from and to a truthful 0.
        amount: 0,
        amount_unit: 'Spark',
        agents: 0,
        tasks: 0,
        local: true,
        // Honest state (d1.1): no payout rail is wired yet, so the real Spark is
        // the figure and the money is marked pending. This sample is only the
        // OFFLINE skeleton; refresh() overwrites it with the real ledger.
        payout_pending: true,
        // No invented settlement series -- the sparkline stays empty until real
        // settlement rows arrive (fetchEarnings fills it).
        spark_series: [],
        primary: { label: 'Resume', action: 'resume', target: 'recipes' },
        secondary: { label: 'Ask anything', action: 'ask' }
      },
      rows: [
        {
          // Continue = the user's OWN in-progress work. Empty until fetchAgents
          // finds live agents; render() paints the honest "Nothing here yet" card
          // for an empty row. The old six invented half-done tasks ("Trip to Goa
          // 30%", "Invoice chaser 80%", "Fix STT streaming 45%"...) persisted on a
          // fresh box because fetchAgents returns early when there are no agents,
          // so a brand-new machine showed someone else's fake history.
          title: 'Continue', accent: 'teal', see_all: 'agents_browse',
          cards: []
        },
        // FLAGSHIP agents row - the REAL HART OS product agents, always featured
        // (flagship:true keeps refresh() from replacing it with live dashboard
        // rows). Each card dispatches through the existing hero command bar.
        {
          title: 'Flagship agents', note: 'ready to run, fully local',
          accent: 'violet', see_all: 'agents_browse', flagship: true,
          cards: [
            // image = the BUNDLED no-network brand poster (the offline default).
            // The continuous network art (app_poster.agent_art_url -> card.image_url)
            // layers ON TOP only when no static image is set, so a fresh offline
            // boot already shows rich agent art (#143/d8 + GF4).
            { title: 'Auto Research', icon: 'travel_explore', meta: 'scout the web, then synthesize',
              image: '/shell/static/app_art/agent-auto-research.svg',
              action: 'ask', prompt: 'Start the Auto Research agent on a topic I care about' },
            { title: 'Trading', icon: 'candlestick_chart', meta: 'paper-trade live signals',
              image: '/shell/static/app_art/agent-trading.svg',
              action: 'ask', prompt: 'Open the Trading agent' },
            { title: 'Tutor', icon: 'school', meta: 'learn anything, step by step',
              image: '/shell/static/app_art/agent-tutor.svg',
              action: 'ask', prompt: 'Be my Tutor' },
            { title: 'English Learning', icon: 'menu_book', meta: 'grammar and vocabulary',
              image: '/shell/static/app_art/agent-english-learning.svg',
              action: 'ask', prompt: 'Start English Learning' },
            { title: 'Spoken English', icon: 'record_voice_over', meta: 'practice speaking out loud',
              image: '/shell/static/app_art/agent-spoken-english.svg',
              action: 'ask', prompt: 'Practice Spoken English with me' },
            { title: 'Speech Therapy', icon: 'spatial_audio', meta: 'guided exercises',
              image: '/shell/static/app_art/agent-speech-therapy.svg',
              action: 'ask', prompt: 'Start a Speech Therapy session' }
          ]
        },
        {
          // "from the network" -- so it must come FROM the network. There is no
          // fetch that ever populates this row (grep-verified), so its five entries
          // (Invoice Chaser / Resume Builder / Sheet Wizard / Trip Planner / Deal
          // Hunter) were permanent fiction presented as live hive activity. Empty
          // until a real feed exists; render() shows the honest empty card, and the
          // agent's compose() can still fill it over A2UI.
          title: 'Top agents in the hive today', note: 'from the network',
          accent: 'magenta', see_all: 'communities', ranked: true,
          cards: []
        }
      ]
    };
  }

  // ───────────────────────────────────────────────────────────────────────
  // CARD + ROW RENDERERS
  // ───────────────────────────────────────────────────────────────────────

  // The card ART gradient when no photo is supplied (offline-safe). Seeded per
  // card so a row reads as varied, not flat. Resolves the row accent to its hex
  // then delegates to the ONE brand-art gradient (HartBrandArt.gradient) shared
  // with the desktop icons - no parallel palette/math here.
  function gradientArt(accent, seed) {
    var ba = BA();
    return ba.gradient(ba.spectrumHex[accent], seed);
  }

  function makeCard(card, accent, seed, ranked) {
    var el = document.createElement('div');
    el.className = 'hh-card' + (card.format ? ' hh-' + esc(card.format) : '') + (ranked ? ' hh-ranked' : '');
    el.setAttribute('role', 'button');
    el.setAttribute('tabindex', '0');
    el.setAttribute('aria-label', card.title || 'card');

    // Empty-state card (graceful row fallback).
    if (card.empty) {
      el.className = 'hh-card hh-card-empty';
      el.removeAttribute('role');
      el.removeAttribute('tabindex');
      el.textContent = card.title || 'Nothing here yet';
      return el;
    }

    var artWrap = ranked ? document.createElement('div') : el;
    if (ranked) {
      // Numbered leaderboard card: big stroked number + an inner art tile.
      var num = document.createElement('div');
      num.className = 'hh-rank-num';
      num.textContent = String((seed || 0) + 1);
      el.appendChild(num);
      artWrap.className = 'hh-rank-inner';
      el.appendChild(artWrap);
    }

    var art = document.createElement('div');
    art.className = 'hh-card-art';
    // The card photo, in priority order:
    //   card.image     - a same-origin / data URL the producer already resolved
    //   card.image_url - a remote web/news photo, routed through the same-origin
    //                    fetch-once ImageCache (/api/media/image) so it is cached
    //                    and never a mixed-content/CSP miss in the old cage WebKit
    // The gradient ALWAYS paints first (no empty flash); the photo fades in over
    // it. When no photo is supplied the local media index is asked for one below.
    var imgSrc = card.image ? card.image
      : (card.image_url ? webImageURL(card.image_url) : '');
    var hasImage = !!imgSrc;
    art.style.background = gradientArt(accent, seed);
    if (hasImage) { attachLazyImage(art, imgSrc); }
    artWrap.appendChild(art);

    var scrim = document.createElement('div');
    scrim.className = 'hh-card-scrim';
    artWrap.appendChild(scrim);

    var glyphEl = null;
    if (card.icon && !hasImage) {
      glyphEl = document.createElement('div');
      glyphEl.className = 'hh-card-ic';
      glyphEl.innerHTML = BA().glyphHTML(card.icon);   // shared brand-art glyph renderer
      artWrap.appendChild(glyphEl);
    }
    // No producer photo: hydrate one from the LOCAL semantic media index, keyed by
    // the card's topic/title (cached, lazy, gradient-fallback on a miss). This is
    // what makes every card load a real photo once the idle indexer has captioned
    // the user's library - fully local, never egress.
    if (!hasImage) { hydrateCardImage(card, art, glyphEl); }
    if (card.live) {
      var live = document.createElement('div');
      live.className = 'hh-card-live';
      live.innerHTML = '<span class="hh-dot"></span>' + esc(card.live);
      artWrap.appendChild(live);
    } else if (card.badge) {
      var badge = document.createElement('div');
      badge.className = 'hh-card-badge';
      badge.textContent = card.badge;
      artWrap.appendChild(badge);
    }

    var body = document.createElement('div');
    body.className = 'hh-card-body';
    var t = document.createElement('div');
    t.className = 'hh-card-title';
    t.textContent = card.title || '';
    body.appendChild(t);
    if (card.meta) {
      var meta = document.createElement('div');
      meta.className = 'hh-card-meta';
      meta.textContent = card.meta;
      body.appendChild(meta);
    }
    artWrap.appendChild(body);

    if (typeof card.progress === 'number' && card.progress >= 0) {
      var prog = document.createElement('div');
      prog.className = 'hh-card-prog';
      prog.style.width = Math.max(0, Math.min(1, card.progress)) * 100 + '%';
      artWrap.appendChild(prog);
    }

    function activate() { cardAction(card); }
    el.addEventListener('click', activate);
    el.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); }
    });
    return el;
  }

  function makeRow(row, idx) {
    var spec = BA().spectrum;
    var accent = row.accent || spec[idx % spec.length];
    var rowEl = document.createElement('div');
    rowEl.className = 'hh-row hh-accent-' + accent;

    var head = document.createElement('div');
    head.className = 'hh-row-head';
    var title = document.createElement('div');
    title.className = 'hh-row-title';
    title.textContent = row.title || '';
    head.appendChild(title);
    if (row.note) {
      var note = document.createElement('div');
      note.className = 'hh-row-note';
      note.textContent = row.note;
      head.appendChild(note);
    }
    if (row.see_all) {
      var see = document.createElement('button');
      see.className = 'hh-see-all';
      see.type = 'button';
      see.textContent = 'See all';
      see.addEventListener('click', function () { openTarget(row.see_all); });
      head.appendChild(see);
    }
    rowEl.appendChild(head);

    var cards = document.createElement('div');
    cards.className = 'hh-cards';
    var list = (row.cards && row.cards.length) ? row.cards
      : [{ empty: true, title: 'Nothing here yet' }];
    list.forEach(function (c, i) {
      cards.appendChild(makeCard(c, accent, i, !!row.ranked));
    });
    rowEl.appendChild(cards);
    return rowEl;
  }

  // ── Lazy image loading (IntersectionObserver, single shared observer) ──
  var _io = null;
  function _ensureIO() {
    if (_io || typeof IntersectionObserver !== 'function') return _io;
    _io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        var img = en.target;
        var src = img.getAttribute('data-src');
        if (src) { img.src = src; img.removeAttribute('data-src'); }
        _io.unobserve(img);
      });
    }, { rootMargin: '200px' });
    return _io;
  }
  function _lazyObserve(img) {
    var io = _ensureIO();
    if (io) { io.observe(img); return; }
    var src = img.getAttribute('data-src'); // no IO: load immediately
    if (src) { img.src = src; img.removeAttribute('data-src'); }
  }

  // ── Card photo hydration (real photos via the EXISTING media index) ──
  // attachLazyImage builds the ONE lazy <img> the card art uses, shared by the
  // producer-supplied photo path (card.image / card.image_url) and the local
  // semantic-search hydration below - no parallel <img> construction. The
  // _hhImaged guard makes a second call a no-op (gradient already replaced).
  function attachLazyImage(art, src) {
    if (!src || art._hhImaged) return;
    art._hhImaged = true;
    var img = document.createElement('img');
    img.alt = '';
    img.setAttribute('loading', 'lazy');
    img.setAttribute('data-src', src);
    img.addEventListener('load', function () { img.classList.add('hh-loaded'); });
    art.appendChild(img);
    _lazyObserve(img);
  }

  // Route a remote web/news image through the same-origin fetch-once ImageCache
  // (/api/media/image). Same-origin keeps the old cage WebKit happy (no mixed
  // content / CSP) and the bytes are cached + LRU-bounded server-side (<1ms
  // re-serve). An already-relative / data URL is returned untouched.
  function webImageURL(url) {
    var u = String(url || '');
    if (u.indexOf('http://') === 0 || u.indexOf('https://') === 0) {
      return shell() + '/api/media/image?url=' + encodeURIComponent(u);
    }
    return u;
  }

  // Local-search hydration: at most ONE query per topic, memoised (a miss caches
  // null so a re-render never re-queries). A hit serves the local file as a
  // small, disk-cached PNG thumbnail through the EXISTING shell file route -
  // never a new fetch path, never personal bytes off the device.
  var _imgSearchCache = {};          // topic -> src | null (miss)
  function thumbURL(path) {
    return shell() + '/api/shell/files/thumbnail?path=' +
      encodeURIComponent(path) + '&size=512';
  }
  function pickLocalImage(data) {
    var results = (data && data.results) || [];
    for (var i = 0; i < results.length; i++) {
      var r = results[i];
      // Skip videos (the thumbnail route serves images only) and pathless hits.
      if (r && r.path && r.kind !== 'video') return thumbURL(r.path);
    }
    return null;
  }
  function applyHydratedImage(art, glyphEl, src) {
    if (!src) return;
    attachLazyImage(art, src);
    // The photo carries the card now; drop the placeholder glyph (the card.image
    // branch never draws a glyph over a photo, so match that).
    if (glyphEl && glyphEl.parentNode) {
      try { glyphEl.parentNode.removeChild(glyphEl); } catch (e) { console.debug('hartHome: placeholder glyph removeChild failed', e); }
    }
  }
  function hydrateCardImage(card, art, glyphEl) {
    var topic = String((card && (card.topic || card.title)) || '').trim();
    if (!topic || card.empty) return;
    if (Object.prototype.hasOwnProperty.call(_imgSearchCache, topic)) {
      applyHydratedImage(art, glyphEl, _imgSearchCache[topic]);
      return;
    }
    var url = shell() + '/api/media/search?q=' + encodeURIComponent(topic) + '&limit=3';
    getJSON(url, 3500).then(function (d) {
      var src = pickLocalImage(d);
      _imgSearchCache[topic] = src;          // null on a miss -> never re-query
      applyHydratedImage(art, glyphEl, src);
    }).catch(function (e) { console.debug('hartHome: card image search failed', e); _imgSearchCache[topic] = null; });
  }

  // ───────────────────────────────────────────────────────────────────────
  // ACTIONS - every card/CTA routes through an EXISTING shell global. No fork.
  // ───────────────────────────────────────────────────────────────────────
  function openTarget(target) {
    if (target && typeof window.openPanel === 'function') window.openPanel(target);
  }
  function ask(prefill) {
    // Reuse the hero command bar (one dispatch path). Open the orb/bar focus.
    var input = document.getElementById('hart-hero-input');
    if (input) { input.focus(); if (prefill) input.value = prefill; }
    if (!input && typeof window.toggleAssistantChat === 'function') window.toggleAssistantChat();
  }
  function cardAction(card) {
    var a = card.action || 'open';
    if (a === 'ask') { ask(card.prompt || ''); return; }
    if (a === 'resume' || a === 'open') {
      if (card.target) { openTarget(card.target); return; }
      // Resume with no explicit panel: hand the title to the agent to continue.
      if (a === 'resume') { ask('Resume ' + (card.title || 'my last task')); return; }
      openTarget('agents_browse');
      return;
    }
    if (card.target) openTarget(card.target);
  }
  function heroBtnAction(btn) {
    if (!btn) return;
    if (btn.action === 'ask') { ask(''); return; }
    if (btn.action === 'resume') {
      if (btn.target) { openTarget(btn.target); return; }
      ask('Resume my last task');
      return;
    }
    if (btn.target) openTarget(btn.target);
  }

  // ───────────────────────────────────────────────────────────────────────
  // HERO - VISUAL-FIRST (rule b8): the earnings are a NUMBER THAT ANIMATES UP
  // plus a small sparkline data-viz; the orb SPEAKS the narrative (narrateOnce)
  // instead of printing a paragraph. Minimal text, honest money state (d1.1).
  // ───────────────────────────────────────────────────────────────────────

  // Count a number up from 0 with an ease-out, respecting reduced motion. The
  // unit ("Spark") is a sibling element so only the figure animates. _animatedAmt
  // guards a double-bounce: when a re-render carries the SAME value (e.g. the
  // offline sample paints twice), the second paint sets it instantly instead of
  // re-animating; a genuinely new value (sample -> real earnings) still animates.
  var _animatedAmt = null;
  function countUp(el, target) {
    target = Number(target) || 0;
    var reduce = false;
    try {
      reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    } catch (e) { console.debug('hartHome: matchMedia reduced-motion probe failed', e); }
    if (reduce || typeof window.requestAnimationFrame !== 'function' || target <= 0 ||
        target === _animatedAmt) {
      el.textContent = _fmt(target);
      _animatedAmt = target;
      return;
    }
    _animatedAmt = target;
    var dur = 1100, start = null;
    function frame(ts) {
      if (start == null) start = ts;
      var p = Math.min(1, (ts - start) / dur);
      var e = 1 - Math.pow(1 - p, 3);            // easeOutCubic
      el.textContent = _fmt(Math.round(target * e));
      if (p < 1) requestAnimationFrame(frame);
      else el.textContent = _fmt(target);
    }
    requestAnimationFrame(frame);
  }

  // A small brand-gradient sparkline (the earnings data-viz). Pure SVG string,
  // old-WebKit-safe (string concat, no template literals).
  function sparklineSVG(series) {
    var n = series ? series.length : 0;
    if (n < 2) return '';
    var w = 232, h = 44, max = Math.max.apply(null, series), min = Math.min.apply(null, series);
    var span = (max - min) || 1, i, x, y, pts = [];
    for (i = 0; i < n; i++) {
      x = (i / (n - 1)) * w;
      y = h - ((series[i] - min) / span) * (h - 8) - 4;
      pts.push(x.toFixed(1) + ',' + y.toFixed(1));
    }
    var d = pts.join(' ');
    var area = '0,' + h + ' ' + d + ' ' + w.toFixed(1) + ',' + h;
    var hex = BA().spectrumHex;     // shared brand spectrum (teal -> cyan -> blue)
    return '' +
      '<svg viewBox="0 0 ' + w + ' ' + h + '" width="' + w + '" height="' + h + '" preserveAspectRatio="none" aria-hidden="true">' +
      '<defs><linearGradient id="hhSparkG" x1="0" y1="0" x2="1" y2="0">' +
      '<stop offset="0" stop-color="' + hex.teal + '"/>' +
      '<stop offset="0.55" stop-color="' + hex.cyan + '"/>' +
      '<stop offset="1" stop-color="' + hex.blue + '"/>' +
      '</linearGradient></defs>' +
      '<polygon points="' + area + '" fill="rgba(0,230,195,0.12)"/>' +
      '<polyline points="' + d + '" fill="none" stroke="url(#hhSparkG)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>' +
      '</svg>';
  }

  function buildHero(hero) {
    hero = hero || {};
    var wrap = document.createElement('div');
    wrap.className = 'hh-hero';

    var eb = document.createElement('div');
    eb.className = 'hh-eyebrow';
    eb.textContent = hero.eyebrow || 'Earned on the hive';
    wrap.appendChild(eb);

    // THE BIG NUMBER. A numeric amount animates up (the money moment); a string
    // amount (agent-composed narrative) renders as-is for back-compat.
    var amountRow = document.createElement('div');
    amountRow.className = 'hh-amount-row';
    var amt = document.createElement('span');
    amt.className = 'hh-amount';
    if (typeof hero.amount === 'number') {
      amountRow.appendChild(amt);
      var unit = document.createElement('span');
      unit.className = 'hh-amount-unit';
      unit.textContent = hero.amount_unit || 'Spark';
      amountRow.appendChild(unit);
      countUp(amt, hero.amount);
    } else {
      amt.textContent = hero.amount != null ? String(hero.amount) : '0 Spark';
      amountRow.appendChild(amt);
    }
    wrap.appendChild(amountRow);

    // Small data-viz: the settlement sparkline (real rows feed it).
    if (hero.spark_series && hero.spark_series.length > 1) {
      var sv = sparklineSVG(hero.spark_series);
      if (sv) {
        var svWrap = document.createElement('div');
        svWrap.className = 'hh-spark';
        svWrap.innerHTML = sv;
        wrap.appendChild(svWrap);
      }
    }

    // Honest meta strip (minimal text, NOT a paragraph): payout-pending pill,
    // the canonical rate equivalent, and a compact agents/tasks stat.
    var meta = document.createElement('div');
    meta.className = 'hh-hero-meta';
    if (hero.payout_pending) {
      var pill = document.createElement('span');
      pill.className = 'hh-pill';
      pill.innerHTML = '<span class="hh-pill-dot"></span>Payout pending';
      meta.appendChild(pill);
    }
    if (typeof hero.usd_equiv === 'number' && hero.usd_equiv > 0) {
      var usd = document.createElement('span');
      usd.className = 'hh-usd';
      usd.textContent = '~ $' + hero.usd_equiv.toFixed(2) + ' at the hive rate';
      meta.appendChild(usd);
    }
    var agents = (typeof hero.agents === 'number') ? hero.agents : 0;
    var tasks = (typeof hero.tasks === 'number') ? hero.tasks : 0;
    if (agents || tasks) {
      var stat = document.createElement('span');
      stat.className = 'hh-stat';
      stat.innerHTML = '<b>' + agents + '</b> ' + (agents === 1 ? 'agent' : 'agents') +
        ' · <b>' + tasks + '</b> ' + (tasks === 1 ? 'task' : 'tasks') +
        (hero.local ? ' · <span class="hh-local-mini"><span class="hh-shield"></span>fully local</span>' : '');
      meta.appendChild(stat);
    }
    wrap.appendChild(meta);

    var cta = document.createElement('div');
    cta.className = 'hh-cta';
    var p = hero.primary || { label: 'Resume', action: 'resume' };
    var pb = document.createElement('button');
    pb.className = 'hh-btn hh-btn-primary';
    pb.type = 'button';
    pb.innerHTML = '<span class="mi material-icons-round" aria-hidden="true">play_arrow</span>' + esc(p.label || 'Resume');
    pb.addEventListener('click', function () { heroBtnAction(p); });
    cta.appendChild(pb);
    var s = hero.secondary || { label: 'Ask anything', action: 'ask' };
    var sb = document.createElement('button');
    sb.className = 'hh-btn';
    sb.type = 'button';
    sb.innerHTML = '<span class="mi material-icons-round" aria-hidden="true">add</span>' + esc(s.label || 'Ask anything');
    sb.addEventListener('click', function () { heroBtnAction(s); });
    cta.appendChild(sb);
    wrap.appendChild(cta);

    return wrap;
  }

  // ───────────────────────────────────────────────────────────────────────
  // RENDER - the single paint. Idempotent: rebuilds #hart-home from a payload.
  // ───────────────────────────────────────────────────────────────────────
  var _root = null;
  var _payload = null;

  function mountRoot() {
    _root = document.getElementById('hart-home');
    if (_root) return _root;
    _root = document.createElement('div');
    _root.id = 'hart-home';
    _root.className = 'hart-home';
    _root.setAttribute('aria-label', 'HART home');
    var wp = document.querySelector('.wallpaper');
    if (wp && wp.parentNode) wp.parentNode.insertBefore(_root, wp.nextSibling);
    else document.body.appendChild(_root);
    return _root;
  }

  // Height-table FALLBACK for "how many rows fit" - used ONLY when the rows
  // region is not laid out yet (e.g. the home is hidden), so the measured fit
  // below cannot read a real height. The desktop never vertically scrolls; the
  // agent composes 2-3 rows and deeper categories open via "See all".
  function maxVisibleRows() {
    var h = window.innerHeight || 1080;
    if (h >= 980) return 3;
    if (h >= 760) return 2;
    return 1;
  }

  // Fill the fixed rows region TOP-DOWN with as many rows as fit on ONE screen.
  // The first row is the highest priority (Continue), so it always wins a slot
  // and always keeps its header. MEASURED fit (no magic-number cap): append a
  // row, sum what the region now needs, and if that exceeds the region height,
  // roll the row back. This is why a full-size hero plus three tall rows never
  // clips the top row's header at 1080 (only as many as truly fit are shown) -
  // and, because the kept rows hug the bottom, they never climb into the docked
  // orb's zone. Falls back to the height table only when the region is not laid
  // out yet (e.g. the home is hidden), where measurement is impossible.
  //
  // We must NOT use scrollHeight here: the region is justify-content:flex-end,
  // and a flex container does NOT report content that overflows its TOP edge in
  // scrollHeight (top overflow is unreachable/unscrollable), so scrollHeight
  // would stay == clientHeight and hide the very overflow we need to catch. We
  // sum each row's own border-box height + the row gaps + the region padding
  // instead (box-sizing-independent: offsetHeight is always border-box and
  // clientHeight is always content-area + padding).
  function appendRowsToFit(rows, list) {
    var avail = rows.clientHeight;            // flex:1 region height (one reflow)
    var measured = avail > 1;
    var cap = measured ? Math.min(list.length, 4)
                       : Math.min(list.length, maxVisibleRows());
    var gap = 0, pad = 0;
    if (measured) {
      var cs = window.getComputedStyle(rows);
      gap = parseFloat(cs.rowGap || cs.gap) || 0;
      pad = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
    }
    for (var i = 0; i < cap; i++) {
      rows.appendChild(makeRow(list[i], i));
      if (!measured) continue;
      var kids = rows.children, n = kids.length, used = pad;
      for (var k = 0; k < n; k++) used += kids[k].offsetHeight;
      if (n > 1) used += gap * (n - 1);
      // Overflow == this row does not fit. Keep at least one row even on a tiny
      // viewport (better a clipped single row than a blank rows region).
      if (used > avail + 2 && n > 1) {
        rows.removeChild(rows.lastChild);
        break;
      }
    }
  }

  // Hide the legacy desktop-icon layer while the home is the active view. The home
  // (z-index 30, transparent in its gaps) sits over .hart-desktop (z-index 20), so the
  // old launcher icons (Appearance/Feed/Agents/Recipes/Notifications) bled through and
  // overlapped the hero. The home supersedes them (top-bar nav + the agent rows).
  // Restored when the home deactivates (setActive(false)) so a non-home view keeps icons.
  function setDesktopLayer(hidden) {
    try {
      var dl = document.querySelector('.hart-desktop');
      if (dl) dl.style.display = hidden ? 'none' : '';
    } catch (e) { console.debug('hartHome: toggle desktop layer failed', e); }
  }

  function render(payload) {
    _payload = payload || samplePayload();
    var root = mountRoot();
    root.innerHTML = '';
    root.appendChild(buildHero(_payload.hero));
    var rows = document.createElement('div');
    rows.className = 'hh-rows';
    // Append the rows container FIRST so it has a laid-out height to measure,
    // then fit rows into it (top-priority row first).
    root.appendChild(rows);
    appendRowsToFit(rows, _payload.rows || []);
    // Reveal (opacity transition) on the next frame.
    requestAnimationFrame(function () { root.classList.add('hh-ready'); });
    setDesktopLayer(true);
    // Ask the orb to dock to the right hero zone (best-effort; hartHero owns it).
    try { if (typeof window.HartOrbHomeMode === 'function') window.HartOrbHomeMode(true); } catch (e) { console.debug('hartHome: HartOrbHomeMode(true) failed', e); }
  }

  // ───────────────────────────────────────────────────────────────────────
  // LIVE DATA - upgrade the sample with real endpoints, each degrading alone.
  // ───────────────────────────────────────────────────────────────────────
  function fetchEarnings(hero) {
    // REAL earnings (d1.1): the figure is the user's actual share of value their
    // node generated working alongside the hive - the settled compute ledger,
    // NOT a vanity number. Path (self-scoped, no parallel ledger):
    //   1) /api/social/resonance/wallet -> the Spark balance + the user_id
    //   2) /api/compute/earnings/<uid>  -> total_spark_in_window (settled
    //      api_cost_recovery rows = real hive-work earnings) + the per-settlement
    //      rows that feed the sparkline.
    // On 401/offline we keep the sample skeleton (honest: marked payout-pending,
    // never a fabricated rupee figure).
    return getJSON(backend() + '/api/social/resonance/wallet', 3500).then(function (d) {
      var w = (d && d.data) ? d.data : d;
      if (w && (typeof w.spark === 'number')) hero.spark_balance = w.spark;
      var uid = w && (w.user_id || w.uid || w.id);
      if (!uid) {
        // No identity: fall back to the real balance as the figure (still real,
        // not invented), keep the payout-pending honesty.
        if (typeof hero.spark_balance === 'number') hero.amount = hero.spark_balance;
        return;
      }
      return getJSON(backend() + '/api/compute/earnings/' + encodeURIComponent(uid) +
          '?days=7&limit=50', 3500).then(function (e) {
        var m = (e && e.meta) ? e.meta : {};
        var rows = (e && e.data) ? e.data : [];
        var earned = (typeof m.total_spark_in_window === 'number') ? m.total_spark_in_window : 0;
        hero.amount = earned;                 // the REAL earned Spark (number -> animates)
        hero.amount_unit = 'Spark';
        hero.earned_real = true;              // gates the spoken narrative (real story only)
        hero.payout_pending = true;           // no payout rail wired yet -> honest
        // sparkline = settlements oldest -> newest (rows come newest-first).
        if (rows.length) {
          hero.spark_series = rows.map(function (r) { return r.amount_spark || 0; }).reverse();
        }
      }).catch(function (e) {
        console.debug('hartHome: earnings window fetch failed (using balance)', e);
        if (typeof hero.spark_balance === 'number') hero.amount = hero.spark_balance;
      });
    }).catch(function (e) { console.debug('hartHome: earnings balance fetch failed (keeping sample)', e); });
  }
  // The canonical Spark<->USD rate (SPARK_PER_USD) surfaced by the PUBLIC
  // estimate endpoint - so the honest "~ $X at the hive rate" line uses the one
  // server constant, never a parallel literal baked into this file (DRY).
  function fetchRate(hero) {
    return getJSON(backend() + '/api/compute/earnings/estimate', 3500).then(function (d) {
      var data = (d && d.data) ? d.data : d;
      if (data && typeof data.spark_per_usd === 'number' && data.spark_per_usd > 0) {
        hero.spark_per_usd = data.spark_per_usd;
      }
    }).catch(function (e) { console.debug('hartHome: spark rate estimate fetch failed', e); });
  }
  // The orb SPEAKS the earnings narrative ONCE per session (rule b8: voice, not
  // a text wall) - only when the data is REAL and there is a story (earned > 0),
  // so a fresh node never narrates a sample. Reuses the shell's speakText path.
  function narrateOnce(hero) {
    try {
      if (window._hartHomeNarrated) return;
      if (!hero || !hero.earned_real) return;
      if (typeof hero.amount !== 'number' || hero.amount <= 0) return;
      if (typeof window.speakText !== 'function') return;
      window._hartHomeNarrated = true;
      var a = (typeof hero.agents === 'number') ? hero.agents : 0;
      var t = (typeof hero.tasks === 'number') ? hero.tasks : 0;
      var line = 'You earned ' + _fmt(hero.amount) + ' Spark on the hive';
      if (t) {
        line += ', ' + a + (a === 1 ? ' agent ran ' : ' agents ran ') +
          t + (t === 1 ? ' task' : ' tasks') + ' overnight';
      }
      line += ', fully local. Payout pending.';
      // small delay so it follows the boot greeting rather than racing it.
      setTimeout(function () { try { window.speakText(line, 'home_earnings'); } catch (e) { console.debug('hartHome: speakText earnings narrative failed', e); } }, 1600);
    } catch (e) { console.debug('hartHome: narrateOnce failed', e); }
  }
  function fetchAgents(payload) {
    return getJSON(backend() + '/api/social/dashboard/agents', 3500).then(function (d) {
      var agents = (d && d.agents) ? d.agents : [];
      if (!agents.length) return;
      var running = agents.filter(function (a) { return a.status === 'running'; });
      payload.hero.agents = running.length || agents.length;
      // tasks overnight: sum any per-agent task counter the dashboard exposes,
      // else fall back to the agent count (still a true, non-fake number).
      var tasks = 0;
      agents.forEach(function (a) {
        var n = a.tasks_completed || a.task_count || a.tasks || 0;
        if (typeof n === 'number') tasks += n;
      });
      payload.hero.tasks = tasks || agents.length;
      // Continue row = running/in-progress agents (Netflix Continue-Watching).
      var cont = (running.length ? running : agents).slice(0, 8).map(function (a) {
        return {
          title: a.name || a.goal_type || 'Agent',
          icon: a.icon || 'smart_toy',
          meta: a.status === 'running' ? (a.detail || 'running') : '',
          live: a.status === 'running' ? 'running' : '',
          progress: (typeof a.progress === 'number') ? a.progress : undefined,
          action: 'open', target: 'agents_browse'
        };
      });
      // Continue row only - the "Flagship agents" row is curated (always the
      // real product agents), so live dashboard agents feed Continue, not it.
      _replaceRow(payload, 'Continue', { title: 'Continue', accent: 'teal', see_all: 'agents_browse', cards: cont });
    }).catch(function (e) { console.debug('hartHome: agents dashboard fetch failed (keeping sample)', e); });
  }
  function fetchRecipes(payload) {
    // Try the two known recipe routes; first that answers wins, else keep sample.
    return _firstOK([
      backend() + '/api/recipes',
      backend() + '/api/social/recipes'
    ], 3500).then(function (d) {
      if (!d) return;
      var recipes = d.recipes || (d.data && d.data.recipes) || d.data || [];
      if (!recipes || !recipes.length) return;
      var cards = recipes.slice(0, 10).map(function (r) {
        return {
          title: r.name || r.title || r.prompt || 'Recipe',
          icon: r.icon || 'auto_awesome',
          meta: r.steps ? (r.steps + ' steps') : '',
          badge: 'Replay', action: 'open', target: 'recipes'
        };
      });
      _replaceRow(payload, 'Recipes',
        { title: 'Recipes', note: 'replay without re-thinking', accent: 'amber', see_all: 'recipes', cards: cards },
        true);
    }).catch(function (e) { console.debug('hartHome: recipes fetch failed (keeping sample)', e); });
  }

  function _replaceRow(payload, title, newRow, appendIfMissing) {
    var rows = payload.rows || (payload.rows = []);
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].title === title) { rows[i] = newRow; return; }
    }
    if (appendIfMissing) rows.push(newRow);
  }
  function _firstOK(urls, ms) {
    var i = 0;
    function next() {
      if (i >= urls.length) return Promise.resolve(null);
      var u = urls[i++];
      return getJSON(u, ms).catch(function () { return next(); });
    }
    return next();
  }
  function _fmt(n) {
    try { return Number(n).toLocaleString(); } catch (e) { return String(n); }
  }

  // refresh(): paint sample now (instant), then upgrade from live endpoints and
  // re-render once when they settle. Never blocks first paint on the network.
  var _refreshing = false;
  function refresh() {
    if (_refreshing) return;
    _refreshing = true;
    var payload = _payload || samplePayload();
    render(payload);
    Promise.all([
      fetchEarnings(payload.hero),
      fetchRate(payload.hero),
      fetchAgents(payload),
      fetchRecipes(payload)
    ]).then(function () {
      // Honest money equivalent from the canonical rate (only when both are real).
      if (typeof payload.hero.usd_equiv !== 'number' &&
          typeof payload.hero.amount === 'number' && payload.hero.amount > 0 &&
          payload.hero.spark_per_usd) {
        payload.hero.usd_equiv = payload.hero.amount / payload.hero.spark_per_usd;
      }
      render(payload);
      narrateOnce(payload.hero);     // the orb speaks the story (once, real-only)
      _refreshing = false;
    }, function (e) { console.debug('hartHome: refresh Promise.all rejected', e); _refreshing = false; });
  }

  // ───────────────────────────────────────────────────────────────────────
  // TOP-BAR NAV (brand | nav | omnibox | orb-sm | avatar). Each tab maps to a
  // REAL manifest/system panel id so it never opens an empty panel; Home brings
  // the composed canvas to the front. Reuses the canonical openPanel - no fork.
  // ───────────────────────────────────────────────────────────────────────
  var NAV_MAP = {
    home: null,                 // special: re-show the composed home canvas
    agents: 'agents_browse',    // PANEL_MANIFEST 'agents_browse' -> /agents
    apps: 'app_store',          // the App Store surface
    hive: 'communities',        // the hive / network surface
    earn: 'resonance',          // 'Resonance & Karma' = the earnings/wallet page
    account: 'user_accounts'    // SYSTEM_PANELS account page
  };
  function setActiveTab(tab) {
    try {
      var tabs = document.querySelectorAll('.top-bar-nav .tb-tab');
      for (var i = 0; i < tabs.length; i++) {
        tabs[i].classList.toggle('tb-active', (tabs[i].getAttribute('data-tab') || '') === tab);
      }
    } catch (e) { console.debug('hartHome: setActiveTab failed', e); }
  }
  function navTo(tab) {
    tab = (tab || 'home').toLowerCase();
    setActiveTab(tab);
    if (tab === 'home' || !NAV_MAP[tab]) {
      if (window.HartHome) window.HartHome.setActive(true);
      return;
    }
    if (typeof window.openPanel === 'function') window.openPanel(NAV_MAP[tab]);
  }
  window.HartHomeNav = navTo;

  // ───────────────────────────────────────────────────────────────────────
  // PUBLIC API - the agent composes the home through these (A2UI read path).
  // ───────────────────────────────────────────────────────────────────────
  window.HartHome = {
    // The agent's live composition. Accepts the full payload OR a partial
    // {hero} / {rows} which merges over the current surface (fluid re-compose).
    compose: function (payload) {
      if (!payload) return;
      var base = _payload || samplePayload();
      var merged = {
        hero: payload.hero || base.hero,
        rows: payload.rows || base.rows
      };
      render(merged);
      // Re-compose the pre-blurred cosmic bloom to the palette the agent just
      // composed (the mood may have retinted --hart-amb-*-rgb). ONE compose-time
      // blur pass, reused per frame -- never a live/per-frame blur. Guarded: the
      // bloom is optional (potato skips the canvas), so a missing fn is a no-op.
      if (typeof window !== 'undefined' && window.composeHartBloom) {
        try { window.composeHartBloom(); } catch (e) { /* backdrop never breaks compose */ }
      }
    },
    render: render,
    refresh: refresh,
    // Omnibox -> the EXISTING hero command bar (one dispatch path; e2 "reuse the
    // hero command bar, do not build a new search system").
    ask: function (prefill) { ask(prefill || ''); },
    nav: navTo,
    setActive: function (on) {
      var root = mountRoot();
      root.classList.toggle('hh-hidden', !on);
      // Re-fit the rows now that the region is laid out: if the home was composed
      // while hidden (display:none), the row-fit measurement read 0 and fell back
      // to the height table, which could over-fill and clip on reveal. Repainting
      // here re-measures against the real visible height. render() also re-docks
      // the orb to home mode, so the trailing call only matters for the off path.
      if (on) render(_payload || samplePayload());
      else { setDesktopLayer(false); try { if (typeof window.HartOrbHomeMode === 'function') window.HartOrbHomeMode(false); } catch (e) { console.debug('hartHome: HartOrbHomeMode(false) failed', e); } }
    }
  };

  function start() {
    if (!document.body) { return setTimeout(start, 100); }
    // Omnibox shortcut (e2): Super+K / Ctrl+K focuses the canonical hero command
    // bar - the truthful binding behind the top-bar pill's "Super K" hint.
    try {
      document.addEventListener('keydown', function (e) {
        if ((e.key === 'k' || e.key === 'K') &&
            (e.metaKey || e.ctrlKey || (e.getModifierState && e.getModifierState('Meta')))) {
          e.preventDefault();
          ask('');
        }
      });
    } catch (e) { console.debug('hartHome: omnibox keydown bind failed', e); }
    refresh();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
