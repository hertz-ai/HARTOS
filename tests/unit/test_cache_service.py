"""
Tests for HART OS CacheService.

Covers:
- Basic operations: set/get, delete, has, None values
- TTL expiry and zero-TTL (no expiry)
- LRU eviction at max_size boundary
- Namespace-scoped clear
- Hit/miss stats and reset
- Lifecycle protocol: health, start, stop
- Disk persistence: set_persistent, get_persistent, disk unavailable
"""

import time
import unittest
from unittest.mock import patch, MagicMock

from core.platform.cache import (
    CacheService, get_cache, _MISSING,
    DEFAULT_MAX_SIZE, DEFAULT_TTL,
)


class TestCacheBasicOperations(unittest.TestCase):
    """Test core set/get/delete/has operations."""

    def setUp(self):
        # Disable disk for unit tests
        with patch.object(CacheService, '_init_disk'):
            self.cache = CacheService(max_size=100, default_ttl=60)
            self.cache._disk = None

    def test_set_and_get(self):
        self.cache.set('key1', 'value1')
        self.assertEqual(self.cache.get('key1'), 'value1')

    def test_get_missing_returns_default(self):
        self.assertIsNone(self.cache.get('nonexistent'))
        self.assertEqual(self.cache.get('nonexistent', 'fallback'), 'fallback')

    def test_delete_existing_key(self):
        self.cache.set('key1', 'value1')
        result = self.cache.delete('key1')
        self.assertTrue(result)
        self.assertIsNone(self.cache.get('key1'))

    def test_delete_missing_key(self):
        result = self.cache.delete('nonexistent')
        self.assertFalse(result)

    def test_has_key_exists(self):
        self.cache.set('key1', 'value1')
        self.assertTrue(self.cache.has('key1'))

    def test_has_key_missing(self):
        self.assertFalse(self.cache.has('nonexistent'))

    def test_set_none_value(self):
        """None is a valid cached value, distinct from a cache miss."""
        self.cache.set('key_none', None)
        # get() with sentinel default proves we got a hit, not a miss
        result = self.cache.get('key_none', _MISSING)
        self.assertIsNone(result)
        self.assertTrue(self.cache.has('key_none'))

    def test_overwrite_existing_key(self):
        self.cache.set('key1', 'old')
        self.cache.set('key1', 'new')
        self.assertEqual(self.cache.get('key1'), 'new')


class TestCacheTTL(unittest.TestCase):
    """Test TTL expiry behavior."""

    def setUp(self):
        with patch.object(CacheService, '_init_disk'):
            self.cache = CacheService(max_size=100, default_ttl=60)
            self.cache._disk = None

    def test_expired_key_returns_default(self):
        self.cache.set('short', 'data', ttl=0.01)
        time.sleep(0.05)
        self.assertIsNone(self.cache.get('short'))

    def test_has_expired_key(self):
        self.cache.set('short', 'data', ttl=0.01)
        time.sleep(0.05)
        self.assertFalse(self.cache.has('short'))

    def test_zero_ttl_never_expires(self):
        self.cache.set('forever', 'data', ttl=0)
        # No sleep — just verify it persists
        self.assertEqual(self.cache.get('forever'), 'data')
        self.assertTrue(self.cache.has('forever'))

    def test_custom_ttl(self):
        self.cache.set('medium', 'data', ttl=10)
        # Should still be alive
        self.assertEqual(self.cache.get('medium'), 'data')


class TestCacheLRU(unittest.TestCase):
    """Test LRU eviction at capacity."""

    def setUp(self):
        with patch.object(CacheService, '_init_disk'):
            self.cache = CacheService(max_size=3, default_ttl=60)
            self.cache._disk = None

    def test_eviction_when_over_max_size(self):
        self.cache.set('a', 1)
        self.cache.set('b', 2)
        self.cache.set('c', 3)
        # Cache is full — inserting 'd' should evict 'a' (oldest)
        self.cache.set('d', 4)
        self.assertFalse(self.cache.has('a'))
        self.assertTrue(self.cache.has('b'))
        self.assertTrue(self.cache.has('c'))
        self.assertTrue(self.cache.has('d'))

    def test_lru_order_updated_on_get(self):
        self.cache.set('a', 1)
        self.cache.set('b', 2)
        self.cache.set('c', 3)
        # Access 'a' — moves it to most recently used
        self.cache.get('a')
        # Insert 'd' — should evict 'b' (now the oldest)
        self.cache.set('d', 4)
        self.assertTrue(self.cache.has('a'))
        self.assertFalse(self.cache.has('b'))
        self.assertTrue(self.cache.has('c'))
        self.assertTrue(self.cache.has('d'))

    def test_lru_overwrite_does_not_grow(self):
        self.cache.set('a', 1)
        self.cache.set('b', 2)
        self.cache.set('c', 3)
        # Overwrite 'b' — size should remain 3
        self.cache.set('b', 99)
        self.cache.set('d', 4)
        # 'a' is oldest and should be evicted
        self.assertFalse(self.cache.has('a'))
        self.assertEqual(self.cache.get('b'), 99)


