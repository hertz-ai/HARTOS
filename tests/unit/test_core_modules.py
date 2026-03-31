"""
Comprehensive tests for HARTOS core modules:
  - session_cache.TTLCache
  - config_cache (is_bundled, get_db_url, get_action_api, get_secret, reload_config)
  - http_pool (pooled_get, pooled_post, pooled_patch, session singleton)
  - circuit_breaker (CircuitBreaker states, decorator, CircuitBreakerOpenError)
  - port_registry (get_port, is_os_mode, get_local_llm_url, check_port_available)
  - platform_paths (get_data_dir, get_db_dir, get_prompts_dir, cross-OS)

Target: 70+ tests across 6 files.
"""

import os
import sys
import time
import threading
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Fixtures — reset module-level caches between tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_caches():
    """Reset all module-level caches so tests are isolated."""
    # platform_paths
    from core import platform_paths
    platform_paths.reset_cache()

    # config_cache
    from core import config_cache
    with config_cache._config_lock:
        config_cache._config = None

    # port_registry
    from core import port_registry
    port_registry._os_mode_cached = None
    port_registry.invalidate_llm_url()

    # http_pool — reset the singleton session
    from core import http_pool
    http_pool._session = None

    # Clean env vars that tests might set
    env_keys = [
        'NUNBA_BUNDLED', 'NUNBA_PORT', 'NUNBA_DATA_DIR', 'HARTOS_DATA_DIR',
        'HART_OS_MODE', 'HARTOS_BACKEND_PORT', 'HEVOLVE_LOCAL_LLM_URL',
        'CUSTOM_LLM_BASE_URL', 'LLAMA_CPP_PORT', 'HEVOLVE_LOCAL_LLM_MODEL',
        'DB_URL', 'ACTION_API', 'XDG_DATA_HOME',
    ]
    saved = {k: os.environ.pop(k, None) for k in env_keys}
    yield
    # Restore
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    # Re-reset caches after test
    platform_paths.reset_cache()
    port_registry._os_mode_cached = None
    port_registry.invalidate_llm_url()
    with config_cache._config_lock:
        config_cache._config = None
    http_pool._session = None


# ===========================================================================
# TTLCache tests
# ===========================================================================

class TestTTLCacheBasic:
    """Basic get/set/delete operations."""

    def test_set_and_get(self):
        from core.session_cache import TTLCache
        c = TTLCache()
        c['a'] = 1
        assert c['a'] == 1

    def test_get_missing_raises_keyerror(self):
        from core.session_cache import TTLCache
        c = TTLCache()
        with pytest.raises(KeyError):
            _ = c['missing']

    def test_delete(self):
        from core.session_cache import TTLCache
        c = TTLCache()
        c['a'] = 1
        del c['a']
        with pytest.raises(KeyError):
            _ = c['a']

    def test_overwrite(self):
        from core.session_cache import TTLCache
        c = TTLCache()
        c['k'] = 'old'
        c['k'] = 'new'
        assert c['k'] == 'new'

    def test_get_default(self):
        from core.session_cache import TTLCache
        c = TTLCache()
        assert c.get('nope') is None
        assert c.get('nope', 42) == 42

    def test_get_existing(self):
        from core.session_cache import TTLCache
        c = TTLCache()
        c['x'] = 99
        assert c.get('x') == 99

    def test_contains_true(self):
        from core.session_cache import TTLCache
        c = TTLCache()
        c['k'] = 1
        assert 'k' in c

    def test_contains_false(self):
        from core.session_cache import TTLCache
        c = TTLCache()
        assert 'k' not in c

    def test_len_empty(self):
        from core.session_cache import TTLCache
        c = TTLCache()
        assert len(c) == 0

    def test_len_after_inserts(self):
        from core.session_cache import TTLCache
        c = TTLCache()
        c['a'] = 1
        c['b'] = 2
        assert len(c) == 2

    def test_keys_values_items(self):
        from core.session_cache import TTLCache
        c = TTLCache()
        c['a'] = 1
        c['b'] = 2
        assert set(c.keys()) == {'a', 'b'}
        assert set(c.values()) == {1, 2}
        assert set(c.items()) == {('a', 1), ('b', 2)}

    def test_clear(self):
        from core.session_cache import TTLCache
        c = TTLCache()
        c['a'] = 1
        c['b'] = 2
        c.clear()
        assert len(c) == 0

    def test_pop_existing(self):
        from core.session_cache import TTLCache
        c = TTLCache()
        c['k'] = 10
        assert c.pop('k') == 10
        assert 'k' not in c

    def test_pop_missing_default(self):
        from core.session_cache import TTLCache
        c = TTLCache()
        assert c.pop('missing', 'default') == 'default'

    def test_pop_missing_raises(self):
        from core.session_cache import TTLCache
        c = TTLCache()
        with pytest.raises(KeyError):
            c.pop('missing')

    def test_setdefault_missing(self):
        from core.session_cache import TTLCache
        c = TTLCache()
        val = c.setdefault('k', 42)
        assert val == 42
        assert c['k'] == 42

    def test_setdefault_existing(self):
        from core.session_cache import TTLCache
        c = TTLCache()
        c['k'] = 1
        val = c.setdefault('k', 99)
        assert val == 1


