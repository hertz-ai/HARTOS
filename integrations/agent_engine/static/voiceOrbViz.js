/*
 * voiceOrbViz.js — HART OS glass-shell NATIVE voice visualiser.
 *
 * A framework-agnostic (vanilla, no React) canvas renderer so the OS liquid-ui
 * shell can draw the floating voice orb itself — no React bundle, no iframe.
 * The look matches Nunba's React VoiceVisualizer (3 energy bands -> sine
 * harmonics, neon teal glow, breathing core); this is the deliberate native
 * twin for the lean OS shell, kept separate from the React component on purpose
 * (forcing React into the shell would pull an unnecessary dependency).
 *
 * Served by the shell at /shell/static/voiceOrbViz.js and used via:
 *   const orb = window.HartVoiceOrbViz(canvas, { size, style });
 *   orb.connectAudioElement(ttsAudioEl);  // speaking (TTS playback element)
 *   orb.connectStream(micStream);         // listening (mic)
 *   orb.setActive(true|false);
 *   orb.setBreathing(true|false);         // idle breathe modulation (persisted pref)
 *   orb.setStyle('vibrant'|'ring-orb'|'nebula'|'minimal'|'pulse');  // #140 varieties
 *   orb.disconnect(); orb.destroy();
 *
 * Orb VARIETIES (#140 / checklist c3 "why do we have different orb and are the
 * orb switchable?"): the visual is COLOUR- + FLAG-parameterised from a small
 * per-style palette table (STYLES). The DEFAULT style 'vibrant' holds the EXACT
 * legacy teal numbers, so the out-of-the-box canvas is pixel-identical to before
 * — switching is purely additive. Only the hue triples + a couple of booleans
 * (rings / filled waveform / wave amplitude / breathe pulse gain) vary per style,
 * so no geometry changes and the hang-free software floor is untouched (the whole
 * canvas is already gated by the active flag — it costs nothing when idle).
 */
