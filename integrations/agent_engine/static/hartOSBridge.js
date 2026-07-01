/* ═══════════════════════════════════════════════════════════════════════════
 * hartOSBridge.js — the typed Shell<->OS bridge SDK for the HART OS WebView shell
 * (#133 / W3).
 *
 * HART OS IS the operating system, so the shell UI and the OS server run on the
 * same machine. Instead of stringly-typed magic-string calls like
 *   fetch('/api/shell/power/action', {action: 'reboot'})
 * whose handler shells out and fire-and-forgets the result, the shell calls a
 * TYPED op:
 *   hartOS.power.reboot()
 * which the OS server runs NATIVELY (logind D-Bus) and RESULT-CHECKS. A polkit
 * denial / failure REJECTS the returned promise with a real error, instead of
 * looking like success.
 *
 * One contract: every op goes through POST /api/os/invoke {domain, op, params};
 * hartOS.contract() introspects the whole surface (implemented + planned domains).
 *
 * No build step, no framework — a plain IIFE that attaches window.hartOS, loaded
 * via <script defer src="/shell/static/hartOSBridge.js">.
 * ═══════════════════════════════════════════════════════════════════════════ */
(function (global) {
  'use strict';

  var BASE = '/api/os';

  /**
   * Invoke a typed OS op. Resolves with the server's {ok:true, ...} payload;
   * REJECTS with an Error (err.detail = payload, err.status = HTTP status) when the
   * op fails, is denied, or is not implemented — so callers never mistake a masked
   * failure for success.
   * @param {string} domain  e.g. 'power'
   * @param {string} op      e.g. 'reboot'
   * @param {object} [params]
   * @returns {Promise<object>}
   */
  function invoke(domain, op, params) {
    return fetch(BASE + '/invoke', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ domain: domain, op: op, params: params || {} })
    }).then(function (resp) {
      return resp.json().catch(function () {
        return { ok: false, error: 'bad response (HTTP ' + resp.status + ')' };
      }).then(function (data) {
        if (!resp.ok || !data || data.ok === false) {
          var msg = (data && data.error) || ('HTTP ' + resp.status);
          var err = new Error('hartOS.' + domain + '.' + op + ' failed: ' + msg);
          err.detail = data;
          err.status = resp.status;
          throw err;
        }
        return data;
      });
    });
  }

  /* ── power domain (native via logind D-Bus) ── */
  var power = {
    /** Restart the machine now. */
    reboot: function () { return invoke('power', 'reboot'); },
    /** Power the machine off now. */
    shutdown: function () { return invoke('power', 'shutdown'); },
    /** Suspend to RAM (sleep). */
    suspend: function () { return invoke('power', 'suspend'); },
    /** Hibernate to disk. */
    hibernate: function () { return invoke('power', 'hibernate'); },
    /** Lock all sessions. */
    lock: function () { return invoke('power', 'lock'); },
    /** Restart into the firmware (UEFI) setup on the next boot. */
    firmwareSetup: function () { return invoke('power', 'firmware_setup'); },
    /**
     * Which power ops this box offers (firmware_setup is gated on the UEFI probe).
     * @returns {Promise<object>} map of op -> boolean
     */
    capabilities: function () {
      return fetch(BASE + '/power/capabilities')
        .then(function (r) { return r.json(); })
        .then(function (d) { return (d && d.capabilities) || {}; })
        .catch(function () { return {}; });
    }
  };

  var hartOS = {
    /** Low-level typed dispatcher (domain, op, params) -> Promise. */
    invoke: invoke,
    power: power,
    /**
     * The self-describing op manifest (implemented + planned domains). Lets the UI
     * render only the ops the OS actually implements + an honest "not yet" for the
     * planned domains (disk / network / display).
     * @returns {Promise<object>}
     */
    contract: function () {
      return fetch(BASE + '/contract').then(function (r) { return r.json(); });
    }
  };

  global.hartOS = hartOS;
  /* Also export for a module bundler / node --check-style consumers. */
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = hartOS;
  }
})(typeof window !== 'undefined' ? window : this);