class TestCacheNamespace(unittest.TestCase):
    """Test namespace-scoped clear."""

    def setUp(self):
        with patch.object(CacheService, '_init_disk'):
            self.cache = CacheService(max_size=100, default_ttl=60)
            self.cache._disk = None

    def test_clear_namespace(self):
        self.cache.set('tts:voice:alba', 'v1')
        self.cache.set('tts:voice:nova', 'v2')
        self.cache.set('model:config:phi', 'cfg')
        count = self.cache.clear('tts')
        self.assertEqual(count, 2)
        self.assertFalse(self.cache.has('tts:voice:alba'))
        self.assertFalse(self.cache.has('tts:voice:nova'))
        # Other namespace untouched
        self.assertTrue(self.cache.has('model:config:phi'))

    def test_clear_all(self):
        self.cache.set('a', 1)
        self.cache.set('b', 2)
        self.cache.set('c', 3)
        count = self.cache.clear()
        self.assertEqual(count, 3)
        self.assertFalse(self.cache.has('a'))
        self.assertFalse(self.cache.has('b'))
        self.assertFalse(self.cache.has('c'))

    def test_clear_returns_count(self):
        self.cache.set('ns:a', 1)
        self.cache.set('ns:b', 2)
        self.cache.set('other:c', 3)
        self.assertEqual(self.cache.clear('ns'), 2)
        self.assertEqual(self.cache.clear('nonexistent'), 0)

    def test_clear_empty_namespace(self):
        count = self.cache.clear('empty')
        self.assertEqual(count, 0)


class TestCacheStats(unittest.TestCase):
    """Test hit/miss counters and stats reporting."""

    def setUp(self):
        with patch.object(CacheService, '_init_disk'):
            self.cache = CacheService(max_size=100, default_ttl=60)
            self.cache._disk = None

    def test_hit_count(self):
        self.cache.set('key', 'val')
        self.cache.get('key')
        self.cache.get('key')
        self.assertEqual(self.cache.hit_count, 2)

    def test_miss_count(self):
        self.cache.get('missing1')
        self.cache.get('missing2')
        self.assertEqual(self.cache.miss_count, 2)

    def test_hit_rate(self):
        self.cache.set('key', 'val')
        self.cache.get('key')       # hit
        self.cache.get('missing')   # miss
        self.assertAlmostEqual(self.cache.hit_rate(), 0.5)

    def test_hit_rate_no_gets(self):
        self.assertAlmostEqual(self.cache.hit_rate(), 0.0)

    def test_reset_stats(self):
        self.cache.set('key', 'val')
        self.cache.get('key')
        self.cache.get('missing')
        self.cache.reset_stats()
        self.assertEqual(self.cache.hit_count, 0)
        self.assertEqual(self.cache.miss_count, 0)
        self.assertAlmostEqual(self.cache.hit_rate(), 0.0)

    def test_stats_dict(self):
        self.cache.set('key', 'val')
        self.cache.get('key')
        s = self.cache.stats()
        self.assertEqual(s['size'], 1)
        self.assertEqual(s['max_size'], 100)
        self.assertEqual(s['hit_count'], 1)
        self.assertEqual(s['miss_count'], 0)
        self.assertAlmostEqual(s['hit_rate'], 1.0)
        self.assertIn('disk_available', s)


class TestCacheLifecycle(unittest.TestCase):
    """Test Lifecycle protocol integration."""

    def setUp(self):
        with patch.object(CacheService, '_init_disk'):
            self.cache = CacheService(max_size=100, default_ttl=60)
            self.cache._disk = None

    def test_health_started(self):
        self.cache.start()
        h = self.cache.health()
        self.assertEqual(h['status'], 'ok')
        self.assertIn('size', h)
        self.assertIn('hit_rate', h)

    def test_health_stopped(self):
        h = self.cache.health()
        self.assertEqual(h['status'], 'stopped')

    def test_stop_clears_data(self):
        self.cache.start()
        self.cache.set('key', 'val')
        self.assertTrue(self.cache.has('key'))
        self.cache.stop()
        self.assertFalse(self.cache.has('key'))
        self.assertEqual(self.cache.health()['status'], 'stopped')

    def test_start_stop_idempotent(self):
        self.cache.start()
        self.cache.start()  # no error on double start
        self.cache.stop()
        self.cache.stop()   # no error on double stop