class TestTTLCacheExpiration:
    """TTL expiration behavior."""

    def test_entry_expires_after_ttl(self):
        from core.session_cache import TTLCache
        c = TTLCache(ttl_seconds=0)  # instant expiry
        c['k'] = 'val'
        # Expired immediately (ttl=0, any elapsed > 0 is expired)
        time.sleep(0.05)
        with pytest.raises(KeyError):
            _ = c['k']

    def test_contains_false_after_expiry(self):
        from core.session_cache import TTLCache
        c = TTLCache(ttl_seconds=0)
        c['k'] = 1
        time.sleep(0.05)
        assert 'k' not in c

    def test_len_excludes_expired(self):
        from core.session_cache import TTLCache
        c = TTLCache(ttl_seconds=1)
        c['a'] = 1
        c['b'] = 2
        # Artificially backdate timestamps so entries are expired
        for k in list(c._timestamps):
            c._timestamps[k] -= 10
        assert len(c) == 0

    def test_get_returns_default_after_expiry(self):
        from core.session_cache import TTLCache
        c = TTLCache(ttl_seconds=0)
        c['k'] = 'val'
        time.sleep(0.05)
        assert c.get('k', 'gone') == 'gone'

    def test_stats_shows_expired(self):
        from core.session_cache import TTLCache
        c = TTLCache(ttl_seconds=1)
        c['a'] = 1
        # Backdate so it's definitely expired
        c._timestamps['a'] -= 10
        stats = c.stats()
        assert stats['active'] == 0
        assert stats['expired'] >= 1


class TestTTLCacheMaxSize:
    """Max size eviction."""

    def test_evicts_oldest_when_full(self):
        from core.session_cache import TTLCache
        c = TTLCache(max_size=3)
        c['a'] = 1
        c['b'] = 2
        c['c'] = 3
        c['d'] = 4  # should evict 'a'
        assert 'a' not in c
        assert c['d'] == 4
        assert len(c) == 3

    def test_evicts_multiple_to_stay_at_max(self):
        from core.session_cache import TTLCache
        c = TTLCache(max_size=2)
        c['a'] = 1
        c['b'] = 2
        c['c'] = 3
        # 'a' should be evicted, 'b' and 'c' remain
        assert len(c) <= 2
        assert c['c'] == 3

    def test_overwrite_does_not_increase_size(self):
        from core.session_cache import TTLCache
        c = TTLCache(max_size=2)
        c['a'] = 1
        c['b'] = 2
        c['a'] = 10  # overwrite, not a new entry
        assert len(c) == 2
        assert c['a'] == 10


