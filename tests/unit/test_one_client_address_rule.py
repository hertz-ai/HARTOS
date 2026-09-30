"""Every "who is calling / is it this machine" decision uses core.auth_local.

Review of 291e548df, F3: after client_address() became the one rule, about a
dozen checks still read request.remote_addr (or the WSGI REMOTE_ADDR /
X-Forwarded-For) themselves: the credential endpoints (ai_key_vault), the
shell APIs, the MCP bridge, the /chat and commercial-API rate keys, the
social rate limiters, and in Nunba the dispatcher's is_local_environ (which
took the FIRST forwarded hop, so behind TRUSTED_PROXY a client writing
'X-Forwarded-For: 127.0.0.1' was this machine) and two user_id fallbacks.
Each is now core.auth_local.client_address / client_key / _is_local_request
/ is_local_environ, and a source guard makes a new direct read fail CI.

Behavioural tests drive the real checks inside a real Flask request; the
source guard (test_source_guard_*) walks the AST of both repositories.
"""
import ast
import os

import pytest
from flask import Flask

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NUNBA = os.path.join(os.path.dirname(ROOT), 'Nunba-HART-Companion')
PROXY = '10.0.0.1'
SPOOF = {'X-Forwarded-For': '127.0.0.1'}


@pytest.fixture
def app(monkeypatch):
    for k in ('TRUSTED_PROXY', 'NUNBA_CI', 'HART_SHELL_TOKEN'):
        monkeypatch.delenv(k, raising=False)
    return Flask(__name__)


def _ctx(app, remote, headers=None):
    return app.test_request_context(environ_base={'REMOTE_ADDR': remote},
                                    headers=headers or {})


# ── the one rule, as a WSGI environ (Nunba's dispatcher) ────────────────

@pytest.mark.parametrize('remote, xff, local', [
    ('127.0.0.1', '', True),
    ('::ffff:127.0.0.1', '', True),
    ('127.0.0.1', '203.0.113.9', False),       # a local proxy, remote client
    (PROXY, '127.0.0.1', False),               # a forwarded claim is remote
    (PROXY, '127.0.0.1, 203.0.113.9', False),
    ('192.168.0.9', '127.0.0.1', False),
])
def test_is_local_environ_is_the_request_rule(monkeypatch, remote, xff, local):
    from core.auth_local import is_local_environ
    monkeypatch.setenv('TRUSTED_PROXY', PROXY)
    environ = {'REMOTE_ADDR': remote}
    if xff:
        environ['HTTP_X_FORWARDED_FOR'] = xff
    assert is_local_environ(environ) is local


def test_client_key_falls_back_to_the_socket_peer(app, monkeypatch):
    """A trusted proxy that names no client: the key is the proxy (charged
    as one client), never '' (every such request sharing one empty key)."""
    from core.auth_local import client_key
    monkeypatch.setenv('TRUSTED_PROXY', PROXY)
    with _ctx(app, PROXY):
        assert client_key() == PROXY
    with _ctx(app, PROXY, {'X-Forwarded-For': '198.51.100.7'}):
        assert client_key() == '198.51.100.7'


# ── the converted call sites ────────────────────────────────────────────

def test_shell_api_refuses_a_forwarded_loopback_claim(app, monkeypatch):
    from integrations.agent_engine.shell_auth import shell_auth_ok
    monkeypatch.setenv('TRUSTED_PROXY', PROXY)
    with _ctx(app, PROXY, SPOOF):
        assert shell_auth_ok()[0] is False
    with _ctx(app, '127.0.0.1'):
        assert shell_auth_ok()[0] is True
    with _ctx(app, '0.0.0.0'):
        assert shell_auth_ok()[0] is False


def test_mcp_bridge_loopback_check_is_the_rule(app, monkeypatch):
    from integrations.mcp.mcp_http_bridge import _is_loopback_request
    monkeypatch.setenv('TRUSTED_PROXY', PROXY)
    with _ctx(app, PROXY, SPOOF):
        assert _is_loopback_request() is False
    with _ctx(app, '::ffff:127.0.0.1'):
        assert _is_loopback_request() is True


