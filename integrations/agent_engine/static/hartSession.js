/*
 * hartSession.js — ONE shared accessor for /api/shell/session-state.
 *
 * Multiple shell modules persist user state into the same single JSON blob
 * (desktop icons, wallpaper choice, …). If each kept its own cached copy and
 * POSTed the whole blob, the last writer would clobber the others' keys. This
 * is the single source: one in-memory blob loaded once, mutated by key, and
 * saved (whole-blob, debounced) — so writers never stomp each other.
 *
 * Must load BEFORE hartDesktop.js / hartPersonalize.js. Plain classic script.
 */
(function () {
  'use strict';
  var API = '/api/shell/session-state';   // same-origin -> relative is correct
  var blob = null, loaded = false, waiters = [], saveTimer = null;

  function load() {
    fetch(API).then(function (r) { return r.json(); }).then(finish).catch(function () { finish({}); });
  }
  function finish(d) {
    blob = (d && typeof d === 'object') ? d : {};
    loaded = true;
    var ws = waiters; waiters = [];
    ws.forEach(function (cb) { try { cb(blob); } catch (e) {} });
  }
  function save() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      fetch(API, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                   body: JSON.stringify(blob || {}) }).catch(function () {});
    }, 500);
  }

  window.HartSession = {
    // cb(blob) once the blob is loaded (immediately if already loaded).
    ready: function (cb) { if (loaded) { try { cb(blob); } catch (e) {} } else if (cb) waiters.push(cb); },
    get: function (k, dflt) { return (blob && Object.prototype.hasOwnProperty.call(blob, k)) ? blob[k] : dflt; },
    set: function (k, v) { if (!blob) blob = {}; blob[k] = v; save(); }
  };

  // WebKitGTK-safe fetch-timeout signal. The AbortSignal.timeout static method is
  // missing on the NixOS 24.11 WebKitGTK -- calling it throws a TypeError that
  // kills the whole script. Feature-detect it (this wrapper is the ONLY place in
  // the static modules that may name it) and fall back to an AbortController so
  // every other module can time out safely. Returns null if neither exists.
  window.HartTimeoutSignal = function (ms) {
    try { return AbortSignal.timeout(ms); }
    catch (e) {
      if (typeof AbortController === 'undefined') return null;
      var ac = new AbortController();
      setTimeout(function () { try { ac.abort(); } catch (x) {} }, ms);
      return ac.signal;
    }
  };

  load();
})();