class TestTTLCacheLoader:
    """Loader callback behavior."""

    def test_loader_called_on_miss(self):
        from core.session_cache import TTLCache
        loader = MagicMock(return_value='loaded_val')
        c = TTLCache(loader=loader)
        assert c['mykey'] == 'loaded_val'
        loader.assert_called_once_with('mykey')

    def test_loader_caches_result(self):
        from core.session_cache import TTLCache
        call_count = 0
        def loader(key):
            nonlocal call_count
            call_count += 1
            return f'val_{key}'
        c = TTLCache(loader=loader)
        assert c['k'] == 'val_k'
        assert c['k'] == 'val_k'
        assert call_count == 1  # second call served from cache

    def test_loader_returns_none_raises_keyerror(self):
        from core.session_cache import TTLCache
        c = TTLCache(loader=lambda k: None)
        with pytest.raises(KeyError):
            _ = c['k']

    def test_loader_exception_raises_keyerror(self):
        from core.session_cache import TTLCache
        c = TTLCache(loader=lambda k: (_ for _ in ()).throw(RuntimeError('fail')))
        with pytest.raises(KeyError):
            _ = c['k']

    def test_loader_used_in_contains(self):
        from core.session_cache import TTLCache
        c = TTLCache(loader=lambda k: 'found')
        assert 'anything' in c

    def test_loader_not_called_when_cached(self):
        from core.session_cache import TTLCache
        loader = MagicMock(return_value='loaded')
        c = TTLCache(loader=loader)
        c['k'] = 'direct'
        assert c['k'] == 'direct'
        loader.assert_not_called()

    def test_loader_used_after_expiry(self):
        from core.session_cache import TTLCache
        c = TTLCache(ttl_seconds=0, loader=lambda k: 'reloaded')
        c['k'] = 'original'
        time.sleep(0.01)
        assert c['k'] == 'reloaded'


class TestTTLCacheThreadSafety:
    """Thread safety."""

    def test_concurrent_writes(self):
        from core.session_cache import TTLCache
        c = TTLCache(max_size=5000)
        errors = []

        def writer(start):
            try:
                for i in range(500):
                    c[f'{start}_{i}'] = i
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # All 2000 keys should be present (max_size=5000)
        assert len(c) == 2000

    def test_concurrent_read_write(self):
        from core.session_cache import TTLCache
        c = TTLCache()
        c['shared'] = 0
        errors = []

        def reader():
            try:
                for _ in range(200):
                    c.get('shared', None)
            except Exception as e:
                errors.append(e)

        def writer():
            try:
                for i in range(200):
                    c['shared'] = i
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(3)]
        threads.append(threading.Thread(target=writer))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors


class TestTTLCacheCleanup:
    """Periodic cleanup triggered every 100 writes."""

    def test_cleanup_triggers_after_100_writes(self):
        from core.session_cache import TTLCache
        c = TTLCache(ttl_seconds=0)
        # Insert 99 entries (no cleanup yet)
        for i in range(99):
            c[f'k{i}'] = i
        time.sleep(0.01)
        # The 100th write triggers cleanup
        c['trigger'] = 'x'
        # After cleanup, expired entries are removed; only 'trigger' remains
        # (it was just written so not yet expired)
        # Internal data should be cleaned
        assert c._cleanup_counter == 0


# ===========================================================================
# config_cache tests
# ===========================================================================

class TestConfigCacheIsBundled:
    """is_bundled() detection."""

    def test_not_bundled_by_default(self):
        from core.config_cache import is_bundled
        # No NUNBA_BUNDLED env, not frozen
        assert is_bundled() is False

    def test_bundled_via_env(self):
        from core.config_cache import is_bundled
        os.environ['NUNBA_BUNDLED'] = '1'
        assert is_bundled() is True

    def test_bundled_via_frozen(self):
        from core.config_cache import is_bundled
        with patch.object(sys, 'frozen', True, create=True):
            assert is_bundled() is True


class TestConfigCacheGetSecret:
    """get_secret() env-first resolution."""

    def test_env_takes_precedence(self):
        from core.config_cache import get_secret
        os.environ['MY_SECRET'] = 'from_env'
        assert get_secret('MY_SECRET', 'default') == 'from_env'

    def test_falls_back_to_config(self):
        from core import config_cache
        config_cache._config = {'SOME_KEY': 'from_config'}
        assert config_cache.get_secret('SOME_KEY') == 'from_config'

    def test_returns_default(self):
        from core import config_cache
        config_cache._config = {}
        assert config_cache.get_secret('NOPE', 'fallback') == 'fallback'


