/*
 * hartMarketplace.js — HART OS free-software marketplace (Phase C).
 *
 * A curated catalog of popular FREE / open-source apps by category (Flathub
 * app-ids — Flathub is the cross-platform free-software hub the NixOS-based
 * HART OS installs from), with live search and an AI-native "describe what you
 * need" fallback that routes to the agent. Reuses the shell's EXISTING installer
 * routes (/api/apps/search + /api/apps/install -> app_installer.py: Flatpak/Nix/
 * AppImage) — one installer, no fork. Heavy HTML built here so loadAppStorePanel
 * stays a tiny delegate.
 */
(function () {
  'use strict';

  // Curated free software (Flathub ids). c = featured category.
  var CATALOG = [
    { id: 'org.mozilla.firefox', n: 'Firefox', c: 'Web', i: 'public', d: 'Fast, private web browser' },
    { id: 'com.brave.Browser', n: 'Brave', c: 'Web', i: 'public', d: 'Privacy-first browser' },
    { id: 'org.chromium.Chromium', n: 'Chromium', c: 'Web', i: 'public', d: 'Open-source browser' },
    { id: 'org.videolan.VLC', n: 'VLC', c: 'Media', i: 'movie', d: 'Plays virtually everything' },
    { id: 'com.obsproject.Studio', n: 'OBS Studio', c: 'Media', i: 'videocam', d: 'Stream & record' },
    { id: 'org.audacityteam.Audacity', n: 'Audacity', c: 'Media', i: 'graphic_eq', d: 'Audio editor' },
    { id: 'org.gimp.GIMP', n: 'GIMP', c: 'Creative', i: 'brush', d: 'Image editor' },
    { id: 'org.inkscape.Inkscape', n: 'Inkscape', c: 'Creative', i: 'gesture', d: 'Vector graphics' },
    { id: 'org.kde.krita', n: 'Krita', c: 'Creative', i: 'palette', d: 'Digital painting' },
    { id: 'org.blender.Blender', n: 'Blender', c: 'Creative', i: 'view_in_ar', d: '3D creation suite' },
    { id: 'org.libreoffice.LibreOffice', n: 'LibreOffice', c: 'Productivity', i: 'description', d: 'Full office suite' },
    { id: 'org.mozilla.Thunderbird', n: 'Thunderbird', c: 'Productivity', i: 'mail', d: 'Email client' },
    { id: 'md.obsidian.Obsidian', n: 'Obsidian', c: 'Productivity', i: 'hub', d: 'Knowledge base' },
    { id: 'com.visualstudio.code', n: 'VS Code', c: 'Develop', i: 'code', d: 'Code editor' },
    { id: 'rest.insomnia.Insomnia', n: 'Insomnia', c: 'Develop', i: 'api', d: 'API client' },
    { id: 'io.github.shiftey.Desktop', n: 'GitHub Desktop', c: 'Develop', i: 'merge_type', d: 'Git for humans' },
    { id: 'org.telegram.desktop', n: 'Telegram', c: 'Chat', i: 'send', d: 'Messaging' },
    { id: 'org.signal.Signal', n: 'Signal', c: 'Chat', i: 'lock', d: 'Private messaging' },
    { id: 'com.discordapp.Discord', n: 'Discord', c: 'Chat', i: 'forum', d: 'Voice & text chat' },
    { id: 'com.valvesoftware.Steam', n: 'Steam', c: 'Games', i: 'sports_esports', d: 'Game library' },
    { id: 'net.lutris.Lutris', n: 'Lutris', c: 'Games', i: 'videogame_asset', d: 'Open gaming platform' },
    { id: 'org.prismlauncher.PrismLauncher', n: 'Prism Launcher', c: 'Games', i: 'casino', d: 'Minecraft launcher' }
  ];
  var CATS = ['Web', 'Creative', 'Media', 'Productivity', 'Develop', 'Chat', 'Games'];

  // Single base for the EXISTING installer route surface (app_installer.py). The
  // determinate flow composes the background-job endpoints off it
  // ('/api/apps/install/start' + '/api/apps/install/progress'); the synchronous
  // '/api/apps/install' remains the same family. One base, no parallel installer.
  var INSTALL_API = '/api/apps/install';

  function toast(t, m, sev) { if (window.showToast) window.showToast(t, m, sev || 'info'); }

  // Reverse-DNS Flathub id (org.mozilla.firefox). A search hit without a dotted
  // id (or a junk id) has no bundled tile, so it skips straight to the glyph.
  var FLATHUB_ID = /^[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9][A-Za-z0-9_-]*)+$/;

  // Bundled, no-network app LOGO URL (offline-first). MUST match the ONE
  // convention owned by shell_manifest.bundled_app_logo:
  // /shell/static/app_art/apps/<flathub_id>.svg. The <img> loads it directly
  // same-origin; if the tile is missing the onerror handler swaps in the Material
  // glyph (the documented fallback), so a miss degrades cleanly with the net OFF.
  function appLogoURL(app) {
    var id = (app && app.id) || '';
    if (!FLATHUB_ID.test(id)) return '';
    return '/shell/static/app_art/apps/' + id + '.svg';
  }

  // Determinate install progress bar. The backend now runs the install as a
  // one-at-a-time background job (POST /api/apps/install/start) and publishes
  // real phase checkpoints (downloading -> installing -> verifying -> done) plus
  // a bounded fraction over GET /api/apps/install/progress. This bar fills to that
  // fraction (no indeterminate sweep) and the button text tracks the phase, so the
  // user sees genuine progress and, at the end, the honest "verified" confirmation.
  // Built with inline styles (no CSS edit). Returns {set, remove}.
  function progressBar(btn) {
    if (!btn || !btn.parentNode) return null;
    var track = document.createElement('div');
    track.setAttribute('style', 'height:3px;margin-top:6px;border-radius:3px;overflow:hidden;' +
      'background:rgba(255,255,255,0.12)');
    var fill = document.createElement('div');
    fill.setAttribute('style', 'height:100%;width:0%;border-radius:3px;' +
      'background:linear-gradient(90deg,var(--hart-accent,#00E6C3),var(--hart-a2,#9B5CFF));' +
      'transition:width 0.45s ease');
    track.appendChild(fill);
    btn.parentNode.appendChild(track);
    return {
      set: function (frac) {
        var f = (typeof frac === 'number' && frac >= 0) ? frac : 0;
        if (f > 1) f = 1;
        fill.style.width = (f * 100).toFixed(1) + '%';
      },
      remove: function () { if (track.parentNode) track.parentNode.removeChild(track); }
    };
  }

  function phaseLabel(phase) {
    if (phase === 'downloading') return 'Downloading…';
    if (phase === 'verifying') return 'Verifying…';
    if (phase === 'done') return 'Finishing…';
    return 'Installing…';   // installing + any unknown intermediate phase
  }

  // Terminal outcome handler. Drives the button + toast off the REAL job result
  // (success/verified/staged/error). staged is NEVER a success (a downloaded-but-
  // not-applied file); verified is the post-install confirmation the background
  // job reads back from the platform handler.
  function finishInstall(app, btn, bar, res) {
    res = res || {};
    if (bar) bar.remove();
    if (res.success) {
      if (btn) { btn.disabled = true; btn.textContent = 'Installed'; btn.classList.add('is-installed'); }
      var detail = res.verified
        ? ((res.platform || 'flatpak') + ' - verified, added to your apps')
        : ((res.platform || 'flatpak') + ' - added (verification pending)');
      toast('Installed ' + app.n, detail, 'success');
    } else if (res.staged) {
      if (btn) { btn.disabled = false; btn.textContent = 'Install'; }
      toast('Downloaded ' + app.n, 'Staged - finish from the App Store when ready', 'warning');
    } else {
      if (btn) { btn.disabled = false; btn.textContent = 'Retry'; }
      toast('Could not install ' + app.n, res.error || 'The installer is unavailable on this system', 'error');
    }
  }

  // Poll the background job's progress until it reaches a terminal phase. The
  // 'token' ties this poller to its own job: if a newer install supersedes it (the
  // backend is one-at-a-time and re-tokens each job) we stop quietly instead of
  // reporting someone else's result onto this card.
  function pollProgress(app, btn, bar, token) {
    var tries = 0;
    function tick() {
      tries++;
      fetch(INSTALL_API + '/progress',
        { signal: window.HartTimeoutSignal ? window.HartTimeoutSignal(6000) : null })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          res = res || {};
          if (token != null && res.token != null && res.token !== token) {
            // Our slot was taken over by a newer install. Release this card.
            if (bar) bar.remove();
            if (btn) { btn.disabled = false; btn.textContent = 'Install'; }
            return;
          }
          var phase = res.phase || 'installing';
          if (bar && typeof res.fraction === 'number') bar.set(res.fraction);
          if (btn) btn.textContent = phaseLabel(phase);
          if (phase === 'done' || phase === 'error') { finishInstall(app, btn, bar, res); return; }
          setTimeout(tick, 700);
        })
        .catch(function () {
          // Transient poll failure: retry a bounded number of times before giving
          // up (covers a brief shell hiccup without spinning forever).
          if (tries < 80) { setTimeout(tick, 1000); return; }
          finishInstall(app, btn, bar, { error: 'Lost contact with the installer' });
        });
    }
    tick();
  }

  function install(app, btn) {
    if (btn) { btn.disabled = true; btn.textContent = 'Starting…'; }
    var bar = progressBar(btn);
    if (bar) bar.set(0.05);
    fetch(INSTALL_API + '/start', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      // pass both shapes: {package,platform} (what the existing UI sends) AND a
      // source hint (app_installer keys flatpak off a 'flatpak:' source).
      body: JSON.stringify({ package: app.id, platform: 'flatpak', source: 'flatpak:' + app.id }),
      signal: window.HartTimeoutSignal ? window.HartTimeoutSignal(15000) : null })
      .then(function (r) {
        var status = r.status;
        return r.json().catch(function () { return { ok: r.ok }; }).then(function (j) {
          j = j || {}; j._status = status; return j;
        });
      })
      .then(function (res) {
        res = res || {};
        if (res._status === 409 || res.busy) {
          // Another app is installing (backend is one-at-a-time). Reset and ask
          // the user to retry shortly. Never silently queue or fake progress.
          if (bar) bar.remove();
          if (btn) { btn.disabled = false; btn.textContent = 'Install'; }
          toast('Please wait', 'Another app is installing. Try again in a moment.', 'warning');
          return;
        }
        if (res.ok === false) {
          if (bar) bar.remove();
          if (btn) { btn.disabled = false; btn.textContent = 'Retry'; }
          toast('Could not install ' + app.n, res.error || 'The installer is unavailable on this system', 'error');
          return;
        }
        // Job accepted. Switch to determinate polling.
        var prog = res.progress || {};
        if (bar && typeof prog.fraction === 'number') bar.set(prog.fraction);
        if (btn) btn.textContent = phaseLabel(prog.phase || 'installing');
        pollProgress(app, btn, bar, res.token);
      })
      .catch(function () {
        if (bar) bar.remove();
        if (btn) { btn.disabled = false; btn.textContent = 'Retry'; }
        toast('Install failed', app.n + ' - no response from the installer', 'error');
      });
  }

  // app.installed === true renders a non-interactive "Installed" state instead of
  // an Install button (reused for the pre-bundled / already-installed section).
  function appCard(app) {
    // Premium vertical card: icon + name/category/description up top, a full-
    // width Install action below — frosted-glass styling lives in _CSS_DESKTOP
    // (.hart-app-card). Built with DOM nodes (no innerHTML) so names/descriptions
    // are inserted as text, never markup.
    var el = document.createElement('div'); el.className = 'hart-app-card';
    var top = document.createElement('div'); top.className = 'hac-top';
    var ic = document.createElement('div'); ic.className = 'hac-ic';
    // Material glyph is the fallback (built once, reused on an image miss).
    var glyphHTML = '<span class="mi material-icons-round" aria-hidden="true">' + (app.i || 'apps') + '</span>';
    var logo = appLogoURL(app);
    if (logo) {
      // Bundled official/brand logo, offline-first. object-fit:contain keeps a
      // real logo un-cropped inside the 52px .hac-ic plate; a missing tile fires
      // onerror -> the Material glyph (never a broken-image icon).
      var img = document.createElement('img');
      img.alt = ''; img.setAttribute('aria-hidden', 'true'); img.loading = 'lazy';
      img.setAttribute('style', 'width:100%;height:100%;object-fit:contain;border-radius:inherit;display:block');
      img.onerror = function () { ic.innerHTML = glyphHTML; };
      img.src = logo;
      ic.appendChild(img);
    } else {
      ic.innerHTML = glyphHTML;
    }
    var body = document.createElement('div'); body.className = 'hac-body';
    if (app.c) { var cat = document.createElement('div'); cat.className = 'hac-cat'; cat.textContent = app.c; body.appendChild(cat); }
    var nm = document.createElement('div'); nm.className = 'hac-name'; nm.textContent = app.n; nm.title = app.n;
    var ds = document.createElement('div'); ds.className = 'hac-desc'; ds.textContent = app.d || '';
    body.appendChild(nm); body.appendChild(ds);
    top.appendChild(ic); top.appendChild(body);
    var btn = document.createElement('button'); btn.type = 'button';
    if (app.installed) {
      btn.className = 'ds-btn ds-btn-tonal ds-btn-sm is-installed';
      btn.textContent = 'Installed'; btn.disabled = true;
    } else {
      btn.className = 'ds-btn ds-btn-tonal ds-btn-sm';
      btn.textContent = 'Install';
      btn.addEventListener('click', function () { install(app, btn); });
    }
    el.appendChild(top); el.appendChild(btn);
    return el;
  }

  function localSearch(q) {
    q = q.toLowerCase();
    return CATALOG.filter(function (a) {
      return a.n.toLowerCase().indexOf(q) >= 0 || (a.d || '').toLowerCase().indexOf(q) >= 0 || a.c.toLowerCase().indexOf(q) >= 0;
    });
  }

  // AI-native: route a natural-language need to the agent for a recommendation.
  function askAgent(q) {
    if (window.toggleAssistantChat && !document.getElementById('assistant-chat').classList.contains('open'))
      window.toggleAssistantChat();
    var aci = document.getElementById('ac-input');
    if (aci && window.acSend) { aci.value = 'Recommend a free, open-source app for: ' + q; window.acSend(); }
  }

  window.hartRenderMarketplace = function (el) {
    if (!el) return;
    el.innerHTML = '';
    var wrap = document.createElement('div'); wrap.className = 'hart-mkt ds-fade-in';

    var head = document.createElement('div'); head.className = 'hart-mkt-head';
    var title = document.createElement('div'); title.className = 'ds-panel-title'; title.textContent = 'App Store';
    head.appendChild(title);
    var sub = document.createElement('div'); sub.className = 'ds-body-sm ds-text-muted';
    sub.textContent = 'Free, open-source software from Flathub - one click to install.';
    head.appendChild(sub);
    wrap.appendChild(head);

    var row = document.createElement('div'); row.className = 'hart-mkt-search';
    var inp = document.createElement('input'); inp.className = 'ds-input'; inp.type = 'text';
    inp.placeholder = 'Search apps, or describe what you need…';
    var go = document.createElement('button'); go.className = 'ds-btn ds-btn-primary ds-btn-sm'; go.type = 'button';
    go.textContent = 'Search';
    row.appendChild(inp); row.appendChild(go); wrap.appendChild(row);

    // Already-installed / pre-bundled apps live in their own section ABOVE the
    // featured catalogue, reusing the SAME /api/apps/installed source the
    // permissions panel uses (no parallel list) so the steward can see what's
    // already on the system. Populated async; stays empty/hidden until the fetch
    // returns rows (keeps the curated catalogue first-paint instant).
    // Own class (NOT .hart-mkt-section) so it isn't mistaken for a featured
    // category section; it still reuses the .ds-section-label + .hart-app-grid
    // inner structure when populated.
    var installedSec = document.createElement('div'); installedSec.className = 'hart-mkt-installed';
    installedSec.style.display = 'none';
    wrap.appendChild(installedSec);

    var results = document.createElement('div'); results.id = 'hart-mkt-results'; wrap.appendChild(results);

    function loadInstalled() {
      fetch('/api/apps/installed', { signal: window.HartTimeoutSignal ? window.HartTimeoutSignal(6000) : null })
        .then(function (r) { return r.json(); }).then(function (data) {
          var apps = (data && data.apps) || [];
          if (!apps.length) return;                       // nothing installed -> stay hidden
          installedSec.innerHTML = '';
          var lbl = document.createElement('div'); lbl.className = 'ds-section-label';
          lbl.textContent = 'Installed (' + apps.length + ')';
          installedSec.appendChild(lbl);
          var g = document.createElement('div'); g.className = 'hart-app-grid';
          apps.slice(0, 24).forEach(function (a) {
            g.appendChild(appCard({ id: a.app_id || a.name, n: a.name || a.app_id,
              c: a.platform || 'system', i: 'check_circle',
              d: a.version ? ('v' + a.version) : '', installed: true }));
          });
          installedSec.appendChild(g);
          installedSec.style.display = '';
        }).catch(function () { /* installed list is best-effort; never block the store */ });
    }

    function renderFeatured() {
      results.innerHTML = '';
      CATS.forEach(function (cat) {
        var apps = CATALOG.filter(function (a) { return a.c === cat; });
        if (!apps.length) return;
        var sec = document.createElement('div'); sec.className = 'hart-mkt-section';
        var lbl = document.createElement('div'); lbl.className = 'ds-section-label'; lbl.textContent = cat;
        sec.appendChild(lbl);
        var g = document.createElement('div'); g.className = 'hart-app-grid';
        apps.forEach(function (a) { g.appendChild(appCard(a)); });
        sec.appendChild(g);
        results.appendChild(sec);
      });
    }

    function noResults() {
      results.innerHTML = '';
      var d = document.createElement('div'); d.className = 'ds-body-md ds-text-muted';
      d.style.marginBottom = '8px'; d.textContent = 'No app matched. Let HART find one for you?';
      var b = document.createElement('button'); b.className = 'ds-btn ds-btn-tonal ds-btn-sm'; b.type = 'button';
      b.textContent = 'Ask HART';
      b.addEventListener('click', function () { askAgent(inp.value.trim()); });
      results.appendChild(d); results.appendChild(b);
    }

    function doSearch() {
      var q = inp.value.trim();
      if (!q) { renderFeatured(); return; }
      results.innerHTML = '<div class="ds-body-sm ds-text-muted">Searching Flathub…</div>';
      var local = localSearch(q);
      var g = document.createElement('div'); g.className = 'hart-app-grid';
      var have = {};
      local.forEach(function (a) { have[a.n.toLowerCase()] = 1; g.appendChild(appCard(a)); });
      fetch('/api/apps/search?q=' + encodeURIComponent(q),
        { signal: window.HartTimeoutSignal ? window.HartTimeoutSignal(15000) : null })
        .then(function (r) { return r.json(); }).then(function (data) {
          (data.results || []).slice(0, 18).forEach(function (p) {
            if (have[(p.name || '').toLowerCase()]) return;
            g.appendChild(appCard({ id: p.source || p.name, n: p.name, c: p.platform || 'flatpak',
              i: 'inventory_2', d: p.description || ((p.platform || '') + ' ' + (p.version || '')) }));
          });
          if (g.children.length) { results.innerHTML = ''; results.appendChild(g); } else noResults();
        }).catch(function () {
          if (g.children.length) { results.innerHTML = ''; results.appendChild(g); } else noResults();
        });
    }

    go.addEventListener('click', doSearch);
    inp.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); doSearch(); } });

    renderFeatured();
    loadInstalled();          // surface already-installed / pre-bundled apps
    el.appendChild(wrap);
  };
})();
