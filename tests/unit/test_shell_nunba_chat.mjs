/*
 * Behavioural test: Nunba is the shell's chat (2026-10-12).
 *
 * The shell's own chat bubble (#agent-pill / #assistant-chat) is gone. acSend
 * stays the ONE intent dispatcher: theme words and a named app still resolve
 * locally, and free-form intent is a conversation that lands in Nunba's
 * composer through window.HartNunba (postMessage into the same-origin
 * assistant panel frame; Nunba's half is landing-page/src/utils/shellBridge.js).
 *
 * Drives the REAL inline block, sliced from the shell python renders
 * (test_shell_poll_diet.mjs's idiom), on the shared DOM shim with the panel
 * primitives stubbed, and asserts the OBSERVABLE side effects:
 *   [A] a named app launches via openPanel and never reaches Nunba
 *   [B] free-form intent opens the assistant panel; the text waits for Nunba's
 *       nunba:ready and is then posted as nunba:compose {send:true} to our origin
 *   [C] nunba:turn start/done drives window._hartThinking (the orb lamp)
 *   [D] foreign origins / other windows are ignored
 *   [E] a prefill (compose without send) posts send:false
 *   [F] toggleAssistantChat closes and reopens; a fresh frame must announce again
 *   [G] a page that never announces does not hang: toast + lamp cleared
 *
 * Run:  node tests/unit/test_shell_nunba_chat.mjs
 */
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { makeRealm, makeEl } from './shell_dom_shim.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..', '..');
const ORIGIN = 'http://127.0.0.1:6800';

let failures = 0;
function ok(c, m) { if (c) { console.log('  OK   ' + m); } else { failures++; console.log(' FAIL  ' + m); } }
function eq(a, b, m) { ok(a === b, m + '  (got ' + JSON.stringify(a) + ', want ' + JSON.stringify(b) + ')'); }

function renderShell() {
  const PY = [process.env.HART_TEST_PYTHON, 'C:/Users/sathi/miniconda3/python.exe', 'python', 'python3'].filter(Boolean);
  const RENDER = "import sys; sys.path.insert(0,'.');" +
    "from integrations.agent_engine.liquid_ui_service import LiquidUIService;" +
    "(getattr(sys.stdout,'reconfigure',lambda **k:None))(encoding='utf-8');" +
    "print(LiquidUIService().render_desktop_shell())";
  for (const py of PY) {
    try { return execFileSync(py, ['-c', RENDER], { cwd: ROOT, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] }); }
    catch (e) { /* next interpreter */ }
  }
  return null;
}
const HTML = renderShell();
ok(!!HTML, 'rendered the real desktop shell through python');
ok(HTML.indexOf('id="assistant-chat"') < 0 && HTML.indexOf('id="agent-pill"') < 0, 'the shell ships no chat bubble and no pill of its own');
function chatBlock() {
  const s = HTML.indexOf('// ═══ Nunba is the chat ═══');
  const e = HTML.indexOf('function handleThemeCommand(text, resp) {');
  ok(s >= 0 && e > s, 'sliced the inline Nunba-chat block');
  return HTML.slice(s, e);
}

function boot() {
  const R = makeRealm();
  const panelsEl = R.el('panels');
  const calls = { openPanel: [], closePanel: [], loadIframe: [], toasts: [], theme: [], posted: [] };
  R.sandbox.location = { origin: ORIGIN };
  R.sandbox.panels = {};
  R.sandbox.MANIFEST = { files: { title: 'Files' }, firefox: { title: 'Firefox', exec: 'firefox' } };
  R.sandbox.showToast = (t, m, sev) => calls.toasts.push({ t, m, sev });
  R.sandbox.handleThemeCommand = (lower, resp) => { calls.theme.push(lower); resp.textContent = 'Applied'; };
  R.sandbox.loadIframe = (id, route) => calls.loadIframe.push({ id, route });
  // The panel primitives, as openPanel/closePanel shape the DOM: #panel-<id>
  // holding #panel-body-<id> with the route iframe.
  R.sandbox.openPanel = (id) => {
    calls.openPanel.push(id);
    if (R.sandbox.panels[id]) return;                       // reuse + front (the real one)
    const el = makeEl('div', R.state); el.setAttribute('id', 'panel-' + id); el.dataset.panelId = id;
    const body = makeEl('div', R.state); body.setAttribute('id', 'panel-body-' + id);
    const frame = makeEl('iframe', R.state); frame.setAttribute('class', 'route-frame');
    frame.contentWindow = { postMessage: (m, origin) => calls.posted.push({ m, origin, frame }) };
    body.appendChild(frame); el.appendChild(body); panelsEl.appendChild(el);
    R.sandbox.panels[id] = { el };
    R.notifyObservers([{ type: 'childList' }]);
  };
  R.sandbox.closePanel = (id) => {
    calls.closePanel.push(id);
    const p = R.sandbox.panels[id]; if (!p) return;
    p.el.remove(); delete R.sandbox.panels[id];
    R.notifyObservers([{ type: 'childList' }]);
  };
  R.run(chatBlock(), 'shell-nunba-chat.js');
  function frameOf(id) { return R.document.getElementById('panel-body-' + id).querySelector('.route-frame'); }
  function message(data, extra) {
    const f = frameOf('assistant');
    R.window.dispatch('message', Object.assign({ origin: ORIGIN, source: f && f.contentWindow, data }, extra || {}));
  }
  return { R, calls, frameOf, message, S: R.sandbox };
}

