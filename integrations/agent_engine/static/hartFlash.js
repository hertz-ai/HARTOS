/*
 * hartFlash.js — "Flash HART OS to USB" wizard for the glass shell.
 *
 * The desktop-side installer flow the steward asked for: when the user wants to
 * flash HART OS to a USB stick, the shell drives the PROVEN flasher
 * (scripts/hart_usb_flasher.py via /api/shell/flash/*) — pick a removable disk,
 * pick the variant, confirm the destructive write, watch a LIVE progress bar.
 *
 * Exposes window.loadFlashWizard(container); loadSystemPanel('flash') calls it.
 * Only removable/USB disks are offered by the backend (never a system disk).
 *
 * Classic script for OLD WebKitGTK: NO template literals, NO optional chaining,
 * NO nullish coalescing. var/function + string concatenation + explicit checks.
 */
(function () {
  'use strict';

  function S() {
    try { if (typeof SHELL !== 'undefined' && SHELL) return SHELL; } catch (e) {}
    return (window.SHELL || '');
  }
  function sig(ms) {
    try { if (window._sig) return window._sig(ms); } catch (e) {}
    if (window.HartTimeoutSignal) return window.HartTimeoutSignal(ms);
    return null;
  }
  function toast(t, m, sev) {
    try { if (window.showToast) window.showToast(t, m, sev || 'info'); } catch (e) {}
  }
  function esc(s) {
    var d = document.createElement('div');
    d.textContent = (s == null) ? '' : String(s);
    return d.innerHTML;
  }
  function getJSON(url, ms) {
    return fetch(url, { signal: sig(ms || 8000) }).then(function (r) { return r.json(); });
  }
  function postJSON(url, body, ms) {
    return fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}), signal: sig(ms || 8000)
    }).then(function (r) { return r.json().catch(function () { return {}; }); });
  }

  var ROOT = null, DISKS = [], TAG = null, GH = true, sel = null, variant = 'desktop', poll = null;

  function injectStyle() {
    if (document.getElementById('hf-style')) return;
    var css =
      '.hf-wrap{padding:6px 2px;color:var(--hart-text)}' +
      '.hf-lead{font-size:13px;color:var(--hart-muted);margin-bottom:12px}' +
      '.hf-warn{font-size:12px;color:#ffcc66;background:rgba(255,180,60,.1);' +
      'border:1px solid rgba(255,180,60,.3);border-radius:8px;padding:8px 10px;margin-bottom:12px}' +
      '.hf-sec-lbl{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;' +
      'color:var(--hart-muted);margin:14px 0 6px}' +
      '.hf-disk{display:flex;align-items:center;gap:10px;padding:10px;border-radius:10px;cursor:pointer;' +
      'background:var(--hart-surface,rgba(255,255,255,.05));border:1px solid transparent;margin-bottom:6px}' +
      '.hf-disk:hover{background:var(--hart-surface-hover,rgba(255,255,255,.09))}' +
      '.hf-disk.sel{border-color:var(--hart-accent,#3b82f6);background:rgba(59,130,246,.12)}' +
      '.hf-disk .mi{font-size:24px;color:var(--hart-accent,#3b82f6)}' +
      '.hf-disk-name{flex:1;font-size:13px;font-weight:500}' +
      '.hf-disk-size{font-size:12px;color:var(--hart-muted);font-variant-numeric:tabular-nums}' +
      '.hf-variants{display:flex;gap:8px;margin-bottom:8px}' +
      '.hf-var{flex:1;padding:8px;border-radius:8px;text-align:center;cursor:pointer;font-size:12px;' +
      'background:var(--hart-surface,rgba(255,255,255,.05));border:1px solid transparent;color:var(--hart-muted)}' +
      '.hf-var.on{background:var(--hart-accent,#3b82f6);color:#fff}' +
      '.hf-danger{font-size:12px;color:#ff8a8a;margin:10px 0}' +
      '.hf-btn{display:inline-flex;align-items:center;gap:6px;padding:10px 16px;border-radius:10px;border:0;' +
      'cursor:pointer;font-size:13px;font-weight:600;background:var(--hart-accent,#3b82f6);color:#fff;margin-top:8px}' +
      '.hf-btn[disabled]{opacity:.5;cursor:default}' +
      '.hf-btn.danger{background:#e0484d}' +
      '.hf-track{height:8px;border-radius:6px;overflow:hidden;background:rgba(255,255,255,.12);margin:14px 0 6px}' +
      '.hf-fill{height:100%;width:0;border-radius:6px;background:linear-gradient(90deg,#00D4AA,#3b82f6);' +
      'transition:width .4s ease}' +
      '.hf-pct{font-size:12px;color:var(--hart-muted);font-variant-numeric:tabular-nums}' +
      '.hf-log{margin-top:10px;font-family:var(--hart-mono,monospace);font-size:11px;color:var(--hart-muted);' +
      'background:rgba(0,0,0,.25);border-radius:8px;padding:8px;max-height:140px;overflow:auto;white-space:pre-wrap}' +
      '.hf-done{font-size:13px;font-weight:600;color:var(--hart-active,#22c55e);margin-top:10px}' +
      '.hf-err{font-size:13px;font-weight:600;color:#ff8a8a;margin-top:10px}' +
      '.hf-empty{font-size:12px;color:var(--hart-muted);padding:10px 2px}';
    var st = document.createElement('style'); st.id = 'hf-style'; st.textContent = css;
    document.head.appendChild(st);
  }

  function pickScreen() {
    var h = '<div class="hf-wrap">';
    h += '<div class="hf-lead">Write HART OS to a removable USB stick. The target disk is fully erased. ' +
      'Only removable/USB disks are shown - system disks can never be selected.</div>';
    if (!GH) {
      h += '<div class="hf-warn">The GitHub CLI (gh) is not available here, so the release image cannot be ' +
        'downloaded to flash. Connect to the internet with gh installed, or flash from another machine.</div>';
    }
    h += '<div class="hf-sec-lbl">Target disk</div>';
    if (!DISKS.length) {
      h += '<div class="hf-empty">No removable USB disk detected. Insert a stick and reopen this panel.</div>';
    } else {
      for (var i = 0; i < DISKS.length; i++) {
        var d = DISKS[i];
        h += '<div class="hf-disk" data-n="' + esc(d.number) + '">' +
          '<span class="mi material-icons-round" aria-hidden="true">usb</span>' +
          '<span class="hf-disk-name">' + esc(d.model) + '</span>' +
          '<span class="hf-disk-size">' + esc(d.size_human) + '</span></div>';
      }
    }
    h += '<div class="hf-sec-lbl">Variant</div><div class="hf-variants">';
    var vs = ['desktop', 'server', 'edge'];
    for (var j = 0; j < vs.length; j++) {
      h += '<div class="hf-var' + (vs[j] === variant ? ' on' : '') + '" data-v="' + vs[j] + '">' + vs[j] + '</div>';
    }
    h += '</div>';
    h += '<div class="hf-lead" style="margin-top:8px">Image: <strong>' + esc(TAG || 'latest nightly') + '</strong></div>';
    h += '<button type="button" class="hf-btn" id="hf-next" disabled>' +
      '<span class="mi material-icons-round">arrow_forward</span>Continue</button>';
    h += '</div>';
    ROOT.innerHTML = h;

    var disks = ROOT.querySelectorAll('.hf-disk');
    for (var k = 0; k < disks.length; k++) {
      disks[k].addEventListener('click', function () {
        for (var m = 0; m < disks.length; m++) disks[m].classList.remove('sel');
        this.classList.add('sel');
        sel = this.getAttribute('data-n');
        var nb = document.getElementById('hf-next');
        if (nb) nb.disabled = !(GH && sel !== null);
      });
    }
    var vars = ROOT.querySelectorAll('.hf-var');
    for (var v = 0; v < vars.length; v++) {
      vars[v].addEventListener('click', function () {
        for (var w = 0; w < vars.length; w++) vars[w].classList.remove('on');
        this.classList.add('on'); variant = this.getAttribute('data-v');
      });
    }
    var nx = document.getElementById('hf-next');
    if (nx) nx.addEventListener('click', confirmScreen);
  }

  function confirmScreen() {
    var d = null;
    for (var i = 0; i < DISKS.length; i++) { if (String(DISKS[i].number) === String(sel)) d = DISKS[i]; }
    if (!d) { pickScreen(); return; }
    var h = '<div class="hf-wrap">';
    h += '<div class="hf-sec-lbl">Confirm</div>';
    h += '<div class="hf-disk sel"><span class="mi material-icons-round">usb</span>' +
      '<span class="hf-disk-name">' + esc(d.model) + '</span>' +
      '<span class="hf-disk-size">' + esc(d.size_human) + '</span></div>';
    h += '<div class="hf-danger">This permanently ERASES ' + esc(d.model) + ' (' + esc(d.size_human) +
      ') and writes the <strong>' + esc(variant) + '</strong> image (' + esc(TAG || 'latest nightly') +
      '). Make sure this is the right stick.</div>';
    h += '<button type="button" class="hf-btn danger" id="hf-go">' +
      '<span class="mi material-icons-round">bolt</span>Erase &amp; Flash</button> ';
    h += '<button type="button" class="hf-btn" id="hf-back" style="background:var(--hart-surface,rgba(255,255,255,.08));color:var(--hart-text)">Back</button>';
    h += '</div>';
    ROOT.innerHTML = h;
    var go = document.getElementById('hf-go'); if (go) go.addEventListener('click', startFlash);
    var bk = document.getElementById('hf-back'); if (bk) bk.addEventListener('click', pickScreen);
  }

  function progressScreen() {
    var h = '<div class="hf-wrap">';
    h += '<div class="hf-sec-lbl">Flashing</div>';
    h += '<div class="hf-track"><div class="hf-fill" id="hf-fill"></div></div>';
    h += '<div class="hf-pct" id="hf-pct">Starting...</div>';
    h += '<div class="hf-log" id="hf-log"></div>';
    h += '<div id="hf-final"></div>';
    h += '</div>';
    ROOT.innerHTML = h;
  }

  function startFlash() {
    progressScreen();
    postJSON(S() + '/api/shell/flash/start', { device: sel, variant: variant, tag: TAG })
      .then(function (r) {
        if (!r || !r.success) {
          renderFinal(false, (r && r.error) ? r.error : 'Could not start the flash.');
          return;
        }
        toast('Flashing', 'Writing ' + variant + ' to USB...', 'info');
        startPolling();
      })
      .catch(function () { renderFinal(false, 'Could not start the flash.'); });
  }

  function startPolling() {
    if (poll) clearInterval(poll);
    poll = setInterval(function () {
      getJSON(S() + '/api/shell/flash/progress', 6000)
        .then(function (j) {
          if (!j) return;
          var fill = document.getElementById('hf-fill');
          var pct = document.getElementById('hf-pct');
          var frac = (typeof j.fraction === 'number') ? j.fraction : 0;
          if (fill) fill.style.width = Math.round(frac * 100) + '%';
          if (pct) pct.textContent = (j.message || '') + '  (' + Math.round(frac * 100) + '%)';
          var log = document.getElementById('hf-log');
          if (log && j.lines && j.lines.join) log.textContent = j.lines.join('\n');
          if (j.state === 'done') { stopPolling(); renderFinal(true, j.message || 'Flash complete.'); }
          else if (j.state === 'error') { stopPolling(); renderFinal(false, j.message || 'Flash failed.'); }
        })
        .catch(function () {});
    }, 1200);
  }
  function stopPolling() { if (poll) { clearInterval(poll); poll = null; } }

  function renderFinal(ok, msg) {
    var box = document.getElementById('hf-final');
    if (ok) {
      var fill = document.getElementById('hf-fill'); if (fill) fill.style.width = '100%';
      if (box) box.innerHTML = '<div class="hf-done">' + esc(msg) + ' You can remove the stick and boot it.</div>';
      toast('Flash complete', 'The USB stick is bootable.', 'success');
    } else {
      if (box) box.innerHTML = '<div class="hf-err">' + esc(msg) + '</div>' +
        '<button type="button" class="hf-btn" id="hf-retry" style="margin-top:10px">Back</button>';
      var rb = document.getElementById('hf-retry'); if (rb) rb.addEventListener('click', pickScreen);
      toast('Flash failed', msg, 'error');
    }
  }

  window.loadFlashWizard = function (container) {
    ROOT = container || document.getElementById('sys-flash');
    if (!ROOT) return;
    injectStyle();
    sel = null; variant = 'desktop';
    ROOT.innerHTML = '<div class="hf-empty">Scanning for removable disks...</div>';
    getJSON(S() + '/api/shell/flash/disks', 12000)
      .then(function (j) {
        if (!j || j.available === false) {
          ROOT.innerHTML = '<div class="hf-err">Flasher unavailable' +
            ((j && j.error) ? (': ' + esc(j.error)) : '') + '</div>';
          return;
        }
        DISKS = j.disks || [];
        TAG = j.tag || null;
        GH = (j.gh !== false);
        pickScreen();
      })
      .catch(function () {
        ROOT.innerHTML = '<div class="hf-err">Could not reach the flasher service.</div>';
      });
  };
})();
