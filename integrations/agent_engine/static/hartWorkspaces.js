/*
 * hartWorkspaces.js — HART OS virtual desktops (client-side).
 *
 * The HART OS kiosk compositor is cage (one fullscreen window) — it has no
 * window-manager workspaces. (The sway-backed /api/shell/workspaces in
 * shell_desktop_apis.py is a SEPARATE compositor layer that returns one fallback
 * workspace under cage, so it is not the source here.) The shell's own virtual
 * desktops live here, grouping the shell's floating panels: each panel is tagged
 * with the desktop it opened on; a switch shows/hides panels. Desktop icons stay
 * global (the Windows model). The switcher bar + the Workspaces settings panel
 * are two views onto the SAME client state (hartWorkspaceInfo /
 * hartSwitchWorkspace) — one source of truth, no parallel path.
 *
 * Reuses #panels (windows) and a MutationObserver to tag new windows — no edit
 * to openPanel. Plain classic script.
 */
(function () {
  'use strict';
  var COUNT = 4, current = 1;
  var pc = null, bar = null;

  function tagPanel(node) {
    if (node && node.classList && node.classList.contains('panel') && !node.getAttribute('data-ws')) {
      node.setAttribute('data-ws', String(current));
    }
  }

  function apply() {
    if (pc) {
      Array.prototype.forEach.call(pc.querySelectorAll('.panel'), function (p) {
        var ws = parseInt(p.getAttribute('data-ws') || '1', 10);
        p.style.display = (ws === current) ? '' : 'none';
      });
    }
    if (bar) {
      Array.prototype.forEach.call(bar.querySelectorAll('.hart-ws-dot'), function (d, i) {
        d.classList.toggle('active', (i + 1) === current);
      });
    }
    // Keep the settings-panel squares live too (both views share this state).
    Array.prototype.forEach.call(document.querySelectorAll('.hart-ws-square'), function (sq) {
      sq.classList.toggle('active', parseInt(sq.getAttribute('data-ws-square'), 10) === current);
    });
  }

  window.hartSwitchWorkspace = function (n) {
    n = Math.max(1, Math.min(COUNT, n | 0));
    if (n === current) return;
    current = n;
    apply();
  };
  window.hartWorkspaceInfo = function () { return { count: COUNT, current: current }; };

  function buildBar() {
    bar = document.getElementById('hart-ws-switcher');
    if (!bar) return;
    bar.innerHTML = '';
    for (var i = 1; i <= COUNT; i++) {
      (function (n) {
        var b = document.createElement('button');
        b.className = 'hart-ws-dot' + (n === current ? ' active' : '');
        b.type = 'button';
        b.textContent = String(n);
        b.setAttribute('aria-label', 'Switch to desktop ' + n);
        b.addEventListener('click', function () { window.hartSwitchWorkspace(n); });
        bar.appendChild(b);
      })(i);
    }
  }

  function init() {
    pc = document.getElementById('panels');
    if (!pc) { return setTimeout(init, 300); }
    buildBar();
    new MutationObserver(function (muts) {
      muts.forEach(function (m) { Array.prototype.forEach.call(m.addedNodes, tagPanel); });
      apply();
    }).observe(pc, { childList: true });

    // Ctrl+Alt+Left/Right switch; Ctrl+Alt+1..4 jump (the common Linux binding,
    // and what the shortcuts panel already advertises as workspace_left/right).
    document.addEventListener('keydown', function (e) {
      if (e.ctrlKey && e.altKey && !e.shiftKey) {
        if (e.key === 'ArrowRight') { e.preventDefault(); window.hartSwitchWorkspace(current + 1); }
        else if (e.key === 'ArrowLeft') { e.preventDefault(); window.hartSwitchWorkspace(current - 1); }
        else if (/^[1-9]$/.test(e.key)) { e.preventDefault(); window.hartSwitchWorkspace(parseInt(e.key, 10)); }
      }
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
