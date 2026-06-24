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

  function toast(t, m, sev) { if (window.showToast) window.showToast(t, m, sev || 'info'); }

  // Honest install: drive the button through its own lifecycle off the REAL
  // server response (the old code fired-and-forgot a misleading "Installing"
  // toast even when the platform was unavailable / the install failed). The
  // installer returns {success, staged, name, error, platform}; staged is NEVER
  // success (a downloaded-but-not-applied file), so it gets its own message.
  function install(app, btn) {
    if (btn) { btn.disabled = true; btn.textContent = 'Installing…'; }
    fetch('/api/apps/install', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      // pass both shapes: {package,platform} (what the existing UI sends) AND a
      // source hint (app_installer keys flatpak off a 'flatpak:' source).
      body: JSON.stringify({ package: app.id, platform: 'flatpak', source: 'flatpak:' + app.id }),
      signal: window.HartTimeoutSignal ? window.HartTimeoutSignal(120000) : null })
      .then(function (r) { return r.json().catch(function () { return { success: r.ok }; }); })
      .then(function (res) {
        res = res || {};
        if (res.success) {
          if (btn) { btn.textContent = 'Installed'; btn.classList.add('is-installed'); }
          toast('Installed ' + app.n, (res.platform || 'flatpak') + ' - added to your apps', 'success');
        } else if (res.staged) {
          if (btn) { btn.disabled = false; btn.textContent = 'Install'; }
          toast('Downloaded ' + app.n, 'Staged - finish from the App Store when ready', 'warning');
        } else {
          if (btn) { btn.disabled = false; btn.textContent = 'Retry'; }
          toast('Could not install ' + app.n, res.error || 'The installer is unavailable on this system', 'error');
        }
      })
      .catch(function () {
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
    ic.innerHTML = '<span class="mi material-icons-round" aria-hidden="true">' + (app.i || 'apps') + '</span>';
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