def test_credential_endpoints_use_the_rule(app, monkeypatch):
    """ai_key_vault.is_local_request guards the credential routes."""
    from hartos.ai_key_vault import is_local_request
    monkeypatch.setenv('TRUSTED_PROXY', PROXY)
    with _ctx(app, PROXY, SPOOF):
        assert is_local_request() is False
    with _ctx(app, '0.0.0.0'):
        assert is_local_request() is False   # bind-any is not a client
    with _ctx(app, '127.0.0.1'):
        assert is_local_request() is True


def test_social_rate_limiter_charges_the_client(app, monkeypatch):
    from flask import g
    from integrations.social import rate_limiter as rl
    monkeypatch.setenv('TRUSTED_PROXY', PROXY)
    seen = []
    monkeypatch.setattr(rl._limiter, 'check',
                        lambda key, *a, **k: seen.append(key) or True)

    @rl.rate_limit('post')
    def view():
        return 'ok'
    with _ctx(app, PROXY, {'X-Forwarded-For': '198.51.100.7'}):
        g.user = None
        view()
    assert seen and '198.51.100.7' in seen[0], seen


def test_redis_rate_limiter_key_is_the_client(app, monkeypatch):
    from security.rate_limiter_redis import RedisRateLimiter
    monkeypatch.setenv('TRUSTED_PROXY', PROXY)
    lim = RedisRateLimiter.__new__(RedisRateLimiter)
    with _ctx(app, PROXY, {'X-Forwarded-For': '198.51.100.7'}):
        assert lim._get_key('chat') == 'rl:chat:ip:198.51.100.7'


def test_commercial_api_brute_force_is_keyed_by_the_client(app, monkeypatch):
    from integrations.agent_engine import commercial_api as ca
    monkeypatch.setenv('TRUSTED_PROXY', PROXY)
    seen = []
    monkeypatch.setattr(ca, '_check_brute_force',
                        lambda ip: seen.append(ip) or True)
    view = ca.require_api_key(lambda: 'ok') if hasattr(
        ca, 'require_api_key') else None
    assert view is not None, 'commercial_api exposes require_api_key'
    with _ctx(app, PROXY, {'X-Forwarded-For': '198.51.100.7'}):
        view()
    assert seen == ['198.51.100.7'], seen


# ── source guard ────────────────────────────────────────────────────────

# The one reader, and its documented exception: Nunba's dispatcher falls back
# to a loopback-only REMOTE_ADDR check when HARTOS cannot be imported.
ALLOWED = {
    ('HARTOS', os.path.join('core', 'auth_local.py')): None,
    ('Nunba', os.path.join('routes', 'auth.py')): '_is_local_environ_without_hartos',
}
_READ_KEYS = {'x-forwarded-for', 'http_x_forwarded_for', 'remote_addr'}
_SKIP_DIRS = {'tests', 'venv', '.venv', 'python-embed', 'build', 'dist',
              'node_modules', '__pycache__', '.git', 'agent-ledger-opensource',
              'landing-page', '.claude', 'scratchpad'}


def _py_files(root):
    """The repository's TRACKED Python files outside tests and vendored
    trees (an IDE's plugin cache is not the product)."""
    import subprocess
    out = subprocess.run(['git', 'ls-files', '*.py'], cwd=root,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    for rel in out.stdout.splitlines():
        parts = rel.split('/')   # git prints forward slashes on every OS
        if any(p in _SKIP_DIRS or p.startswith('venv') for p in parts[:-1]):
            continue
        yield os.path.join(root, rel)


def _reads(tree):
    """(lineno, enclosing function) of each direct client-address read:
    `x.remote_addr`, `.get('X-Forwarded-For' | 'HTTP_X_FORWARDED_FOR' |
    'REMOTE_ADDR')`, `x['REMOTE_ADDR']`.  A header dict WRITTEN by a caller
    ({'X-Forwarded-For': ...}) is not a read."""
    out = []

    def visit(node, fn):
        for child in ast.iter_child_nodes(node):
            name = fn
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            hit = False
            if isinstance(child, ast.Attribute) and child.attr == 'remote_addr' \
                    and isinstance(child.ctx, ast.Load):
                hit = True
            elif isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) \
                    and child.func.attr == 'get' and child.args \
                    and isinstance(child.args[0], ast.Constant) \
                    and str(child.args[0].value).lower() in _READ_KEYS:
                hit = True
            elif isinstance(child, ast.Subscript) and isinstance(child.slice, ast.Constant) \
                    and str(child.slice.value).lower() in _READ_KEYS \
                    and isinstance(child.ctx, ast.Load):
                hit = True
            if hit:
                out.append((child.lineno, name))
            visit(child, name)
    visit(tree, None)
    return out


