"""Tests for core performance optimization modules."""
import pytest
import json
import os
import sys
import time
import asyncio
import threading
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))


class TestConfigCache:
    """Test cached configuration loading."""

    def test_get_config_returns_dict(self):
        from core.config_cache import get_config
        config = get_config()
        assert isinstance(config, dict)

    def test_get_config_cached_singleton(self):
        from core.config_cache import get_config
        config1 = get_config()
        config2 = get_config()
        assert config1 is config2  # Same object reference = cached

    def test_get_secret_from_env(self):
        from core.config_cache import get_secret
        os.environ['TEST_SECRET_KEY_12345'] = 'test_value'
        try:
            assert get_secret('TEST_SECRET_KEY_12345') == 'test_value'
        finally:
            del os.environ['TEST_SECRET_KEY_12345']

    def test_get_secret_from_config(self):
        from core.config_cache import get_config, get_secret
        config = get_config()
        if 'OPENAI_API_KEY' in config:
            assert get_secret('OPENAI_API_KEY') != ''

    def test_get_secret_default(self):
        from core.config_cache import get_secret
        assert get_secret('NONEXISTENT_KEY_XYZ', 'default') == 'default'

    def test_reload_config(self):
        from core.config_cache import reload_config
        config = reload_config()
        assert isinstance(config, dict)


class TestHTTPPool:
    """Test connection-pooled HTTP sessions."""

    def test_session_singleton(self):
        from core.http_pool import get_http_session
        s1 = get_http_session()
        s2 = get_http_session()
        assert s1 is s2

    def test_session_has_adapters(self):
        from core.http_pool import get_http_session
        session = get_http_session()
        assert 'http://' in session.adapters
        assert 'https://' in session.adapters

    def test_pooled_get_function_exists(self):
        from core.http_pool import pooled_get
        assert callable(pooled_get)

    def test_pooled_post_function_exists(self):
        from core.http_pool import pooled_post
        assert callable(pooled_post)

    def test_pooled_request_function_exists(self):
        from core.http_pool import pooled_request
        assert callable(pooled_request)


class TestEventLoop:
    """Test singleton event loop management."""

    def test_get_or_create_returns_loop(self):
        from core.event_loop import get_or_create_event_loop
        loop = get_or_create_event_loop()
        assert isinstance(loop, asyncio.AbstractEventLoop)
        assert not loop.is_closed()

    def test_loop_reused(self):
        from core.event_loop import get_or_create_event_loop
        loop1 = get_or_create_event_loop()
        loop2 = get_or_create_event_loop()
        assert loop1 is loop2

    def test_run_async(self):
        from core.event_loop import run_async

        async def add(a, b):
            return a + b

        result = run_async(add(3, 4))
        assert result == 7

    def test_thread_isolation(self):
        from core.event_loop import get_or_create_event_loop
        main_loop = get_or_create_event_loop()
        thread_loop = [None]

        def get_thread_loop():
            thread_loop[0] = get_or_create_event_loop()

        t = threading.Thread(target=get_thread_loop)
        t.start()
        t.join()

        # Different threads get different loops
        assert thread_loop[0] is not main_loop


