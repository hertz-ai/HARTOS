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

  // How many panels live on desktop n (occupancy — drives the per-segment dots).
  function windowsOn(n) {
    var c = 0;
    if (pc) Array.prototype.forEach.call(pc.querySelectorAll('.panel'), function (p) {
      if ((parseInt(p.getAttribute('data-ws') || '1', 10)) === n) c++;
    });
    return c;
  }

  // Slide the accent thumb under the active segment (tracked by position+width).
  function moveThumb() {
    var seg = bar && bar.querySelector('.hart-pager-seg[data-ws="' + current + '"]');
    var th = bar && bar.querySelector('.hart-pager-thumb');
    if (seg && th) { th.style.transform = 'translateX(' + seg.offsetLeft + 'px)'; th.style.width = seg.offsetWidth + 'px'; }
  }

  // Paint per-segment occupancy dots + active state, and stamp data-multiws — the
  // Dimension-3 visibility hook the pager OWNS (it alone knows occupancy; Gate 4).
  //
  // The pager reveals once ANY window is open (occ >= 1 OR not on desktop 1), NOT
  // only when >1 desktop is already occupied. The old ">1 occupied" rule was a
  // discoverability DEADLOCK: a panel is tagged with the CURRENT desktop, so to
  // ever occupy a 2nd desktop you must first SWITCH — but the switcher was hidden
  // until a 2nd desktop was already occupied. Revealing it as soon as there's a
  // window to distribute (or you've already navigated off desktop 1) breaks that
  // loop while still keeping the rail out of the way on a pristine, empty desktop.
  function paintOccupancy() {
    if (!bar) return;
    var occ = 0, total = 0;
    Array.prototype.forEach.call(bar.querySelectorAll('.hart-pager-seg'), function (seg) {
      var n = parseInt(seg.dataset.ws, 10), c = windowsOn(n);
      if (c) occ++;
      total += c;
      var box = seg.querySelector('.hps-occ');
      if (box) {
        box.innerHTML = '';
        for (var k = 0; k < Math.min(c, 3); k++) box.appendChild(document.createElement('i'));
      }
      seg.classList.toggle('empty', c === 0);
      seg.classList.toggle('active', n === current);
    });
    var usable = total > 0 || current !== 1;   // a window exists, or we've navigated away
    document.documentElement.setAttribute('data-multiws', usable ? '1' : '0');
  }

  function apply() {
    if (pc) {
      Array.prototype.forEach.call(pc.querySelectorAll('.panel'), function (p) {
        var ws = parseInt(p.getAttribute('data-ws') || '1', 10);
        p.style.display = (ws === current) ? '' : 'none';
      });
    }
    moveThumb();
    paintOccupancy();
    // Keep the settings-panel squares live too (both views share this state).
    Array.prototype.forEach.call(document.querySelectorAll('.hart-ws-square'), function (sq) {
      sq.classList.toggle('active', parseInt(sq.getAttribute('data-ws-square'), 10) === current);
    });
  }

  // Mirror the shell-local switch to the REAL compositor (workspace.switch /
  // com.hart.Compositor IPC §4.8) so a desktop change ALSO moves native windows
  // where a window manager is present. Fire-and-forget: the client-side panel
  // show/hide in apply() above is authoritative for the glass UI on EVERY tier,
  // and the backend degrades to a 200 no-op under a compositor with no live WM
  // (cage / hart-comp before its IPC backend lands), so a failure here must never
  // disturb the shell.
  //
  // FOLLOW-UP (swaymsg-shim gap): the backend routes this through HartWmClient,
  // which is still a swaymsg shim, so on the real hart-comp desktop native-window
  // switching stays an HONEST no-op until the com.hart.Compositor IPC backend
  // (compositor/IPC_PROTOCOL.md) replaces the shim. We do NOT fake the switch.
  function pushCompositorSwitch(n) {
    try {
      if (typeof window.fetch !== 'function') return;
      window.fetch('/api/shell/workspaces/switch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: n, name: String(n) })
      }).catch(function () { /* degrade silently */ });
    } catch (e) { /* degrade silently */ }
  }

  window.hartSwitchWorkspace = function (n) {
    n = Math.max(1, Math.min(COUNT, n | 0));
    if (n === current) return;
    current = n;
    apply();
    // Both the pager segments (buildBar) and the Workspaces-settings squares
    // (liquid_ui_service.py) call this ONE fn — one source of truth — so wiring
    // the real-compositor switch HERE covers both with no parallel path.
    pushCompositorSwitch(n);
  };
  window.hartWorkspaceInfo = function () { return { count: COUNT, current: current }; };

  // Segmented glass rail: a sliding accent thumb + per-desktop occupancy dots.
  // Replaces the 4 flat numbered .hart-ws-dot buttons. Scroll-wheel cycles.
  function buildBar() {
    bar = document.getElementById('hart-ws-switcher');
    if (!bar) return;
    bar.innerHTML = '<div class="hart-pager-thumb"></div>';
    for (var i = 1; i <= COUNT; i++) {
      (function (n) {
        var seg = document.createElement('button');
        seg.className = 'hart-pager-seg';
        seg.type = 'button';
        seg.dataset.ws = String(n);
        seg.setAttribute('aria-label', 'Desktop ' + n);
        seg.innerHTML = '<span class="hps-n lg-num">' + n + '</span><span class="hps-occ"></span>';
        seg.addEventListener('click', function () { window.hartSwitchWorkspace(n); });
        bar.appendChild(seg);
      })(i);
    }
    bar.addEventListener('wheel', function (e) {
      e.preventDefault();
      window.hartSwitchWorkspace(current + (e.deltaY > 0 ? 1 : -1));
    }, { passive: false });
  }

  function init() {
    pc = document.getElementById('panels');
    if (!pc) { return setTimeout(init, 300); }
    buildBar();
    apply();   // initial thumb position + data-multiws (before any panel opens)
    new MutationObserver(function (muts) {
      muts.forEach(function (m) { Array.prototype.forEach.call(m.addedNodes, tagPanel); });
      apply();   // occupancy stays live as panels open/close (Dimension-3 hook)
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
