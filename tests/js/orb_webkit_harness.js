/*
 * orb_webkit_harness.js — execute voiceOrbViz.js as if on OLD WebKitGTK.
 *
 * The NixOS 24.11 ISO ships a WebKitGTK that lacks AbortSignal.timeout(); a raw
 * call throws a TypeError that crashes the whole inline <script> block (this is
 * what killed toggleStartMenu — and would kill the orb's initHartOrb, which
 * lives in the same block). This harness proves the orb RENDERER itself never
 * depends on such APIs: it deletes AbortSignal, stubs the canvas/AudioContext,
 * loads voiceOrbViz.js, builds an orb and drives every method — asserting none
 * of it throws on the old engine.
 *
 * Usage:  node orb_webkit_harness.js /abs/path/to/voiceOrbViz.js
 * Exit 0 + "PASS" on success; non-zero + "FAIL: ..." otherwise.
 */
'use strict';

global.window = global;        // voiceOrbViz attaches HartVoiceOrbViz to window
global.AbortSignal = undefined; // simulate old WebKitGTK (no AbortSignal.timeout)
global.requestAnimationFrame = function () { return 1; };
global.cancelAnimationFrame = function () {};

function AudioCtx() { this.state = 'running'; this.destination = {}; }
AudioCtx.prototype.createAnalyser = function () {
  return { fftSize: 0, smoothingTimeConstant: 0, connect: function () {}, getByteFrequencyData: function () {} };
};
AudioCtx.prototype.createMediaElementSource = function () { return { connect: function () {}, disconnect: function () {} }; };
AudioCtx.prototype.createMediaStreamSource = function () { return { connect: function () {}, disconnect: function () {} }; };
AudioCtx.prototype.resume = function () {};
AudioCtx.prototype.close = function () {};
global.AudioContext = AudioCtx;

function noop() {}
function ctx2d() {
  return {
    clearRect: noop, beginPath: noop, arc: noop, fill: noop, moveTo: noop,
    lineTo: noop, closePath: noop, stroke: noop,
    createRadialGradient: function () { return { addColorStop: noop }; },
    fillStyle: null, strokeStyle: null, lineWidth: 0, globalCompositeOperation: null,
  };
}
var canvas = { width: 320, height: 320, getContext: function () { return ctx2d(); } };

function fail(msg) { console.error('FAIL: ' + msg); process.exit(1); }

var vizPath = process.argv[2];
if (!vizPath) fail('no voiceOrbViz.js path argument');

try {
  require(vizPath); // runs the IIFE -> window.HartVoiceOrbViz
} catch (e) {
  fail('loading voiceOrbViz threw on old WebKit: ' + e);
}

if (typeof global.HartVoiceOrbViz !== 'function') fail('HartVoiceOrbViz not defined');

var orb;
try {
  orb = global.HartVoiceOrbViz(canvas, {});
} catch (e) {
  fail('createVoiceOrbViz threw: ' + e);
}

var api = ['setActive', 'connectAudioElement', 'connectStream', 'disconnect', 'destroy'];
for (var i = 0; i < api.length; i++) {
  if (typeof orb[api[i]] !== 'function') fail('missing API method: ' + api[i]);
}

try {
  orb.setActive(true);
  orb.setActive(false);
  orb.connectAudioElement(null);
  orb.connectStream(null);
  orb.disconnect();
  orb.destroy();
} catch (e) {
  fail('an orb method threw with AbortSignal removed: ' + e);
}

console.log('PASS: voiceOrbViz renders + drives without AbortSignal (old-WebKitGTK safe)');
process.exit(0);