class TestConfigCacheURLs:
    """Endpoint resolution: bundled vs standalone."""

    def test_get_db_url_bundled(self):
        from core.config_cache import get_db_url
        os.environ['NUNBA_BUNDLED'] = '1'
        url = get_db_url()
        assert 'localhost' in url
        assert '5000' in url

    def test_get_db_url_bundled_custom_port(self):
        from core.config_cache import get_db_url
        os.environ['NUNBA_BUNDLED'] = '1'
        os.environ['NUNBA_PORT'] = '9999'
        assert '9999' in get_db_url()

    def test_get_action_api_bundled(self):
        from core.config_cache import get_action_api
        os.environ['NUNBA_BUNDLED'] = '1'
        url = get_action_api()
        assert url.endswith('/create_action')

    def test_get_action_api_standalone_env(self):
        from core import config_cache
        config_cache._config = {}
        os.environ['ACTION_API'] = 'http://cloud.example.com/action'
        assert config_cache.get_action_api() == 'http://cloud.example.com/action'

    def test_get_db_url_standalone_env(self):
        from core import config_cache
        config_cache._config = {}
        os.environ['DB_URL'] = 'http://db.example.com'
        assert config_cache.get_db_url() == 'http://db.example.com'

    def test_reload_config_resets(self):
        from core import config_cache
        config_cache._config = {'old': True}
        # reload_config sets _config to None then re-loads
        # Since no config file exists in test env, it will return {} or similar
        result = config_cache.reload_config()
        assert isinstance(result, dict)


# ===========================================================================
# http_pool tests
# ===========================================================================

class TestHttpPool:
    """Connection-pooled HTTP functions."""

    def test_session_singleton(self):
        from core.http_pool import get_http_session
        s1 = get_http_session()
        s2 = get_http_session()
        assert s1 is s2

    def test_session_has_retry_adapter(self):
        from core.http_pool import get_http_session
        s = get_http_session()
        adapter = s.get_adapter('http://example.com')
        assert adapter.max_retries.total == 3

    def test_session_default_content_type(self):
        from core.http_pool import get_http_session
        s = get_http_session()
        assert s.headers.get('Content-Type') == 'application/json'

    def test_pooled_get_calls_session(self):
        from core import http_pool
        mock_session = MagicMock()
        mock_session.get.return_value = MagicMock(status_code=200)
        http_pool._session = mock_session
        resp = http_pool.pooled_get('http://example.com/test')
        mock_session.get.assert_called_once_with(
            'http://example.com/test', timeout=(5, 30))
        assert resp.status_code == 200

    def test_pooled_post_calls_session(self):
        from core import http_pool
        mock_session = MagicMock()
        mock_session.post.return_value = MagicMock(status_code=201)
        http_pool._session = mock_session
        resp = http_pool.pooled_post('http://example.com/api', json={'key': 'val'})
        mock_session.post.assert_called_once_with(
            'http://example.com/api', timeout=(5, 30), json={'key': 'val'})
        assert resp.status_code == 201

    def test_pooled_patch_calls_session(self):
        from core import http_pool
        mock_session = MagicMock()
        mock_session.patch.return_value = MagicMock(status_code=200)
        http_pool._session = mock_session
        resp = http_pool.pooled_patch('http://example.com/item/1', json={'x': 1})
        mock_session.patch.assert_called_once()

    def test_pooled_put_calls_session(self):
        from core import http_pool
        mock_session = MagicMock()
        mock_session.put.return_value = MagicMock(status_code=200)
        http_pool._session = mock_session
        http_pool.pooled_put('http://example.com/item/1')
        mock_session.put.assert_called_once()

    def test_pooled_delete_calls_session(self):
        from core import http_pool
        mock_session = MagicMock()
        mock_session.delete.return_value = MagicMock(status_code=204)
        http_pool._session = mock_session
        http_pool.pooled_delete('http://example.com/item/1')
        mock_session.delete.assert_called_once()

    def test_custom_timeout(self):
        from core import http_pool
        mock_session = MagicMock()
        mock_session.get.return_value = MagicMock()
        http_pool._session = mock_session
        http_pool.pooled_get('http://example.com', timeout=10)
        mock_session.get.assert_called_once_with('http://example.com', timeout=10)

    def test_pooled_post_llm_logging_no_crash(self):
        """POST to /chat/completions triggers LLM logging — must not crash."""
        from core import http_pool
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'choices': [{'message': {'content': 'hello', 'reasoning_content': ''}}],
            'usage': {'completion_tokens': 5},
        }
        mock_session = MagicMock()
        mock_session.post.return_value = mock_resp
        http_pool._session = mock_session
        resp = http_pool.pooled_post(
            'http://localhost:8080/v1/chat/completions',
            json={'messages': [{'role': 'user', 'content': 'hi'}]})
        assert resp is mock_resp

    def test_retry_status_forcelist(self):
        from core.http_pool import get_http_session
        s = get_http_session()
        adapter = s.get_adapter('https://example.com')
        assert 502 in adapter.max_retries.status_forcelist


