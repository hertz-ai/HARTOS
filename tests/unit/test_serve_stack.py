"""core.serve must reproduce, exactly, what the three entry points hardcoded.

This is the equivalence proof for collapsing the duplicated serve stacks. The
values below are transcribed from the call sites BEFORE the refactor:

  HARTOS hart_intelligence_entry._serve_app
  Nunba  app.py:start_flask   (cx_Freeze / desktop)
  Nunba  main.py __main__     (dev + HART OS daemon)

All three set keep_alive_timeout=120, h11_max_incomplete_size=16MB,
accesslog=None, errorlog='-'. Only main.py set server_names. If a value here
changes, a deployment's behaviour changed with it.
"""
import unittest

from core.serve import (
    ACCESS_LOG,
    ERROR_LOG,
    KEEP_ALIVE_TIMEOUT,
    MAX_INCOMPLETE_SIZE,
    UNIX_SOCKET_SERVER_NAME,
    build_asgi_app,
    local_server_names,
    make_hypercorn_config,
    shared_config_values,
)


class TestSharedValuesMatchThePreRefactorLiterals(unittest.TestCase):
    """Pins the constants. Transcribed from the call sites, not from core.serve."""

    def test_keep_alive_timeout_is_120(self):
        self.assertEqual(KEEP_ALIVE_TIMEOUT, 120)

    def test_max_incomplete_size_is_16mb(self):
        self.assertEqual(MAX_INCOMPLETE_SIZE, 16 * 1024 * 1024)
        self.assertEqual(MAX_INCOMPLETE_SIZE, 16777216)

    def test_access_log_is_none_and_error_log_is_dash(self):
        self.assertIsNone(ACCESS_LOG)
        self.assertEqual(ERROR_LOG, '-')

    def test_shared_config_values_reports_all_four(self):
        self.assertEqual(shared_config_values(), {
            'keep_alive_timeout': 120,
            'h11_max_incomplete_size': 16 * 1024 * 1024,
            'accesslog': None,
            'errorlog': '-',
        })


class TestConfigEquivalence(unittest.TestCase):

    def test_applies_the_four_shared_settings(self):
        cfg = make_hypercorn_config(['0.0.0.0:5000'])
        self.assertEqual(cfg.keep_alive_timeout, 120)
        self.assertEqual(cfg.h11_max_incomplete_size, 16 * 1024 * 1024)
        self.assertIsNone(cfg.accesslog)
        self.assertEqual(cfg.errorlog, '-')

    def test_bind_passes_through_verbatim_for_each_entry_shape(self):
        # app.py                     -> 0.0.0.0:port
        # main.py desktop            -> bind_host:port
        # main.py HART OS daemon     -> unix:<path>
        # _serve_app                 -> host:port
        for bind in (['0.0.0.0:5000'], ['127.0.0.1:5000'],
                     ['unix:/tmp/hart.sock'], ['0.0.0.0:6777']):
            with self.subTest(bind=bind):
                self.assertEqual(make_hypercorn_config(bind).bind, bind)

    def test_server_names_set_only_when_given(self):
        """Two of three never set it; setting one would change Host handling."""
        self.assertFalse(make_hypercorn_config(['0.0.0.0:5000']).server_names)
        self.assertEqual(
            make_hypercorn_config(['0.0.0.0:5000'],
                                  server_names=['Nunba']).server_names,
            ['Nunba'])

    def test_bind_is_copied_not_aliased(self):
        """A caller mutating its own list afterwards must not move the bind."""
        caller_list = ['0.0.0.0:5000']
        cfg = make_hypercorn_config(caller_list)
        caller_list.append('0.0.0.0:9999')
        self.assertEqual(cfg.bind, ['0.0.0.0:5000'])


