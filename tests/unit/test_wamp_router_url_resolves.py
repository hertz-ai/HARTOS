"""The relay/federation router URL must be RESOLVABLE, and must name the right host.

WHY
───
WAMP is the central relay AND federation transport here, so "where is the router"
is load-bearing. It had four answers: WAMP_URL (core/wamp_url.py), CBURL
(peer_link/telemetry.py:98, nat.py:217), CBREALM, and a literal hardcoded in 15
files. Meanwhile Nunba SHIPS a local router
(wamp_router.start_wamp_router(port=8088, host='127.0.0.1'), declared in the freeze),
which none of the hardcoded sites could ever reach.

Consequence: in flat mode — central off, the configuration the charter's honesty bar
demands — the RPC path could not reach the local router at all. Central was not
preferred, it was the only reachable option.

THE HOST RENAME IS A RENAME, NOT A REDIRECT
───────────────────────────────────────────
The hardcodes said aws_rasa; the run scripts (scripts/run.sh:49) export azurekong.
Steward 2026-08-18: azurekong is correct. VERIFIED the same day that both names are
the SAME MACHINE, so changing the default moves nothing:

    aws_rasa.hertzai.com   -> 106.51.181.24   /ws HTTP 200 (11280 bytes)
    azurekong.hertzai.com  -> 106.51.181.24   /ws HTTP 200 (11280 bytes)
    both: /publish -> 405 (POST-only bridge present)

An earlier attempt at this change was REVERTED before commit precisely because that
equivalence had not been measured yet — the fear was that WAMP_URL=azurekong from
the run scripts would send the RPC to a DIFFERENT router and fail as a timeout on
the CREATE/REUSE path. Measuring it is what made the change safe.

These tests are offline: they pin resolution behaviour, not connectivity.
"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from core.wamp_url import (  # noqa: E402
    DEFAULT_ROUTER_URL, LEGACY_ROUTER_ALIAS, resolve_publish_url, resolve_router_url,
)


class TheDefaultNamesTheCorrectHost(unittest.TestCase):

    def test_unset_resolves_to_azurekong(self):
        self.assertEqual('ws://azurekong.hertzai.com:8088/ws',
                         resolve_router_url({}),
                         "the no-env default is not the steward-confirmed host")

    def test_it_agrees_with_what_the_run_scripts_export(self):
        """scripts/run.sh:49 exports WAMP_URL=ws://azurekong...:8088/ws. Default and
        explicit setting must name the SAME host, or a node's behaviour silently
        depends on whether it was launched via the run script."""
        from urllib.parse import urlsplit
        via_script = resolve_router_url(
            {'WAMP_URL': 'ws://azurekong.hertzai.com:8088/ws'})
        self.assertEqual(urlsplit(DEFAULT_ROUTER_URL).netloc,
                         urlsplit(via_script).netloc)

    def test_the_legacy_alias_is_recorded_but_not_the_default(self):
        self.assertIn('aws_rasa', LEGACY_ROUTER_ALIAS)
        self.assertNotEqual(LEGACY_ROUTER_ALIAS, DEFAULT_ROUTER_URL)

    def test_blank_is_treated_as_unset(self):
        self.assertEqual(DEFAULT_ROUTER_URL, resolve_router_url({'WAMP_URL': '  '}))


class ANodeCanBePointedElsewhere(unittest.TestCase):
    """The reason the resolver exists at all."""

    def test_a_regional_or_peer_host_is_used_as_is(self):
        self.assertEqual('ws://regional-3.lan:8088/ws',
                         resolve_router_url({'WAMP_URL': 'ws://regional-3.lan:8088/ws'}))

    def test_the_LOCAL_router_nunba_ships_is_reachable(self):
        """wamp_router.start_wamp_router(port=8088, host='127.0.0.1') runs on a
        bundled node. Before this resolver the RPC path reached past it to a remote
        box, so 'works with central switched off' was not actually true."""
        self.assertEqual(
            'ws://127.0.0.1:8088/ws',
            resolve_router_url({'WAMP_URL': 'ws://127.0.0.1:8088/ws'}))

    def test_wss_is_preserved(self):
        self.assertEqual('wss://edge.example:8088/ws',
                         resolve_router_url({'WAMP_URL': 'wss://edge.example:8088/ws'}))


class ItReconcilesTheTwoDialects(unittest.TestCase):
    """WAMP_URL is one name carrying two protocols — producers set a ws router,
    consumers wanted an http publish bridge. This module owns both directions."""

    def test_an_http_publish_url_becomes_a_ws_router_url(self):
        self.assertEqual('ws://host:8088/ws',
                         resolve_router_url({'WAMP_URL': 'http://host:8088/publish'}))

    def test_https_becomes_wss(self):
        self.assertEqual('wss://host:8088/ws',
                         resolve_router_url({'WAMP_URL': 'https://host:8088/publish'}))

    def test_router_and_publish_never_disagree_about_the_host(self):
        env = {'WAMP_URL': 'ws://regional-3.lan:8088/ws'}
        self.assertIn('regional-3.lan', resolve_router_url(env))
        self.assertIn('regional-3.lan', resolve_publish_url(env),
                      "the RPC router and the publish bridge point at different "
                      "hosts — that split is exactly what this module exists to "
                      "prevent")

    def test_an_unparseable_value_is_left_alone(self):
        """Fail loudly on a bad URL rather than silently redirect somewhere the
        operator never configured — same contract as resolve_publish_url."""
        self.assertEqual('not a url', resolve_router_url({'WAMP_URL': 'not a url'}))


class TheRpcHelperUsesTheResolver(unittest.TestCase):

    def _src(self):
        return open(os.path.join(REPO, 'helper.py'),
                    encoding='utf-8', errors='replace').read()

    def test_helper_resolves_instead_of_hardcoding(self):
        # assertTrue, not assertIn: assertIn dumps the whole 3.3k-line file.
        self.assertTrue('resolve_router_url' in self._src(),
                        "helper.subscribe_and_return no longer resolves the router — "
                        "the node is pinned to one relay again")

    def test_the_hardcoded_literal_is_gone(self):
        self.assertFalse(
            'transports="ws://aws_rasa.hertzai.com:8088/ws"' in self._src(),
            "the hardcoded router literal is back in helper.py")


if __name__ == '__main__':
    unittest.main()


class TheCentralHostIsCanonicalised(unittest.TestCase):
    """Steward 2026-08-18: "canonicalise the url host centrally from constants if
    exists rather than maintaining multiple copies, or a getter method if from
    config."

    The hostname was written out in FOUR places in two spellings (config_cache's
    central DB URL, wamp_url's router default, 15 hardcoded call sites, and the run
    scripts). core.constants.CENTRAL_HOST is now the one literal; consumers compose
    their own URL from it plus the port that belongs to their service.
    """

    def test_constants_owns_the_one_hostname(self):
        from core.constants import CENTRAL_HOST, CENTRAL_HOST_LEGACY_ALIAS
        self.assertEqual('azurekong.hertzai.com', CENTRAL_HOST)
        self.assertEqual('aws_rasa.hertzai.com', CENTRAL_HOST_LEGACY_ALIAS,
                         "the legacy alias is recorded so a reader who greps the "
                         "old literal finds the explanation, not a dead end")

    def test_the_router_url_is_COMPOSED_not_retyped(self):
        from core.constants import CENTRAL_HOST
        self.assertIn(CENTRAL_HOST, DEFAULT_ROUTER_URL)

    def test_the_router_port_comes_from_the_registry_not_a_literal(self):
        """port_registry registers 'crossbar': 8088. It was MISSING once and
        get_port returned 0, making the bridge URL ws://localhost:0/ws — resolving
        it beats re-typing it."""
        from core.port_registry import get_port
        self.assertIn(':%d/' % (get_port('crossbar') or 8088), DEFAULT_ROUTER_URL)

    def test_the_central_db_url_uses_the_same_host(self):
        """Different service, different port, SAME hostname — so a rename lands in
        one place instead of drifting between the DB and the router."""
        from core.constants import CENTRAL_HOST
        from core.config_cache import get_central_db_url
        self.assertIn(CENTRAL_HOST, get_central_db_url())

    def test_no_regression_in_either_composed_value(self):
        from core.config_cache import _DEFAULT_CENTRAL_DB_URL
        self.assertEqual('ws://azurekong.hertzai.com:8088/ws', DEFAULT_ROUTER_URL)
        self.assertEqual('https://azurekong.hertzai.com:8443/db', _DEFAULT_CENTRAL_DB_URL)


class UnifyingTheHostDidNotCostTheFlexibility(unittest.TestCase):
    """Steward 2026-08-18: "keep it flexible for individual systems to have their
    own server, unifying shd not strip away that flexibility."

    CENTRAL_HOST de-duplicates a literal that was written out four times. It is a
    DEFAULT, not a mandate. These tests fail if a later "simplification" collapses
    the per-service overrides into the constant.
    """

    def test_a_node_can_run_its_OWN_wamp_server(self):
        self.assertEqual('ws://127.0.0.1:8088/ws',
                         resolve_router_url({'WAMP_URL': 'ws://127.0.0.1:8088/ws'}),
                         "a node can no longer point at its own router — the "
                         "constant became a mandate instead of a default")

    def test_router_and_publish_BOTH_follow_the_node_s_own_server(self):
        """Overriding must move the whole transport, not half of it: a node whose
        RPC is local but whose publish still goes to central is a split brain."""
        own = {'WAMP_URL': 'ws://127.0.0.1:8088/ws'}
        self.assertIn('127.0.0.1', resolve_router_url(own))
        self.assertIn('127.0.0.1', resolve_publish_url(own))

    def test_a_regional_relay_is_still_selectable(self):
        self.assertEqual('ws://regional-3.lan:8088/ws',
                         resolve_router_url({'WAMP_URL': 'ws://regional-3.lan:8088/ws'}))

    def test_the_db_and_the_router_are_INDEPENDENT_knobs(self):
        """A node may run its own router while still reading the central DB, or the
        reverse. Collapsing them into one HART_HOST would break that."""
        import importlib
        import core.config_cache as cc
        prev = os.environ.get('HEVOLVE_CENTRAL_DB_URL')
        os.environ['HEVOLVE_CENTRAL_DB_URL'] = 'https://my-private-cloud:8443/db'
        try:
            importlib.reload(cc)
            self.assertEqual('https://my-private-cloud:8443/db', cc.get_central_db_url())
            # the router default is untouched by the DB override
            self.assertIn('azurekong', resolve_router_url({}))
        finally:
            if prev is None:
                os.environ.pop('HEVOLVE_CENTRAL_DB_URL', None)
            else:
                os.environ['HEVOLVE_CENTRAL_DB_URL'] = prev
            importlib.reload(cc)

    def test_an_empty_db_override_still_means_disabled(self):
        """config_cache documents '' as "no central available — skip cross-device
        merge". Composing the default must not have broken that."""
        import importlib
        import core.config_cache as cc
        prev = os.environ.get('HEVOLVE_CENTRAL_DB_URL')
        os.environ['HEVOLVE_CENTRAL_DB_URL'] = ''
        try:
            importlib.reload(cc)
            self.assertEqual('', cc.get_central_db_url())
        finally:
            if prev is None:
                os.environ.pop('HEVOLVE_CENTRAL_DB_URL', None)
            else:
                os.environ['HEVOLVE_CENTRAL_DB_URL'] = prev
            importlib.reload(cc)