# ===========================================================================
# circuit_breaker tests
# ===========================================================================

class TestCircuitBreaker:
    """CircuitBreaker state transitions."""

    def test_starts_closed(self):
        from core.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(threshold=3)
        assert cb.state == CircuitState.CLOSED

    def test_stays_closed_below_threshold(self):
        from core.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_opens_at_threshold(self):
        from core.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(threshold=3, cooldown=60)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_is_open_blocks_requests(self):
        from core.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker(threshold=1, cooldown=60)
        cb.record_failure()
        assert cb.is_open() is True

    def test_half_open_after_cooldown(self):
        from core.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(threshold=1, cooldown=0.01)
        cb.record_failure()
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_allows_one_probe(self):
        from core.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker(threshold=1, cooldown=0.01)
        cb.record_failure()
        time.sleep(0.02)
        # First call: probe allowed
        assert cb.is_open() is False
        # Second call: blocked
        assert cb.is_open() is True

    def test_success_resets_to_closed(self):
        from core.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(threshold=2)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_manual_reset(self):
        from core.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(threshold=1)
        cb.record_failure()
        cb.reset()
        assert cb.state == CircuitState.CLOSED

    def test_get_stats(self):
        from core.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker(name='test_cb', threshold=5, cooldown=30)
        cb.record_failure()
        stats = cb.get_stats()
        assert stats['name'] == 'test_cb'
        assert stats['failures'] == 1
        assert stats['state'] == 'closed'


class TestCircuitBreakerDecorator:
    """with_circuit_breaker decorator."""

    def test_decorator_passes_through_on_closed(self):
        from core.circuit_breaker import CircuitBreaker, with_circuit_breaker
        cb = CircuitBreaker(threshold=5)

        @with_circuit_breaker(cb)
        def succeed():
            return 42

        assert succeed() == 42

    def test_decorator_records_failure(self):
        from core.circuit_breaker import CircuitBreaker, with_circuit_breaker
        cb = CircuitBreaker(threshold=5)

        @with_circuit_breaker(cb)
        def fail():
            raise ValueError('boom')

        with pytest.raises(ValueError):
            fail()
        assert cb.get_stats()['failures'] == 1

    def test_decorator_raises_open_error(self):
        from core.circuit_breaker import CircuitBreaker, with_circuit_breaker, CircuitBreakerOpenError
        cb = CircuitBreaker(threshold=1, cooldown=60)
        cb.record_failure()

        @with_circuit_breaker(cb)
        def blocked():
            return 'never'

        with pytest.raises(CircuitBreakerOpenError):
            blocked()

    def test_decorator_uses_fallback(self):
        from core.circuit_breaker import CircuitBreaker, with_circuit_breaker
        cb = CircuitBreaker(threshold=1, cooldown=60)
        cb.record_failure()

        @with_circuit_breaker(cb, fallback=lambda: 'fallback_val')
        def blocked():
            return 'never'

        assert blocked() == 'fallback_val'


