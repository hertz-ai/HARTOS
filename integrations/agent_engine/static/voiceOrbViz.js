/*
 * voiceOrbViz.js — HART OS glass-shell NATIVE voice visualiser.
 *
 * A framework-agnostic (vanilla, no React) canvas renderer so the OS liquid-ui
 * shell can draw the floating voice orb itself — no React bundle, no iframe.
 * The look matches Nunba's React VoiceVisualizer (3 energy bands -> sine
 * harmonics, neon #6C63FF glow, breathing core); this is the deliberate native
 * twin for the lean OS shell, kept separate from the React component on purpose
 * (forcing React into the shell would pull an unnecessary dependency).
 *
 * Served by the shell at /shell/static/voiceOrbViz.js and used via:
 *   const orb = window.HartVoiceOrbViz(canvas, { size });
 *   orb.connectAudioElement(ttsAudioEl);  // speaking (TTS playback element)
 *   orb.connectStream(micStream);         // listening (mic)
 *   orb.setActive(true|false);
 *   orb.disconnect(); orb.destroy();
 */
(function (global) {
  'use strict';

  var PTS = 180;

  function createVoiceOrbViz(canvas, opts) {
    opts = opts || {};
    var ctx = canvas.getContext('2d');
    if (!ctx) return { connectAudioElement: function () {}, connectStream: function () {}, disconnect: function () {}, setActive: function () {}, destroy: function () {} };

    var audioCtx = null, analyser = null, source = null, lastKey = null;
    var active = false, rafId = null;
    var s = { bass: 0, mid: 0, treble: 0, bassCur: 0, midCur: 0, trebleCur: 0, time: 0, dir: 1, wasQuiet: false };
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

    function render() {
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
      ctx.clearRect(0, 0, W, H);

      var bg = ctx.createRadialGradient(cx, cy, baseR - 10, cx, cy, baseR + 70);
      bg.addColorStop(0, 'rgba(108,99,255,' + (0.02 + energy * 0.06).toFixed(3) + ')');
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
        var rectified = (wave * wave) / (Math.abs(wave) + 8);
        oR[i] = baseR + rectified * (baseR / 100);
        if (oR[i] > maxPeakR) maxPeakR = oR[i];
      }

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
        fg.addColorStop(0.3, 'rgba(80,60,220,' + (0.08 + energy * 0.15).toFixed(3) + ')');
        fg.addColorStop(0.7, 'rgba(108,99,255,' + (0.15 + energy * 0.25).toFixed(3) + ')');
        fg.addColorStop(1, 'rgba(150,140,255,' + (0.25 + energy * 0.4).toFixed(3) + ')');
        ctx.fillStyle = fg;
      } else {
        ctx.fillStyle = 'rgba(108,99,255,0.05)';
      }
      ctx.fill();

      ctx.globalCompositeOperation = 'lighter';
      drawRing('rgba(108,99,255,' + (0.04 + energy * 0.05).toFixed(3) + ')', 14);
      drawRing('rgba(108,99,255,' + (0.08 + energy * 0.1).toFixed(3) + ')', 6);
      drawRing('rgba(170,165,255,' + (0.5 + energy * 0.5).toFixed(3) + ')', 1.8);
      ctx.globalCompositeOperation = 'source-over';

      var breathe1 = Math.sin(t * 1.2) * 0.3 + Math.sin(t * 1.9) * 0.15;
      var breathe2 = Math.sin(t * 0.8) * 0.2 + Math.cos(t * 1.4) * 0.1;
      var glowR = (8 + energy * 12 + breathe1 * 4) * 3;
      var cg = ctx.createRadialGradient(cx, cy, 0, cx, cy, glowR);
      cg.addColorStop(0, 'rgba(200,195,255,' + (0.15 + energy * 0.5 + breathe1 * 0.08).toFixed(3) + ')');
      cg.addColorStop(0.3, 'rgba(108,99,255,' + (0.08 + energy * 0.2 + breathe2 * 0.04).toFixed(3) + ')');
      cg.addColorStop(0.6, 'rgba(80,60,200,' + (0.03 + energy * 0.08 + breathe1 * 0.02).toFixed(3) + ')');
      cg.addColorStop(1, 'rgba(108,99,255,0)');
      ctx.fillStyle = cg;
      ctx.beginPath(); ctx.arc(cx, cy, glowR, 0, Math.PI * 2); ctx.fill();

      var coreR = 3 + energy * 6 + breathe1 * 1.5;
      var cg2 = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreR);
      cg2.addColorStop(0, 'rgba(220,215,255,' + (0.3 + energy * 0.5 + breathe2 * 0.1).toFixed(3) + ')');
      cg2.addColorStop(0.5, 'rgba(108,99,255,' + (0.1 + energy * 0.3 + breathe1 * 0.05).toFixed(3) + ')');
      cg2.addColorStop(1, 'rgba(108,99,255,0)');
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