class TestCacheDisk(unittest.TestCase):
    """Test disk persistence (mocked diskcache)."""

    def setUp(self):
        with patch.object(CacheService, '_init_disk'):
            self.cache = CacheService(max_size=100, default_ttl=60)
            self.cache._disk = None

    def test_set_persistent_writes_to_disk(self):
        mock_disk = MagicMock()
        self.cache._disk = mock_disk
        self.cache.set_persistent('key', 'val', ttl=120)
        # Memory should have the value
        self.assertEqual(self.cache.get('key'), 'val')
        # Disk should have been called
        mock_disk.set.assert_called_once_with('key', 'val', expire=120)

    def test_set_persistent_zero_ttl(self):
        mock_disk = MagicMock()
        self.cache._disk = mock_disk
        self.cache.set_persistent('key', 'val', ttl=0)
        # Zero TTL maps to no-expiry (None) on disk
        mock_disk.set.assert_called_once_with('key', 'val', expire=None)

    def test_get_persistent_disk_fallback(self):
        mock_disk = MagicMock()
        mock_disk.get.return_value = 'disk_value'
        self.cache._disk = mock_disk
        # Key not in memory — should fall back to disk
        result = self.cache.get_persistent('key', 'default')
        self.assertEqual(result, 'disk_value')
        mock_disk.get.assert_called_once_with('key', _MISSING)
        # Should be promoted to memory now
        self.assertEqual(self.cache.get('key'), 'disk_value')

    def test_get_persistent_memory_hit_skips_disk(self):
        mock_disk = MagicMock()
        self.cache._disk = mock_disk
        self.cache.set('key', 'mem_value')
        result = self.cache.get_persistent('key', 'default')
        self.assertEqual(result, 'mem_value')
        mock_disk.get.assert_not_called()

    def test_get_persistent_disk_miss(self):
        mock_disk = MagicMock()
        mock_disk.get.return_value = _MISSING
        self.cache._disk = mock_disk
        result = self.cache.get_persistent('missing', 'default')
        self.assertEqual(result, 'default')

    def test_disk_unavailable(self):
        """When diskcache is not installed, set_persistent falls back to memory-only."""
        self.cache._disk = None
        self.cache.set_persistent('key', 'val')
        self.assertEqual(self.cache.get('key'), 'val')

    def test_disk_write_error_silent(self):
        mock_disk = MagicMock()
        mock_disk.set.side_effect = OSError("disk full")
        self.cache._disk = mock_disk
        # Should not raise — logs and falls back to memory
        self.cache.set_persistent('key', 'val')
        self.assertEqual(self.cache.get('key'), 'val')

    def test_stop_closes_disk(self):
        mock_disk = MagicMock()
        self.cache._disk = mock_disk
        self.cache.start()
        self.cache.stop()
        mock_disk.close.assert_called_once()
        self.assertIsNone(self.cache._disk)


class TestCacheDefaults(unittest.TestCase):
    """Test default constants and sentinel."""

    def test_default_max_size(self):
        self.assertEqual(DEFAULT_MAX_SIZE, 4096)

    def test_default_ttl(self):
        self.assertEqual(DEFAULT_TTL, 300)

    def test_missing_sentinel_is_unique(self):
        self.assertIsNot(_MISSING, None)
        self.assertIsNot(_MISSING, False)
        self.assertIsNot(_MISSING, 0)


class TestGetCacheHelper(unittest.TestCase):
    """Test the module-level get_cache() convenience function."""

    def test_get_cache_returns_none_when_not_bootstrapped(self):
        """When no cache service is registered, get_cache returns None."""
        from core.platform.registry import reset_registry
        reset_registry()
        result = get_cache()
        self.assertIsNone(result)

    def test_get_cache_returns_service_when_registered(self):
        from core.platform.registry import get_registry, reset_registry
        reset_registry()
        registry = get_registry()
        with patch.object(CacheService, '_init_disk'):
            registry.register('cache', CacheService, singleton=True)
            cache = get_cache()
            self.assertIsInstance(cache, CacheService)
        reset_registry()


if __name__ == '__main__':
    unittest.main()
