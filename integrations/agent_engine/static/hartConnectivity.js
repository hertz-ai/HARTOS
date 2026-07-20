/*
 * hartConnectivity.js — OS connectivity cluster (wifi / bluetooth / battery /
 * volume) for the HART OS glass shell top-bar.
 *
 * WHAT: a top-bar indicator cluster (material-icons-round glyphs that reflect
 * LIVE polled state, like the system widget) + a quick-settings popover (toggle
 * wifi/bt, pick a wifi network, a volume slider, battery %). It works on EVERY
 * tier including the cage floor because every backend probe degrades to a clean
 * {available:false} when the tool/hardware is absent (the live USB may have
 * none of nmcli / bluetoothctl / wpctl / upower) — an indicator with no data
 * shows a NEUTRAL "unknown" glyph, never an error.
 *
 * DRY: the cluster READS one consolidated endpoint
 * (/api/shell/connectivity/summary, served same-process on SHELL=:6800) and the
 * quick-settings ACTIONS reuse the EXISTING per-domain endpoints
 * (/api/shell/network/wifi[/connect], /api/shell/wifi/toggle,
 * /api/shell/bluetooth[/power|/connect], /api/shell/battery,
 * /api/shell/volume[/mute]) — it is NOT a parallel control path. It reuses the
 * shell's HartSession / showToast / HartTimeoutSignal / hartStates helpers and
 * matches the glass styling of the existing .tray-btn buttons.
 *
 * Classic IIFE script for OLD WebKitGTK: NO template literals, NO optional
 * chaining, NO nullish coalescing. var/function + string concatenation +
 * explicit checks only. Loaded deferred AFTER the inline shell config (so
 * SHELL / showToast / _sig exist by run time, accessed defensively).
 */