# ===========================================================================
# port_registry tests
# ===========================================================================

class TestPortRegistry:
    """Port resolution and OS mode detection."""

    def test_app_mode_by_default(self):
        from core.port_registry import is_os_mode
        assert is_os_mode() is False

    def test_os_mode_via_env(self):
        from core.port_registry import is_os_mode
        os.environ['HART_OS_MODE'] = 'true'
        assert is_os_mode() is True

    def test_get_port_app_mode(self):
        from core.port_registry import get_port
        assert get_port('backend') == 6777

    def test_get_port_os_mode(self):
        from core.port_registry import get_port
        os.environ['HART_OS_MODE'] = '1'
        assert get_port('backend') == 677

    def test_get_port_explicit_override(self):
        from core.port_registry import get_port
        assert get_port('backend', override=9999) == 9999

    def test_get_port_env_override(self):
        from core.port_registry import get_port
        os.environ['HARTOS_BACKEND_PORT'] = '7777'
        assert get_port('backend') == 7777

    def test_get_port_env_override_beats_os_mode(self):
        from core.port_registry import get_port
        os.environ['HART_OS_MODE'] = 'true'
        os.environ['HARTOS_BACKEND_PORT'] = '1234'
        assert get_port('backend') == 1234

    def test_get_port_unknown_service(self):
        from core.port_registry import get_port
        assert get_port('nonexistent') == 0

    def test_get_all_ports_returns_dict(self):
        from core.port_registry import get_all_ports
        ports = get_all_ports()
        assert isinstance(ports, dict)
        assert 'backend' in ports

    def test_get_mode_label(self):
        from core.port_registry import get_mode_label
        assert get_mode_label() == 'APP'
        os.environ['HART_OS_MODE'] = 'true'
        from core import port_registry
        port_registry._os_mode_cached = None
        assert get_mode_label() == 'OS'

    def test_invalid_env_port_falls_back(self):
        from core.port_registry import get_port
        os.environ['HARTOS_BACKEND_PORT'] = 'not_a_number'
        assert get_port('backend') == 6777  # falls back to APP default


class TestLLMURLResolution:
    """get_local_llm_url resolution chain."""

    def test_default_uses_port_registry(self):
        from core.port_registry import get_local_llm_url
        url = get_local_llm_url()
        assert '8080' in url
        assert url.endswith('/v1')

    def test_canonical_env_var(self):
        from core.port_registry import get_local_llm_url
        os.environ['HEVOLVE_LOCAL_LLM_URL'] = 'http://127.0.0.1:9090/v1'
        url = get_local_llm_url()
        assert url == 'http://127.0.0.1:9090/v1'

    def test_custom_base_url(self):
        from core.port_registry import get_local_llm_url, invalidate_llm_url
        os.environ['CUSTOM_LLM_BASE_URL'] = 'http://myhost:1234'
        url = get_local_llm_url()
        assert 'myhost:1234' in url
        assert url.endswith('/v1')

    def test_deprecated_port_var(self):
        from core.port_registry import get_local_llm_url
        os.environ['LLAMA_CPP_PORT'] = '8888'
        url = get_local_llm_url()
        assert '8888' in url

    def test_is_local_llm_true(self):
        from core.port_registry import is_local_llm
        assert is_local_llm() is True  # default is localhost

    def test_is_local_llm_via_model_env(self):
        from core.port_registry import is_local_llm
        os.environ['HEVOLVE_LOCAL_LLM_MODEL'] = 'qwen2.5'
        assert is_local_llm() is True

    def test_invalidate_clears_cache(self):
        from core.port_registry import get_local_llm_url, invalidate_llm_url, _llm_url_cache
        url1 = get_local_llm_url()
        invalidate_llm_url()
        from core import port_registry
        assert port_registry._llm_url_cache == ''

    def test_set_local_llm_url(self):
        from core.port_registry import set_local_llm_url, get_local_llm_url, invalidate_llm_url
        set_local_llm_url('http://127.0.0.1:7777/v1')
        assert os.environ.get('HEVOLVE_LOCAL_LLM_URL') == 'http://127.0.0.1:7777/v1'

    def test_validate_rejects_bad_url(self):
        from core.port_registry import _validate_llm_url
        assert _validate_llm_url('') is False
        assert _validate_llm_url('ftp://bad') is False
        assert _validate_llm_url('http://') is False

    def test_validate_accepts_good_url(self):
        from core.port_registry import _validate_llm_url
        assert _validate_llm_url('http://127.0.0.1:8080/v1') is True
        assert _validate_llm_url('https://cloud.example.com/v1') is True


