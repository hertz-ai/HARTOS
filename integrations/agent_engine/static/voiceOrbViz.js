/*
 * voiceOrbViz.js — HART OS glass-shell NATIVE voice visualiser.
 *
 * A framework-agnostic (vanilla, no React) canvas renderer so the OS liquid-ui
 * shell can draw the floating voice orb itself — no React bundle, no iframe.
 *
 * This is the brand centerpiece: a LIVING, BREATHING orb, NOT a hard disc.
 *   - IDLE: it breathes (a slow ease-in-out sine, ~5s period) — calm and alive.
 *     Voice / audio energy intensifies the breath.
 *   - EDGELESS: there is NO solid background disc and NO hard ring outline. The
 *     orb is a soft volumetric glow with a luminous core that fades to full
 *     transparency at the rim (radial alpha falloff), so it merges with whatever
 *     sits behind it.
 *   - BRAND SPECTRUM: it is coloured with the Hevolve spectrum (teal, cyan,
 *     blue, violet, magenta) as a flowing iridescent gradient that rotates over
 *     time — never the old off-brand flat purple, never clipped to a circle.
 *
 * Served by the shell at /shell/static/voiceOrbViz.js and used via:
 *   var orb = window.HartVoiceOrbViz(canvas, { size });
 *   orb.connectAudioElement(ttsAudioEl);  // speaking (TTS playback element)
 *   orb.connectStream(micStream);         // listening (mic)
 *   orb.setActive(true|false);
 *   orb.disconnect(); orb.destroy();
 *
 * WebKitGTK-safe (the cage shell runtime): no template literals, no optional
 * chaining, no nullish coalescing, no AbortSignal. Only canvas 2D primitives
 * that the test harness stubs (clearRect / beginPath / arc / fill / moveTo /
 * lineTo / closePath / stroke / createRadialGradient -> addColorStop).
 */
