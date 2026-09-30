/*
 * Behavioural test: every floating sheet in the shell closes through ONE
 * dismissal set (integrations/agent_engine/static/hartDismiss.js).
 *
 * THE DEFECT (box, 2026-09-22): the Wi-Fi popover and the start menu closed on
 * outside clicks INSIDE the shell document (mousedown capture; click bubble) but
 * nothing closed them when the press landed on another surface or inside an
 * iframed panel, because those presses never reach the host document at all.
 * The context menu already had the complete set (pointerdown capture, Escape,
 * scroll/resize, window blur; blur is what fires when focus enters an iframe or
 * another surface). This driver proves the set is now shared, per sheet:
 *
 *   [A] hartDismiss.arm() itself: which events dismiss, which do not, disarm.
 *   [B] context menu   (hartContextMenu.js)   closes on blur / outside press / Escape / scroll
 *   [C] Wi-Fi popover  (hartConnectivity.js)  same set; a press on the cluster does NOT
 *                                             double-toggle; a scroll INSIDE the list stays
 *   [D] senses proof   (hartSenses.js)        same set; a press inside the pod stays
 *   [E] start menu     (inline shell script)  same set, via the REAL toggleStartMenu
 *
 * Run:  node tests/unit/test_shell_dismiss_sheets.mjs
 * (test_shell_dismiss_sheets.py shells out so pytest/CI pick it up.)
 */
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { makeRealm, makeEl, mkEv } from './shell_dom_shim.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..', '..');
const STATIC = join(ROOT, 'integrations', 'agent_engine', 'static');
const read = (f) => readFileSync(join(STATIC, f), 'utf8');

let failures = 0;
function ok(cond, msg) { if (cond) { console.log('  OK   ' + msg); } else { failures++; console.log(' FAIL  ' + msg); } }

function loadDismiss(R) { R.run(read('hartDismiss.js'), 'hartDismiss.js'); }

// ════════════════════════════════════════════════════════════════════════════
// [A] the helper on its own
// ════════════════════════════════════════════════════════════════════════════
(function testHelper() {
  console.log('\n[A] hartDismiss.arm(): the dismissal set');
  const R = makeRealm();
  loadDismiss(R);
  const D = R.window.HartDismiss;
  ok(D && typeof D.arm === 'function', 'window.HartDismiss.arm exists');

  const sheet = R.el('sheet');
  const inner = makeEl('div', R.state); sheet.appendChild(inner);
  const scroller = makeEl('div', R.state); sheet.appendChild(scroller);
  const outside = R.el('outside');

  function armed() {
    const got = [];
    const disarm = D.arm({ els: [sheet], onDismiss(reason) { got.push(reason); } });
    return { got, disarm };
  }

  let s = armed();
  R.document.dispatch('pointerdown', mkEv(inner));
  ok(s.got.length === 0, 'pointerdown INSIDE the sheet does not dismiss');
  R.document.dispatch('pointerdown', mkEv(outside));
  ok(s.got[0] === 'pointer', 'pointerdown OUTSIDE dismisses (capture, before any click handler)');
  R.document.dispatch('pointerdown', mkEv(outside));
  ok(s.got.length === 1, 'a fired set is disarmed: the next outside press does nothing');

  s = armed();
  const esc = R.document.dispatch('keydown', mkEv(R.body, { key: 'Escape' }));
  ok(s.got[0] === 'escape', 'Escape dismisses');
  ok(esc.defaultPrevented && esc._stopped, 'Escape is consumed (wins over the shell shortcut table)');

  s = armed();
  R.document.dispatch('keydown', mkEv(R.body, { key: 'ArrowDown' }));
  ok(s.got.length === 0, 'other keys are left to the sheet (keyboard nav stays with the menu)');

  R.window.dispatch('blur');
  ok(s.got[0] === 'blur', 'window blur dismisses: focus entering an iframe or another surface');

  s = armed();
  R.window.dispatch('scroll', mkEv(scroller));
  ok(s.got.length === 0, 'a scroll INSIDE the sheet (its own list) does not dismiss');
  R.window.dispatch('scroll', mkEv(R.document));
  ok(s.got[0] === 'scroll', 'a page scroll dismisses');

  s = armed();
  R.window.dispatch('resize');
  ok(s.got[0] === 'resize', 'resize dismisses');

  s = armed();
  s.disarm();
  R.document.dispatch('pointerdown', mkEv(outside));
  R.window.dispatch('blur');
  ok(s.got.length === 0, 'disarm() removes the whole set (no dismissal after close)');
  ok((R.document._listeners.pointerdown || []).length === 0 &&
     (R.window._listeners.blur || []).length === 0,
     'no listener is left behind once disarmed (no leak across open/close cycles)');

  const withFn = [];
  D.arm({ inside(t) { return t === inner; }, onDismiss(r) { withFn.push(r); } });
  R.document.dispatch('pointerdown', mkEv(inner));
  R.document.dispatch('pointerdown', mkEv(outside));
  ok(withFn.length === 1 && withFn[0] === 'pointer', 'inside() callback form works for sheets whose bounds are not one element');
})();

