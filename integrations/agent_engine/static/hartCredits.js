/*
 * hartCredits.js — HART OS "About & Credits" surface (#143 offline-art).
 *
 * Renders the third-party art licence ledger (docs/THIRD_PARTY_ART.md, parsed by
 * /api/shell/credits) so every bundled attribution-required asset shows its
 * credit line in the OS itself - the doc's own binding rule. Heavy DOM lives here
 * (window.hartRenderCredits) so loadCreditsPanel stays a brace-safe delegate,
 * mirroring the hartMarketplace.js pattern. OFFLINE-first: the ledger reads a
 * bundled doc, so this works with the network OFF. Built with DOM nodes +
 * textContent (no innerHTML for ledger data) so a doc string can never inject
 * markup.
 */
(function () {
  'use strict';

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  // One ledger section -> a titled table (columns from the parsed header row).
  function renderSection(sec) {
    var wrap = el('div', 'hart-credits-section');
    wrap.style.marginTop = '18px';
    if (sec.heading) wrap.appendChild(el('div', 'ds-section-label', sec.heading));
    var cols = sec.columns || [];
    var rows = sec.rows || [];
    if (!rows.length) {
      wrap.appendChild(el('div', 'ds-body-sm ds-text-muted', 'Nothing bundled from this source yet.'));
      return wrap;
    }
    var table = el('table', 'hart-credits-table');
    table.setAttribute('style', 'width:100%;border-collapse:collapse;font-size:12px');
    var thead = el('thead'); var htr = el('tr');
    cols.forEach(function (c) {
      var th = el('th', null, c);
      th.setAttribute('style', 'text-align:left;padding:6px 8px;color:var(--hart-muted);' +
        'border-bottom:1px solid var(--hart-glass-border);font-weight:600;white-space:nowrap');
      htr.appendChild(th);
    });
    thead.appendChild(htr); table.appendChild(thead);
    var tbody = el('tbody');
    rows.forEach(function (row) {
      var tr = el('tr');
      cols.forEach(function (c) {
        var td = el('td', null, row[c] || '');
        td.setAttribute('style', 'padding:6px 8px;color:var(--hart-body);' +
          'border-bottom:1px solid rgba(255,255,255,0.05);vertical-align:top');
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody); wrap.appendChild(table);
    return wrap;
  }

  window.hartRenderCredits = function (root) {
    if (!root) return;
    root.innerHTML = '';
    var wrap = el('div', 'hart-credits ds-fade-in');

    var head = el('div', 'hart-credits-head');
    head.appendChild(el('div', 'ds-panel-title', 'About & Credits'));
    head.appendChild(el('div', 'ds-body-sm ds-text-muted',
      'Bundled third-party art, its source, and the licence credit that ships with HART OS.'));
    wrap.appendChild(head);
    root.appendChild(wrap);

    var sig = window.HartTimeoutSignal ? window.HartTimeoutSignal(6000) : null;
    fetch('/api/shell/credits', { signal: sig })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        data = data || {};
        if (data.binding_rule) {
          var b = el('div', 'ds-body-sm', data.binding_rule);
          b.setAttribute('style', 'margin-top:12px;padding:10px 12px;border-radius:10px;' +
            'background:rgba(255,255,255,0.04);border:1px solid var(--hart-glass-border);' +
            'color:var(--hart-muted);line-height:1.5');
          wrap.appendChild(b);
        }
        var sections = data.sections || [];
        var shown = 0;
        sections.forEach(function (sec) {
          // Only render tables (sections with columns); skip prose-only headings.
          if (sec && sec.columns && sec.columns.length) { wrap.appendChild(renderSection(sec)); shown++; }
        });
        if (!shown) {
          wrap.appendChild(el('div', 'ds-body-md ds-text-muted',
            'No third-party art is bundled - all OS art is first-party generated.'));
        }
      })
      .catch(function (e) {
        console.debug('hartCredits: credits fetch failed', e);
        wrap.appendChild(el('div', 'ds-body-md ds-text-muted',
          'Credits are unavailable right now.'));
      });
  };
})();
