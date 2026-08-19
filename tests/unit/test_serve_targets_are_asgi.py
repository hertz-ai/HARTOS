"""Every shipped server command must target an ASGI app that mounts /peer_link.

This encodes the standing requirement: `/peer_link` works on every serving
path. It is a WebSocket, so a command that hands a pure-WSGI server the plain
Flask app (`hart_intelligence_entry:app`) serves HTTP perfectly and drops every
websocket -- PeerLink.accept() never fires and PeerLinkManager._links stays
empty for skill broadcast, claude_hive_session._publish_via_peer_link and
collect() shard fan-out. It logs one routine-looking line, if any.

That is not hypothetical. Both of the files checked here shipped that way:
  * deploy/linux/systemd/hart-backend.service ran `python -m waitress
    ... hart_intelligence_entry:app`  (waitress has no websocket support at all)
  * deploy/cloud/Dockerfile.prod ran `gunicorn --worker-class gevent
    ... hart_intelligence_entry:app`  (plain WSGI worker)
and the HART OS ISO inherits the systemd unit via
deploy/distro/build-iso.sh, so the regional image inherited the defect too.

Both now target `asgi:application`, which is
peer_link_asgi(AsyncioWSGIMiddleware(app)) built by the one canonical
core.serve.build_asgi_app.

If a serve command legitimately moves, re-point this test. Do not delete it:
the failure mode is silent, so nothing else notices.
"""
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Servers that cannot serve a websocket no matter what they are handed.
_WSGI_ONLY_SERVERS = ('waitress',)
# gunicorn worker classes that are WSGI-only. gunicorn itself is fine with an
# ASGI worker (uvicorn), so the worker class is what decides, not the binary.
_WSGI_WORKER_CLASSES = ('gevent', 'sync', 'gthread', 'eventlet', 'tornado')
# The WSGI callable. Fine for a WSGI server; fatal for /peer_link.
_WSGI_TARGET = 'hart_intelligence_entry:app'
_ASGI_TARGET = 'asgi:application'


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding='utf-8') as fh:
        return fh.read()


def _exec_lines(text):
    """Lines that actually launch something, ignoring comments."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('ExecStart=') or line.startswith('CMD '):
            out.append(line)
    return out


class TestSystemdUnitServesAsgi(unittest.TestCase):

    def setUp(self):
        self.text = _read('deploy', 'linux', 'systemd', 'hart-backend.service')
        self.cmds = _exec_lines(self.text)

    def test_has_an_execstart(self):
        self.assertTrue(self.cmds, 'no uncommented ExecStart= found — re-point this test')

    def test_does_not_use_a_wsgi_only_server(self):
        for cmd in self.cmds:
            for srv in _WSGI_ONLY_SERVERS:
                self.assertNotIn(
                    srv, cmd,
                    f'{srv} cannot serve a websocket, so /peer_link would be '
                    f'silently unavailable on HART OS and the distro ISO: {cmd}')

    def test_targets_the_asgi_app(self):
        self.assertTrue(
            any(_ASGI_TARGET in c for c in self.cmds),
            f'ExecStart must target {_ASGI_TARGET} (the peer_link-capable '
            f'stack), not {_WSGI_TARGET}: {self.cmds}')

    def test_does_not_target_the_bare_wsgi_callable(self):
        for cmd in self.cmds:
            self.assertNotIn(_WSGI_TARGET, cmd, f'bare WSGI callable: {cmd}')


class TestCloudImageServesAsgi(unittest.TestCase):

    def setUp(self):
        self.text = _read('deploy', 'cloud', 'Dockerfile.prod')
        self.cmds = _exec_lines(self.text)

    def test_has_a_cmd(self):
        self.assertTrue(self.cmds, 'no uncommented CMD found — re-point this test')

    def test_worker_class_is_not_wsgi_only(self):
        for cmd in self.cmds:
            m = re.search(r'--worker-class[= ]([^\s"\']+)', cmd)
            if not m:
                continue
            wc = m.group(1)
            self.assertFalse(
                any(wc == w or wc.endswith('.' + w) for w in _WSGI_WORKER_CLASSES),
                f'--worker-class {wc} is WSGI-only; /peer_link cannot be served. '
                f'Use uvicorn.workers.UvicornWorker.')

    def test_targets_the_asgi_app(self):
        self.assertTrue(
            any(_ASGI_TARGET in c for c in self.cmds),
            f'CMD must target {_ASGI_TARGET}: {self.cmds}')

    def test_does_not_target_the_bare_wsgi_callable(self):
        for cmd in self.cmds:
            self.assertNotIn(_WSGI_TARGET, cmd, f'bare WSGI callable: {cmd}')


class TestAsgiEntryDelegatesToCanonicalBuilder(unittest.TestCase):
    """asgi.py must not hand-roll the stack — one builder, one mount.

    Checked over the AST, not the text: asgi.py's own docstring names
    `peer_link_asgi(AsyncioWSGIMiddleware(app))` when explaining what it
    delegates to, and a plain string match counts that prose as a violation.
    Documentation must not be able to pass or fail a structural assertion.
    """

    def setUp(self):
        import ast
        self.tree = ast.parse(_read('asgi.py'))
        self.ast = ast

    def _called_names(self):
        return {
            (getattr(n.func, 'attr', None) or getattr(n.func, 'id', None))
            for n in self.ast.walk(self.tree)
            if isinstance(n, self.ast.Call)
        }

    def test_uses_build_asgi_app(self):
        self.assertIn('build_asgi_app', self._called_names())

    def test_does_not_construct_middleware_itself(self):
        self.assertNotIn(
            'AsyncioWSGIMiddleware', self._called_names(),
            'asgi.py must delegate to core.serve.build_asgi_app, not rebuild '
            'the stack — that is how the mount drifted in the first place.')

    def test_exposes_module_level_application(self):
        assigned = {
            t.id
            for node in self.tree.body
            if isinstance(node, self.ast.Assign)
            for t in node.targets
            if isinstance(t, self.ast.Name)
        }
        self.assertIn('application', assigned,
                      'servers target asgi:application — it must be a '
                      'module-level name, not built inside a function')


if __name__ == '__main__':
    unittest.main()
