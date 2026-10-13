"""core.public_url: the ONE reader of this node's public base URL.

A forged Host must never become the URL a payment gateway sends the buyer
and the signed confirmation to (ALLOWED_HOSTS=* is the deploy default).
These drive the real reader under a real Flask request context; only the
environment is set.
"""
import os
from unittest.mock import patch

from flask import Flask

import pytest

from core.public_url import (
    PUBLIC_URL_VAR, allowed_hosts, configured_public_url, internet_base_url,
    is_public_host, trusted_request_base,
)


def _in_request(host, env, reader=trusted_request_base):
    """``reader()`` inside a request carrying ``host``, with ``env`` applied
    and HARTOS_PUBLIC_URL absent unless ``env`` sets it."""
    app = Flask('t')
    with patch.dict(os.environ, env, clear=False), \
            app.test_request_context('/', headers={'Host': host}):
        if PUBLIC_URL_VAR not in env:
            os.environ.pop(PUBLIC_URL_VAR, None)
        return reader()


def test_a_forged_host_under_the_wildcard_is_not_used():
    assert _in_request('evil.example', {'ALLOWED_HOSTS': '*'}) == ''


def test_an_explicitly_allowed_host_is_used():
    assert _in_request('shop.example', {'ALLOWED_HOSTS': 'shop.example'}) == 'http://shop.example'


def test_an_allowed_host_matches_without_its_port_and_case():
    """The match ignores the port and the case; the root is the request's
    own (Werkzeug renders the host lower-cased)."""
    assert _in_request('Shop.Example:6777', {'ALLOWED_HOSTS': 'shop.example'}) == 'http://shop.example:6777'


def test_loopback_is_used_under_the_wildcard():
    assert _in_request('localhost:6777', {'ALLOWED_HOSTS': '*'}) == 'http://localhost:6777'


@pytest.mark.parametrize('host', ['[::1]:6777', '[::1]'])
def test_an_ipv6_loopback_host_is_read_with_and_without_its_port(host):
    """An rsplit on ':' took '[::1]' (no port) to '[:' and trusted nothing."""
    assert _in_request(host, {'ALLOWED_HOSTS': '*'}) == 'http://' + host


# ── the ONE parse of ALLOWED_HOSTS ───────────────────────────────────────

def test_allowed_hosts_is_the_operators_list_with_the_wildcard_kept(monkeypatch):
    monkeypatch.setenv('ALLOWED_HOSTS', ' shop.example, *,, 10.0.0.5 ')
    assert allowed_hosts() == frozenset({'shop.example', '*', '10.0.0.5'})
    monkeypatch.delenv('ALLOWED_HOSTS')
    assert allowed_hosts() == frozenset({'localhost', '127.0.0.1'})


# ── reachability: a trusted host is not always one the internet can reach ──

@pytest.mark.parametrize('host,public', [
    ('localhost', False), ('127.0.0.1', False), ('[::1]', False), ('::1', False),
    ('172.17.0.1', False), ('10.0.0.5', False), ('192.168.0.9', False),
    ('169.254.1.1', False), ('0.0.0.0', False), ('desktop', False), ('', None),
    # 203.0.113.0/24 (TEST-NET-3) is a reserved documentation range and NOT
    # global to ipaddress, which is the right answer for a callback address.
    ('203.0.113.5', False),
    # A port, a trailing dot and the shorthand IPv4 spellings the socket
    # layer accepts must not turn a private literal into a "dotted name"
    # (the review of f08ff3e4c measured each of these answering True).
    ('10.0.0.5:6777', False), ('192.168.1.2:80', False), ('127.0.0.1.', False),
    ('localhost.', False), ('127.1', False), ('0x7f.1', False),
    # Names the public DNS never resolves: IANA special-use and the LAN
    # conventions.  A trusted request from one is this node, but no gateway
    # can reach it.
    ('node.local', False), ('svc.cluster.local', False), ('hartos.lan', False),
    ('host.docker.internal', False), ('nas.home.arpa', False),
    ('box.localdomain', False), ('shop.example', False), ('api.test', False),
    ('x.invalid', False), ('abc.onion', False),
    # Malformed brackets are no host (urlparse raised ValueError to a direct
    # caller); a label that is all digits is never a public TLD (RFC 1123
    # 2.1), which also closes the spellings neither parser accepts on one
    # platform ('255.16777215' is rejected by Windows' inet_addr and taken
    # by glibc); multicast is not a host anyone posts to (the review of
    # 9e154b956 measured each of these).
    ('[10.0.0.5]:80', False), ('[1.1.1.1]', False), ('[]', False), ('[::1', False),
    ('08.1', False), ('256.1', False), ('1.2.3.4.5', False), ('255.16777215', False),
    ('224.0.0.1', False), ('ff02::1', False), ('[ff02::1]:6777', False),
    ('1.1.1.1', True), ('2606:4700:4700::1111', True), ('1.1.1.1:443', True),
    ('[2606:4700:4700::1111]:443', True),
    ('shop.example.com', True), ('API.Example.Com', True), ('api.hevolve.ai.', True),
    ('shop1.example.com', True), ('1shop.example.com', True),
])
def test_is_public_host(host, public):
    assert is_public_host(host) is bool(public)


