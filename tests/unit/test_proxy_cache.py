"""install_proxy_cache wraps getproxies with a TTL cache so requests/urllib stop
reading the Windows registry on every HTTP call (the superadmin-report CPU burn
in the 2026-06-13 dig). Same proxies returned, just not re-resolved per call."""
import os
import sys
import urllib.request

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.proxy_cache as pc  # noqa: E402


class TestInstallProxyCache:
    def test_getproxies_resolved_once_then_cached(self):
        orig = urllib.request.getproxies
        calls = {'n': 0}

        def fake_getproxies():
            calls['n'] += 1
            return {'http': 'http://proxy:8080'}

        urllib.request.getproxies = fake_getproxies
        try:
            pc._reset_for_test()
            assert pc.install_proxy_cache(ttl_seconds=1000) is True
            a = urllib.request.getproxies()
            b = urllib.request.getproxies()
            c = urllib.request.getproxies()
            assert a == b == c == {'http': 'http://proxy:8080'}
            assert calls['n'] == 1, (
                f"registry/getproxies resolved {calls['n']}x (expected 1 — cache miss)")
        finally:
            urllib.request.getproxies = orig
            pc._reset_for_test()

    def test_requests_reference_also_repointed(self):
        try:
            import requests.utils as ru
        except Exception:
            return  # requests not installed — urllib patch still applies, nothing to assert
        orig_url = urllib.request.getproxies
        orig_req = ru.getproxies
        urllib.request.getproxies = lambda: {'x': 'y'}
        try:
            pc._reset_for_test()
            pc.install_proxy_cache(ttl_seconds=1000)
            # requests.utils.getproxies must now be the SAME cached wrapper, so
            # requests' per-call lookups hit the cache instead of the registry.
            assert ru.getproxies is urllib.request.getproxies
        finally:
            urllib.request.getproxies = orig_url
            ru.getproxies = orig_req
            pc._reset_for_test()

    def test_idempotent_install(self):
        pc._reset_for_test()
        assert pc.install_proxy_cache() is True
        assert pc.install_proxy_cache() is True  # second call is a no-op True
        pc._reset_for_test()
