"""Importing security must not wait on a Redis that is not there.

security/jwt_manager.py built its singleton TokenBlocklist at IMPORT, and
its __init__ pinged redis://localhost:6379. On a desktop with no Redis
(REDIS_URL unset -- the default), that refusal takes 4.10 s on Windows
(measured 2026-09-23), and `security` is imported transitively by
integrations.vision -> frame_store -> native_hive_loader. So every process
paid 4 s before doing anything: Nunba's boot, each GPU worker spawn (whose
get_catalog populate imports vision), every test run. Measured:
get_catalog in a fresh process 4.35 s, of which the populate itself is
0.05 s.

The blocklist still uses Redis when it is there. It connects on first use,
once, with a bounded connect timeout; the in-memory blocklist is always
authoritative for this process, as before.
"""
import importlib
import sys
import time
from unittest.mock import MagicMock, patch

import pytest


def _fresh_module(from_url):
    fake_redis = MagicMock()
    fake_redis.from_url = from_url
    sys.modules.pop('security.jwt_manager', None)
    with patch.dict(sys.modules, {'redis': fake_redis}):
        mod = importlib.import_module('security.jwt_manager')
    return mod, fake_redis


@pytest.fixture(autouse=True)
def _restore_module():
    saved = sys.modules.get('security.jwt_manager')
    yield
    if saved is not None:
        sys.modules['security.jwt_manager'] = saved
    else:
        sys.modules.pop('security.jwt_manager', None)


def test_importing_does_not_touch_redis():
    from_url = MagicMock()
    _fresh_module(from_url)
    assert from_url.call_count == 0, (
        'importing security.jwt_manager connected to Redis; with no Redis '
        'that costs 4 s in every process')


def test_first_use_connects_once_with_a_bounded_timeout():
    client = MagicMock()
    from_url = MagicMock(return_value=client)
    mod, fake = _fresh_module(from_url)
    with patch.dict(sys.modules, {'redis': fake}):
        mod._blocklist.add('jti-1')
        mod._blocklist.is_blocked('jti-2')
    assert from_url.call_count == 1
    timeout = from_url.call_args.kwargs.get('socket_connect_timeout')
    assert timeout is not None and 0 < timeout <= 2, (
        f'connect must be bounded; got socket_connect_timeout={timeout!r}')
    client.setex.assert_called_once()          # Redis still used when present


def test_an_absent_redis_is_asked_once_and_memory_still_works():
    client = MagicMock()
    client.ping.side_effect = ConnectionError('10061 refused')
    from_url = MagicMock(return_value=client)
    mod, fake = _fresh_module(from_url)
    with patch.dict(sys.modules, {'redis': fake}):
        for i in range(5):
            mod._blocklist.add(f'jti-{i}')
        assert mod._blocklist.is_blocked('jti-3') is True
        assert mod._blocklist.is_blocked('never') is False
    assert from_url.call_count == 1, 'a missing Redis must not be retried per call'
    client.setex.assert_not_called()


def test_redis_not_installed_is_fine():
    sys.modules.pop('security.jwt_manager', None)
    with patch.dict(sys.modules, {'redis': None}):   # import redis -> ImportError
        mod = importlib.import_module('security.jwt_manager')
        mod._blocklist.add('x')
        assert mod._blocklist.is_blocked('x') is True
