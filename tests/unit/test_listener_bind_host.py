"""A desktop's own listeners bind loopback; every other node keeps 0.0.0.0.

Measured 2026-09-14 on an installed desktop: the vision frame socket (5460)
and the web channel (8765) listened on 0.0.0.0 with no credential, and the
installer's firewall rule allows inbound on Private and Public networks.
core.port_registry.bind_host is the one rule both listeners take their
interface from: the service's own variable, then NUNBA_BIND_HOST, then
loopback on a bundled desktop (core.config_cache.is_bundled) and 0.0.0.0
elsewhere.  The reachability tests bind real sockets on free ports: a
loopback client still connects (the SPA's path), and the machine's LAN
address is refused.

    python -m pytest tests/unit/test_listener_bind_host.py --noconftest -q
"""
import asyncio
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.port_registry import bind_host, get_lan_ip  # noqa: E402

_HOST_VARS = ('NUNBA_BIND_HOST', 'WEB_ADAPTER_HOST')


@pytest.fixture
def clean_env(monkeypatch):
    for name in _HOST_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _bundled(value):
    return patch('core.config_cache.is_bundled', return_value=value)


async def _refused(host, port):
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), 2)
    except (OSError, asyncio.TimeoutError):
        return True
    writer.close()
    return False


@pytest.mark.parametrize('bundled, expected', [(True, '127.0.0.1'),
                                               (False, '0.0.0.0')])
def test_the_default_follows_is_bundled(clean_env, bundled, expected):
    with _bundled(bundled):
        assert bind_host() == expected
        assert bind_host('WEB_ADAPTER_HOST') == expected


def test_nunba_bind_host_is_set_once_for_every_listener(clean_env):
    clean_env.setenv('NUNBA_BIND_HOST', '0.0.0.0')
    with _bundled(True):
        assert bind_host() == '0.0.0.0'
        assert bind_host('WEB_ADAPTER_HOST') == '0.0.0.0'


def test_a_services_own_variable_wins(clean_env):
    clean_env.setenv('NUNBA_BIND_HOST', '0.0.0.0')
    clean_env.setenv('WEB_ADAPTER_HOST', '10.0.0.5')
    with _bundled(True):
        assert bind_host('WEB_ADAPTER_HOST') == '10.0.0.5'
        assert bind_host() == '0.0.0.0'


def test_a_blank_variable_is_not_a_choice(clean_env):
    clean_env.setenv('NUNBA_BIND_HOST', '  ')
    with _bundled(True):
        assert bind_host() == '127.0.0.1'


def test_the_web_channel_takes_its_host_from_the_rule(clean_env, tmp_path):
    from integrations.channels.web_adapter import create_web_adapter
    extra = {'upload_dir': str(tmp_path)}
    with _bundled(True):
        assert create_web_adapter(extra=extra)._host == '127.0.0.1'
    with _bundled(False):
        assert create_web_adapter(extra=extra)._host == '0.0.0.0'
    clean_env.setenv('WEB_ADAPTER_HOST', '10.0.0.5')
    with _bundled(True):
        assert create_web_adapter(extra=extra)._host == '10.0.0.5'
        assert create_web_adapter(host='192.0.2.1', extra=extra)._host == '192.0.2.1'


@pytest.mark.asyncio
async def test_the_vision_socket_takes_a_loopback_client_and_refuses_the_lan(clean_env):
    import websockets
    from integrations.vision.vision_service import VisionService
    svc = VisionService.__new__(VisionService)   # the frame socket only, no sidecar
    svc._ws_port = 0                               # a free port: the desktop holds 5460
    svc._running = True
    with _bundled(True):
        task = asyncio.create_task(svc._ws_serve())
        try:
            for _ in range(300):
                if svc._ws_port:
                    break
                await asyncio.sleep(0.01)
            assert svc._ws_port, 'the frame socket never bound'
            async with websockets.connect(f'ws://127.0.0.1:{svc._ws_port}',
                                          open_timeout=5):
                pass
            lan = get_lan_ip()
            if lan:
                assert await _refused(lan, svc._ws_port), \
                    f'the frame socket answers on the LAN address {lan}'
        finally:
            svc._running = False
            await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
async def test_the_web_channel_takes_a_loopback_client_and_refuses_the_lan(clean_env, tmp_path):
    from integrations.channels.web_adapter import create_web_adapter
    clean_env.setenv('WEB_ADAPTER_PORT', '0')     # a free port: the desktop holds 8765
    with _bundled(True):
        adapter = create_web_adapter(extra={'upload_dir': str(tmp_path)})
    assert await adapter.connect()
    try:
        port = adapter._site._server.sockets[0].getsockname()[1]
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection('127.0.0.1', port), 5)
        writer.close()
        lan = get_lan_ip()
        if lan:
            assert await _refused(lan, port), \
                f'the web channel answers on the LAN address {lan}'
    finally:
        await adapter.disconnect()