def test_a_host_carrying_userinfo_is_not_trusted():
    """'attacker.example@shop.example.com' names the trusted host after the
    '@', so the match passed and the request root carried the userinfo
    into a gateway URL (the review of 9e154b956 measured it under central's
    ALLOWED_HOSTS shape).  A Host header is never a URL with credentials."""
    assert _in_request('attacker.example@shop.example.com',
                       {'ALLOWED_HOSTS': 'shop.example.com'}) == ''
    assert _in_request('attacker.example@shop.example.com',
                       {'ALLOWED_HOSTS': '*'}, internet_base_url) == ''
    assert _in_request('shop.example.com', {'ALLOWED_HOSTS': 'shop.example.com'}) == 'http://shop.example.com'


def test_internet_base_url_prefers_the_configured_url(monkeypatch):
    monkeypatch.setenv(PUBLIC_URL_VAR, 'https://node.example/')
    assert internet_base_url() == 'https://node.example'


def test_internet_base_url_refuses_a_trusted_loopback_or_lan_root():
    """A desktop approving a PhonePe mandate from localhost, or central
    answering a request addressed by its docker bridge address: trusted,
    but no gateway can post a confirmation there (the review of af987a962
    measured a callback on localhost)."""
    assert _in_request('localhost:6777', {'ALLOWED_HOSTS': '*'}, internet_base_url) == ''
    assert _in_request('172.17.0.1:6777', {'ALLOWED_HOSTS': '172.17.0.1'}, internet_base_url) == ''
    assert _in_request('desktop:6777', {'ALLOWED_HOSTS': 'desktop'}, internet_base_url) == ''
    assert _in_request('node.local:6777', {'ALLOWED_HOSTS': 'node.local'}, internet_base_url) == ''


def test_internet_base_url_takes_a_trusted_public_root():
    assert _in_request('shop.example.com', {'ALLOWED_HOSTS': 'shop.example.com'},
                       internet_base_url) == 'http://shop.example.com'
    assert _in_request('evil.example.com', {'ALLOWED_HOSTS': 'shop.example.com'},
                       internet_base_url) == ''


def test_the_configured_url_wins_over_any_host_and_loses_its_trailing_slash():
    out = _in_request('evil.example', {'ALLOWED_HOSTS': '*',
                                       PUBLIC_URL_VAR: 'https://node.example/'},
                      internet_base_url)
    assert out == 'https://node.example'


def test_outside_a_request_without_a_configured_url_it_is_empty(monkeypatch):
    monkeypatch.delenv(PUBLIC_URL_VAR, raising=False)
    assert trusted_request_base() == ''
    assert internet_base_url() == ''


def test_the_configured_reader_alone(monkeypatch):
    monkeypatch.delenv(PUBLIC_URL_VAR, raising=False)
    assert configured_public_url() == ''
    monkeypatch.setenv(PUBLIC_URL_VAR, '  https://node.example//  ')
    assert configured_public_url() == 'https://node.example'