// ════════════════════════════════════════════════════════════════════════════
// [B] the context menu uses the shared set
// ════════════════════════════════════════════════════════════════════════════
(function testContextMenu() {
  console.log('\n[B] hartContextMenu.js  closes through the shared set');
  const R = makeRealm();
  loadDismiss(R);
  R.run(read('hartContextMenu.js'), 'hartContextMenu.js');
  const M = R.window.HartCtxMenu;
  const outside = R.el('outside');

  function open() { M.open([{ label: 'One', onClick() {} }, { label: 'Two', onClick() {} }], 40, 40); return M.isOpen(); }

  ok(open(), 'menu opens');
  R.window.dispatch('blur');
  ok(!M.isOpen(), 'window blur (focus into an iframe / another surface) closes the menu');

  ok(open(), 'menu re-opens');
  const menuEl = R.document.getElementById('hart-ctxmenu');
  R.document.dispatch('pointerdown', mkEv(menuEl.querySelector('.hart-ctx-item')));
  ok(M.isOpen(), 'a press on a menu row keeps it open');
  R.document.dispatch('pointerdown', mkEv(outside));
  ok(!M.isOpen(), 'a press outside closes the menu');

  ok(open(), 'menu re-opens');
  R.document.dispatch('keydown', mkEv(R.body, { key: 'ArrowDown' }));
  ok(M.isOpen(), 'ArrowDown stays with the menu (keyboard nav intact)');
  ok(R.document.getElementById('hart-ctxmenu').getAttribute('data-active-idx') === '0', 'ArrowDown moved the roving focus to the first row');
  R.document.dispatch('keydown', mkEv(R.body, { key: 'Escape' }));
  ok(!M.isOpen(), 'Escape closes the menu');

  ok(open(), 'menu re-opens');
  R.window.dispatch('scroll', mkEv(R.document));
  ok(!M.isOpen(), 'a page scroll closes the menu');
  ok((R.document._listeners.keydown || []).length === 0, 'closing removed the keyboard listeners (no leak)');
})();

// ════════════════════════════════════════════════════════════════════════════
// [C] the Wi-Fi quick-settings popover
// ════════════════════════════════════════════════════════════════════════════
(function testConnectivityPopover() {
  console.log('\n[C] hartConnectivity.js  popover closes through the shared set');
  const R = makeRealm();
  loadDismiss(R);
  const topbar = makeEl('div', R.state); topbar.setAttribute('class', 'top-bar-right'); R.body.appendChild(topbar);
  R.el('hc-net-list');
  R.sandbox.fetch = () => new Promise(() => {});   // probes never settle; irrelevant here
  R.run(read('hartConnectivity.js'), 'hartConnectivity.js');
  const C = R.window.HartConnectivity;
  const outside = R.el('outside');

  function isOpen() { const p = R.document.getElementById('hc-popover'); return !!(p && p.classList.contains('open')); }

  C.open();
  ok(isOpen(), 'popover opens');
  R.window.dispatch('blur');
  ok(!isOpen(), 'window blur closes the popover (the box defect: a press on another surface / an iframe)');

  C.open();
  const pop = R.document.getElementById('hc-popover');
  // renderPopover writes the list as an innerHTML string (the shim keeps no
  // children for those), so re-parent the registered list INTO the popover, as
  // the real DOM has it, before probing an inside scroll.
  const list = R.document.getElementById('hc-net-list');
  pop.appendChild(list);
  R.document.dispatch('pointerdown', mkEv(pop));
  ok(isOpen(), 'a press inside the popover keeps it open');
  R.window.dispatch('scroll', mkEv(list));
  ok(isOpen(), 'scrolling the network list does not close it');
  R.document.dispatch('pointerdown', mkEv(outside));
  ok(!isOpen(), 'a press outside closes it');

  C.open();
  R.document.dispatch('keydown', mkEv(R.body, { key: 'Escape' }));
  ok(!isOpen(), 'Escape closes it');

  // The cluster button toggles the popover on click. Its pointerdown must NOT
  // dismiss first, or the click would immediately re-open (a double toggle).
  C.open();
  const cluster = R.document.getElementById('hc-cluster');
  R.document.dispatch('pointerdown', mkEv(cluster));
  ok(isOpen(), 'a press on the tray cluster does not dismiss (the click toggles it instead)');
  cluster.click();
  ok(!isOpen(), 'the cluster click then closes it (one toggle, not two)');
  ok((R.document._listeners.pointerdown || []).length === 0, 'no dismiss listener left armed after close');
})();

