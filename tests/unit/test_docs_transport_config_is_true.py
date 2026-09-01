"""The documented transport defaults must match the code.

docs/getting-started/configuration.md now tells an operator which router a node
talks to and how to point it at their own server. That table is only useful if it
is TRUE — a config doc that has drifted is worse than none, because it is trusted.

This is a DOC-vs-CODE consistency test, not a source-shape grep: it imports the
real modules, reads the real defaults, and asserts the doc quotes them. If someone
changes a default without updating the table (or vice versa), this fails and names
both sides.
"""
import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

DOC = os.path.join(REPO, 'docs', 'getting-started', 'configuration.md')


def _doc():
    return open(DOC, encoding='utf-8', errors='replace').read()


class TheDocumentedDefaultsAreReal(unittest.TestCase):

    def test_wamp_url_default(self):
        from core.wamp_url import DEFAULT_ROUTER_URL
        self.assertIn(DEFAULT_ROUTER_URL, _doc(),
                      "configuration.md does not quote the real WAMP_URL default "
                      "(%s)" % DEFAULT_ROUTER_URL)

    def test_central_db_url_default(self):
        from core.config_cache import _DEFAULT_CENTRAL_DB_URL
        self.assertIn(_DEFAULT_CENTRAL_DB_URL, _doc(),
                      "configuration.md does not quote the real "
                      "HEVOLVE_CENTRAL_DB_URL default (%s)" % _DEFAULT_CENTRAL_DB_URL)

    def test_the_crossbar_port_override_name(self):
        from core.port_registry import ENV_OVERRIDES
        name = ENV_OVERRIDES.get('crossbar')
        self.assertTrue(name, "port_registry no longer registers a crossbar override")
        self.assertIn(name, _doc(),
                      "configuration.md documents the wrong env var for the "
                      "crossbar port; the registry says %s" % name)

    def test_peerlink_cburl_and_cbrealm_defaults(self):
        """What a node ACTUALLY talks to when CBURL/CBREALM are unset.

        This used to regex telemetry.py for a literal `'CBURL', '<default>'`,
        which contradicts this file's own contract ("not a source-shape grep")
        and duly broke when the code improved: 44aad83 routed the fallback
        through core.wamp_url.resolve_router_url(), so the literal became `''`,
        the `[^']+` regex matched nothing, and the test failed while the code
        was MORE correct than before. Reading the resolved value instead cannot
        fail that way.

        CentralConnection.__init__ only assigns attributes -- it opens no
        socket and starts no thread -- so constructing one is a safe way to ask
        the code what it resolved.
        """
        import importlib

        for var in ('CBURL', 'CBREALM'):
            os.environ.pop(var, None)
        telemetry = importlib.import_module('core.peer_link.telemetry')
        conn = telemetry.CentralConnection()

        for var, value in (('CBURL', conn._crossbar_url),
                           ('CBREALM', conn._realm)):
            self.assertTrue(
                value, "%s resolved to nothing; a node would have no router" % var)
            self.assertIn(
                value, _doc(),
                "configuration.md quotes the wrong %s default; with %s unset "
                "the code resolves to %s" % (var, var, value))

    def test_the_canonical_host_constant_is_the_documented_one(self):
        from core.constants import CENTRAL_HOST, CENTRAL_HOST_LEGACY_ALIAS
        doc = _doc()
        self.assertIn(CENTRAL_HOST, doc)
        self.assertIn(CENTRAL_HOST_LEGACY_ALIAS, doc,
                      "the doc no longer explains the legacy alias, so a reader who "
                      "meets it in the 14 remaining files has nowhere to look")


class TheDocKeepsTheFlexibilityPromise(unittest.TestCase):
    """The doc tells operators they can run their own server. If that stops being
    true, the doc is actively misleading — so pin the claim to behaviour."""

    def test_the_documented_own_server_example_actually_works(self):
        from core.wamp_url import resolve_router_url, resolve_publish_url
        own = {'WAMP_URL': 'ws://127.0.0.1:8088/ws'}
        self.assertIn('127.0.0.1', resolve_router_url(own))
        self.assertIn('127.0.0.1', resolve_publish_url(own),
                      "the doc promises both paths follow WAMP_URL; the publish "
                      "bridge did not")

    def test_the_documented_empty_db_disable_actually_works(self):
        import importlib
        import core.config_cache as cc
        prev = os.environ.get('HEVOLVE_CENTRAL_DB_URL')
        os.environ['HEVOLVE_CENTRAL_DB_URL'] = ''
        try:
            importlib.reload(cc)
            self.assertEqual('', cc.get_central_db_url(),
                             "the doc says empty disables cross-device reads; it "
                             "no longer does")
        finally:
            if prev is None:
                os.environ.pop('HEVOLVE_CENTRAL_DB_URL', None)
            else:
                os.environ['HEVOLVE_CENTRAL_DB_URL'] = prev
            importlib.reload(cc)


if __name__ == '__main__':
    unittest.main()
