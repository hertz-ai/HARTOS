// Sustained-drag LEAK HUNT for the HART shell.
// Injected into the served shell; drives a long synthetic orb drag and samples
// accumulation metrics over time. Degradation-over-time is an ACCUMULATION
// signature, so we watch counts that should be FLAT, not frame time.
(function () {
  var out = { samples: [], meta: {} };
  var hero = document.getElementById('hart-hero-orbwrap') ||
             document.getElementById('hart-hero');
  out.meta.heroFound = !!hero;

  function snap(label) {
    var s = {
      t: Math.round(performance.now()),
      label: label,
      domNodes: document.getElementsByTagName('*').length,
      // transient/effect elements that SHOULD be removed after use
      ripples: document.querySelectorAll('.lg-orb-ripple').length,
      toasts: document.querySelectorAll('.ds-toast, .toast').length,
      canvases: document.getElementsByTagName('canvas').length,
      styles: document.getElementsByTagName('style').length,
      // inline style attr on the hero: grows if someone appends instead of sets
      heroStyleLen: hero ? (hero.getAttribute('style') || '').length : -1,
      heroClasses: hero ? hero.className.length : -1,
    };
    if (performance.memory) {
      s.heapMB = +(performance.memory.usedJSHeapSize / 1048576).toFixed(2);
    }
    out.samples.push(s);
    return s;
  }

  function pointer(type, x, y) {
    var ev;
    try {
      ev = new PointerEvent(type, {
        bubbles: true, cancelable: true, pointerId: 1, isPrimary: true,
        clientX: x, clientY: y, button: 0, buttons: type === 'pointerup' ? 0 : 1
      });
    } catch (e) {
      ev = document.createEvent('MouseEvents');
      ev.initMouseEvent(type.replace('pointer', 'mouse'), true, true, window,
                        0, x, y, x, y, false, false, false, false, 0, null);
    }
    (hero || document.body).dispatchEvent(ev);
  }

  // One drag cycle = press, N moves along a circle, release. Mirrors a human
  // dragging the orb around, which is what the steward did.
  function dragCycle(cx, cy, r, steps) {
    pointer('pointerdown', cx + r, cy);
    for (var i = 1; i <= steps; i++) {
      var a = (i / steps) * Math.PI * 2;
      pointer('pointermove', Math.round(cx + Math.cos(a) * r),
                             Math.round(cy + Math.sin(a) * r));
    }
    pointer('pointerup', cx + r, cy);
  }

  var CYCLES = 40, STEPS = 30;
  var cx = Math.round(window.innerWidth / 2), cy = Math.round(window.innerHeight / 2);
  snap('baseline');
  var n = 0;
  var timer = setInterval(function () {
    dragCycle(cx, cy, 120, STEPS);
    n++;
    if (n % 5 === 0) snap('after_' + n + '_drags');
    if (n >= CYCLES) {
      clearInterval(timer);
      // let any deferred cleanup run before the final sample
      setTimeout(function () {
        snap('final');
        var a = out.samples[0], z = out.samples[out.samples.length - 1];
        out.meta.dragCycles = CYCLES * 1;
        out.meta.moveEvents = CYCLES * STEPS;
        out.meta.domGrowth = z.domNodes - a.domNodes;
        out.meta.rippleGrowth = z.ripples - a.ripples;
        out.meta.canvasGrowth = z.canvases - a.canvases;
        out.meta.styleGrowth = z.styles - a.styles;
        out.meta.heroStyleGrowth = z.heroStyleLen - a.heroStyleLen;
        if (a.heapMB && z.heapMB) out.meta.heapGrowthMB = +(z.heapMB - a.heapMB).toFixed(2);
        window.__leakhunt = out;
        document.title = 'LEAKHUNT_DONE';
        try {
          fetch('/leakhunt', { method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(out) });
        } catch (e) { document.title = 'LEAKHUNT_POST_FAIL'; }
      }, 1200);
    }
  }, 40);
})();