class TestLocalServerNames(unittest.TestCase):
    """The Host allowlist main.py passes: what it admits, spelled out."""

    def test_unix_socket_bind_is_exactly_the_proxy_host(self):
        # main.py used server_names=['Nunba'] on the socket before this
        # function existed; the Liquid UI proxy sends exactly that.
        self.assertEqual(local_server_names(['unix:/run/hart/nunba.sock']),
                         ['Nunba'])
        self.assertEqual(UNIX_SOCKET_SERVER_NAME, 'Nunba')

    def test_loopback_tcp_bind_admits_every_loopback_spelling_at_the_port(self):
        names = local_server_names(['127.0.0.1:5000'])
        for host in ('127.0.0.1:5000', 'localhost:5000', '[::1]:5000',
                     'Nunba'):
            with self.subTest(host=host):
                self.assertIn(host, names)
        # Exact match with the port: the bare name or another port is not
        # what a client of THIS server sends, and must not be admitted.
        for host in ('localhost', '127.0.0.1', 'localhost:5001',
                     'evil.example:5000', 'nunba'):
            with self.subTest(host=host):
                self.assertNotIn(host, names)

    def test_port_80_also_admits_the_portless_form(self):
        names = local_server_names(['127.0.0.1:80'])
        self.assertIn('localhost', names)
        self.assertIn('localhost:80', names)

    def test_concrete_bind_address_is_admitted_wildcard_is_not(self):
        self.assertIn('192.168.1.5:5000',
                      local_server_names(['192.168.1.5:5000']))
        self.assertIn('[fe80::1]:5000',
                      local_server_names(['fe80::1:5000']))
        wildcard = local_server_names(['0.0.0.0:5000'])
        self.assertNotIn('0.0.0.0:5000', wildcard)
        self.assertIn('localhost:5000', wildcard)

    def test_malformed_bind_raises(self):
        with self.assertRaises(ValueError):
            local_server_names(['localhost'])


def _free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class TestHostAllowlistServedByRealHypercorn(unittest.TestCase):
    """A real Flask app behind real Hypercorn, probed with real Host headers.

    This is the defect that failed every Nunba cypress-e2e shard: with
    server_names=['Nunba'] on a TCP bind, `Host: localhost:5000` got a 404
    from /health before Flask ran.  The allowlist must let loopback clients
    through and still 404 a rebinding host.
    """

    @classmethod
    def setUpClass(cls):
        import asyncio
        import threading
        import time
        import urllib.error
        import urllib.request

        from flask import Flask
        from hypercorn.asyncio import serve

        app = Flask('serve_stack_host_allowlist')

        @app.route('/health')
        def _health():
            return 'ok'

        cls.port = _free_port()
        bind = [f'127.0.0.1:{cls.port}']
        cfg = make_hypercorn_config(bind, server_names=local_server_names(bind))
        asgi = build_asgi_app(app)
        cls._loop = asyncio.new_event_loop()
        cls._stop = asyncio.Event()

        def _run():
            asyncio.set_event_loop(cls._loop)
            cls._loop.run_until_complete(
                serve(asgi, cfg, shutdown_trigger=cls._stop.wait))

        cls._thread = threading.Thread(target=_run, daemon=True)
        cls._thread.start()

        deadline = time.monotonic() + 30
        while True:
            try:
                urllib.request.urlopen(
                    f'http://127.0.0.1:{cls.port}/health', timeout=2)
                break
            except urllib.error.HTTPError:
                break  # the server answered; readiness is all we wait for
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls._loop.call_soon_threadsafe(cls._stop.set)
        cls._thread.join(timeout=10)

    def _status(self, host):
        import urllib.error
        import urllib.request

        req = urllib.request.Request(f'http://127.0.0.1:{self.port}/health')
        req.add_header('Host', host)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, b''

    def test_loopback_hosts_reach_the_app(self):
        for host in (f'localhost:{self.port}', f'127.0.0.1:{self.port}',
                     'Nunba'):
            with self.subTest(host=host):
                self.assertEqual(self._status(host), (200, b'ok'))

    def test_rebinding_host_gets_404_before_the_app(self):
        for host in ('evil.example', f'evil.example:{self.port}',
                     f'localhost:{self.port + 1}'):
            with self.subTest(host=host):
                self.assertEqual(self._status(host)[0], 404)


class TestAsgiStack(unittest.TestCase):

    def test_wraps_wsgi_in_the_peer_link_listener(self):
        """The composition is peer_link_asgi(AsyncioWSGIMiddleware(app))."""
        from core.peer_link.server import PEER_LINK_PATH

        sentinel = object()
        asgi = build_asgi_app(sentinel)
        self.assertTrue(callable(asgi))
        # peer_link_asgi returns its own coroutine function when enabled, and
        # the untouched next_app when the kill switch is set. Either way it must
        # not be the bare sentinel.
        self.assertIsNot(asgi, sentinel)
        self.assertEqual(PEER_LINK_PATH, '/peer_link')

    def test_kill_switch_still_returns_an_app(self):
        """HEVOLVE_PEER_LINK_SERVER=0 must degrade, not crash the boot path."""
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {'HEVOLVE_PEER_LINK_SERVER': '0'}):
            self.assertTrue(callable(build_asgi_app(object())))


if __name__ == '__main__':
    unittest.main()
