/*
 * hartStates.js — designed empty / offline / loading states (never naive).
 *
 * ONE reusable component used by every panel that can have no data, so the shell
 * never shows a raw "Loading…", a stack trace, an HTTP code, or "failed". Voice
 * rules: name WHAT is missing + WHEN it returns, give ONE action, animate in
 * calmly. Offline auto-recovers (silent retry) — the copy promises it, so we keep
 * the promise. Loading reuses the EXISTING .ds-skeleton-* shimmer (no new loader).
 *
 * Public API (the integration contract — panels target these globals):
 *   window.hartEmptyState({kind:'loading'|'offline'|'empty', icon, title, msg, onRetry})
 *     -> returns a DOM node (.lg-empty / .lg-empty-{kind}), styled in _CSS_LIVING_GLASS.
 *   window.hartLoadInto(container, url, render, copy)
 *     -> DRY fetch-with-designed-states: shows loading, then render(container,data)
 *        on success, or a breathing offline card with a working "Try again" +
 *        silent auto-recover on failure.
 *
 * Plain classic script. No deps beyond the shell's existing HartTimeoutSignal.
 */
(function () {
  'use strict';

  // textContent escape so any title/msg is injection-safe (panels may pass copy
  // derived from data).
  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }

  window.hartEmptyState = function (opts) {
    opts = opts || {};
    var k = opts.kind || 'empty';
    var el = document.createElement('div');
    el.className = 'lg-empty lg-empty-' + k;

    if (k === 'loading') {
      // Reuse the existing skeleton shimmer — one title + a few cards.
      el.innerHTML =
        '<div class="ds-skeleton ds-skeleton-title"></div>' +
        '<div class="ds-skeleton ds-skeleton-card"></div>' +
        '<div class="ds-skeleton ds-skeleton-card"></div>' +
        '<div class="ds-skeleton ds-skeleton-card"></div>';
      return el;
    }

    var icon = opts.icon || (k === 'offline' ? 'cloud_off' : 'inbox');
    el.innerHTML =
      '<div class="lg-empty-disc"><span class="mi material-icons-round" aria-hidden="true">' + esc(icon) + '</span></div>' +
      '<div class="lg-empty-title">' + esc(opts.title || (k === 'offline' ? 'Waiting for the hive' : 'Nothing here yet')) + '</div>' +
      '<div class="lg-empty-msg">' + esc(opts.msg || '') + '</div>' +
      (opts.onRetry ? '<button type="button" class="ds-btn ds-btn-text lg-empty-retry">Try again</button>' : '');
    if (opts.onRetry) {
      var btn = el.querySelector('.lg-empty-retry');
      if (btn) btn.addEventListener('click', opts.onRetry);
    }
    return el;
  };

  // DRY fetch-with-designed-states wrapper (used by every panel loader).
  // copy = { title, msg } for the offline card (optional; sensible defaults).
  window.hartLoadInto = function (container, url, render, copy) {
    if (!container) return;
    container.innerHTML = '';
    container.appendChild(window.hartEmptyState({ kind: 'loading' }));
    var tries = 0;
    (function go() {
      fetch(url, { signal: window.HartTimeoutSignal ? window.HartTimeoutSignal(6000) : null })
        .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
        .then(function (d) { container.innerHTML = ''; render(container, d); })
        .catch(function () {
          container.innerHTML = '';
          container.appendChild(window.hartEmptyState({
            kind: 'offline',
            title: (copy && copy.title) || 'Reconnecting',
            msg: (copy && copy.msg) || 'Could not load this yet. It will appear once the connection is restored.',
            onRetry: go
          }));
          // Silent auto-recover — the copy promised it. Capped so we never spin forever.
          if (tries++ < 30) setTimeout(go, 8000);
        });
    })();
  };
})();
