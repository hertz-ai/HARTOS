"""The compute relay serves only the owner it subscribed for.

A phone behind NAT publishes on com.hertzai.hevolve.compute.request.{owner}
and this node runs the request as that owner.  The handler used to read the
owner from HEVOLVE_OWNER_USER_ID on every message, which was only safe while
that value never changed after boot.  Nunba now keeps it in step with sign-in
and sign-out, so the subscription (made for the owner at join time) and the
env can diverge: a request arriving on the OLD owner's topic would have been
run as the NEW owner.  The relay binds the owner at subscription and drops
requests once the node's owner has moved on.

hartos.reuse_recipe (autogen/langchain) is stubbed: it is the crossbar
module's import-time boundary, not what is under test.
"""
import asyncio
import importlib
import sys
import types

import pytest

OWNER_ENV = 'HEVOLVE_OWNER_USER_ID'
TOPIC = 'com.hertzai.hevolve.compute.request.{}'


class FakeSession:
    def __init__(self):
        self.subs = {}
        self.published = []

    async def subscribe(self, handler, topic, options=None):
        self.subs[topic] = handler

    def publish(self, topic, payload):
        self.published.append((topic, payload))


class _Resp:
    ok = True

    def json(self):
        return {'text': 'done'}


@pytest.fixture
def xbar(monkeypatch):
    stub = types.ModuleType('hartos.reuse_recipe')
    stub.chat_agent = stub.crossbar_multiagent = stub.time_based_execution = (
        lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, 'hartos.reuse_recipe', stub)
    monkeypatch.delitem(sys.modules, 'hartos.crossbar_server', raising=False)
    mod = importlib.import_module('hartos.crossbar_server')
    monkeypatch.setitem(sys.modules, 'hartos.crossbar_server', mod)

    posts = []

    def fake_post(url, json=None, timeout=None):
        posts.append(json)
        return _Resp()

    monkeypatch.setattr('requests.post', fake_post)
    session = FakeSession()
    monkeypatch.setattr(mod, 'wamp_session', session)
    return mod, session, posts


def _subscribe(mod, session):
    asyncio.run(mod._subscribe_compute_relay(session))


def test_request_on_the_owners_topic_runs_as_that_owner(xbar, monkeypatch):
    mod, session, posts = xbar
    monkeypatch.setenv(OWNER_ENV, 'user-A')
    _subscribe(mod, session)

    handler = session.subs[TOPIC.format('user-A')]
    asyncio.run(handler({'text': 'hi', 'request_id': 'r1'}))

    assert [p['user_id'] for p in posts] == ['user-A']
    assert [t for t, _ in session.published] == [
        'com.hertzai.hevolve.compute.response.user-A']


def test_owner_change_after_subscribing_drops_the_old_topics_requests(xbar, monkeypatch):
    """RED before the fix: the request ran as user-B, and its reply went to
    user-B's topic, though it arrived on user-A's."""
    mod, session, posts = xbar
    monkeypatch.setenv(OWNER_ENV, 'user-A')
    _subscribe(mod, session)
    handler = session.subs[TOPIC.format('user-A')]

    monkeypatch.setenv(OWNER_ENV, 'user-B')          # sign-in on this desktop
    asyncio.run(handler({'text': 'hi', 'request_id': 'r2'}))

    assert posts == []
    assert session.published == []


def test_body_claiming_another_user_is_still_rejected(xbar, monkeypatch):
    mod, session, posts = xbar
    monkeypatch.setenv(OWNER_ENV, 'user-A')
    _subscribe(mod, session)
    handler = session.subs[TOPIC.format('user-A')]

    asyncio.run(handler({'text': 'hi', 'user_id': 'user-X'}))

    assert posts == []


def test_no_owner_subscribes_nothing(xbar, monkeypatch):
    mod, session, _ = xbar
    monkeypatch.delenv(OWNER_ENV, raising=False)
    _subscribe(mod, session)
    assert session.subs == {}
