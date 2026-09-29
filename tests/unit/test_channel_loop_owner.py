"""FlaskChannelIntegration owns the adapters' event loop.

Every caller on another thread (Flask workers, the dispatcher's expert pool,
the announcement broadcaster, on-demand binding wiring) reaches adapters via
running_loop() / ensure_running() / send_threadsafe().  ChannelRegistry has
no loop of its own; reading `registry._loop` always returned None.
"""
import asyncio
import threading

import pytest

from integrations.channels.base import SendResult
from integrations.channels.flask_integration import FlaskChannelIntegration


class _Registry:
    def __init__(self):
        self.sent = []

    async def send_to_channel(self, channel, chat_id, text, **kwargs):
        self.sent.append((channel, chat_id, text))
        return SendResult(success=True, message_id='m1')


def _integration(loop=None):
    fi = FlaskChannelIntegration.__new__(FlaskChannelIntegration)
    fi.registry = _Registry()
    fi._loop = loop
    fi._thread = None
    return fi


@pytest.fixture
def live_loop():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=5)
    loop.close()


def test_running_loop_is_none_until_the_loop_runs():
    assert _integration().running_loop() is None
    idle = asyncio.new_event_loop()
    try:
        assert _integration(idle).running_loop() is None
    finally:
        idle.close()


def test_send_threadsafe_without_a_loop_returns_none():
    assert _integration().send_threadsafe('discord', 'c1', 'hi') is None


def test_send_threadsafe_waits_for_the_result(live_loop):
    fi = _integration(live_loop)
    result = fi.send_threadsafe('discord', 'c1', 'hi', wait=5)
    assert result.success is True
    assert fi.registry.sent == [('discord', 'c1', 'hi')]


def test_send_threadsafe_without_wait_returns_a_future(live_loop):
    fi = _integration(live_loop)
    future = fi.send_threadsafe('slack', 'C9', 'yo')
    assert future.result(timeout=5).message_id == 'm1'


def test_ensure_running_reuses_a_running_loop(live_loop, monkeypatch):
    fi = _integration(live_loop)
    monkeypatch.setattr(fi, 'start', lambda: pytest.fail('must not restart'))
    assert fi.ensure_running() == (live_loop, False)


def test_ensure_running_starts_the_loop_and_reports_it(live_loop, monkeypatch):
    fi = _integration()
    monkeypatch.setattr(fi, 'start', lambda: setattr(fi, '_loop', live_loop))
    assert fi.ensure_running(timeout_s=1) == (live_loop, True)


def test_ensure_running_gives_up_after_the_timeout(monkeypatch):
    fi = _integration()
    monkeypatch.setattr(fi, 'start', lambda: None)
    loop, started = fi.ensure_running(timeout_s=0.3)
    assert loop is None and started is True


def test_router_resolves_the_loop_through_the_owner(live_loop, monkeypatch):
    from integrations.channels import flask_integration
    from integrations.channels.response.router import ChannelResponseRouter
    fi = _integration(live_loop)
    monkeypatch.setattr(flask_integration, 'get_channel_integration', lambda: fi)
    assert ChannelResponseRouter._get_send_loop() is live_loop
