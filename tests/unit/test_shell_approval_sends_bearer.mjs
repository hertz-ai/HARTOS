/*
 * The shell's approval buttons POST the answer to /api/agent/approval, which
 * names the approver from the caller's Bearer (never the body).  Drives the REAL
 * _postApproval from the rendered desktop shell with a stubbed fetch and
 * localStorage: the signed-in access_token goes out as a Bearer; signed out
 * sends none.
 */
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const RENDER = "import sys; sys.path.insert(0,'.');" +
  "from integrations.agent_engine.liquid_ui_service import LiquidUIService;" +
  "(getattr(sys.stdout,'reconfigure',lambda **k:None))(encoding='utf-8');" +
  "print(LiquidUIService().render_desktop_shell())";
let html = null;
for (const py of [process.env.HART_TEST_PYTHON, 'python', 'python3'].filter(Boolean)) {
  try {
    html = execFileSync(py, ['-c', RENDER], { cwd: ROOT, encoding: 'utf-8', maxBuffer: 1 << 28 });
    if (html && html.includes('_postApproval')) break;
  } catch (e) { html = null; }
}
if (!html) { console.log('SKIP: could not render the shell'); process.exit(0); }

const start = html.indexOf('function _postApproval');
let depth = 0, end = -1;
for (let i = html.indexOf('{', start); i < html.length; i++) {
  if (html[i] === '{') depth++;
  else if (html[i] === '}' && --depth === 0) { end = i + 1; break; }
}
const fn = html.slice(start, end);

let failures = 0;
function ok(c, m) { if (c) console.log('  OK   ' + m); else { failures++; console.log(' FAIL  ' + m); } }

function run(token) {
  const calls = [];
  const ctx = {
    SHELL: 'http://node',
    fetch: (url, opts) => { calls.push({ url, opts }); return Promise.resolve(); },
    localStorage: { getItem: (k) => (k === 'access_token' ? token : null) },
    JSON,
  };
  vm.createContext(ctx);
  vm.runInContext(fn + '; _postApproval("42", "ap2_pay:p1", "approve");', ctx);
  return calls;
}

let calls = run('tok-1');
ok(calls.length === 1 && calls[0].url === 'http://node/api/agent/approval', 'posts to the approval route');
ok(calls[0].opts.headers.Authorization === 'Bearer tok-1', 'signed in: the token goes out as a Bearer');
ok(!('approver' in JSON.parse(calls[0].opts.body)), 'the body names no approver');
calls = run(null);
ok(calls[0].opts.headers.Authorization === undefined, 'signed out: no Authorization header');

console.log(failures ? 'RESULT: FAIL' : 'RESULT: ALL PASS');
process.exit(failures ? 1 : 0);
