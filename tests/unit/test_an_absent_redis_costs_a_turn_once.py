"""A Redis that refused costs a turn once, not every new ledger.

Measured 22 Nov 2026 on the installed Nunba by hevolve-66e6 (request
msg_1795326762473_nquh6t, memory nunba_first_turn_latency_anatomy.md):
- Every new ledger dialled localhost:6379, where nothing listens on a
  desktop.
- It did so twice per turn: get_production_backend (agent_ledger/core.py)
  and create_production_ledger (factory.py, reached through the
  user_ledgers cache loader, under that cache's lock).
- One refused connect on this Windows desktop takes 4.08 s (::1, then
  127.0.0.1).
So about 8 s of the first reply went to a server that is not there.
RedisBackend.__init__ is the one connect and ping that both go through.

Only the network is faked: redis.Redis is a recorder whose ping refuses
or answers.  RedisBackend, get_production_backend, core.redis_client and
core.circuit_breaker run for real.
"""

import sys

import pytest

redis = pytest.importorskip('redis')

from agent_ledger import backends  # noqa: E402
from agent_ledger.backends import RedisBackend  # noqa: E402

REFUSED = ("Error 10061 connecting to localhost:6399. No connection could "
           "be made because the target machine actively refused it.")


class _Endpoint:
    """What a Redis endpoint did: the clients made for it, and its pings."""

    def __init__(self):
        self.answers = False
        self.made = []
        self.pings = 0

    def redis_class(self):
        endpoint = self

        class _RecordingRedis:
            def __init__(self, **kwargs):
                endpoint.made.append(kwargs)

            def ping(self):
                endpoint.pings += 1
                if not endpoint.answers:
                    raise redis.exceptions.ConnectionError(REFUSED)
                return True

        return _RecordingRedis


@pytest.fixture
def endpoint(monkeypatch):
    found = _Endpoint()
    monkeypatch.setattr(redis, 'Redis', found.redis_class())
    # Each test starts with nothing remembered as refused.
    monkeypatch.setattr(backends, '_refused_redis', None, raising=False)
    return found


def test_a_refused_redis_is_dialled_once_for_two_new_ledgers(endpoint):
    for _ in range(2):
        with pytest.raises(ConnectionError):
            RedisBackend(host='localhost', port=6399)
    assert endpoint.pings == 1


def test_the_dial_gives_up_after_a_second_and_is_not_retried(endpoint):
    with pytest.raises(ConnectionError):
        RedisBackend(host='localhost', port=6399)
    made = endpoint.made[0]
    assert made.get('socket_connect_timeout') is not None
    assert made['socket_connect_timeout'] <= 1
    assert 'retry' in made and made['retry'] is None


def test_the_skipped_dial_says_which_endpoint_and_for_how_long(endpoint):
    with pytest.raises(ConnectionError):
        RedisBackend(host='localhost', port=6399)
    with pytest.raises(ConnectionError) as skipped:
        RedisBackend(host='localhost', port=6399)
    said = str(skipped.value)
    assert 'localhost:6399' in said
    assert '300' in said


def test_get_production_backend_falls_back_without_dialling_again(endpoint):
    from agent_ledger.core import get_production_backend
    assert get_production_backend() is None
    assert get_production_backend() is None
    assert endpoint.pings == 1


def test_another_endpoint_is_still_dialled(endpoint):
    with pytest.raises(ConnectionError):
        RedisBackend(host='localhost', port=6399)
    with pytest.raises(ConnectionError):
        RedisBackend(host='localhost', port=6400)
    assert endpoint.pings == 2


def test_after_the_cooldown_it_dials_again_and_an_answer_is_used(
        endpoint, monkeypatch):
    import core.circuit_breaker as circuit_breaker
    clock = [1000.0]
    monkeypatch.setattr(circuit_breaker.time, 'monotonic', lambda: clock[0])

    with pytest.raises(ConnectionError):
        RedisBackend(host='localhost', port=6399)
    clock[0] += 301
    endpoint.answers = True
    assert RedisBackend(host='localhost', port=6399).redis_client is not None
    assert RedisBackend(host='localhost', port=6399).redis_client is not None
    assert endpoint.pings == 3


def test_reads_and_writes_on_a_live_redis_keep_no_time_limit(endpoint):
    """Pin: the distributed coordinator keeps backend.redis_client for
    pub/sub and locks (coordinator_backends._try_redis_backend), and a
    blocking listen must not time out.  Only the connect is limited."""
    endpoint.answers = True
    RedisBackend(host='localhost', port=6399)
    assert endpoint.made[-1].get('socket_timeout') is None


def test_without_hartos_core_the_dial_still_gives_up_after_a_second(
        endpoint, monkeypatch):
    """agent_ledger is also used on its own: with no core package the
    connect is still limited (there is then no memory of refusals)."""
    monkeypatch.setitem(sys.modules, 'core.redis_client', None)
    monkeypatch.setitem(sys.modules, 'core.circuit_breaker', None)
    with pytest.raises(ConnectionError):
        RedisBackend(host='localhost', port=6399)
    made = endpoint.made[0]
    assert made.get('socket_connect_timeout') is not None
    assert made['socket_connect_timeout'] <= 1
    assert 'retry' in made and made['retry'] is None


def test_no_redis_package_is_still_an_import_error(monkeypatch):
    """Pin: get_production_backend and create_production_ledger tell
    "not installed" from "not reachable" by ImportError."""
    monkeypatch.setattr(backends, '_refused_redis', None, raising=False)
    monkeypatch.setitem(sys.modules, 'redis', None)
    with pytest.raises(ImportError):
        RedisBackend(host='localhost', port=6399)