class TestTTLCache:
    """Test TTL-based session cache."""

    def test_basic_set_get(self):
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, name='test')
        cache['key1'] = 'value1'
        assert cache['key1'] == 'value1'

    def test_contains(self):
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, name='test')
        cache['key1'] = 'value1'
        assert 'key1' in cache
        assert 'key2' not in cache

    def test_delete(self):
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, name='test')
        cache['key1'] = 'value1'
        del cache['key1']
        assert 'key1' not in cache

    def test_get_default(self):
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, name='test')
        assert cache.get('missing', 'default') == 'default'

    def test_pop(self):
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, name='test')
        cache['key1'] = 'value1'
        val = cache.pop('key1')
        assert val == 'value1'
        assert 'key1' not in cache

    def test_ttl_expiration(self):
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=0.1, name='test')  # 100ms TTL
        cache['key1'] = 'value1'
        assert 'key1' in cache
        time.sleep(0.2)
        assert 'key1' not in cache

    def test_max_size_eviction(self):
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, max_size=3, name='test')
        cache['a'] = 1
        cache['b'] = 2
        cache['c'] = 3
        cache['d'] = 4  # Should evict 'a'
        assert 'a' not in cache
        assert 'd' in cache

    def test_len(self):
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, name='test')
        cache['a'] = 1
        cache['b'] = 2
        assert len(cache) == 2

    def test_keys_values_items(self):
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, name='test')
        cache['a'] = 1
        cache['b'] = 2
        assert set(cache.keys()) == {'a', 'b'}
        assert set(cache.values()) == {1, 2}
        assert set(cache.items()) == {('a', 1), ('b', 2)}

    def test_clear(self):
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, name='test')
        cache['a'] = 1
        cache['b'] = 2
        cache.clear()
        assert len(cache) == 0

    def test_stats(self):
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, max_size=100, name='test_stats')
        cache['a'] = 1
        stats = cache.stats()
        assert stats['name'] == 'test_stats'
        assert stats['total'] == 1
        assert stats['active'] == 1
        assert stats['max_size'] == 100

    def test_thread_safety(self):
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, max_size=10000, name='test')
        errors = []

        def writer(start):
            try:
                for i in range(100):
                    cache[f'key_{start}_{i}'] = i
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


class TestFileCache:
    """Test cached JSON file I/O."""

    def test_cached_json_load(self, tmp_path):
        from core.file_cache import cached_json_load, invalidate_file_cache
        invalidate_file_cache()

        test_file = tmp_path / "test.json"
        test_file.write_text('{"key": "value"}')

        data = cached_json_load(str(test_file))
        assert data == {"key": "value"}

    def test_cache_hit(self, tmp_path):
        from core.file_cache import cached_json_load, invalidate_file_cache
        invalidate_file_cache()

        test_file = tmp_path / "test.json"
        test_file.write_text('{"key": "value"}')

        data1 = cached_json_load(str(test_file))
        data2 = cached_json_load(str(test_file))
        assert data1 == data2

    def test_cache_returns_copy(self, tmp_path):
        from core.file_cache import cached_json_load, invalidate_file_cache
        invalidate_file_cache()

        test_file = tmp_path / "test.json"
        test_file.write_text('{"key": "value"}')

        data1 = cached_json_load(str(test_file))
        data1['key'] = 'mutated'
        data2 = cached_json_load(str(test_file))
        assert data2['key'] == 'value'  # Original cached data not mutated

    def test_cache_invalidation(self, tmp_path):
        from core.file_cache import cached_json_load, invalidate_file_cache
        invalidate_file_cache()

        test_file = tmp_path / "test.json"
        test_file.write_text('{"version": 1}')
        data1 = cached_json_load(str(test_file))

        invalidate_file_cache(str(test_file))
        test_file.write_text('{"version": 2}')
        data2 = cached_json_load(str(test_file))
        assert data2['version'] == 2

    def test_cached_json_save(self, tmp_path):
        from core.file_cache import cached_json_save, cached_json_load, invalidate_file_cache
        invalidate_file_cache()

        test_file = str(tmp_path / "output.json")
        cached_json_save(test_file, {"saved": True})
        data = cached_json_load(test_file)
        assert data == {"saved": True}

    def test_file_not_found(self, tmp_path):
        from core.file_cache import cached_json_load, invalidate_file_cache
        invalidate_file_cache()

        with pytest.raises(FileNotFoundError):
            cached_json_load(str(tmp_path / "nonexistent.json"))

    def test_cache_stats(self, tmp_path):
        from core.file_cache import cached_json_load, cache_stats, invalidate_file_cache
        invalidate_file_cache()

        test_file = tmp_path / "stats_test.json"
        test_file.write_text('{"data": [1,2,3]}')
        cached_json_load(str(test_file))

        stats = cache_stats()
        assert stats['cached_files'] >= 1
