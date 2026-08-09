"""Behavioural tests for security/tls_config.py — the outbound-HTTPS / MITM
defense (force-https upgrade, TLS cert verification, bounded timeout).

This module shipped with ZERO dedicated tests. A regression that disabled
`verify` or stopped upgrading http:// would be a SILENT man-in-the-middle hole,
which is exactly the class of change a test must catch. No network here:
`upgrade_url` is pure, and the session is mocked for the request tests.
"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture(autouse=True)
def tls():
    """A fresh module singleton + clean env per test — `_secure_session` is a
    module global, so leaking it across tests would couple them."""
    import security.tls_config as _tls
    _tls._secure_session = None
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('HEVOLVE_ENV', None)
        os.environ.pop('HEVOLVE_CA_BUNDLE', None)
        yield _tls
    _tls._secure_session = None


class TestUpgradeUrl:
    def test_remote_http_is_upgraded(self, tls):
        assert tls.upgrade_url('http://api.example.com/x') == 'https://api.example.com/x'

    def test_uppercase_scheme_is_upgraded(self, tls):
        """Regression: urlparse lowercases the scheme, so 'HTTP://' passed the
        http check, but the old case-sensitive str.replace missed it and left the
        request on plaintext HTTP — a silent MITM bypass. Now upgraded."""
        assert tls.upgrade_url('HTTP://api.example.com/x') == 'https://api.example.com/x'

    def test_https_left_unchanged(self, tls):
        assert tls.upgrade_url('https://api.example.com') == 'https://api.example.com'

    @pytest.mark.parametrize('host', ['localhost', '127.0.0.1', '::1', '0.0.0.0'])
    def test_localhost_not_upgraded(self, tls, host):
        """Local calls stay http — there is no MITM on loopback, and upgrading
        would break a service with no local TLS. IPv6 literals must be bracketed
        in the URL authority ('[::1]'); urlparse strips the brackets so the
        extracted hostname ('::1') matches _LOCALHOST_HOSTS."""
        netloc = f'[{host}]' if ':' in host else host
        u = f'http://{netloc}:5002/x'
        assert tls.upgrade_url(u) == u

    def test_localhost_lookalike_is_upgraded(self, tls):
        """Membership is EXACT, not substring: 'localhost.evil.com' is a REMOTE
        host and must be forced to https, not waved through as local."""
        assert tls.upgrade_url('http://localhost.evil.com/x') == 'https://localhost.evil.com/x'

    def test_query_param_http_is_preserved(self, tls):
        """Only the SCHEME is upgraded — an http:// inside a query value is left
        intact, never mangled."""
        assert tls.upgrade_url('http://api.x/p?redir=http://leak') == 'https://api.x/p?redir=http://leak'

    def test_dev_mode_allows_plain_http(self, tls):
        """HEVOLVE_ENV=development is the explicit escape hatch for local dev."""
        with patch.dict(os.environ, {'HEVOLVE_ENV': 'development'}):
            assert tls.upgrade_url('http://api.example.com') == 'http://api.example.com'


class TestSecureSession:
    def test_cert_verification_is_enabled(self, tls):
        """verify MUST be truthy (True) — never False. verify=False is a MITM
        hole; this is the load-bearing assertion of the module."""
        assert tls.get_secure_session().verify is True

    def test_session_is_a_singleton(self, tls):
        assert tls.get_secure_session() is tls.get_secure_session()

    def test_ca_bundle_from_env_when_present(self, tls, tmp_path):
        ca = tmp_path / 'ca.pem'
        ca.write_text('dummy')
        with patch.dict(os.environ, {'HEVOLVE_CA_BUNDLE': str(ca)}):
            tls._secure_session = None
            assert tls.get_secure_session().verify == str(ca)

    def test_missing_ca_bundle_falls_back_to_system_cas_not_off(self, tls):
        """A configured-but-absent CA bundle must fall back to verify=True (system
        CAs), NEVER silently disable verification."""
        with patch.dict(os.environ, {'HEVOLVE_CA_BUNDLE': '/nonexistent/ca.pem'}):
            tls._secure_session = None
            assert tls.get_secure_session().verify is True


class TestSecureRequest:
    def test_default_timeout_is_applied(self, tls):
        """No request may be unbounded — a missing timeout defaults to 30s."""
        fake = MagicMock()
        with patch.object(tls, 'get_secure_session', return_value=fake):
            tls.secure_request('GET', 'http://api.example.com')
        assert fake.request.call_args.kwargs['timeout'] == 30

    def test_caller_timeout_is_preserved(self, tls):
        fake = MagicMock()
        with patch.object(tls, 'get_secure_session', return_value=fake):
            tls.secure_request('GET', 'http://api.example.com', timeout=5)
        assert fake.request.call_args.kwargs['timeout'] == 5

    def test_url_is_upgraded_before_the_request(self, tls):
        """secure_get('http://…') must reach the network as https://…"""
        fake = MagicMock()
        with patch.object(tls, 'get_secure_session', return_value=fake):
            tls.secure_get('http://api.example.com/x')
        assert fake.request.call_args.args == ('GET', 'https://api.example.com/x')

    @pytest.mark.parametrize('verb,fn', [
        ('GET', 'secure_get'), ('POST', 'secure_post'),
        ('PUT', 'secure_put'), ('DELETE', 'secure_delete')])
    def test_every_verb_wrapper_routes_through_secure_request(self, tls, verb, fn):
        """All four convenience wrappers upgrade + go through the secure session
        with the right method — none is a plain requests.<verb> bypass."""
        fake = MagicMock()
        with patch.object(tls, 'get_secure_session', return_value=fake):
            getattr(tls, fn)('http://api.example.com/x')
        assert fake.request.call_args.args == (verb, 'https://api.example.com/x')
