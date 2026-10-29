"""The paid API's relay reaches this node's own /chat wherever the node serves it.

Measured live on the installed Nunba desktop 2026-10-29 18:19 (task #160): a
free key's POST /api/v1/intelligence/chat answered 503 "Intelligence backend
unavailable; this call was not billed", and the node logged "Metered
api_intelligence /chat transport error: HTTPConnectionPool(host='localhost',
port=6777) ... NewConnectionError".  The relay dialled get_port('backend')
(6777); the bundled desktop serves HARTOS in-process on the Flask port (5000)
and never binds 6777, so no metered call could ever be answered there.

core.port_registry.get_local_backend_url is the one resolver of the live
local serve port.  The relay takes its loopback probe but not its
HEVOLVE_BASE_URL override: central sets that variable to its LAN address, and
a paying customer's call stays on loopback there (its own /chat rate-limit
key, never the docker address every public caller shares, task #123).

Behavioural: the real _metered_backend_chat in a request context; the port
probe and the /chat client are the stubbed boundaries.
"""
import os
import sys

import pytest
from flask import Flask, g

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class _Sent(Exception):
    """The relay handed its request to the /chat client; nothing is sent."""


@pytest.fixture
def relay(monkeypatch):
    """Call the real relay once; return the URL it gave the /chat client."""
    import core.chat_client as chat_client
    from integrations.agent_engine import commercial_api

    dialled = []

    def capture(url, *a, **k):
        dialled.append(url)
        raise _Sent()

    monkeypatch.setattr(chat_client, 'post_chat', capture)

    def call():
        app = Flask(__name__)
        with app.test_request_context('/api/v1/intelligence/chat', method='POST'):
            g.api_key = {'id': 'key-1', 'user_id': 'cust-42', 'tier': 'free'}
            ok, _text, status = commercial_api._metered_backend_chat(
                'What is a recipe?', 'api_intelligence')
        assert (ok, status) == (False, 502), 'the stubbed client never answers'
        assert len(dialled) == 1, dialled
        return dialled[0]
    return call


def _listening(monkeypatch, *services):
    import core.port_registry as pr
    ports = {pr.get_port(s) for s in services}
    monkeypatch.setattr(pr, '_is_port_listening',
                        lambda port, *a, **k: int(port) in ports)
    return pr


def test_on_the_bundled_desktop_the_relay_dials_the_flask_port(relay, monkeypatch):
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)
    pr = _listening(monkeypatch, 'flask')
    assert relay() == f'http://localhost:{pr.get_port("flask")}/chat'


def test_on_a_standalone_node_the_relay_dials_the_backend_port(relay, monkeypatch):
    monkeypatch.delenv('HEVOLVE_BASE_URL', raising=False)
    pr = _listening(monkeypatch, 'backend')
    assert relay() == f'http://localhost:{pr.get_port("backend")}/chat'


def test_central_keeps_the_relay_on_loopback_whatever_its_base_url(relay, monkeypatch):
    # Central's own setting, measured 2026-10-29: its LAN address on :6777.
    monkeypatch.setenv('HEVOLVE_BASE_URL', 'http://192.168.0.9:6777')
    pr = _listening(monkeypatch, 'backend')
    assert relay() == f'http://localhost:{pr.get_port("backend")}/chat'


def test_the_resolver_skips_the_base_url_only_when_asked(monkeypatch):
    monkeypatch.setenv('HEVOLVE_BASE_URL', 'http://192.168.0.9:6777/')
    pr = _listening(monkeypatch, 'flask')
    assert pr.get_local_backend_url() == 'http://192.168.0.9:6777'
    assert pr.get_local_backend_url(honor_base_url=False) == \
        f'http://localhost:{pr.get_port("flask")}'
