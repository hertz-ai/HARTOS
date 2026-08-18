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