def _violations(repo_name, root):
    bad = []
    for path in _py_files(root):
        rel = os.path.relpath(path, root)
        try:
            tree = ast.parse(open(path, encoding='utf-8').read())
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
        allowed_fn = ALLOWED.get((repo_name, rel), False)
        for line, fn in _reads(tree):
            if allowed_fn is None or (allowed_fn and fn == allowed_fn):
                continue
            bad.append(f'{repo_name}:{rel}:{line} ({fn})')
    return bad


def test_source_guard_detector_is_not_vacuous():
    sample = ast.parse(
        'def f(request, environ):\n'
        '    a = request.remote_addr\n'
        '    b = request.headers.get("X-Forwarded-For")\n'
        '    c = environ["REMOTE_ADDR"]\n'
        '    d = environ.get("HTTP_X_FORWARDED_FOR", "")\n'
        '    send(headers={"X-Forwarded-For": a})\n')
    assert [ln for ln, _ in _reads(sample)] == [2, 3, 4, 5]


def test_source_guard_hartos_has_one_client_address_reader():
    bad = _violations('HARTOS', ROOT)
    assert not bad, ('read the client address through core.auth_local '
                     '(client_address / client_key / _is_local_request / '
                     'is_local_environ), not directly:\n' + '\n'.join(bad))


@pytest.mark.skipif(not os.path.isdir(NUNBA), reason='Nunba checkout absent')
def test_source_guard_nunba_has_one_client_address_reader():
    bad = _violations('Nunba', NUNBA)
    assert not bad, ('Nunba reads the client address through HARTOS '
                     'core.auth_local (routes.auth delegates):\n' + '\n'.join(bad))


# ── Nunba imports no private HARTOS core name ───────────────────────────
# Review of Nunba ad9ce346 / 48d562f7: tts/backend_venv.py imported
# core.venv_paths._reset_cache_for_tests and _validate_backend_name.  A
# private name is free to change in HARTOS without a caller audit (the rule
# 492a57aab applied to hart_cli), so a Nunba use of one breaks the installed
# app silently.  core.venv_paths exports public names; this fails on any
# `from core[.x] import _name` or `<imported core module>._name` in Nunba
# (tests excepted: a test may reach into what it tests), and on a Nunba
# module it cannot parse (a file the guard cannot read is not a clean one).
#
# One shape is allowed: the compatibility fallback for an older HARTOS
# (Nunba runs against whatever HARTOS an install carries).  A private import
# inside `except ImportError` whose `try` imported a PUBLIC name from the same
# module under the same local name, e.g.
#     try:
#         from core.venv_paths import validate_backend_name as _validate
#     except ImportError:
#         from core.venv_paths import _validate_backend_name as _validate

def _is_core(module):
    return bool(module) and (module == 'core' or module.startswith('core.'))


def _compat_fallbacks(tree):
    """ids of the ImportFrom nodes that are the allowed fallback shape."""
    ok = set()
    for t in ast.walk(tree):
        if not isinstance(t, ast.Try):
            continue
        public = {(n.module, a.asname or a.name)
                  for s in t.body for n in ast.walk(s)
                  if isinstance(n, ast.ImportFrom) and _is_core(n.module)
                  for a in n.names if not a.name.startswith('_')}
        for h in t.handlers:
            names = [h.type] if not isinstance(h.type, ast.Tuple) else list(h.type.elts)
            if not any(isinstance(x, ast.Name) and x.id in
                       ('ImportError', 'ModuleNotFoundError') for x in names):
                continue
            for s in h.body:
                for n in ast.walk(s):
                    if isinstance(n, ast.ImportFrom) and _is_core(n.module) and all(
                            (n.module, a.asname or a.name) in public
                            for a in n.names):
                        ok.add(id(n))
    return ok


