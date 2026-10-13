"""core.public_url: the ONE reader of this node's public base URL.

A forged Host must never become the URL a payment gateway sends the buyer
and the signed confirmation to (ALLOWED_HOSTS=* is the deploy default).
These drive the real reader under a real Flask request context; only the
environment is set.
"""
import os
from unittest.mock import patch

from flask import Flask

from core.public_url import (
    PUBLIC_URL_VAR, configured_public_url, public_base_url, trusted_request_base,
)


def _in_request(host, env):
    """public_base_url() inside a request carrying ``host``, with ``env``
    applied and HARTOS_PUBLIC_URL absent unless ``env`` sets it."""
    app = Flask('t')
    with patch.dict(os.environ, env, clear=False), \
            app.test_request_context('/', headers={'Host': host}):
        if PUBLIC_URL_VAR not in env:
            os.environ.pop(PUBLIC_URL_VAR, None)
        return public_base_url()


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


def test_the_configured_url_wins_over_any_host_and_loses_its_trailing_slash():
    out = _in_request('evil.example', {'ALLOWED_HOSTS': '*',
                                       PUBLIC_URL_VAR: 'https://node.example/'})
    assert out == 'https://node.example'


def test_outside_a_request_without_a_configured_url_it_is_empty(monkeypatch):
    monkeypatch.delenv(PUBLIC_URL_VAR, raising=False)
    assert trusted_request_base() == ''
    assert public_base_url() == ''


def test_the_configured_reader_alone(monkeypatch):
    monkeypatch.delenv(PUBLIC_URL_VAR, raising=False)
    assert configured_public_url() == ''
    monkeypatch.setenv(PUBLIC_URL_VAR, '  https://node.example//  ')
    assert configured_public_url() == 'https://node.example'