(function () {
  'use strict';

  // ── Defensive access to function-scoped shell globals (NOT on window) ──
  function S() {
    try { if (typeof SHELL !== 'undefined' && SHELL) return SHELL; } catch (e) { console.debug('hartConnectivity: SHELL global probe failed', e); }
    return (window.SHELL || '');
  }
  function sig(ms) {
    try { if (window._sig) return window._sig(ms); } catch (e) { console.debug('hartConnectivity: window._sig probe failed', e); }
    if (window.HartTimeoutSignal) return window.HartTimeoutSignal(ms);
    try {
      if (typeof AbortController === 'undefined') return null;
      var c = new AbortController();
      setTimeout(function () { try { c.abort(); } catch (x) { console.debug('hartConnectivity: abort on timeout failed', x); } }, ms);
      return c.signal;
    } catch (e) { console.debug('hartConnectivity: AbortController unavailable', e); return null; }
  }
  function toast(title, msg, sev) {
    try { if (window.showToast) window.showToast(title, msg, sev || 'info'); } catch (e) { console.debug('hartConnectivity: showToast failed', e); }
  }
  function esc(s) {
    var d = document.createElement('div');
    d.textContent = (s == null) ? '' : String(s);
    return d.innerHTML;
  }
  function getJSON(url, ms) {
    return fetch(url, { signal: sig(ms || 5000) }).then(function (r) {
      if (!r.ok) throw new Error('http ' + r.status);
      return r.json();
    });
  }
  function postJSON(url, body, ms) {
    return fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}), signal: sig(ms || 8000)
    }).then(function (r) { return r.json().catch(function () { return {}; }); });
  }

  // ── Glyph resolvers (neutral 'unknown' when no live data) ──
  function wifiGlyph(w) {
    if (!w || !w.available) return 'wifi_off';            // tool absent -> neutral
    if (!w.enabled) return 'wifi_off';
    if (!w.connected) return 'wifi_find';                 // radio on, not joined
    var sgl = (typeof w.signal === 'number') ? w.signal : 100;
    if (sgl >= 75) return 'wifi';
    if (sgl >= 50) return 'network_wifi_3_bar';
    if (sgl >= 25) return 'network_wifi_2_bar';
    return 'network_wifi_1_bar';
  }
  function btGlyph(b) {
    if (!b || !b.available) return 'bluetooth_disabled';  // adapter absent
    if (!b.powered) return 'bluetooth_disabled';
    if (b.connected_count > 0) return 'bluetooth_connected';
    return 'bluetooth';
  }
  function batGlyph(bat) {
    if (!bat || !bat.available || typeof bat.percent !== 'number') return 'battery_unknown';
    var charging = (bat.state === 'charging' || bat.state === 'full' || bat.plugged_in);
    if (charging) return 'battery_charging_full';
    var p = bat.percent;
    if (p > 90) return 'battery_full';
    if (p > 70) return 'battery_6_bar';
    if (p > 50) return 'battery_4_bar';
    if (p > 30) return 'battery_3_bar';
    if (p > 15) return 'battery_2_bar';
    return 'battery_alert';
  }
  function volGlyph(v) {
    if (!v || !v.available || typeof v.volume !== 'number') return 'volume_up';
    if (v.muted || v.volume === 0) return 'volume_off';
    if (v.volume < 40) return 'volume_down';
    return 'volume_up';
  }

  // ── Indicator cluster markup ──
  function indicator(id, glyph, label) {
    return '<button type="button" class="tray-btn hc-ind" id="' + id + '" ' +
      'aria-label="' + esc(label) + '" title="' + esc(label) + '">' +
      '<span class="mi material-icons-round" aria-hidden="true">' + glyph + '</span>' +
      '</button>';
  }

  var STATE = null, pollTimer = null, mounted = false;
  // In-flight guards: at most ONE summary probe and ONE wifi scan outstanding at
  // a time. Both endpoints shell out to nmcli/bluetoothctl/wpctl server-side and
  // can take several seconds on a software-rendered box; the 8s poller, the
  // popover-open, the toggle callbacks and every Rescan all re-trigger them, so
  // without a guard a slow probe pile-up saturates the small shell thread pool and
  // freezes every other shell fetch. Aborting the client fetch does NOT cancel the
  // server subprocess, so the only safe lever here is: never stack a new request
  // on a pending one.
  var _refreshing = false;
  var _loadingNetworks = false;

  function setGlyph(id, glyph) {
    var el = document.getElementById(id);
    if (!el) return;
    var ic = el.querySelector('.mi');
    if (ic) ic.textContent = glyph;
  }

  function paint() {
    if (!STATE) return;
    setGlyph('hc-wifi', wifiGlyph(STATE.wifi));
    setGlyph('hc-bt', btGlyph(STATE.bluetooth));
    setGlyph('hc-bat', batGlyph(STATE.battery));
    setGlyph('hc-vol', volGlyph(STATE.volume));
    var bat = STATE.battery;
    var pct = document.getElementById('hc-bat-pct');
    if (pct) pct.textContent = (bat && bat.available && typeof bat.percent === 'number') ? (bat.percent + '%') : '';
    // Reflect connection on the cluster (mirrors the SYSTEM widget's muted look
    // when nothing is live) — purely cosmetic, never an error spew.
    var cluster = document.getElementById('hc-cluster');
    if (cluster) {
      var anyLive = (STATE.wifi && STATE.wifi.available) ||
                    (STATE.bluetooth && STATE.bluetooth.available) ||
                    (STATE.battery && STATE.battery.available) ||
                    (STATE.volume && STATE.volume.available);
      cluster.classList.toggle('hc-dim', !anyLive);
    }
    if (popoverOpen()) renderPopover();   // keep the open popover in sync
  }

  function refresh() {
    if (_refreshing) return;   // coalesce onto the pending probe (never pile up)
    _refreshing = true;
    getJSON(S() + '/api/shell/connectivity/summary', 5000)
      .then(function (d) { STATE = d || {}; paint(); })
      .catch(function (e) { /* keep last glyphs; degrade to neutral on first fail */
        console.debug('hartConnectivity: connectivity summary probe failed', e);
        if (!STATE) { STATE = {}; paint(); }
      })
      .then(function () { _refreshing = false; });   // release in BOTH outcomes
  }

  // ── Quick-settings popover ──
  function popoverOpen() {
    var p = document.getElementById('hc-popover');
    return !!(p && p.classList.contains('open'));
  }
  function ensurePopover() {
    var p = document.getElementById('hc-popover');
    if (p) return p;
    p = document.createElement('div');
    p.id = 'hc-popover';
    p.className = 'hc-popover glass';
    p.setAttribute('role', 'dialog');
    p.setAttribute('aria-label', 'Quick settings');
    document.body.appendChild(p);
    return p;
  }

  function toggleWifi() {
    var w = STATE && STATE.wifi;
    var next = !(w && w.enabled);
    postJSON(S() + '/api/shell/wifi/toggle', { enable: next })
      .then(function () { toast('Wi-Fi', next ? 'On' : 'Off', 'success'); setTimeout(refresh, 600); })
      .catch(function (e) { console.error('hartConnectivity: wifi toggle POST failed', e); toast('Wi-Fi', 'Not available', 'warning'); });
  }
  function toggleBt() {
    var b = STATE && STATE.bluetooth;
    var next = !(b && b.powered);
    postJSON(S() + '/api/shell/bluetooth/power', { powered: next })
      .then(function () { toast('Bluetooth', next ? 'On' : 'Off', 'success'); setTimeout(refresh, 600); })
      .catch(function (e) { console.error('hartConnectivity: bluetooth power POST failed', e); toast('Bluetooth', 'Not available', 'warning'); });
  }
  function setVol(v) {
    postJSON(S() + '/api/shell/volume', { volume: v })
      .then(function () { if (STATE && STATE.volume) { STATE.volume.volume = v; STATE.volume.muted = (v === 0); } paint(); })
      .catch(function (e) { console.error('hartConnectivity: set volume POST failed', e); });
  }
  function toggleVolMute() {
    postJSON(S() + '/api/shell/volume/mute', {})
      .then(function (d) { if (d && d.available) STATE.volume = d; paint(); })
      .catch(function (e) { console.error('hartConnectivity: volume mute toggle POST failed', e); });
  }
  // Resolve the shell's in-glass prompt modal (defined in the inline shell script).
  // Same defensive pattern as S()/sig(): it may be a global function decl OR on window.
  function getPrompt() {
    try { if (typeof dsPrompt !== 'undefined' && dsPrompt) return dsPrompt; } catch (e) { console.debug('hartConnectivity: dsPrompt global probe failed', e); }
    if (window.dsPrompt) return window.dsPrompt;
    return null;
  }
  function doConnect(ssid, pw) {
    toast('Wi-Fi', 'Connecting to ' + ssid + '…', 'info');
    postJSON(S() + '/api/shell/network/wifi/connect', { ssid: ssid, password: pw }, 32000)
      .then(function (d) {
        if (d && (d.success || d.connected)) { toast('Wi-Fi', 'Connected to ' + ssid, 'success'); }
        else { toast('Wi-Fi', (d && d.error) ? d.error : 'Could not connect', 'warning'); }
        setTimeout(refresh, 800);
      })
      .catch(function (e) { console.error('hartConnectivity: wifi connect POST failed', e); toast('Wi-Fi', 'Could not connect', 'warning'); });
  }
  function connectWifi(ssid, secured) {
    if (!secured) { doConnect(ssid, ''); return; }
    // Secured network: ask for the password via the in-shell GLASS modal (dsPrompt),
    // NOT window.prompt — the embedded WebKitGTK layer-shell surface frequently no-ops
    // window.prompt (the embedder must wire WebKitScriptDialog), so a native prompt can
    // silently return null and the connect never fires. Native prompt is last-resort only.
    var prompter = getPrompt();
    if (prompter) {
      prompter('Connect to ' + ssid, 'Enter the Wi-Fi password',
        { type: 'password', okLabel: 'Connect' })
        .then(function (pw) { if (pw !== null && pw !== undefined) doConnect(ssid, pw); });
      return;
    }
    var fallback = (window.prompt) ? window.prompt('Password for "' + ssid + '"', '') : '';
    if (fallback === null) return;   // cancelled
    doConnect(ssid, fallback || '');
  }

  function renderPopover() {
    var p = document.getElementById('hc-popover');
    if (!p) return;
    var w = (STATE && STATE.wifi) || {};
    var b = (STATE && STATE.bluetooth) || {};
    var bat = (STATE && STATE.battery) || {};
    var vol = (STATE && STATE.volume) || {};

    var wifiOn = !!(w.available && w.enabled);
    var btOn = !!(b.available && b.powered);

    var html = '<div class="hc-pop-title">Quick settings</div>';

    // Toggles row (wifi + bluetooth)
    html += '<div class="hc-toggles">';
    html += '<button type="button" class="hc-toggle' + (wifiOn ? ' on' : '') +
      (w.available ? '' : ' hc-na') + '" id="hc-tg-wifi">' +
      '<span class="mi material-icons-round">' + wifiGlyph(w) + '</span>' +
      '<span class="hc-toggle-lbl">' + (w.available ? (wifiOn ? (w.connected && w.ssid ? esc(w.ssid) : 'Wi-Fi on') : 'Wi-Fi off') : 'No Wi-Fi') + '</span>' +
      '</button>';
    html += '<button type="button" class="hc-toggle' + (btOn ? ' on' : '') +
      (b.available ? '' : ' hc-na') + '" id="hc-tg-bt">' +
      '<span class="mi material-icons-round">' + btGlyph(b) + '</span>' +
      '<span class="hc-toggle-lbl">' + (b.available ? (btOn ? (b.connected_count > 0 ? (b.connected_count + ' connected') : 'Bluetooth on') : 'Bluetooth off') : 'No Bluetooth') + '</span>' +
      '</button>';
    html += '</div>';

    // Volume slider
    var vpct = (typeof vol.volume === 'number') ? vol.volume : 0;
    html += '<div class="hc-row hc-vol-row">' +
      '<button type="button" class="hc-mini" id="hc-mute" aria-label="Mute"' +
      (vol.available ? '' : ' disabled') + '>' +
      '<span class="mi material-icons-round">' + volGlyph(vol) + '</span></button>' +
      '<input type="range" min="0" max="100" value="' + vpct + '" id="hc-vol-slider" ' +
      'class="hc-slider" aria-label="Volume"' + (vol.available ? '' : ' disabled') + '>' +
      '<span class="hc-vol-pct">' + (vol.available ? (vpct + '%') : '--') + '</span>' +
      '</div>';

    // Battery line
    html += '<div class="hc-row hc-bat-row">' +
      '<span class="mi material-icons-round">' + batGlyph(bat) + '</span>' +
      '<span class="hc-bat-text">' +
      (bat.available && typeof bat.percent === 'number'
        ? (bat.percent + '% · ' + (bat.state === 'charging' ? 'Charging' : (bat.state === 'full' ? 'Full' : 'On battery')))
        : 'No battery') +
      '</span></div>';

    // Wi-Fi network list (only when radio is on)
    html += '<div class="hc-net-head"><span>Networks</span>' +
      '<button type="button" class="hc-mini" id="hc-net-rescan" aria-label="Rescan">' +
      '<span class="mi material-icons-round">refresh</span></button></div>';
    html += '<div class="hc-net-list" id="hc-net-list"></div>';

    p.innerHTML = html;

    var tw = document.getElementById('hc-tg-wifi'); if (tw) tw.addEventListener('click', toggleWifi);
    var tb = document.getElementById('hc-tg-bt'); if (tb) tb.addEventListener('click', toggleBt);
    var mu = document.getElementById('hc-mute'); if (mu) mu.addEventListener('click', toggleVolMute);
    var sl = document.getElementById('hc-vol-slider');
    if (sl) {
      sl.addEventListener('input', function () {
        var pc = document.querySelector('#hc-popover .hc-vol-pct');
        if (pc) pc.textContent = this.value + '%';
      });
      sl.addEventListener('change', function () { setVol(parseInt(this.value, 10) || 0); });
    }
    var rs = document.getElementById('hc-net-rescan');
    if (rs) rs.addEventListener('click', function () { loadNetworks(true); });

    loadNetworks(false);
  }

  function loadNetworks(rescan) {
    var box = document.getElementById('hc-net-list');
    if (!box) return;
    var w = (STATE && STATE.wifi) || {};
    if (!w.available) {
      box.innerHTML = '<div class="hc-net-empty">Wi-Fi hardware not detected.</div>';
      return;
    }
    // rfkill state: distinguish a BLOCK (chip present, radio off) from no-hardware
    // above, so a soft/hard block is never mis-read as a missing chip.
    if (w.blocked === 'hard') {
      box.innerHTML = '<div class="hc-net-empty">Wi-Fi is blocked by a hardware switch. Use the physical Wi-Fi / airplane switch (or Fn key) to enable the radio.</div>';
      return;
    }
    if (w.blocked === 'soft') {
      box.innerHTML = '<div class="hc-net-empty">Wi-Fi is soft-blocked (airplane mode). Turn it on to see networks.</div>';
      return;
    }
    if (!w.enabled) {
      box.innerHTML = '<div class="hc-net-empty">Wi-Fi is off. Turn it on to see networks.</div>';
      return;
    }
    box.innerHTML = '<div class="hc-net-empty">Scanning…</div>';
    // Coalesce onto a pending scan: the popover re-renders on every poll and on
    // every Rescan, each calling loadNetworks. The spinner above still shows, but
    // we do NOT stack a second /network/wifi fetch (which shells out to nmcli and
    // can take seconds) on top of one already in flight.
    if (_loadingNetworks) return;
    _loadingNetworks = true;
    getJSON(S() + '/api/shell/network/wifi', rescan ? 9000 : 6000)
      .then(function (d) {
        var nets = (d && d.networks) || [];
        if (nets.length === 0) {
          box.innerHTML = '<div class="hc-net-empty">No networks found.</div>';
          return;
        }
        var rows = '';
        for (var i = 0; i < nets.length && i < 12; i++) {
          var n = nets[i];
          var secured = !!(n.security && n.security.replace(/\s/g, '') !== '' && n.security !== '--');
          var sg = (typeof n.signal === 'number') ? n.signal : 0;
          var bars = sg >= 75 ? 'wifi' : sg >= 50 ? 'network_wifi_3_bar' : sg >= 25 ? 'network_wifi_2_bar' : 'network_wifi_1_bar';
          rows += '<button type="button" class="hc-net-item' + (n.active ? ' active' : '') +
            '" data-ssid="' + esc(n.ssid) + '" data-sec="' + (secured ? '1' : '0') + '">' +
            '<span class="mi material-icons-round hc-net-bars">' + bars + '</span>' +
            '<span class="hc-net-ssid">' + esc(n.ssid) + '</span>' +
            (secured ? '<span class="mi material-icons-round hc-net-lock">lock</span>' : '') +
            (n.active ? '<span class="mi material-icons-round hc-net-chk">check</span>' : '') +
            '</button>';
        }
        box.innerHTML = rows;
        var items = box.querySelectorAll('.hc-net-item');
        for (var j = 0; j < items.length; j++) {
          items[j].addEventListener('click', function () {
            if (this.classList.contains('active')) return;   // already joined
            connectWifi(this.getAttribute('data-ssid'), this.getAttribute('data-sec') === '1');
          });
        }
      })
      .catch(function (e) {
        console.debug('hartConnectivity: wifi scan failed', e);
        box.innerHTML = '<div class="hc-net-empty">Could not scan right now.</div>';
      })
      .then(function () { _loadingNetworks = false; });   // release in BOTH outcomes
  }

  function positionPopover() {
    var p = document.getElementById('hc-popover');
    var anchor = document.getElementById('hc-cluster');
    if (!p || !anchor) return;
    var r = anchor.getBoundingClientRect();
    var top = r.bottom + 8;
    // right-align under the cluster, clamped to viewport
    var right = Math.max(8, window.innerWidth - r.right);
    p.style.top = top + 'px';
    p.style.right = right + 'px';
    p.style.left = 'auto';
  }

  function openPopover() {
    var p = ensurePopover();
    renderPopover();
    p.classList.add('open');
    positionPopover();
    refresh();   // freshen the data the moment it opens
    setTimeout(function () { document.addEventListener('mousedown', onOutside, true); }, 0);
  }
  function closePopover() {
    var p = document.getElementById('hc-popover');
    if (p) p.classList.remove('open');
    document.removeEventListener('mousedown', onOutside, true);
  }
  function onOutside(e) {
    var p = document.getElementById('hc-popover');
    var cluster = document.getElementById('hc-cluster');
    if (!p) return;
    if (p.contains(e.target)) return;
    if (cluster && cluster.contains(e.target)) return;
    closePopover();
  }
  function togglePopover() {
    if (popoverOpen()) closePopover(); else openPopover();
  }

  function injectStyle() {
    if (document.getElementById('hc-style')) return;
    var css =
      '#hc-cluster{display:inline-flex;align-items:center;gap:2px}' +
      '#hc-cluster.hc-dim{opacity:.55;filter:grayscale(.4)}' +
      '.hc-ind{width:30px}' +
      '#hc-bat-pct{font-size:10px;font-weight:600;color:var(--hart-muted);' +
      'padding:0 4px 0 1px;font-variant-numeric:tabular-nums;align-self:center;min-width:0}' +
      '.hc-popover{position:fixed;z-index:1200;width:300px;max-width:calc(100vw - 16px);' +
      'padding:12px;border-radius:14px;display:none;' +
      'border:1px solid var(--hart-glass-border,rgba(255,255,255,.12));' +
      'box-shadow:0 12px 40px rgba(0,0,0,.45);max-height:calc(100vh - 64px);overflow:auto}' +
      '.hc-popover.open{display:block}' +
      '.hc-pop-title{font-size:13px;font-weight:600;color:var(--hart-text);margin-bottom:10px}' +
      '.hc-toggles{display:flex;gap:8px;margin-bottom:10px}' +
      '.hc-toggle{flex:1;display:flex;flex-direction:column;align-items:flex-start;gap:4px;' +
      'padding:10px;border-radius:10px;cursor:pointer;text-align:left;' +
      'background:var(--hart-surface,rgba(255,255,255,.05));' +
      'border:1px solid transparent;color:var(--hart-muted);' +
      'transition:background var(--hart-anim-speed,.2s),border-color var(--hart-anim-speed,.2s)}' +
      '.hc-toggle:hover{background:var(--hart-surface-hover,rgba(255,255,255,.09))}' +
      '.hc-toggle.on{background:var(--hart-accent,#3b82f6);color:#fff;border-color:transparent}' +
      '.hc-toggle.on .hc-toggle-lbl{color:rgba(255,255,255,.92)}' +
      '.hc-toggle.hc-na{opacity:.5;cursor:default}' +
      '.hc-toggle .mi{font-size:22px}' +
      '.hc-toggle-lbl{font-size:11px;font-weight:500;white-space:nowrap;overflow:hidden;' +
      'text-overflow:ellipsis;max-width:100%}' +
      '.hc-row{display:flex;align-items:center;gap:8px;padding:6px 2px}' +
      '.hc-row .mi{font-size:20px;color:var(--hart-muted)}' +
      '.hc-mini{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;' +
      'border-radius:8px;cursor:pointer;background:transparent;border:0;color:var(--hart-muted)}' +
      '.hc-mini:hover{background:var(--hart-surface-hover,rgba(255,255,255,.09))}' +
      '.hc-mini[disabled]{opacity:.4;cursor:default}' +
      '.hc-slider{flex:1;accent-color:var(--hart-accent,#3b82f6);height:4px}' +
      '.hc-slider[disabled]{opacity:.4}' +
      '.hc-vol-pct{font-size:11px;color:var(--hart-muted);min-width:34px;text-align:right;' +
      'font-variant-numeric:tabular-nums}' +
      '.hc-bat-text{font-size:12px;color:var(--hart-text)}' +
      '.hc-net-head{display:flex;align-items:center;justify-content:space-between;' +
      'margin:10px 0 4px;font-size:11px;font-weight:600;text-transform:uppercase;' +
      'letter-spacing:.04em;color:var(--hart-muted)}' +
      '.hc-net-list{display:flex;flex-direction:column;gap:2px;max-height:200px;overflow:auto}' +
      '.hc-net-item{display:flex;align-items:center;gap:8px;padding:8px;border-radius:8px;' +
      'cursor:pointer;background:transparent;border:0;color:var(--hart-text);text-align:left;width:100%}' +
      '.hc-net-item:hover{background:var(--hart-surface-hover,rgba(255,255,255,.09))}' +
      '.hc-net-item.active{background:var(--hart-surface,rgba(255,255,255,.06))}' +
      '.hc-net-bars{font-size:18px;color:var(--hart-muted)}' +
      '.hc-net-ssid{flex:1;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
      '.hc-net-lock{font-size:14px;color:var(--hart-muted)}' +
      '.hc-net-chk{font-size:16px;color:var(--hart-active,#22c55e)}' +
      '.hc-net-empty{font-size:12px;color:var(--hart-muted);padding:8px 2px}';
    var st = document.createElement('style');
    st.id = 'hc-style';
    st.textContent = css;
    document.head.appendChild(st);
  }

  function mount() {
    if (mounted) return;
    var tray = document.querySelector('.top-bar-right');
    if (!tray) { return setTimeout(mount, 400); }   // top-bar not built yet
    injectStyle();
    var cluster = document.createElement('div');
    cluster.id = 'hc-cluster';
    cluster.className = 'hc-dim';   // neutral until first poll resolves
    cluster.innerHTML =
      indicator('hc-wifi', 'wifi_off', 'Wi-Fi') +
      indicator('hc-bt', 'bluetooth_disabled', 'Bluetooth') +
      indicator('hc-vol', 'volume_up', 'Volume') +
      indicator('hc-bat', 'battery_unknown', 'Battery') +
      '<span id="hc-bat-pct"></span>';
    // Insert before the clock so the cluster sits in the tray like a real OS.
    var clock = tray.querySelector('.clock');
    if (clock) tray.insertBefore(cluster, clock); else tray.appendChild(cluster);

    // The whole cluster opens the quick-settings popover (any indicator click).
    cluster.addEventListener('click', function (e) {
      e.stopPropagation();
      togglePopover();
    });
    mounted = true;

    refresh();
    pollTimer = setInterval(refresh, 8000);   // keep the indicators live
    window.addEventListener('resize', function () { if (popoverOpen()) positionPopover(); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && popoverOpen()) closePopover();
    });
  }

  // Expose a tiny handle for other shell code / tests (no behaviour leaks).
  window.HartConnectivity = { refresh: refresh, open: openPopover, close: closePopover };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
})();