# ===========================================================================
# platform_paths tests
# ===========================================================================

class TestPlatformPaths:
    """Cross-platform path resolution."""

    def test_get_data_dir_returns_string(self):
        from core.platform_paths import get_data_dir
        d = get_data_dir()
        assert isinstance(d, str)
        assert len(d) > 0

    def test_override_via_nunba_data_dir(self):
        from core.platform_paths import get_data_dir
        os.environ['NUNBA_DATA_DIR'] = '/tmp/test_nunba'
        assert get_data_dir() == '/tmp/test_nunba'

    def test_override_via_hartos_data_dir(self):
        from core.platform_paths import get_data_dir
        os.environ['HARTOS_DATA_DIR'] = '/tmp/hartos_test'
        assert get_data_dir() == '/tmp/hartos_test'

    def test_nunba_override_takes_precedence(self):
        from core.platform_paths import get_data_dir
        os.environ['NUNBA_DATA_DIR'] = '/nunba'
        os.environ['HARTOS_DATA_DIR'] = '/hartos'
        assert get_data_dir() == '/nunba'

    def test_get_db_dir_is_subdir(self):
        from core.platform_paths import get_data_dir, get_db_dir
        assert get_db_dir() == os.path.join(get_data_dir(), 'data')

    def test_get_db_path_default(self):
        from core.platform_paths import get_db_path, get_db_dir
        assert get_db_path() == os.path.join(get_db_dir(), 'hevolve_database.db')

    def test_get_db_path_custom(self):
        from core.platform_paths import get_db_path, get_db_dir
        assert get_db_path('custom.db') == os.path.join(get_db_dir(), 'custom.db')

    def test_get_agent_data_dir(self):
        from core.platform_paths import get_agent_data_dir, get_db_dir
        assert get_agent_data_dir() == os.path.join(get_db_dir(), 'agent_data')

    def test_get_prompts_dir(self):
        from core.platform_paths import get_prompts_dir, get_db_dir
        assert get_prompts_dir() == os.path.join(get_db_dir(), 'prompts')

    def test_get_log_dir(self):
        from core.platform_paths import get_log_dir
        d = get_log_dir()
        assert 'log' in d.lower() or 'Log' in d

    def test_get_memory_graph_dir_no_session(self):
        from core.platform_paths import get_memory_graph_dir, get_db_dir
        assert get_memory_graph_dir() == os.path.join(get_db_dir(), 'memory_graph')

    def test_get_memory_graph_dir_with_session(self):
        from core.platform_paths import get_memory_graph_dir, get_db_dir
        d = get_memory_graph_dir('sess123')
        assert d == os.path.join(get_db_dir(), 'memory_graph', 'sess123')

    def test_reset_cache_allows_new_resolution(self):
        from core.platform_paths import get_data_dir, reset_cache
        os.environ['NUNBA_DATA_DIR'] = '/path1'
        d1 = get_data_dir()
        reset_cache()
        os.environ['NUNBA_DATA_DIR'] = '/path2'
        d2 = get_data_dir()
        assert d1 == '/path1'
        assert d2 == '/path2'

    @pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only')
    def test_windows_default_path(self):
        from core.platform_paths import get_data_dir
        d = get_data_dir()
        assert 'Documents' in d and 'Nunba' in d