(function (global) {
  'use strict';

  var PTS = 180;

  // The Hevolve brand spectrum (teal -> cyan -> blue -> violet -> magenta).
  // RGB triplets so we can blend + vary alpha per render without re-parsing.
  var SPECTRUM = [
    [0, 230, 195],   // #00E6C3 teal
    [41, 197, 255],  // #29C5FF cyan
    [59, 130, 246],  // #3B82F6 blue
    [155, 92, 255],  // #9B5CFF violet
    [255, 46, 154]   // #FF2E9A magenta
  ];

  function rgba(c, a) {
    return 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + a.toFixed(3) + ')';
  }
  // Smoothly sample the spectrum as a continuous loop. p is any real number;
  // its fractional position picks two neighbours and blends between them so the
  // colour FLOWS (iridescent) rather than stepping between fixed swatches.
  function spectrumAt(p) {
    var n = SPECTRUM.length;
    var f = p - Math.floor(p);          // 0..1 around the wheel
    var scaled = f * n;
    var i = Math.floor(scaled) % n;
    var j = (i + 1) % n;
    var t = scaled - Math.floor(scaled);
    var a = SPECTRUM[i], b = SPECTRUM[j];
    return [
      Math.round(a[0] + (b[0] - a[0]) * t),
      Math.round(a[1] + (b[1] - a[1]) * t),
      Math.round(a[2] + (b[2] - a[2]) * t)
    ];
  }

  function createVoiceOrbViz(canvas, opts) {
    opts = opts || {};
    var ctx = canvas.getContext('2d');
    if (!ctx) return { connectAudioElement: function () {}, connectStream: function () {}, disconnect: function () {}, setActive: function () {}, destroy: function () {} };

    var audioCtx = null, analyser = null, source = null, lastKey = null;
    var active = false, rafId = null;
    var s = { bass: 0, mid: 0, treble: 0, bassCur: 0, midCur: 0, trebleCur: 0, time: 0, dir: 1, wasQuiet: false, breath: 0, breathV: 0 };
    var oR = new Float32Array(PTS + 1);
    var freqData = new Uint8Array(256);

    var W = canvas.width, H = canvas.height;
    var cx = W / 2, cy = H / 2, baseR = W * 0.25;

    function ensureCtx() {
      if (!audioCtx || audioCtx.state === 'closed') {
        var AC = global.AudioContext || global.webkitAudioContext;
        audioCtx = new AC();
      }
      if (!analyser) {
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = 512;
        analyser.smoothingTimeConstant = 0.8;
      }
      return audioCtx;
    }
    function dropSource() {
      if (source) { try { source.disconnect(); } catch (e) {} source = null; }
      lastKey = null;
    }
    function connectAudioElement(el) {
      if (!el) return;
      try {
        ensureCtx();
        if (source && lastKey === el) return;
        dropSource();
        source = audioCtx.createMediaElementSource(el);
        source.connect(analyser);
        analyser.connect(audioCtx.destination); // element audio stays audible
        lastKey = el;
      } catch (e) {}
    }
    function connectStream(stream) {
      if (!stream) return;
      try {
        ensureCtx();
        if (source && lastKey === stream) return;
        dropSource();
        source = audioCtx.createMediaStreamSource(stream);
        source.connect(analyser); // mic NOT routed to destination (no echo)
        lastKey = stream;
      } catch (e) {}
    }
    function disconnect() { dropSource(); }
    function setActive(v) {
      active = !!v;
      if (active && audioCtx && audioCtx.state === 'suspended') { try { audioCtx.resume(); } catch (e) {} }
    }

    // A soft, edgeless radial body: a luminous core in 'color', fading to FULL
    // transparency at 'radius' (alpha falloff to the rim, no hard outline, so
    // the orb merges with the background). 'peak' scales the centre intensity.
    function fillGlow(color, radius, peak) {
      if (radius <= 0) return;
      var g = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius);
      g.addColorStop(0, rgba(color, Math.min(0.9, peak)));
      g.addColorStop(0.35, rgba(color, peak * 0.55));
      g.addColorStop(0.7, rgba(color, peak * 0.18));
      g.addColorStop(1, rgba(color, 0));     // -> transparent rim, edgeless
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(cx, cy, radius, 0, Math.PI * 2); ctx.fill();
    }

    function render() {
      rafId = global.requestAnimationFrame(render);
      s.time += 0.016;

      // ── Audio energy (or a gentle synthetic idle when no stream) ──
      if (active && analyser) {
        analyser.getByteFrequencyData(freqData);
        var bS = 0, mS = 0, tS = 0, len = freqData.length;
        for (var i = 0; i < len; i++) {
          if (i < len * 0.15) bS += freqData[i];
          else if (i < len * 0.5) mS += freqData[i];
          else tS += freqData[i];
        }
        s.bass = bS / (len * 0.15) / 255;
        s.mid = mS / (len * 0.35) / 255;
        s.treble = tS / (len * 0.5) / 255;
      } else if (active) {
        s.bass = 0.25 + 0.15 * Math.sin(s.time * 2.3);
        s.mid = 0.3 + 0.2 * Math.sin(s.time * 3.1 + 0.5);
        s.treble = 0.15 + 0.1 * Math.sin(s.time * 4.7 + 1.2);
      } else {
        s.bass *= 0.95; s.mid *= 0.95; s.treble *= 0.95;
      }
      s.bassCur += (s.bass - s.bassCur) * 0.12;
      s.midCur += (s.mid - s.midCur) * 0.10;
      s.trebleCur += (s.treble - s.trebleCur) * 0.08;
      var energy = s.bassCur * 0.5 + s.midCur * 0.35 + s.trebleCur * 0.15;
      if (active) {
        if (energy < 0.03) { s.wasQuiet = true; }
        else if (s.wasQuiet && energy > 0.08) { s.wasQuiet = false; s.dir = -s.dir; }
      }

      // ── BREATHING: a slow, organic, ease-in-out oscillation (~5s period).
      // We integrate a smoothed sine via a critically-damped spring toward the
      // target so it eases IN and OUT (never a linear in-out kink). Energy lifts
      // the breath so the orb "inhales" harder while speaking/listening. ──
      var period = 5.0;                                   // seconds per breath
      var target = (Math.sin(s.time * (Math.PI * 2 / period)) * 0.5 + 0.5); // 0..1
      target = target * target * (3 - 2 * target);        // smoothstep -> ease-in-out
      var lift = 0.4 + energy * 1.6;                       // louder -> deeper breath
      target = target * lift;
      s.breathV += (target - s.breath) * 0.08;            // spring accel
      s.breathV *= 0.82;                                  // damping
      s.breath += s.breathV;
      var breath = s.breath;

      var t = s.time, d = s.dir;
      ctx.clearRect(0, 0, W, H);

      // Iridescent phase: the whole spectrum rotates slowly, faster with energy,
      // so the orb's colour FLOWS through the brand wheel.
      var phase = t * 0.06 + energy * 0.5;

      ctx.globalCompositeOperation = 'lighter'; // additive -> volumetric glow

      // ── Wavy organic membrane (NOT a clipped circle): the radius is perturbed
      // by layered sine harmonics + a breathing swell, so the silhouette is
      // alive and never a hard masked disc. We fill it as a soft gradient with a
      // transparent rim, never stroke it (no outline). ──
      var swell = 1 + breath * 0.10 + energy * 0.16;      // breathing scale
      var maxPeakR = baseR;
      for (var i = 0; i <= PTS; i++) {
        var a = (i / PTS) * Math.PI * 2;
        var idle = 6 * Math.sin(2 * a + t * 0.6) + 4 * Math.sin(3 * a - t * 0.45) + 3 * Math.sin(5 * a + t * 0.7);
        var wave = idle +
          s.bassCur * 50 * Math.sin(2 * a + t * 1.5 * d) +
          s.bassCur * 30 * Math.sin(3 * a - t * 0.8 * d) +
          s.midCur * 38 * Math.sin(4 * a + t * 2.2 * d) +
          s.midCur * 22 * Math.sin(6 * a - t * 1.3 * d) +
          s.trebleCur * 26 * Math.sin(8 * a + t * 3.0 * d) +
          s.trebleCur * 15 * Math.sin(11 * a - t * 1.8 * d);
        var rectified = (wave * wave) / (Math.abs(wave) + 8);
        oR[i] = (baseR + rectified * (baseR / 100)) * swell;
        if (oR[i] > maxPeakR) maxPeakR = oR[i];
      }

      // The membrane: a closed path filled with an iridescent radial gradient
      // that fades to transparency at the (wavy) rim. The gradient stops cycle
      // through the spectrum so the body is multi-hued, not flat.
      ctx.beginPath();
      for (var i = 0; i <= PTS; i++) {
        var a = (i / PTS) * Math.PI * 2;
        var x = cx + Math.cos(a) * oR[i], y = cy + Math.sin(a) * oR[i];
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.closePath();
      var bodyA = 0.10 + energy * 0.22 + breath * 0.04;
      var mg = ctx.createRadialGradient(cx, cy, 0, cx, cy, maxPeakR);
      mg.addColorStop(0.0, rgba(spectrumAt(phase + 0.5), bodyA * 0.9));
      mg.addColorStop(0.35, rgba(spectrumAt(phase + 0.25), bodyA * 0.7));
      mg.addColorStop(0.7, rgba(spectrumAt(phase), bodyA * 0.35));
      mg.addColorStop(1.0, rgba(spectrumAt(phase + 0.75), 0)); // transparent rim
      ctx.fillStyle = mg;
      ctx.fill();

      // ── Volumetric halo: three concentric soft glows whose hue steps around
      // the wheel, all fading to transparency. This is the "merges with the
      // background" body — no disc, no ring, just light. ──
      var haloR = maxPeakR * 1.18;
      fillGlow(spectrumAt(phase + 0.66), haloR, 0.05 + energy * 0.10 + breath * 0.03);
      fillGlow(spectrumAt(phase + 0.33), maxPeakR * 0.92, 0.09 + energy * 0.16 + breath * 0.04);
      fillGlow(spectrumAt(phase), maxPeakR * 0.6, 0.14 + energy * 0.26 + breath * 0.06);

      // ── Luminous core: a bright, near-white centre that breathes. Two stacked
      // gradients (warm-white inner + spectrum mid) give it depth without an
      // edge. ──
      var coreR = (baseR * 0.42) * (0.78 + breath * 0.22 + energy * 0.28);
      var coreHue = spectrumAt(phase + 0.15);
      var cg = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreR);
      cg.addColorStop(0, 'rgba(255,255,255,' + Math.min(0.95, 0.45 + energy * 0.45 + breath * 0.12).toFixed(3) + ')');
      cg.addColorStop(0.45, rgba(coreHue, 0.35 + energy * 0.3 + breath * 0.08));
      cg.addColorStop(1, rgba(coreHue, 0));
      ctx.fillStyle = cg;
      ctx.beginPath(); ctx.arc(cx, cy, coreR, 0, Math.PI * 2); ctx.fill();

      // Bright pinpoint that pulses with the breath — the orb's "spark".
      var dotR = (baseR * 0.06) * (0.8 + breath * 0.4) + energy * (baseR * 0.05);
      var dg = ctx.createRadialGradient(cx, cy, 0, cx, cy, dotR + 0.001);
      dg.addColorStop(0, 'rgba(255,255,255,' + Math.min(1, 0.6 + energy * 0.4 + breath * 0.15).toFixed(3) + ')');
      dg.addColorStop(1, 'rgba(255,255,255,0)');
      ctx.fillStyle = dg;
      ctx.beginPath(); ctx.arc(cx, cy, dotR, 0, Math.PI * 2); ctx.fill();

      ctx.globalCompositeOperation = 'source-over';
    }
    render();

    function destroy() {
      if (rafId) { global.cancelAnimationFrame(rafId); rafId = null; }
      dropSource();
      if (audioCtx && audioCtx.state !== 'closed') { try { audioCtx.close(); } catch (e) {} }
      audioCtx = null; analyser = null;
    }

    return {
      connectAudioElement: connectAudioElement,
      connectStream: connectStream,
      disconnect: disconnect,
      setActive: setActive,
      destroy: destroy,
    };
  }

  global.HartVoiceOrbViz = createVoiceOrbViz;
})(typeof window !== 'undefined' ? window : this);