def _private_core_imports(tree):
    """(lineno, 'module.name') of each private core name Nunba reaches:
    `from core... import _name` (outside the compat fallback) and
    `m._name` / `core.x._name` where m is an imported core module."""
    allowed = _compat_fallbacks(tree)
    modules = {}   # local dotted name -> core module
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if _is_core(a.name):
                    modules[a.asname or a.name] = a.name
        elif isinstance(n, ast.ImportFrom) and n.module == 'core':
            for a in n.names:   # from core import venv_paths [as vp]
                if not a.name.startswith('_'):
                    modules.setdefault(a.asname or a.name, 'core.' + a.name)
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and _is_core(n.module) \
                and id(n) not in allowed:
            out += [(n.lineno, f'{n.module}.{a.name}')
                    for a in n.names if a.name.startswith('_')]
        elif isinstance(n, ast.Attribute) and n.attr.startswith('_') \
                and not n.attr.startswith('__'):
            try:
                base = ast.unparse(n.value)
            except Exception:
                continue
            if base in modules:
                out.append((n.lineno, f'{modules[base]}.{n.attr}'))
    return sorted(out)


def test_private_import_detector_is_not_vacuous():
    sample = ast.parse(
        'from core.venv_paths import _reset_cache_for_tests\n'           # 1
        'from core.venv_paths import venv_root, _validate_backend_name as v\n'
        'from core import _x\n'                                          # 3
        'from core.venv_paths import venv_root\n'
        'from integrations.x import _y\n'
        'from corex import _z\n'
        'import core.venv_paths as vp\n'
        'vp._VENV_ROOT_CACHE = None\n'                                   # 8
        'import core.platform_paths\n'
        'core.platform_paths._cached_data_dir\n'                         # 10
        'from core import venv_paths\n'
        'venv_paths._validate_backend_name("x")\n'                       # 12
        'vp.__file__\n'
        'try:\n'
        '    from core.venv_paths import validate_backend_name as _v\n'
        'except ImportError:\n'
        '    from core.venv_paths import _validate_backend_name as _v\n'  # allowed
        'try:\n'
        '    from core.venv_paths import venv_root\n'
        'except ImportError:\n'
        '    from core.venv_paths import _validate_backend_name as _w\n'  # 21: other name
        'try:\n'
        '    from core.venv_paths import validate_backend_name as _u\n'
        'except Exception:\n'
        '    from core.venv_paths import _validate_backend_name as _u\n')  # 25
    assert [ln for ln, _ in _private_core_imports(sample)] == [
        1, 2, 3, 8, 10, 12, 21, 25]


def _private_core_violations(root):
    """(violations, modules parsed) over a repository's tracked modules."""
    bad, seen = [], 0
    for path in _py_files(root):
        rel = os.path.relpath(path, root)
        try:
            with open(path, encoding='utf-8') as fh:
                tree = ast.parse(fh.read())
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            bad.append(f'{rel}: not parsed ({type(exc).__name__}: {exc})')
            continue
        seen += 1
        bad += [f'{rel}:{ln} {name}' for ln, name in _private_core_imports(tree)]
    return bad, seen


def test_a_module_the_guard_cannot_parse_fails_it(tmp_path):
    import subprocess
    (tmp_path / 'good.py').write_text('x = 1\n', encoding='utf-8')
    (tmp_path / 'broken.py').write_text('def f(:\n', encoding='utf-8')
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True, timeout=60)
    subprocess.run(['git', 'add', '-A'], cwd=tmp_path, check=True, timeout=60)
    bad, seen = _private_core_violations(str(tmp_path))
    assert seen == 1
    assert len(bad) == 1 and bad[0].startswith('broken.py: not parsed'), bad


@pytest.mark.skipif(not os.path.isdir(NUNBA), reason='Nunba checkout absent')
def test_source_guard_nunba_imports_no_private_core_name():
    bad, seen = _private_core_violations(NUNBA)
    assert seen > 50, seen   # 90 tracked non-test modules on 2026-09-28
    assert not bad, ('Nunba reaches a private HARTOS core name (make it '
                     'public in core and keep the old name as an alias), or '
                     'a module the guard cannot parse:\n' + '\n'.join(bad))