// ── [A] a named app never reaches Nunba ────────────────────────────────────
(function namedApp() {
  console.log('\n[A] acSend("open files"): the deterministic fast-path');
  const H = boot();
  H.S.acSend('open files');
  eq(H.calls.openPanel.join(','), 'files', 'the named app launched via openPanel');
  ok(!H.S.panels.assistant, 'the assistant panel did NOT open for an app name');
  eq(H.calls.posted.length, 0, 'nothing posted to Nunba');
  eq(H.S.window._hartThinking, false, 'lamp cleared on the local terminal path');
  ok(H.calls.toasts.some((t) => t.m === 'Opened Files'), 'the outcome reports through the shell toast');
  H.S.acSend('make it dark');
  eq(H.calls.theme.length, 1, 'theme words take the theme fast-path');
  eq(H.S.window._hartThinking, false, 'lamp cleared after the theme path');
})();

// ── [B] free-form intent is a conversation in Nunba ────────────────────────
(function conversation() {
  console.log('\n[B] acSend("what is the weather on mars"): opens Nunba, waits for ready, posts compose+send');
  const H = boot();
  const text = 'what is the weather on mars';
  H.S.acSend(text);
  ok(!!H.S.panels.assistant, 'the assistant panel opened');
  eq(H.S.window._hartThinking, true, 'the lamp is lit while the turn is pending');
  eq(H.calls.posted.length, 0, 'the text WAITS until Nunba\'s page announces itself');
  H.message({ type: 'nunba:ready', route: '/local' });
  eq(H.calls.posted.length, 1, 'ready -> the queued line is posted');
  const p = H.calls.posted[0];
  eq(p.m.type, 'nunba:compose', 'as nunba:compose');
  eq(p.m.text, text, 'with the text');
  eq(p.m.send, true, 'and send:true (dispatch, not a draft)');
  eq(p.origin, ORIGIN, 'to our own origin only');
  // Already ready: the next line goes straight through.
  H.S.acSend('and on venus');
  eq(H.calls.posted.length, 2, 'a ready page gets the next line at once');
  eq(H.calls.posted[1].m.text, 'and on venus', 'verbatim');

  // ── [C] the turn lamp mirrors Nunba ──
  console.log('\n[C] nunba:turn drives the orb lamp');
  H.message({ type: 'nunba:turn', state: 'done' });
  eq(H.S.window._hartThinking, false, 'done -> lamp off');
  H.message({ type: 'nunba:turn', state: 'start' });
  eq(H.S.window._hartThinking, true, 'start -> lamp on');

  // ── [D] foreign origins and other windows are ignored ──
  console.log('\n[D] only the assistant frame, only our origin');
  H.message({ type: 'nunba:turn', state: 'done' }, { origin: 'https://attacker.example' });
  eq(H.S.window._hartThinking, true, 'a foreign origin cannot touch the lamp');
  H.message({ type: 'nunba:turn', state: 'done' }, { source: { other: true } });
  eq(H.S.window._hartThinking, true, 'another window of our origin cannot either');
  H.message({ type: 'hart:action', kind: 'cart.add' });
  eq(H.S.window._hartThinking, true, 'another protocol is ignored');
  H.message({ type: 'nunba:turn', state: 'done' });
  eq(H.S.window._hartThinking, false, 'the frame itself still can');

  // ── [E] a prefill ──
  console.log('\n[E] HartNunba.compose(text, false) prefills without sending');
  H.S.HartNunba.compose('  draft this  ', false);
  const last = H.calls.posted[H.calls.posted.length - 1];
  eq(last.m.text, 'draft this', 'trimmed');
  eq(last.m.send, false, 'send:false');
})();

// ── [F] toggle closes/reopens; a fresh frame must announce again ───────────
(function toggle() {
  console.log('\n[F] toggleAssistantChat + a new frame');
  const H = boot();
  H.S.toggleAssistantChat();
  ok(!!H.S.panels.assistant, 'toggle opens the panel');
  H.message({ type: 'nunba:ready' });
  H.S.window._hartThinking = true;
  H.S.toggleAssistantChat();
  ok(!H.S.panels.assistant && H.calls.closePanel.join(',') === 'assistant', 'toggle closes it');
  eq(H.S.window._hartThinking, false, 'closing the panel mid-turn clears the lamp');
  H.S.acSend('hello again');
  ok(!!H.S.panels.assistant, 'a conversation reopens it');
  eq(H.calls.posted.length, 0, 'the NEW frame has not announced: the line waits');
  H.message({ type: 'nunba:ready' });
  eq(H.calls.posted.length, 1, 'announced -> posted');
  H.S.focusAgent();
  eq(H.calls.openPanel.filter((x) => x === 'assistant').length, 3, 'Super+A (focusAgent) fronts the same panel');
})();

// ── [G] a page without the bridge never hangs the orb ──────────────────────
(function readyTimeout() {
  console.log('\n[G] no nunba:ready within the wait: toast, lamp cleared, queue dropped');
  const H = boot();
  H.S.acSend('tell me a story');
  const t = H.R.timers.find((x) => x.ms === 15000);
  ok(!!t, 'a 15 s ready-wait is armed');
  t.fn();
  eq(H.S.window._hartThinking, false, 'lamp cleared');
  ok(H.calls.toasts.some((x) => x.sev === 'warning' && /still loading/.test(x.m)), 'the person is told');
  H.message({ type: 'nunba:ready' });
  eq(H.calls.posted.length, 0, 'the dropped line is not posted late');
})();

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);