// ════════════════════════════════════════════════════════════════════════════
// [D] the senses proof panel
// ════════════════════════════════════════════════════════════════════════════
(function testSensesPanel() {
  console.log('\n[D] hartSenses.js  proof panel closes through the shared set');
  const R = makeRealm();
  loadDismiss(R);
  const pod = R.el('hart-senses');
  const panel = R.el('hart-senses-panel', pod);
  const btn = R.el('hart-senses-btn', pod);
  R.el('hart-senses-proof', panel);
  R.el('hart-hero');
  R.sandbox.fetch = () => new Promise(() => {});
  R.run(read('hartSenses.js'), 'hartSenses.js');
  R.flushTimers();
  const outside = R.el('outside');

  btn.dispatch('contextmenu', mkEv(btn));
  ok(panel.classList.contains('open'), 'right-click on the eye opens the proof panel');
  R.window.dispatch('blur');
  ok(!panel.classList.contains('open'), 'window blur closes the proof panel');

  btn.dispatch('contextmenu', mkEv(btn));
  R.document.dispatch('pointerdown', mkEv(panel));
  ok(panel.classList.contains('open'), 'a press inside the pod (drag start, proof rows) keeps it open');
  R.document.dispatch('pointerdown', mkEv(outside));
  ok(!panel.classList.contains('open'), 'a press outside closes it');

  btn.dispatch('contextmenu', mkEv(btn));
  R.document.dispatch('keydown', mkEv(R.body, { key: 'Escape' }));
  ok(!panel.classList.contains('open'), 'Escape closes it');

  btn.dispatch('contextmenu', mkEv(btn));
  btn.dispatch('contextmenu', mkEv(btn));
  ok(!panel.classList.contains('open'), 'a second right-click toggles it shut');
  ok((R.document._listeners.pointerdown || []).length === 0, 'no dismiss listener left armed after close');
})();

// ════════════════════════════════════════════════════════════════════════════
// [E] the start menu (the REAL inline toggleStartMenu from the rendered shell)
// ════════════════════════════════════════════════════════════════════════════
(function testStartMenu() {
  console.log('\n[E] inline shell  start menu closes through the shared set');
  const PY = [process.env.HART_TEST_PYTHON, 'C:/Users/sathi/miniconda3/python.exe', 'python', 'python3'].filter(Boolean);
  const RENDER = "import sys; sys.path.insert(0,'.');" +
    "from integrations.agent_engine.liquid_ui_service import LiquidUIService;" +
    "(getattr(sys.stdout,'reconfigure',lambda **k:None))(encoding='utf-8');" +
    "print(LiquidUIService().render_desktop_shell())";
  let html = null;
  for (const py of PY) {
    try { html = execFileSync(py, ['-c', RENDER], { cwd: ROOT, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] }); break; }
    catch (e) { /* try the next interpreter */ }
  }
  ok(!!html, 'rendered the real desktop shell through python');
  if (!html) return;
  const s = html.indexOf('let _startDisarm = null;');   // the closer's state, declared just above toggleStartMenu
  const e = html.indexOf('function filterStart(');
  ok(s >= 0 && e > s, 'sliced the real toggleStartMenu from the inline script');
  const region = html.slice(s, e);

  const R = makeRealm();
  loadDismiss(R);
  const menu = R.el('start-menu');
  R.el('start-search', menu);
  const startBtn = R.el('start-btn-el'); startBtn.setAttribute('class', 'start-btn');
  const outside = R.el('outside');
  R.run('var startOpen = false;\n' + region, 'inline-start-menu.js');
  const toggle = R.window.toggleStartMenu;
  ok(typeof toggle === 'function', 'toggleStartMenu is callable');

  toggle();
  ok(menu.classList.contains('open'), 'start menu opens');
  R.window.dispatch('blur');
  ok(!menu.classList.contains('open'), 'window blur closes the start menu (press on another surface / iframe)');

  toggle();
  R.document.dispatch('pointerdown', mkEv(menu));
  ok(menu.classList.contains('open'), 'a press inside the menu keeps it open');
  R.document.dispatch('pointerdown', mkEv(startBtn));
  ok(menu.classList.contains('open'), 'a press on the start button does not dismiss (its click toggles)');
  R.document.dispatch('pointerdown', mkEv(outside));
  ok(!menu.classList.contains('open'), 'a press outside closes it');

  toggle();
  R.document.dispatch('keydown', mkEv(R.body, { key: 'Escape' }));
  ok(!menu.classList.contains('open'), 'Escape closes it');
  toggle(); toggle();
  ok(!menu.classList.contains('open') && (R.document._listeners.pointerdown || []).length === 0,
     'toggle open then shut leaves no dismiss listener armed');
})();

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);
