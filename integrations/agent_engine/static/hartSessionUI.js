/*
 * hartSessionUI.js — lock screen, password, and live desktop widgets.
 *
 * Turns the bare lock-screen stub into a real one (wallpaper + live clock +
 * password unlock), adds a first-run password SETUP after the "Light Your HART"
 * onboarding, and renders live desktop widgets (clock + system) — the
 * installed-OS session experience.
 *
 * Reuses the shell's own primitives — no parallel path:
 *   window.HartSession      — the single state blob (stores lock_pw_hash, salt)
 *   window.HartTimeoutSignal — WebKitGTK-safe fetch timeout
 *   #lock-screen / #lock-pw  — the existing lock overlay (we drive it)
 *   /api/shell/system/metrics — the existing CPU/RAM/disk source
 *
 * The lock is a UX lock (per-user shell state), NOT the OS security boundary —
 * that remains the display-manager login. Password is stored only as a salted
 * SHA-256 hash via SubtleCrypto (localhost is a secure context); a weak
 * fallback keeps it from crashing if subtle crypto is unavailable.
 *
 * Plain classic script, loaded after the inline shell script + hartSession.js.
 */
(function () {
  'use strict';
  function $(id) { return document.getElementById(id); }
  function ts(ms) { return window.HartTimeoutSignal ? window.HartTimeoutSignal(ms) : null; }

  // ── password hashing (salted SHA-256; weak fallback) ──────────────────────
  function _weakHash(s) {
    var h = 5381;
    for (var i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
    return 'w' + (h >>> 0).toString(16);
  }
  function hashPw(pw, salt) {
    try {
      if (window.crypto && crypto.subtle) {
        var data = new TextEncoder().encode(salt + ':' + pw);
        return crypto.subtle.digest('SHA-256', data).then(function (buf) {
          return Array.prototype.map.call(new Uint8Array(buf),
            function (b) { return ('0' + b.toString(16)).slice(-2); }).join('');
        });
      }
    } catch (e) {}
    return Promise.resolve(_weakHash(salt + ':' + pw));
  }
  function _salt() {
    var s = window.HartSession && window.HartSession.get('lock_salt');
    if (!s) {
      s = (Math.floor(performance.now()) ^ 0x9e3779b9).toString(16) + 'h';
      if (window.HartSession) window.HartSession.set('lock_salt', s);
    }
    return s;
  }
  function storedHash() { return window.HartSession ? window.HartSession.get('lock_pw_hash') : null; }

  window.HartLock = {
    hasPassword: function () { return !!storedHash(); },
    setPassword: function (pw) {
      return hashPw(pw, _salt()).then(function (h) {
        if (window.HartSession) window.HartSession.set('lock_pw_hash', h);
        return true;
      });
    },
    check: function (pw) {
      var want = storedHash();
      if (!want) return Promise.resolve(true);          // no password set => open
      return hashPw(pw, _salt()).then(function (h) { return h === want; });
    }
  };

  // ── live clock (lock screen + desktop widget) ─────────────────────────────
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function tick() {
    var d = new Date();
    var hh = d.getHours(), mm = pad(d.getMinutes());
    var time12 = ((hh % 12) || 12) + ':' + mm + ' ' + (hh < 12 ? 'AM' : 'PM');
    var date = d.toLocaleDateString(undefined,
      { weekday: 'long', month: 'long', day: 'numeric' });
    [['lock-clock', time12], ['lock-date', date],
     ['hw-clock-time', time12], ['hw-clock-date', date]].forEach(function (p) {
      var el = $(p[0]); if (el) el.textContent = p[1];
    });
  }

  // ── lock / unlock (drives the existing #lock-screen) ──────────────────────
  function showLock(setup) {
    var ls = $('lock-screen'); if (!ls) return;
    ls.classList.toggle('setup', !!setup);
    var st = $('lock-status');
    if (st) st.textContent = setup
      ? 'Create a password to lock your HART'
      : (window.HartLock.hasPassword() ? '' : 'No password set — press Enter to enter');
    var pw = $('lock-pw');
    if (pw) { pw.value = ''; pw.placeholder = setup ? 'New password' : 'Password'; }
    ls.classList.add('active');
    if (pw) setTimeout(function () { pw.focus(); }, 50);
  }
  window.HartLock.show = function () { showLock(false); };
  window.HartLock.promptSetup = function () { showLock(true); };

  function onEnter() {
    var ls = $('lock-screen'), pw = $('lock-pw'), st = $('lock-status');
    if (!ls || !pw) return;
    var val = pw.value || '';
    if (ls.classList.contains('setup')) {
      if (val.length < 4) { if (st) st.textContent = 'Use at least 4 characters'; return; }
      window.HartLock.setPassword(val).then(function () {
        ls.classList.remove('setup', 'active');
      });
      return;
    }
    window.HartLock.check(val).then(function (ok) {
      if (ok) { ls.classList.remove('active'); }
      else { if (st) st.textContent = 'Incorrect password'; pw.value = ''; pw.focus(); }
    });
  }
  // Replace the inline stub `unlock()` (which accepted any password) with the
  // real check — same function name, so the existing onkeydown keeps working.
  window.unlock = onEnter;

  // ── desktop widgets (clock + live system) ─────────────────────────────────
  var SHELL = (typeof window.SHELL === 'string') ? window.SHELL : '';
  function bar(pct, label) {
    return '<div class="hw-row"><span>' + label + '</span><span class="hw-val" data-k="' + label +
      '">' + Math.round(pct) + '%</span></div><div class="hw-bar"><i style="width:' +
      Math.max(0, Math.min(100, pct)) + '%"></i></div>';
  }
  function pollMetrics() {
    fetch(SHELL + '/api/shell/system/metrics', { signal: ts(4000) })
      .then(function (r) { return r.json(); })
      .then(function (m) {
        // Flat keys, matching the Task Manager panel: cpu_percent / memory_percent;
        // disk via disk_percent or the first entry of a disks[] array.
        var cpu = m.cpu_percent || 0;
        var mem = m.memory_percent || 0;
        var disk = m.disk_percent;
        if (disk == null && m.disks && m.disks.length) disk = m.disks[0].percent;
        var el = $('hw-sys-body');
        if (el) el.innerHTML = bar(cpu, 'CPU') + bar(mem, 'Memory') + bar(disk || 0, 'Disk');
      }).catch(function () {});
  }

  function mountWidgets() {
    var host = $('hart-widgets');
    if (!host) return;
    host.innerHTML =
      '<div class="hart-widget hw-clock" role="group" aria-label="Clock">' +
      '  <div class="hw-clock-time" id="hw-clock-time">--:--</div>' +
      '  <div class="hw-clock-date" id="hw-clock-date"></div>' +
      '</div>' +
      '<div class="hart-widget hw-sys" role="group" aria-label="System">' +
      '  <div class="hw-title">System</div>' +
      '  <div id="hw-sys-body"><div class="hw-row"><span>loading…</span></div></div>' +
      '</div>';
    pollMetrics();
    setInterval(pollMetrics, 4000);
  }

  // ── boot ──────────────────────────────────────────────────────────────────
  function start() {
    tick(); setInterval(tick, 1000);
    mountWidgets();
    // First-run: if onboarding is already done but no lock password exists yet,
    // offer to set one. hartOnboarding.js removes .onboarding-active when it
    // finishes; we wait for that, then prompt once.
    var prompted = false;
    function maybeSetup() {
      if (prompted) return;
      var onboarding = document.documentElement.classList.contains('onboarding-active');
      if (!onboarding && window.HartSession && !window.HartLock.hasPassword()
          && !window.HartSession.get('lock_setup_skipped')) {
        prompted = true;
        if (window.HartSession) window.HartSession.set('lock_setup_skipped', true);
        window.HartLock.promptSetup();
      }
    }
    if (window.HartSession) window.HartSession.ready(function () { setTimeout(maybeSetup, 4000); });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
