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
    build_asgi_app,
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
