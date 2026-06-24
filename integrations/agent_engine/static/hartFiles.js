/* HART OS — File Explorer (hartFiles.js)
 * ─────────────────────────────────────────────────────────────────────────
 * A Finder/Explorer-grade file manager for the Liquid UI glass shell.
 *
 * Wires ONLY to the canonical backend (no parallel file-op path, no second
 * sandbox): /api/shell/files/{browse,mkdir,delete,move,copy,info} +
 * /api/shell/open-with + /api/shell/files/recent. `move` doubles as rename
 * (the backend has no separate /rename); `info` is Properties.
 *
 * Mounted by loadFileManagerPanel(el) -> window.HartFiles.mount(el). Reuses the
 * global SHELL origin const (lazily, since it is defined after this script) and
 * the shell's design tokens (--hart-accent, --ds-*). Responsive via a
 * ResizeObserver on the panel container (container-width, not viewport), so it
 * adapts correctly whether the Files panel is floating, snapped, or fullscreen.
 */
(function () {
  'use strict';

  function S() { return (typeof SHELL !== 'undefined' && SHELL) || window.SHELL || ''; }
  function sig(ms) {
    try { if (window._sig) return window._sig(ms); } catch (e) {}
    var c = new AbortController(); setTimeout(function () { c.abort(); }, ms || 8000); return c.signal;
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function human(n) {
    n = +n || 0; if (n < 1024) return n + ' B';
    var u = ['KB', 'MB', 'GB', 'TB'], i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
    return n.toFixed(n < 10 ? 1 : 0) + ' ' + u[i];
  }
  function fmtDate(epoch) {
    if (!epoch) return '';
    try {
      var d = new Date(epoch * 1000);
      return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) +
        '  ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    } catch (e) { return ''; }
  }
  function kind(f) {
    if (f.is_dir) return 'Folder';
    var e = (f.extension || '').replace('.', '');
    return e ? e.toUpperCase() + ' file' : 'File';
  }
  function iconFor(f) {
    if (f.is_dir) return 'folder';
    var e = (f.extension || '').toLowerCase().replace('.', '');
    if (/^(png|jpg|jpeg|gif|webp|bmp|svg|heic|ico|tiff)$/.test(e)) return 'image';
    if (/^(mp4|mkv|webm|mov|avi|m4v|flv)$/.test(e)) return 'movie';
    if (/^(mp3|wav|flac|ogg|m4a|aac|opus)$/.test(e)) return 'music_note';
    if (/^(pdf)$/.test(e)) return 'picture_as_pdf';
    if (/^(zip|tar|gz|xz|7z|rar|bz2)$/.test(e)) return 'folder_zip';
    if (/^(txt|md|rst|log)$/.test(e)) return 'article';
    if (/^(py|js|ts|jsx|tsx|c|cpp|h|rs|go|java|sh|rb|php|html|css|json|yaml|yml|toml|nix)$/.test(e)) return 'code';
    if (/^(doc|docx|odt)$/.test(e)) return 'description';
    if (/^(xls|xlsx|csv|ods)$/.test(e)) return 'table_chart';
    if (/^(ppt|pptx|odp)$/.test(e)) return 'slideshow';
    return 'insert_drive_file';
  }
  function basename(p) { p = String(p || '').replace(/[\\/]+$/, ''); var i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\')); return i < 0 ? p : p.slice(i + 1); }
  function dirname(p) { p = String(p || '').replace(/[\\/]+$/, ''); var i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\')); return i <= 0 ? '/' : p.slice(0, i); }
  function joinp(a, b) { return String(a).replace(/[\\/]+$/, '') + '/' + b; }

  function toast(title, msg, type) {
    try { if (window.showToast) return window.showToast(title, msg, type || 'info'); } catch (e) {}
  }

  // ── API layer (canonical backend only) ───────────────────────────────────
  var API = {
    browse: function (path, hidden) {
      var u = S() + '/api/shell/files/browse?path=' + encodeURIComponent(path) + (hidden ? '&hidden=true' : '');
      return fetch(u, { signal: sig(8000) }).then(function (r) { return r.json(); });
    },
    mkdir: function (path) {
      return fetch(S() + '/api/shell/files/mkdir', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: path }), signal: sig(8000) }).then(function (r) { return r.json(); });
    },
    del: function (path) {
      return fetch(S() + '/api/shell/files/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: path }), signal: sig(15000) }).then(function (r) { return r.json(); });
    },
    move: function (src, dst) {
      return fetch(S() + '/api/shell/files/move', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source: src, destination: dst }), signal: sig(15000) }).then(function (r) { return r.json(); });
    },
    copy: function (src, dst) {
      return fetch(S() + '/api/shell/files/copy', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source: src, destination: dst }), signal: sig(30000) }).then(function (r) { return r.json(); });
    },
    info: function (path) {
      return fetch(S() + '/api/shell/files/info?path=' + encodeURIComponent(path), { signal: sig(8000) }).then(function (r) { return r.json(); });
    },
    openWith: function (path) {
      return fetch(S() + '/api/shell/open-with', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: path }), signal: sig(8000) }).then(function (r) { return r.json(); });
    },
    search: function (path, q, recursive, hidden) {
      var u = S() + '/api/shell/files/search?path=' + encodeURIComponent(path) +
        '&q=' + encodeURIComponent(q) + '&recursive=' + (recursive ? 'true' : 'false') +
        (hidden ? '&hidden=true' : '');
      return fetch(u, { signal: sig(15000) }).then(function (r) { return r.json(); });
    },
    chmod: function (path, mode) {
      return fetch(S() + '/api/shell/files/chmod', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: path, mode: mode }), signal: sig(8000) }).then(function (r) { return r.json(); });
    },
    // Thumbnail is an <img src> (GET), so expose the URL builder, not a fetch.
    thumbUrl: function (path, size) {
      return S() + '/api/shell/files/thumbnail?path=' + encodeURIComponent(path) + '&size=' + (size || 96);
    }
  };

  // ── per-instance state ───────────────────────────────────────────────────
  function newState() {
    return {
      cwd: '~', home: '', entries: [], filtered: [],
      back: [], fwd: [],
      sel: [], anchor: -1,
      clip: null,            // { mode:'copy'|'cut', paths:[...] }
      view: 'list',          // 'list' | 'grid'
      sortBy: 'name', sortDir: 1,
      hidden: false, filter: '',
      searchSub: false,      // 'search subfolders' toggle (recursive route)
      searchResults: null,   // when set: entries[] from /search (rel-pathed)
      searchTrunc: false, searchSeq: 0,
      el: null, root: null, ro: null
    };
  }

  // ── CSS (injected once) ──────────────────────────────────────────────────
  function ensureCss() {
    if (document.getElementById('hart-files-css')) return;
    var css = document.createElement('style'); css.id = 'hart-files-css';
    css.textContent = [
      '.hf-root{--hf-acc:var(--hart-accent,#6c63ff);display:flex;flex-direction:column;height:100%;min-height:380px;width:100%;color:var(--ds-text,#e9e7ff);font-family:var(--ds-font-body,system-ui)}',
      '.hf-toolbar{display:flex;align-items:center;gap:6px;padding:8px 10px;flex-wrap:nowrap;border-bottom:1px solid rgba(160,150,255,.14)}',
      '.hf-tb-btn{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;border-radius:9px;border:1px solid rgba(160,150,255,.18);background:rgba(160,150,255,.06);color:inherit;cursor:pointer;flex:0 0 auto;transition:background .15s,transform .1s}',
      '.hf-tb-btn:hover{background:rgba(108,99,255,.22)}.hf-tb-btn:active{transform:scale(.94)}.hf-tb-btn[disabled]{opacity:.32;pointer-events:none}',
      '.hf-tb-btn .mi{font-size:19px}',
      '.hf-crumbs{display:flex;align-items:center;gap:2px;flex:1 1 auto;min-width:0;overflow:hidden;padding:0 6px;background:rgba(0,0,0,.18);border:1px solid rgba(160,150,255,.14);border-radius:9px;height:32px}',
      '.hf-crumb{padding:3px 7px;border-radius:7px;cursor:pointer;white-space:nowrap;color:var(--ds-text-muted,#a9a6c9);font-size:13px;flex:0 0 auto}',
      '.hf-crumb:hover{background:rgba(108,99,255,.2);color:#fff}.hf-crumb.last{color:#fff;font-weight:600}',
      '.hf-crumb-sep{color:rgba(160,150,255,.4);flex:0 0 auto}',
      '.hf-search{height:32px;border-radius:9px;border:1px solid rgba(160,150,255,.18);background:rgba(0,0,0,.18);color:inherit;padding:0 10px;width:150px;flex:0 0 auto;outline:none;font-size:13px}',
      '.hf-search:focus{border-color:var(--hf-acc)}',
      '.hf-body{display:flex;flex:1 1 auto;min-height:0}',
      '.hf-sidebar{width:172px;flex:0 0 auto;overflow-y:auto;padding:10px 8px;border-right:1px solid rgba(160,150,255,.12)}',
      '.hf-side-group{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--ds-text-muted,#8d8ab0);margin:10px 6px 4px}',
      '.hf-side-item{display:flex;align-items:center;gap:9px;padding:7px 9px;border-radius:8px;cursor:pointer;font-size:13px;color:var(--ds-text,#d7d4f0)}',
      '.hf-side-item:hover{background:rgba(108,99,255,.16)}.hf-side-item.active{background:rgba(108,99,255,.26);color:#fff}',
      '.hf-side-item .mi{font-size:18px;color:var(--hf-acc)}',
      '.hf-main{flex:1 1 auto;min-width:0;overflow:auto;position:relative}',
      '.hf-cols{display:flex;align-items:center;position:sticky;top:0;z-index:2;padding:6px 14px;font-size:11.5px;color:var(--ds-text-muted,#8d8ab0);background:rgba(18,16,34,.86);backdrop-filter:blur(8px);border-bottom:1px solid rgba(160,150,255,.12);text-transform:uppercase;letter-spacing:.5px;user-select:none}',
      '.hf-col{cursor:pointer;white-space:nowrap}.hf-col:hover{color:#fff}.hf-col-name{flex:1 1 auto;min-width:0}.hf-col-size{width:92px;text-align:right}.hf-col-kind{width:110px}.hf-col-date{width:172px}',
      '.hf-col .mi{font-size:14px;vertical-align:middle}',
      '.hf-list{padding:4px 6px}',
      '.hf-row{display:flex;align-items:center;padding:7px 8px;border-radius:8px;cursor:default;user-select:none}',
      '.hf-row:hover{background:rgba(160,150,255,.08)}.hf-row.sel{background:rgba(108,99,255,.3)}.hf-row.cut{opacity:.5}',
      '.hf-row .mi{font-size:20px;margin-right:10px;flex:0 0 auto}.hf-ic-dir{color:var(--hf-acc)}.hf-ic-file{color:#9a96c4}',
      '.hf-name{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13.5px}',
      '.hf-size{width:92px;text-align:right;font-size:12px;color:var(--ds-text-muted,#9a96c4)}',
      '.hf-kind{width:110px;font-size:12px;color:var(--ds-text-muted,#9a96c4);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
      '.hf-date{width:172px;font-size:12px;color:var(--ds-text-muted,#9a96c4);white-space:nowrap}',
      '.hf-rename{flex:1 1 auto;background:#11101f;border:1px solid var(--hf-acc);border-radius:5px;color:#fff;font-size:13.5px;padding:2px 6px;outline:none}',
      '.hf-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(104px,1fr));gap:6px;padding:12px}',
      '.hf-tile{display:flex;flex-direction:column;align-items:center;gap:6px;padding:12px 6px;border-radius:10px;cursor:default;user-select:none;text-align:center}',
      '.hf-tile:hover{background:rgba(160,150,255,.08)}.hf-tile.sel{background:rgba(108,99,255,.3)}.hf-tile.cut{opacity:.5}',
      '.hf-tile .mi{font-size:46px}.hf-tile .hf-name{width:100%;white-space:normal;word-break:break-word;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;text-align:center}',
      '.hf-status{display:flex;align-items:center;gap:14px;padding:6px 14px;font-size:12px;color:var(--ds-text-muted,#9a96c4);border-top:1px solid rgba(160,150,255,.12);flex:0 0 auto}',
      '.hf-empty{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:var(--ds-text-muted,#8d8ab0);gap:8px;padding:40px}',
      '.hf-empty .mi{font-size:46px;opacity:.5}',
      '.hf-ctx{position:fixed;z-index:13000;min-width:184px;background:rgba(24,22,42,.97);backdrop-filter:blur(18px);border:1px solid rgba(160,150,255,.22);border-radius:11px;padding:6px;box-shadow:0 18px 50px rgba(0,0,0,.5)}',
      '.hf-ctx-item{display:flex;align-items:center;gap:10px;padding:8px 11px;border-radius:7px;cursor:pointer;font-size:13px;color:var(--ds-text,#e3e0fb)}',
      '.hf-ctx-item:hover{background:rgba(108,99,255,.28)}.hf-ctx-item.danger:hover{background:rgba(255,90,90,.26)}.hf-ctx-item .mi{font-size:17px}.hf-ctx-item[disabled]{opacity:.34;pointer-events:none}',
      '.hf-ctx-sep{height:1px;background:rgba(160,150,255,.16);margin:5px 6px}',
      '.hf-props{position:absolute;inset:0;z-index:14000;display:flex;align-items:center;justify-content:center;background:rgba(8,7,18,.55)}',
      '.hf-props-card{width:min(420px,92%);background:rgba(24,22,42,.98);border:1px solid rgba(160,150,255,.22);border-radius:14px;padding:20px}',
      '.hf-props-row{display:flex;justify-content:space-between;gap:16px;padding:7px 0;border-bottom:1px solid rgba(160,150,255,.1);font-size:13px}',
      '.hf-props-row b{color:var(--ds-text-muted,#9a96c4);font-weight:500}',
      // search-subfolders toggle (sits beside the Filter box)
      '.hf-subfx{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;border-radius:9px;border:1px solid rgba(160,150,255,.18);background:rgba(160,150,255,.06);color:inherit;cursor:pointer;flex:0 0 auto}',
      '.hf-subfx:hover{background:rgba(108,99,255,.22)}.hf-subfx.on{background:rgba(108,99,255,.34);border-color:var(--hf-acc)}.hf-subfx .mi{font-size:18px}',
      // drag-drop: dragged source + drop-target highlight on a folder row / place
      '.hf-row.dragsrc,.hf-tile.dragsrc{opacity:.45}',
      '.hf-row.drop-on,.hf-tile.drop-on,.hf-side-item.drop-on{outline:2px solid var(--hf-acc);outline-offset:-2px;background:rgba(108,99,255,.34)}',
      '.hf-drag-ghost{position:fixed;z-index:13500;pointer-events:none;padding:4px 10px;border-radius:8px;background:rgba(24,22,42,.96);border:1px solid var(--hf-acc);color:#fff;font-size:12.5px;box-shadow:0 10px 30px rgba(0,0,0,.5)}',
      // thumbnail image (list row + grid tile) — same box as the glyph it replaces
      '.hf-thumb{flex:0 0 auto;object-fit:cover;border-radius:4px;background:rgba(0,0,0,.25)}',
      '.hf-row .hf-thumb{width:22px;height:22px;margin-right:10px}',
      '.hf-tile .hf-thumb{width:46px;height:46px;border-radius:7px}',
      // search result rows carry a small relative-path subtitle
      '.hf-rel{font-size:11px;color:var(--ds-text-muted,#8d8ab0);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
      '.hf-row .hf-namewrap{flex:1 1 auto;min-width:0;display:flex;flex-direction:column;justify-content:center}',
      // permissions editor inside Properties
      '.hf-perm{margin-top:6px}',
      '.hf-perm-grid{display:grid;grid-template-columns:auto repeat(3,1fr);gap:6px 10px;align-items:center;margin:8px 0}',
      '.hf-perm-grid .ph{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--ds-text-muted,#8d8ab0);text-align:center}',
      '.hf-perm-grid .rl{font-size:12.5px;color:var(--ds-text-muted,#9a96c4)}',
      '.hf-perm-cb{width:18px;height:18px;accent-color:var(--hf-acc);cursor:pointer;justify-self:center}',
      '.hf-perm-oct{font-family:ui-monospace,Menlo,Consolas,monospace;color:#fff}',
      '.hf-perm-save{height:32px;padding:0 14px;border-radius:8px;border:1px solid var(--hf-acc);background:rgba(108,99,255,.28);color:#fff;cursor:pointer;font-size:12.5px}',
      '.hf-perm-save[disabled]{opacity:.4;pointer-events:none}',
      // responsive (container-width via ResizeObserver classes on .hf-root)
      '.hf-root.hf-narrow .hf-sidebar{position:absolute;left:0;top:0;bottom:0;z-index:9;width:210px;background:rgba(16,14,30,.98);transform:translateX(-105%);transition:transform .22s;border-right:1px solid rgba(160,150,255,.2)}',
      '.hf-root.hf-narrow.hf-side-open .hf-sidebar{transform:none}',
      '.hf-root.hf-narrow .hf-col-kind,.hf-root.hf-narrow .hf-kind{display:none}',
      '.hf-root:not(.hf-narrow) .hf-burger{display:none}',
      '.hf-root.hf-tiny .hf-col-date,.hf-root.hf-tiny .hf-date{display:none}.hf-root.hf-tiny .hf-search{width:96px}'
    ].join('\n');
    document.head.appendChild(css);
  }

  // ── module ───────────────────────────────────────────────────────────────
  var ST = null; // single active instance (the Files panel is a singleton panel)

  function mount(el) {
    if (!el) return;
    ensureCss();
    ST = newState(); ST.el = el;
    el.innerHTML = '<div class="hf-root" tabindex="0"></div>';
    ST.root = el.querySelector('.hf-root');
    bindKeys(ST.root);
    observeWidth(ST.root);
    navigate('~', false);
  }

  function observeWidth(root) {
    function apply(w) {
      root.classList.toggle('hf-narrow', w < 640);
      root.classList.toggle('hf-tiny', w < 460);
      if (w >= 640) root.classList.remove('hf-side-open');
    }
    try {
      ST.ro = new ResizeObserver(function (es) { apply(es[0].contentRect.width); });
      ST.ro.observe(root);
    } catch (e) { apply(root.clientWidth || 800); }
  }

  // ── navigation ───────────────────────────────────────────────────────────
  function navigate(path, pushHistory) {
    if (!ST) return;
    if (pushHistory !== false && ST.cwd && ST.cwd !== path) { ST.back.push(ST.cwd); ST.fwd = []; }
    ST.filter = ''; ST.sel = []; ST.anchor = -1;
    ST.searchResults = null; ST.searchTrunc = false;  // leaving a folder ends a search
    render(true);
    API.browse(path, ST.hidden).then(function (d) {
      if (!d || d.error) { renderError(d && d.error ? d.error : 'Cannot open folder'); return; }
      ST.cwd = d.path || path;
      ST.parent = d.parent || '';
      if (!ST.home && (path === '~' || /(^|[\\/])home[\\/]/.test(ST.cwd) || true)) {
        // remember the first resolved root as Home for the sidebar/places
        if (path === '~') ST.home = ST.cwd;
      }
      ST.entries = (d.entries || []).slice();
      applySort(); render(false);
    }).catch(function () { renderError('File browser unavailable'); });
  }
  function back() { if (ST.back.length) { ST.fwd.push(ST.cwd); navigate(ST.back.pop(), false); } }
  function forward() { if (ST.fwd.length) { ST.back.push(ST.cwd); navigate(ST.fwd.pop(), false); } }
  function up() { if (ST.parent && ST.parent !== ST.cwd) navigate(ST.parent, true); }
  function refresh() { navigate(ST.cwd, false); }

  function applySort() {
    var k = ST.sortBy, dir = ST.sortDir;
    ST.entries.sort(function (a, b) {
      if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1; // dirs first always
      var av, bv;
      if (k === 'size') { av = a.size || 0; bv = b.size || 0; }
      else if (k === 'date') { av = a.modified || 0; bv = b.modified || 0; }
      else if (k === 'kind') { av = (a.extension || ''); bv = (b.extension || ''); }
      else { av = a.name.toLowerCase(); bv = b.name.toLowerCase(); }
      return av < bv ? -dir : av > bv ? dir : 0;
    });
  }
  function visible() {
    // When a subfolder search is active, the result set IS the view (already
    // server-filtered + rel-pathed); the Filter box drives that search instead
    // of an in-memory filter.
    if (ST.searchResults) return ST.searchResults;
    if (!ST.filter) return ST.entries;
    var q = ST.filter.toLowerCase();
    return ST.entries.filter(function (e) { return e.name.toLowerCase().indexOf(q) >= 0; });
  }
  function inSearch() { return !!ST.searchResults; }
  function runSearch() {
    var q = (ST.filter || '').trim();
    if (!q) { ST.searchResults = null; ST.searchTrunc = false; render(false); return; }
    var seq = ++ST.searchSeq;
    API.search(ST.cwd, q, true, ST.hidden).then(function (d) {
      if (seq !== ST.searchSeq) return;          // a newer keystroke won
      if (!d || d.error) { ST.searchResults = []; ST.searchTrunc = false; }
      else { ST.searchResults = (d.entries || []).slice(); ST.searchTrunc = !!d.truncated; }
      ST.sel = []; ST.anchor = -1;
      render(false);
    }).catch(function () {
      if (seq !== ST.searchSeq) return;
      ST.searchResults = []; ST.searchTrunc = false; render(false);
    });
  }
  // Debounced trigger so typing in the Filter box doesn't fire a walk per key.
  var _searchTimer = 0;
  function scheduleSearch() {
    if (_searchTimer) clearTimeout(_searchTimer);
    _searchTimer = setTimeout(function () { _searchTimer = 0; runSearch(); }, 260);
  }

  // ── render ───────────────────────────────────────────────────────────────
  function places() {
    var h = ST.home || '~';
    return [
      { name: 'Home', icon: 'home', path: h },
      { name: 'Desktop', icon: 'desktop_windows', path: joinp(h, 'Desktop') },
      { name: 'Documents', icon: 'description', path: joinp(h, 'Documents') },
      { name: 'Downloads', icon: 'download', path: joinp(h, 'Downloads') },
      { name: 'Pictures', icon: 'image', path: joinp(h, 'Pictures') },
      { name: 'Music', icon: 'music_note', path: joinp(h, 'Music') },
      { name: 'Videos', icon: 'movie', path: joinp(h, 'Videos') }
    ];
  }
  function crumbsHtml() {
    var p = String(ST.cwd || '').replace(/\\/g, '/');
    var parts = p.split('/').filter(Boolean);
    var acc = p.charAt(0) === '/' ? '' : '';
    var html = '';
    if (p.charAt(0) === '/') { html += '<span class="hf-crumb" data-p="/">/</span>'; }
    var cur = p.charAt(0) === '/' ? '' : '';
    parts.forEach(function (seg, i) {
      cur += '/' + seg;
      if (i) html += '<span class="hf-crumb-sep mi material-icons-round" style="font-size:15px">chevron_right</span>';
      html += '<span class="hf-crumb' + (i === parts.length - 1 ? ' last' : '') + '" data-p="' + esc(cur) + '">' + esc(seg) + '</span>';
    });
    return html || '<span class="hf-crumb last">/</span>';
  }
  function sideHtml() {
    var h = '<div class="hf-side-group">Places</div>';
    places().forEach(function (pl) {
      h += '<div class="hf-side-item' + (ST.cwd === pl.path ? ' active' : '') + '" data-nav="' + esc(pl.path) + '">' +
        '<span class="mi material-icons-round">' + pl.icon + '</span>' + esc(pl.name) + '</div>';
    });
    return h;
  }
  function isImage(f) { return !f.is_dir && iconFor(f) === 'image'; }
  // Glyph span + (for images) an <img> thumbnail that replaces the glyph once it
  // loads; on error the <img> removes itself and the glyph stays. cls = the
  // glyph size class ('mi' is shared); klass = extra (hf-ic-dir/file).
  function glyphOrThumb(f, size, klass) {
    var glyph = '<span class="mi material-icons-round ' + klass + '">' + iconFor(f) + '</span>';
    if (!isImage(f)) return glyph;
    var img = '<img class="hf-thumb" alt="" loading="lazy" src="' + esc(API.thumbUrl(f.path, size)) +
      '" onload="this.previousElementSibling&&(this.previousElementSibling.style.display=\'none\')" ' +
      'onerror="this.remove()">';
    return glyph + img;
  }
  function rowHtml(f, i) {
    var selc = ST.sel.indexOf(f.path) >= 0 ? ' sel' : '';
    var cutc = (ST.clip && ST.clip.mode === 'cut' && ST.clip.paths.indexOf(f.path) >= 0) ? ' cut' : '';
    var nameCell;
    if (inSearch() && f.rel) {
      var loc = dirname(f.rel); if (loc === '/' || loc === '.') loc = ST.cwd;
      nameCell = '<span class="hf-namewrap"><span class="hf-name">' + esc(f.name) + '</span>' +
        '<span class="hf-rel">' + esc(loc) + '</span></span>';
    } else {
      nameCell = '<span class="hf-name">' + esc(f.name) + '</span>';
    }
    return '<div class="hf-row' + selc + cutc + '" data-i="' + i + '" data-path="' + esc(f.path) + '" data-dir="' + (f.is_dir ? 1 : 0) + '">' +
      glyphOrThumb(f, 48, f.is_dir ? 'hf-ic-dir' : 'hf-ic-file') +
      nameCell +
      '<span class="hf-size">' + (f.is_dir ? '' : human(f.size)) + '</span>' +
      '<span class="hf-kind">' + esc(kind(f)) + '</span>' +
      '<span class="hf-date">' + fmtDate(f.modified) + '</span></div>';
  }
  function tileHtml(f, i) {
    var selc = ST.sel.indexOf(f.path) >= 0 ? ' sel' : '';
    var cutc = (ST.clip && ST.clip.mode === 'cut' && ST.clip.paths.indexOf(f.path) >= 0) ? ' cut' : '';
    return '<div class="hf-tile' + selc + cutc + '" data-i="' + i + '" data-path="' + esc(f.path) + '" data-dir="' + (f.is_dir ? 1 : 0) + '">' +
      glyphOrThumb(f, 96, f.is_dir ? 'hf-ic-dir' : 'hf-ic-file') +
      '<span class="hf-name">' + esc(f.name) + '</span></div>';
  }
  function sortCaret(col) { return ST.sortBy === col ? ('<span class="mi material-icons-round">' + (ST.sortDir > 0 ? 'arrow_drop_down' : 'arrow_drop_up') + '</span>') : ''; }

  function render(loading) {
    if (!ST || !ST.root) return;
    var canBack = ST.back.length > 0, canFwd = ST.fwd.length > 0, canUp = ST.parent && ST.parent !== ST.cwd;
    var vis = loading ? [] : visible();
    var html = '';
    // toolbar
    html += '<div class="hf-toolbar">' +
      '<button class="hf-tb-btn hf-burger" data-act="side" title="Places"><span class="mi material-icons-round">menu</span></button>' +
      '<button class="hf-tb-btn" data-act="back" ' + (canBack ? '' : 'disabled') + ' title="Back (Alt+Left)"><span class="mi material-icons-round">arrow_back</span></button>' +
      '<button class="hf-tb-btn" data-act="fwd" ' + (canFwd ? '' : 'disabled') + ' title="Forward (Alt+Right)"><span class="mi material-icons-round">arrow_forward</span></button>' +
      '<button class="hf-tb-btn" data-act="up" ' + (canUp ? '' : 'disabled') + ' title="Up (Alt+Up)"><span class="mi material-icons-round">arrow_upward</span></button>' +
      '<button class="hf-tb-btn" data-act="refresh" title="Refresh (F5)"><span class="mi material-icons-round">refresh</span></button>' +
      '<div class="hf-crumbs">' + crumbsHtml() + '</div>' +
      '<input class="hf-search" placeholder="' + (ST.searchSub ? 'Search subfolders…' : 'Filter…') + '" value="' + esc(ST.filter) + '">' +
      '<button class="hf-subfx' + (ST.searchSub ? ' on' : '') + '" data-act="subfx" title="Search subfolders"><span class="mi material-icons-round">' + (ST.searchSub ? 'manage_search' : 'search') + '</span></button>' +
      '<button class="hf-tb-btn" data-act="newfolder" title="New Folder (Ctrl+Shift+N)"><span class="mi material-icons-round">create_new_folder</span></button>' +
      '<button class="hf-tb-btn" data-act="view" title="Toggle view"><span class="mi material-icons-round">' + (ST.view === 'list' ? 'grid_view' : 'view_list') + '</span></button>' +
      '<button class="hf-tb-btn" data-act="hidden" title="Hidden files" style="' + (ST.hidden ? 'background:rgba(108,99,255,.3)' : '') + '"><span class="mi material-icons-round">' + (ST.hidden ? 'visibility' : 'visibility_off') + '</span></button>' +
      '</div>';
    // body
    html += '<div class="hf-body"><div class="hf-sidebar">' + sideHtml() + '</div><div class="hf-main">';
    if (loading) {
      html += '<div class="hf-empty"><span class="mi material-icons-round">hourglass_top</span>Loading…</div>';
    } else if (!vis.length) {
      var emptyMsg = inSearch() ? ('No results for “' + esc(ST.filter) + '” in subfolders')
        : (ST.filter ? 'No items match “' + esc(ST.filter) + '”' : 'This folder is empty');
      html += '<div class="hf-empty"><span class="mi material-icons-round">' + (inSearch() ? 'search_off' : 'folder_open') + '</span>' + emptyMsg + '</div>';
    } else if (ST.view === 'list') {
      html += '<div class="hf-cols">' +
        '<span class="hf-col hf-col-name" data-sort="name">Name ' + sortCaret('name') + '</span>' +
        '<span class="hf-col hf-col-size" data-sort="size">Size ' + sortCaret('size') + '</span>' +
        '<span class="hf-col hf-col-kind" data-sort="kind">Kind ' + sortCaret('kind') + '</span>' +
        '<span class="hf-col hf-col-date" data-sort="date">Modified ' + sortCaret('date') + '</span></div>';
      html += '<div class="hf-list">';
      vis.forEach(function (f, i) { html += rowHtml(f, i); });
      html += '</div>';
    } else {
      html += '<div class="hf-grid">';
      vis.forEach(function (f, i) { html += tileHtml(f, i); });
      html += '</div>';
    }
    html += '</div></div>';
    // status bar
    var selSize = 0; ST.sel.forEach(function (p) { var e = byPath(p); if (e && !e.is_dir) selSize += (e.size || 0); });
    var countLabel = (inSearch() ? (vis.length + ' result' + (vis.length === 1 ? '' : 's') + (ST.searchTrunc ? '+ (showing first ' + vis.length + ')' : ''))
      : (vis.length + ' item' + (vis.length === 1 ? '' : 's')));
    html += '<div class="hf-status"><span>' + countLabel + '</span>' +
      (ST.sel.length ? '<span>' + ST.sel.length + ' selected' + (selSize ? ' · ' + human(selSize) : '') + '</span>' : '') +
      (ST.clip ? '<span style="margin-left:auto">' + ST.clip.mode + ': ' + ST.clip.paths.length + '</span>' : '') + '</div>';

    ST.root.innerHTML = html;
    wire();
  }
  function renderError(msg) {
    if (!ST || !ST.root) return;
    ST.root.innerHTML = '<div class="hf-empty"><span class="mi material-icons-round">error_outline</span>' + esc(msg) +
      '<button class="hf-tb-btn" data-act="refresh" style="width:auto;padding:0 14px;gap:6px"><span class="mi material-icons-round">refresh</span>Retry</button></div>';
    var b = ST.root.querySelector('[data-act="refresh"]'); if (b) b.onclick = refresh;
  }

  function byPath(p) {
    var i;
    for (i = 0; i < ST.entries.length; i++) if (ST.entries[i].path === p) return ST.entries[i];
    if (ST.searchResults) for (i = 0; i < ST.searchResults.length; i++) if (ST.searchResults[i].path === p) return ST.searchResults[i];
    return null;
  }

  // ── wiring ───────────────────────────────────────────────────────────────
  function wire() {
    var r = ST.root;
    r.querySelectorAll('.hf-tb-btn,.hf-subfx').forEach(function (b) {
      b.onclick = function () {
        var a = b.getAttribute('data-act');
        if (a === 'back') back(); else if (a === 'fwd') forward(); else if (a === 'up') up();
        else if (a === 'refresh') refresh(); else if (a === 'newfolder') newFolder();
        else if (a === 'view') { ST.view = ST.view === 'list' ? 'grid' : 'list'; render(false); }
        else if (a === 'hidden') { ST.hidden = !ST.hidden; refresh(); }
        else if (a === 'side') r.classList.toggle('hf-side-open');
        else if (a === 'subfx') {
          ST.searchSub = !ST.searchSub;
          if (ST.searchSub) { if (ST.filter.trim()) runSearch(); else render(false); }
          else { ST.searchResults = null; ST.searchTrunc = false; render(false); }  // back to instant local filter
        }
      };
    });
    r.querySelectorAll('.hf-crumb').forEach(function (c) { c.onclick = function () { navigate(c.getAttribute('data-p'), true); }; });
    r.querySelectorAll('.hf-side-item').forEach(function (s) { s.onclick = function () { r.classList.remove('hf-side-open'); navigate(s.getAttribute('data-nav'), true); }; });
    r.querySelectorAll('.hf-col').forEach(function (c) {
      c.onclick = function () { var k = c.getAttribute('data-sort'); if (ST.sortBy === k) ST.sortDir = -ST.sortDir; else { ST.sortBy = k; ST.sortDir = 1; } applySort(); render(false); };
    });
    var s = r.querySelector('.hf-search');
    if (s) {
      s.oninput = function () {
        ST.filter = s.value;
        if (ST.searchSub) {
          // recursive route (debounced); keep the instant local filter live too
          // so the current dir narrows immediately while the walk returns.
          if (!s.value.trim()) { ST.searchResults = null; ST.searchTrunc = false; }
          scheduleSearch(); renderKeepFocus(s);
        } else {
          renderKeepFocus(s);  // instant filter of the current dir
        }
      };
      s.onkeydown = function (e) {
        if (e.key === 'Enter' && ST.searchSub) { if (_searchTimer) { clearTimeout(_searchTimer); _searchTimer = 0; } runSearch(); }
      };
    }

    r.querySelectorAll('.hf-row,.hf-tile').forEach(function (it) {
      it.onclick = function (ev) { if (it._dragged) { it._dragged = false; return; } onItemClick(it, ev); };
      it.ondblclick = function () { openItem(it.getAttribute('data-path'), it.getAttribute('data-dir') === '1'); };
      it.oncontextmenu = function (ev) { ev.preventDefault(); if (ST.sel.indexOf(it.getAttribute('data-path')) < 0) selectOnly(it.getAttribute('data-path')); itemMenu(ev, it.getAttribute('data-path'), it.getAttribute('data-dir') === '1'); };
      bindDrag(it);
    });
    var main = r.querySelector('.hf-main');
    if (main) main.oncontextmenu = function (ev) { if (ev.target.closest('.hf-row,.hf-tile')) return; ev.preventDefault(); emptyMenu(ev); };
  }

  // ── intra-window drag-drop (MOVE same root / COPY with Ctrl·Cmd) ──────────
  // Reuses the pointer-event + setPointerCapture pattern from hartDesktop.js:
  // arm on pointerdown, treat as a drag only past a 4px threshold (so plain
  // clicks/dblclicks still fire), live-highlight the folder row / Places entry
  // under the pointer, drop -> existing /move or /copy, then refresh.
  function sameRoot(a, b) {
    // Same allowed-root => MOVE is in-place; cross-root would surprise, so COPY.
    // We approximate "same root" by sharing the top two path segments of cwd's
    // home; simplest robust check: both under the current home prefix.
    var h = (ST.home || '').replace(/\\/g, '/');
    a = String(a).replace(/\\/g, '/'); b = String(b).replace(/\\/g, '/');
    if (h && a.indexOf(h) === 0 && b.indexOf(h) === 0) return true;
    // fall back to drive/root-letter comparison
    var ra = a.slice(0, 3), rb = b.slice(0, 3);
    return ra.toLowerCase() === rb.toLowerCase();
  }
  function dropTargetUnder(x, y) {
    var els = (document.elementsFromPoint ? document.elementsFromPoint(x, y) : [document.elementFromPoint(x, y)]);
    for (var i = 0; i < els.length; i++) {
      var el = els[i]; if (!el) continue;
      var side = el.closest && el.closest('.hf-side-item');
      if (side && ST.root.contains(side)) return { el: side, path: side.getAttribute('data-nav'), kind: 'place' };
      var row = el.closest && el.closest('.hf-row,.hf-tile');
      if (row && ST.root.contains(row) && row.getAttribute('data-dir') === '1') {
        return { el: row, path: row.getAttribute('data-path'), kind: 'folder' };
      }
    }
    return null;
  }
  function clearDrop() {
    ST.root.querySelectorAll('.drop-on').forEach(function (e) { e.classList.remove('drop-on'); });
  }
  function bindDrag(it) {
    var armed = false, dragging = false, sx = 0, sy = 0, ghost = null, cur = null, pid = null, p = null;
    it.addEventListener('pointerdown', function (e) {
      if (e.button !== 0) return;
      // If the row isn't selected, select it first so a drag carries it.
      p = it.getAttribute('data-path');
      if (ST.sel.indexOf(p) < 0) selectOnly(p);
      armed = true; dragging = false; sx = e.clientX; sy = e.clientY; pid = e.pointerId;
    });
    it.addEventListener('pointermove', function (e) {
      if (!armed) return;
      if (!dragging) {
        if (Math.abs(e.clientX - sx) + Math.abs(e.clientY - sy) <= 4) return;
        dragging = true; it._dragged = true;
        try { it.setPointerCapture(pid); } catch (_) {}
        it.classList.add('dragsrc');
        ghost = document.createElement('div'); ghost.className = 'hf-drag-ghost';
        var n = ST.sel.length;
        ghost.textContent = n > 1 ? (n + ' items') : basename(p);
        document.body.appendChild(ghost);
      }
      if (ghost) { ghost.style.left = (e.clientX + 12) + 'px'; ghost.style.top = (e.clientY + 14) + 'px'; }
      clearDrop();
      var t = dropTargetUnder(e.clientX, e.clientY);
      // never highlight a target that is itself part of the dragged selection
      if (t && ST.sel.indexOf(t.path) < 0) { t.el.classList.add('drop-on'); cur = t; }
      else cur = null;
    });
    function finish(e) {
      if (!armed) return; armed = false;
      if (!dragging) return;             // a plain click — onclick handles it
      dragging = false;
      try { it.releasePointerCapture(pid); } catch (_) {}
      if (ghost) { try { ghost.remove(); } catch (_) {} ghost = null; }
      it.classList.remove('dragsrc'); clearDrop();
      if (cur && cur.path) {
        var copy = e && (e.ctrlKey || e.metaKey);
        var dest = cur.path;
        // don't drop a folder into itself
        var srcs = ST.sel.slice().filter(function (s) { return s !== dest; });
        if (srcs.length) dropMove(srcs, dest, copy && !sameRootAll(srcs, dest) ? true : copy);
      }
      cur = null;
    }
    it.addEventListener('pointerup', finish);
    it.addEventListener('pointercancel', finish);
  }
  function sameRootAll(srcs, dest) {
    for (var i = 0; i < srcs.length; i++) if (!sameRoot(srcs[i], dest)) return false;
    return true;
  }
  function dropMove(srcs, dest, copy) {
    // Cross-root drags can't MOVE meaningfully; force COPY there.
    if (!copy && !sameRootAll(srcs, dest)) copy = true;
    // Skip MOVEs that would be a no-op (item already lives directly in dest);
    // a COPY into the same folder is still meaningful (the backend renames on
    // collision is not implemented, so it just surfaces the "name exists" warn).
    var work = srcs.filter(function (src) { return copy || dest !== dirname(src); });
    if (!work.length) { clearDrop(); return; }
    var done = 0, errs = 0, n = work.length;
    work.forEach(function (src) {
      var dst = joinp(dest, basename(src));
      var op = copy ? API.copy(src, dst) : API.move(src, dst);
      op.then(function (d) { if (d && d.error) errs++; }).catch(function () { errs++; }).then(function () {
        if (++done === n) finishDrop(copy, errs);
      });
    });
  }
  function finishDrop(copy, errs) {
    if (errs) toast(copy ? 'Copy' : 'Move', errs + ' failed (name may exist)', 'warn');
    else toast(copy ? 'Copied' : 'Moved', 'Done', 'info');
    refresh();
  }
  function renderKeepFocus(prevSearch) {
    var pos = prevSearch ? prevSearch.selectionStart : null;
    render(false);
    var s = ST.root.querySelector('.hf-search');
    if (s) { s.focus(); if (pos != null) try { s.setSelectionRange(pos, pos); } catch (e) {} }
  }

  // ── selection ────────────────────────────────────────────────────────────
  function selectOnly(p) { ST.sel = [p]; ST.anchor = idxOf(p); paintSel(); }
  function idxOf(p) { var v = visible(); for (var i = 0; i < v.length; i++) if (v[i].path === p) return i; return -1; }
  function onItemClick(it, ev) {
    var p = it.getAttribute('data-path'); var i = +it.getAttribute('data-i');
    if (ev.shiftKey && ST.anchor >= 0) {
      var v = visible(), a = Math.min(ST.anchor, i), b = Math.max(ST.anchor, i);
      ST.sel = []; for (var k = a; k <= b; k++) ST.sel.push(v[k].path);
    } else if (ev.ctrlKey || ev.metaKey) {
      var j = ST.sel.indexOf(p); if (j >= 0) ST.sel.splice(j, 1); else ST.sel.push(p); ST.anchor = i;
    } else { ST.sel = [p]; ST.anchor = i; }
    paintSel();
  }
  function paintSel() {
    ST.root.querySelectorAll('.hf-row,.hf-tile').forEach(function (it) {
      it.classList.toggle('sel', ST.sel.indexOf(it.getAttribute('data-path')) >= 0);
    });
    var v = visible(); var selSize = 0; ST.sel.forEach(function (p) { var e = byPath(p); if (e && !e.is_dir) selSize += (e.size || 0); });
    var st = ST.root.querySelector('.hf-status');
    if (st) st.innerHTML = '<span>' + v.length + ' item' + (v.length === 1 ? '' : 's') + '</span>' +
      (ST.sel.length ? '<span>' + ST.sel.length + ' selected' + (selSize ? ' · ' + human(selSize) : '') + '</span>' : '') +
      (ST.clip ? '<span style="margin-left:auto">' + ST.clip.mode + ': ' + ST.clip.paths.length + '</span>' : '');
  }
  function selectAll() { ST.sel = visible().map(function (e) { return e.path; }); paintSel(); }

  // ── open ─────────────────────────────────────────────────────────────────
  function openItem(path, isDir) {
    if (isDir) navigate(path, true);
    else API.openWith(path).then(function (d) { if (d && d.error) toast('Open', d.error, 'warn'); }).catch(function () { toast('Open', 'Could not open file', 'warn'); });
  }

  // ── file operations (canonical backend) ──────────────────────────────────
  function newFolder() {
    var base = joinp(ST.cwd, 'New Folder'); var name = 'New Folder', n = 1, target = base;
    var existing = {}; ST.entries.forEach(function (e) { existing[e.name] = 1; });
    while (existing[name]) { n++; name = 'New Folder ' + n; target = joinp(ST.cwd, name); }
    API.mkdir(target).then(function (d) {
      if (d && d.error) { toast('New Folder', d.error, 'warn'); return; }
      refreshThen(function () { startRename(target); });
    }).catch(function () { toast('New Folder', 'Failed', 'warn'); });
  }
  function refreshThen(cb) {
    API.browse(ST.cwd, ST.hidden).then(function (d) {
      if (d && !d.error) { ST.entries = (d.entries || []).slice(); applySort(); }
      render(false); if (cb) cb();
    });
  }
  function startRename(path) {
    var row = ST.root.querySelector('.hf-row[data-path="' + cssEsc(path) + '"] .hf-name, .hf-tile[data-path="' + cssEsc(path) + '"] .hf-name');
    if (!row) { selectOnly(path); return; }
    var old = basename(path);
    row.innerHTML = '<input class="hf-rename" value="' + esc(old) + '">';
    var inp = row.querySelector('.hf-rename'); inp.focus();
    var dot = old.lastIndexOf('.'); try { inp.setSelectionRange(0, dot > 0 ? dot : old.length); } catch (e) {}
    var done = false;
    function commit(save) {
      if (done) return; done = true;
      var nv = inp.value.trim();
      if (save && nv && nv !== old) {
        API.move(path, joinp(dirname(path), nv)).then(function (d) {
          if (d && d.error) toast('Rename', d.error, 'warn'); refresh();
        }).catch(function () { toast('Rename', 'Failed', 'warn'); refresh(); });
      } else { refresh(); }
    }
    inp.onkeydown = function (e) { if (e.key === 'Enter') { e.preventDefault(); commit(true); } else if (e.key === 'Escape') { e.preventDefault(); commit(false); } };
    inp.onblur = function () { commit(true); };
  }
  function cssEsc(s) { try { return (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/["\\]/g, '\\$&'); } catch (e) { return s; } }

  function delSelected() {
    if (!ST.sel.length) return;
    var paths = ST.sel.slice();
    confirmAct('Delete ' + paths.length + ' item' + (paths.length > 1 ? 's' : '') + '?', 'Moved to Trash where supported.', function () {
      var done = 0, errs = 0;
      paths.forEach(function (p) {
        API.del(p).then(function (d) { if (d && d.error) errs++; }).catch(function () { errs++; }).then(function () {
          if (++done === paths.length) { if (errs) toast('Delete', errs + ' failed', 'warn'); refresh(); }
        });
      });
    });
  }
  function clipboard(mode) { if (!ST.sel.length) return; ST.clip = { mode: mode, paths: ST.sel.slice() }; render(false); toast('Clipboard', ST.sel.length + ' ' + mode, 'info'); }
  function paste() {
    if (!ST.clip || !ST.clip.paths.length) return;
    var c = ST.clip, done = 0, errs = 0, n = c.paths.length;
    c.paths.forEach(function (src) {
      var dst = joinp(ST.cwd, basename(src));
      var op = c.mode === 'cut' ? API.move(src, dst) : API.copy(src, dst);
      op.then(function (d) { if (d && d.error) errs++; }).catch(function () { errs++; }).then(function () {
        if (++done === n) { if (c.mode === 'cut') ST.clip = null; if (errs) toast('Paste', errs + ' failed (name may exist)', 'warn'); refresh(); }
      });
    });
  }
  // POSIX-mode helpers for the permissions editor.
  function normOct(s) { s = String(s == null ? '' : s).replace(/[^0-7]/g, ''); return s.length >= 3 ? s.slice(-3) : null; }
  function permGridHtml(oct3) {
    var rows = [['Owner', 0], ['Group', 1], ['Other', 2]];
    var bits = ['r', 'w', 'x'];
    var h = '<div class="hf-perm-grid"><span></span><span class="ph">R</span><span class="ph">W</span><span class="ph">X</span>';
    rows.forEach(function (rw) {
      var digit = parseInt(oct3.charAt(rw[1]), 8);
      h += '<span class="rl">' + rw[0] + '</span>';
      for (var b = 0; b < 3; b++) {
        var mask = [4, 2, 1][b], on = (digit & mask) ? ' checked' : '';
        h += '<input type="checkbox" class="hf-perm-cb" data-grp="' + rw[1] + '" data-bit="' + bits[b] + '"' + on + '>';
      }
    });
    return h + '</div>';
  }
  function gridToOct(scope) {
    var o = [0, 0, 0];
    scope.querySelectorAll('.hf-perm-cb').forEach(function (cb) {
      if (!cb.checked) return;
      var g = +cb.getAttribute('data-grp'), bit = cb.getAttribute('data-bit');
      o[g] += bit === 'r' ? 4 : bit === 'w' ? 2 : 1;
    });
    return '' + o[0] + o[1] + o[2];
  }
  function properties(path) {
    API.info(path).then(function (d) {
      if (!d || d.error) { toast('Properties', (d && d.error) || 'Unavailable', 'warn'); return; }
      var oct3 = normOct(d.permissions);
      var permBlock = oct3
        ? ('<div class="hf-perm"><div class="hf-props-row" style="border-bottom:none;padding-bottom:0">' +
             '<b>Permissions</b><span class="hf-perm-oct" data-perm-oct>' + esc(oct3) + '</span></div>' +
             permGridHtml(oct3) +
             '<div style="display:flex;justify-content:flex-end"><button class="hf-perm-save" data-perm-save disabled>Apply</button></div></div>')
        : ('<div class="hf-props-row"><b>Permissions</b><span>' + esc(d.permissions || '-') + '</span></div>');
      var card = '<div class="hf-props"><div class="hf-props-card">' +
        '<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px"><span class="mi material-icons-round" style="font-size:34px;color:var(--hart-accent,#6c63ff)">' + iconFor(d) + '</span>' +
        '<div style="font-size:16px;font-weight:600;word-break:break-all">' + esc(d.name) + '</div></div>' +
        '<div class="hf-props-row"><b>Kind</b><span>' + esc(kind(d)) + '</span></div>' +
        '<div class="hf-props-row"><b>Size</b><span>' + (d.is_dir ? '-' : human(d.size)) + '</span></div>' +
        '<div class="hf-props-row"><b>Location</b><span style="word-break:break-all;text-align:right">' + esc(dirname(d.path)) + '</span></div>' +
        '<div class="hf-props-row"><b>Modified</b><span>' + fmtDate(d.modified) + '</span></div>' +
        '<div class="hf-props-row"><b>Created</b><span>' + fmtDate(d.created) + '</span></div>' +
        permBlock +
        '<button class="hf-tb-btn" data-close="1" style="width:100%;margin-top:16px;height:38px;gap:8px">Close</button>' +
        '</div></div>';
      var wrap = document.createElement('div'); wrap.innerHTML = card; var node = wrap.firstChild;
      ST.root.appendChild(node);
      function close() { try { ST.root.removeChild(node); } catch (e) {} }
      node.onclick = function (e) { if (e.target === node || e.target.getAttribute('data-close')) close(); };

      // Permissions editor wiring (only present when we have an octal mode).
      var octEl = node.querySelector('[data-perm-oct]');
      var saveBtn = node.querySelector('[data-perm-save]');
      if (octEl && saveBtn) {
        var orig = oct3;
        function refreshOct() {
          var cur = gridToOct(node);
          octEl.textContent = cur;
          if (cur === orig) saveBtn.setAttribute('disabled', ''); else saveBtn.removeAttribute('disabled');
        }
        node.querySelectorAll('.hf-perm-cb').forEach(function (cb) { cb.onchange = refreshOct; });
        saveBtn.onclick = function (ev) {
          ev.stopPropagation();
          var mode = gridToOct(node);
          saveBtn.setAttribute('disabled', '');
          API.chmod(d.path, mode).then(function (res) {
            if (res && res.error) { toast('Permissions', res.error, 'warn'); saveBtn.removeAttribute('disabled'); return; }
            orig = (res && res.mode) || mode; octEl.textContent = orig;
            toast('Permissions', 'Set to ' + orig, 'info');
            // keep dialog open; reflect any clamp (e.g. Windows) in the grid
            if (res && res.mode && res.mode !== mode) {
              node.querySelectorAll('.hf-perm-cb').forEach(function (cb) {
                var g = +cb.getAttribute('data-grp'), bit = cb.getAttribute('data-bit');
                var digit = parseInt(res.mode.charAt(g), 8), mask = bit === 'r' ? 4 : bit === 'w' ? 2 : 1;
                cb.checked = !!(digit & mask);
              });
            }
          }).catch(function () { toast('Permissions', 'Failed', 'warn'); saveBtn.removeAttribute('disabled'); });
        };
      }
    }).catch(function () { toast('Properties', 'Unavailable', 'warn'); });
  }
  function confirmAct(title, body, onYes) {
    if (window.dsConfirm) { try { window.dsConfirm(title, body, onYes); return; } catch (e) {} }
    if (window.confirm(title + (body ? '\n' + body : ''))) onYes();
  }

  // ── context menus ────────────────────────────────────────────────────────
  function closeCtx() { var m = document.getElementById('hf-ctx'); if (m) m.remove(); document.removeEventListener('click', closeCtx, true); }
  function showCtx(x, y, items) {
    closeCtx();
    var m = document.createElement('div'); m.className = 'hf-ctx'; m.id = 'hf-ctx';
    items.forEach(function (it) {
      if (it === '-') { var s = document.createElement('div'); s.className = 'hf-ctx-sep'; m.appendChild(s); return; }
      var e = document.createElement('div'); e.className = 'hf-ctx-item' + (it.danger ? ' danger' : ''); if (it.disabled) e.setAttribute('disabled', '');
      e.innerHTML = '<span class="mi material-icons-round">' + it.icon + '</span>' + esc(it.label);
      e.onclick = function (ev) { ev.stopPropagation(); closeCtx(); it.fn && it.fn(); };
      m.appendChild(e);
    });
    document.body.appendChild(m);
    var w = m.offsetWidth, h = m.offsetHeight;
    m.style.left = Math.min(x, window.innerWidth - w - 8) + 'px';
    m.style.top = Math.min(y, window.innerHeight - h - 8) + 'px';
    setTimeout(function () { document.addEventListener('click', closeCtx, true); }, 0);
  }
  function itemMenu(ev, path, isDir) {
    var multi = ST.sel.length > 1;
    showCtx(ev.clientX, ev.clientY, [
      { icon: isDir ? 'folder_open' : 'open_in_new', label: isDir ? 'Open' : 'Open', fn: function () { openItem(path, isDir); }, disabled: multi },
      '-',
      { icon: 'content_cut', label: 'Cut', fn: function () { clipboard('cut'); } },
      { icon: 'content_copy', label: 'Copy', fn: function () { clipboard('copy'); } },
      { icon: 'content_paste', label: 'Paste', fn: paste, disabled: !ST.clip },
      '-',
      { icon: 'drive_file_rename_outline', label: 'Rename', fn: function () { startRename(path); }, disabled: multi },
      { icon: 'delete', label: 'Delete', danger: true, fn: delSelected },
      '-',
      { icon: 'info', label: 'Properties', fn: function () { properties(path); }, disabled: multi }
    ]);
  }
  function emptyMenu(ev) {
    showCtx(ev.clientX, ev.clientY, [
      { icon: 'create_new_folder', label: 'New Folder', fn: newFolder },
      { icon: 'content_paste', label: 'Paste', fn: paste, disabled: !ST.clip },
      '-',
      { icon: 'select_all', label: 'Select All', fn: selectAll },
      { icon: 'refresh', label: 'Refresh', fn: refresh },
      { icon: ST.hidden ? 'visibility_off' : 'visibility', label: ST.hidden ? 'Hide Hidden Files' : 'Show Hidden Files', fn: function () { ST.hidden = !ST.hidden; refresh(); } }
    ]);
  }

  // ── keyboard ─────────────────────────────────────────────────────────────
  function bindKeys(root) {
    root.addEventListener('keydown', function (e) {
      if (e.target && e.target.classList && (e.target.classList.contains('hf-rename') || e.target.classList.contains('hf-search'))) {
        if (e.key === 'Escape' && e.target.classList.contains('hf-search')) { ST.filter = ''; ST.searchResults = null; ST.searchTrunc = false; render(false); }
        return;
      }
      var mod = e.ctrlKey || e.metaKey;
      if (e.altKey && e.key === 'ArrowLeft') { e.preventDefault(); back(); }
      else if (e.altKey && e.key === 'ArrowRight') { e.preventDefault(); forward(); }
      else if (e.altKey && e.key === 'ArrowUp') { e.preventDefault(); up(); }
      else if (e.key === 'F5') { e.preventDefault(); refresh(); }
      else if (e.key === 'F2') { e.preventDefault(); if (ST.sel.length === 1) startRename(ST.sel[0]); }
      else if (e.key === 'Delete') { e.preventDefault(); delSelected(); }
      else if (mod && (e.key === 'a' || e.key === 'A')) { e.preventDefault(); selectAll(); }
      else if (mod && (e.key === 'c' || e.key === 'C')) { e.preventDefault(); clipboard('copy'); }
      else if (mod && (e.key === 'x' || e.key === 'X')) { e.preventDefault(); clipboard('cut'); }
      else if (mod && (e.key === 'v' || e.key === 'V')) { e.preventDefault(); paste(); }
      else if (mod && e.shiftKey && (e.key === 'N' || e.key === 'n')) { e.preventDefault(); newFolder(); }
      else if (e.key === 'Enter') { if (ST.sel.length === 1) { var en = byPath(ST.sel[0]); if (en) openItem(en.path, en.is_dir); } }
      else if (e.key === 'Backspace') { e.preventDefault(); up(); }
      else if (e.key === 'Escape') { ST.sel = []; paintSel(); }
    });
  }

  window.HartFiles = { mount: mount, navigate: function (p) { navigate(p, true); }, refresh: refresh };
})();