(function (global) {
  'use strict';

  var PTS = 180;

  // ── Orb style palettes (#140). Each style names the SAME seven hue roles the
  // renderer paints with (so the geometry is shared, only colour + a few flags
  // differ). 'vibrant' = the exact legacy teal numbers => the default canvas is
  // unchanged. rgb triples as [r,g,b]; flags: rings (stroke rings), wave (filled
  // iridescent waveform blob), waveGain (amplitude ×), pulseGain (breathe ×).
  var STYLES = {
    // Teal sphere — the brand default (b1.2). EXACT legacy numbers => no visual
    // regression out of the box; the teal/violet DUOTONE lives in the hero aura +
    // the palette accents, keeping this canvas the calm teal centre.
    'vibrant': {
      glow: [0, 230, 195], dark: [0, 170, 150], bright: [120, 250, 225],
      ringHi: [140, 252, 228], glowDark: [0, 160, 140], halo: [185, 253, 238],
      core: [212, 254, 245], aura: [0, 230, 195],
      rings: true, wave: true, waveGain: 1, pulseGain: 1
    },
    // Ring orb — cyan, rings emphasised + the filled waveform damped so it reads
    // as concentric rings rather than a blob.
    'ring-orb': {
      glow: [41, 197, 255], dark: [20, 118, 178], bright: [150, 220, 255],
      ringHi: [175, 232, 255], glowDark: [18, 104, 165], halo: [200, 236, 255],
      core: [222, 242, 255], aura: [90, 160, 255],
      rings: true, wave: true, waveGain: 0.5, pulseGain: 1
    },
    // Nebula — magenta/violet body with a cyan accent ring (the cosmic duotone).
    'nebula': {
      glow: [180, 70, 220], dark: [110, 40, 150], bright: [255, 150, 232],
      ringHi: [200, 120, 255], glowDark: [120, 42, 145], halo: [255, 192, 242],
      core: [255, 222, 250], aura: [41, 197, 255],
      rings: true, wave: true, waveGain: 1.1, pulseGain: 1
    },
    // Minimal — a calm, near-white core with no rings + no waveform (quiet).
    'minimal': {
      glow: [122, 140, 162], dark: [70, 84, 100], bright: [200, 214, 230],
      ringHi: [200, 214, 230], glowDark: [60, 74, 90], halo: [230, 240, 248],
      core: [236, 241, 244], aura: [0, 230, 195],
      rings: false, wave: false, waveGain: 0.6, pulseGain: 0.4
    },
    // Pulse — teal, but a strong slow breathing pulse (the living-presence look).
    'pulse': {
      glow: [0, 230, 195], dark: [0, 170, 150], bright: [120, 250, 225],
      ringHi: [140, 252, 228], glowDark: [0, 160, 140], halo: [185, 253, 238],
      core: [212, 254, 245], aura: [0, 230, 195],
      rings: true, wave: true, waveGain: 1.15, pulseGain: 1.9
    }
  };
  var DEFAULT_STYLE = 'vibrant';
  // The canonical picker list (id + label). One source; hartPersonalize reads it.
  var STYLE_LIST = [
    { id: 'vibrant', name: 'Vibrant' },
    { id: 'ring-orb', name: 'Ring Orb' },
    { id: 'nebula', name: 'Nebula' },
    { id: 'minimal', name: 'Minimal' },
    { id: 'pulse', name: 'Pulse' }
  ];

  function createVoiceOrbViz(canvas, opts) {
    opts = opts || {};
    var ctx = canvas.getContext('2d');
    if (!ctx) return { connectAudioElement: function () {}, connectStream: function () {}, disconnect: function () {}, setActive: function () {}, setBreathing: function () {}, setStyle: function () {}, destroy: function () {} };

    var audioCtx = null, analyser = null, source = null, lastKey = null;
    var active = false, rafId = null, breathing = (opts.breathing !== false);
    var styleId = (opts.style && STYLES[opts.style]) ? opts.style : DEFAULT_STYLE;
    var pal = STYLES[styleId];
    var s = { bass: 0, mid: 0, treble: 0, bassCur: 0, midCur: 0, trebleCur: 0, time: 0, dir: 1, wasQuiet: false };
    var oR = new Float32Array(PTS + 1);
    var freqData = new Uint8Array(256);

    var W = canvas.width, H = canvas.height;
    var cx = W / 2, cy = H / 2, baseR = W * 0.25;

    // Build an rgba() string from a style hue triple + alpha (the ONE place a
    // colour is composed, so every layer re-tints together when the style flips).
    function rgba(triple, a) {
      return 'rgba(' + triple[0] + ',' + triple[1] + ',' + triple[2] + ',' + a + ')';
    }

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
      if (source) { try { source.disconnect(); } catch (e) { console.debug('voiceOrbViz: audio source disconnect failed', e); } source = null; }
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
      } catch (e) { console.error('voiceOrbViz: connectAudioElement failed', e); }
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
      } catch (e) { console.error('voiceOrbViz: connectStream failed', e); }
    }
    function disconnect() { dropSource(); }
    function setActive(v) {
      active = !!v;
      if (active && audioCtx && audioCtx.state === 'suspended') { try { audioCtx.resume(); } catch (e) { console.debug('voiceOrbViz: audioCtx resume failed', e); } }
      // Restart the loop if the software-floor idle park stopped it. Without this
      // the orb would stay frozen the first time it is asked to speak/listen.
      if (active && rafId === null) { render(); }
    }
    // FIX B: the persisted orb-breathing pref (hartHero owns the localStorage key)
    // gates the slow idle "breathe" modulation of the glow + core. When OFF the orb
    // is a calm, static presence; voice ENERGY still reacts (that is not breathing).
    function setBreathing(v) { breathing = !!v; }
    // #140: switch the orb VARIETY live. Unknown ids are ignored (keeps the current
    // style), so a stale persisted value can never blank the orb. Re-tints on the
    // very next frame (render reads pal through the closure).
    function setStyle(name) {
      if (name && STYLES[name]) { styleId = name; pal = STYLES[name]; }
    }

    function drawRing(color, lw) {
      ctx.beginPath();
      for (var i = 0; i <= PTS; i++) {
        var a = (i / PTS) * Math.PI * 2;
        var x = cx + Math.cos(a) * oR[i], y = cy + Math.sin(a) * oR[i];
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.closePath();
      ctx.strokeStyle = color; ctx.lineWidth = lw; ctx.stroke();
    }

    // SOFTWARE-FLOOR IDLE PARK (2026-08-18, real-HW hover freeze).
    // This loop re-arms unconditionally, so the canvas repaints ~60x/s FOREVER --
    // even inactive, because the idle term below keeps undulating. On a GPU that
    // is free. On the cairo/pixman floor every repaint also re-rasters whatever
    // CSS filter sits on the canvas, and hovering scales it, which is what froze
    // the box. The CSS floor now drops the orb's drop-shadows; this drops the
    // needless frames too, so an idle desktop costs nothing at all.
    //
    // Gated to the software floor ONLY (body.gpu-software / body.webkit-flat,
    // the same verdict classes the CSS floor keys on) so the designed idle
    // undulation is untouched wherever compositing can actually pay for it.
    // Parking is safe: setActive(true) restarts the loop, and the amplitudes are
    // decayed to ~0 before we stop, so the parked frame IS the resting orb.
    function softwareFloor() {
      try {
        var b = global.document && global.document.body;
        return !!(b && (b.classList.contains('gpu-software') ||
                        b.classList.contains('webkit-flat')));
      } catch (e) { return false; }
    }

    function idleAtRest() {
      return !active &&
        s.bassCur < 0.002 && s.midCur < 0.002 && s.trebleCur < 0.002;
    }

    function render() {
      // Park instead of re-arming once everything has settled, on the floor where
      // a spare frame is expensive. Any setActive(true) kicks it back off.
      if (softwareFloor() && idleAtRest()) { rafId = null; return; }
      rafId = global.requestAnimationFrame(render);
      s.time += 0.02;
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
      var t = s.time, d = s.dir;
      var wg = pal.waveGain, pg = pal.pulseGain;
      ctx.clearRect(0, 0, W, H);

      var bg = ctx.createRadialGradient(cx, cy, baseR - 10, cx, cy, baseR + 70);
      bg.addColorStop(0, rgba(pal.glow, (0.02 + energy * 0.06).toFixed(3)));
      bg.addColorStop(1, 'rgba(10,9,20,0)');
      ctx.fillStyle = bg;
      ctx.beginPath(); ctx.arc(cx, cy, baseR + 70, 0, Math.PI * 2); ctx.fill();

      var maxPeakR = baseR;
      for (var i = 0; i <= PTS; i++) {
        var a = (i / PTS) * Math.PI * 2;
        var idle = 6 * Math.sin(2 * a + t * 0.6) + 4 * Math.sin(3 * a - t * 0.45) + 3 * Math.sin(5 * a + t * 0.7);
        var wave = idle +
          s.bassCur * 55 * Math.sin(2 * a + t * 1.5 * d) +
          s.bassCur * 32 * Math.sin(3 * a - t * 0.8 * d) +
          s.midCur * 40 * Math.sin(4 * a + t * 2.2 * d) +
          s.midCur * 24 * Math.sin(6 * a - t * 1.3 * d) +
          s.trebleCur * 28 * Math.sin(8 * a + t * 3.0 * d) +
          s.trebleCur * 16 * Math.sin(11 * a - t * 1.8 * d);
        wave *= wg;
        var rectified = (wave * wave) / (Math.abs(wave) + 8);
        oR[i] = baseR + rectified * (baseR / 100);
        if (oR[i] > maxPeakR) maxPeakR = oR[i];
      }

      // Filled iridescent waveform blob — the main body. Some styles (minimal)
      // drop it entirely for a quiet core; ring-orb damps it via waveGain.
      if (pal.wave) {
        ctx.beginPath();
        for (var i = 0; i <= PTS; i++) {
          var a = (i / PTS) * Math.PI * 2;
          var x = cx + Math.cos(a) * oR[i], y = cy + Math.sin(a) * oR[i];
          if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        for (var i = PTS; i >= 0; i--) {
          var a = (i / PTS) * Math.PI * 2;
          ctx.lineTo(cx + Math.cos(a) * baseR, cy + Math.sin(a) * baseR);
        }
        ctx.closePath();
        if (maxPeakR > baseR + 1) {
          var fg = ctx.createRadialGradient(cx, cy, baseR, cx, cy, maxPeakR);
          fg.addColorStop(0, 'rgba(10,9,20,0)');
          fg.addColorStop(0.3, rgba(pal.dark, (0.08 + energy * 0.15).toFixed(3)));
          fg.addColorStop(0.7, rgba(pal.glow, (0.15 + energy * 0.25).toFixed(3)));
          fg.addColorStop(1, rgba(pal.bright, (0.25 + energy * 0.4).toFixed(3)));
          ctx.fillStyle = fg;
        } else {
          ctx.fillStyle = rgba(pal.glow, 0.05);
        }
        ctx.fill();
      }

      // Stroke rings — some styles (minimal) drop them. Ring #2 uses the style's
      // AURA hue so a duotone style (nebula, ring-orb) reads two-tone; on 'vibrant'
      // aura == glow so it is identical to the legacy look.
      if (pal.rings) {
        ctx.globalCompositeOperation = 'lighter';
        drawRing(rgba(pal.glow, (0.04 + energy * 0.05).toFixed(3)), 14);
        drawRing(rgba(pal.aura, (0.08 + energy * 0.1).toFixed(3)), 6);
        drawRing(rgba(pal.ringHi, (0.5 + energy * 0.5).toFixed(3)), 1.8);
        ctx.globalCompositeOperation = 'source-over';
      }

      var breathe1 = breathing ? ((Math.sin(t * 1.2) * 0.3 + Math.sin(t * 1.9) * 0.15) * pg) : 0;
      var breathe2 = breathing ? ((Math.sin(t * 0.8) * 0.2 + Math.cos(t * 1.4) * 0.1) * pg) : 0;
      var glowR = (8 + energy * 12 + breathe1 * 4) * 3;
      var cg = ctx.createRadialGradient(cx, cy, 0, cx, cy, glowR);
      cg.addColorStop(0, rgba(pal.halo, (0.15 + energy * 0.5 + breathe1 * 0.08).toFixed(3)));
      cg.addColorStop(0.3, rgba(pal.glow, (0.08 + energy * 0.2 + breathe2 * 0.04).toFixed(3)));
      cg.addColorStop(0.6, rgba(pal.glowDark, (0.03 + energy * 0.08 + breathe1 * 0.02).toFixed(3)));
      cg.addColorStop(1, rgba(pal.glow, 0));
      ctx.fillStyle = cg;
      ctx.beginPath(); ctx.arc(cx, cy, glowR, 0, Math.PI * 2); ctx.fill();

      var coreR = 3 + energy * 6 + breathe1 * 1.5;
      var cg2 = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreR);
      cg2.addColorStop(0, rgba(pal.core, (0.3 + energy * 0.5 + breathe2 * 0.1).toFixed(3)));
      cg2.addColorStop(0.5, rgba(pal.glow, (0.1 + energy * 0.3 + breathe1 * 0.05).toFixed(3)));
      cg2.addColorStop(1, rgba(pal.glow, 0));
      ctx.fillStyle = cg2;
      ctx.beginPath(); ctx.arc(cx, cy, coreR, 0, Math.PI * 2); ctx.fill();

      var dotR = 1.5 + energy * 2.5 + Math.sin(t * 2.5) * 0.6;
      ctx.fillStyle = 'rgba(255,255,255,' + (0.15 + energy * 0.7 + breathe2 * 0.1).toFixed(3) + ')';
      ctx.beginPath(); ctx.arc(cx, cy, dotR, 0, Math.PI * 2); ctx.fill();
    }
    render();

    function destroy() {
      if (rafId) { global.cancelAnimationFrame(rafId); rafId = null; }
      dropSource();
      if (audioCtx && audioCtx.state !== 'closed') { try { audioCtx.close(); } catch (e) { console.debug('voiceOrbViz: audioCtx close failed', e); } }
      audioCtx = null; analyser = null;
    }

    return {
      connectAudioElement: connectAudioElement,
      connectStream: connectStream,
      disconnect: disconnect,
      setActive: setActive,
      setBreathing: setBreathing,
      setStyle: setStyle,
      getStyle: function () { return styleId; },
      destroy: destroy,
    };
  }

  // Expose the canonical style list (id + label) + default so the customization
  // hub (hartPersonalize.js) drives its picker from ONE source, no parallel table.
  createVoiceOrbViz.STYLES = STYLE_LIST;
  createVoiceOrbViz.DEFAULT_STYLE = DEFAULT_STYLE;
  global.HartVoiceOrbViz = createVoiceOrbViz;
})(typeof window !== 'undefined' ? window : this);
