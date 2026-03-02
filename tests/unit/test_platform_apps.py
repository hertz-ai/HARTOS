"""
Tests for core.platform.app_manifest and core.platform.app_registry.

Covers: AppManifest creation, serialization, from_panel_manifest,
AppRegistry register/unregister/search/list_by_type/list_by_group,
backward compat with shell_manifest.py, events, health.
"""

import unittest

from core.platform.app_manifest import AppManifest, AppType
from core.platform.app_registry import AppRegistry


# ─── Test Data ────────────────────────────────────────────────

FEED_PANEL = AppManifest(
    id='feed', name='Feed', version='1.0.0',
    type=AppType.NUNBA_PANEL.value, icon='rss_feed',
    entry={'route': '/social'}, group='Discover',
    default_size=(800, 600), tags=['social', 'posts'],
)

CODING_PANEL = AppManifest(
    id='coding', name='Coding Agent', version='1.0.0',
    type=AppType.NUNBA_PANEL.value, icon='code',
    entry={'route': '/social/coding'}, group='Create',
    default_size=(900, 700), tags=['code', 'agent'],
)

HARDWARE_MONITOR = AppManifest(
    id='hardware_monitor', name='Hardware Monitor', version='1.0.0',
    type=AppType.SYSTEM_PANEL.value, icon='memory',
    entry={'api': '/api/shell/system/metrics'}, group='System',
    permissions=['system_read'],
)

RUSTDESK = AppManifest(
    id='rustdesk', name='RustDesk', version='1.3.0',
    type=AppType.DESKTOP_APP.value, icon='desktop_windows',
    entry={'exec': 'rustdesk', 'bridge': 'rustdesk_bridge'},
    group='Remote', platforms=['linux', 'windows', 'macos'],
    permissions=['network', 'display', 'input'],
    description='Open-source remote desktop',
    tags=['remote', 'vnc', 'desktop'],
)

LLAMA_CPP = AppManifest(
    id='llama_cpp', name='LLM Engine', version='1.0.0',
    type=AppType.SERVICE.value, icon='psychology',
    entry={'http': 'http://localhost:8080'},
    auto_start=True, permissions=['compute'],
)


class TestAppManifest(unittest.TestCase):
    """AppManifest dataclass."""

    def test_creation(self):
        m = FEED_PANEL
        self.assertEqual(m.id, 'feed')
        self.assertEqual(m.type, 'nunba_panel')
        self.assertEqual(m.default_size, (800, 600))

    def test_to_dict(self):
        d = FEED_PANEL.to_dict()
        self.assertEqual(d['id'], 'feed')
        self.assertEqual(d['type'], 'nunba_panel')
        self.assertIsInstance(d['default_size'], list)

    def test_from_dict(self):
        d = RUSTDESK.to_dict()
        m = AppManifest.from_dict(d)
        self.assertEqual(m.id, 'rustdesk')
        self.assertEqual(m.type, 'desktop_app')
        self.assertEqual(m.default_size, (800, 600))

    def test_roundtrip(self):
        d = RUSTDESK.to_dict()
        m = AppManifest.from_dict(d)
        d2 = m.to_dict()
        self.assertEqual(d, d2)

    def test_from_panel_manifest(self):
        """Convert shell_manifest.py format."""
        panel = {
            'title': 'Feed', 'icon': 'rss_feed',
            'route': '/social', 'group': 'Discover',
            'default_size': [800, 600],
        }
        m = AppManifest.from_panel_manifest('feed', panel)
        self.assertEqual(m.id, 'feed')
        self.assertEqual(m.name, 'Feed')
        self.assertEqual(m.type, 'nunba_panel')
        self.assertEqual(m.entry, {'route': '/social'})
        self.assertEqual(m.group, 'Discover')

    def test_from_system_panel(self):
        panel = {
            'title': 'Hardware Monitor', 'icon': 'memory',
            'loader': 'loadHardwareMonitor', 'group': 'System',
            'default_size': [700, 500],
            'apis': ['/api/shell/system/metrics'],
        }
        m = AppManifest.from_system_panel('hardware_monitor', panel)
        self.assertEqual(m.type, 'system_panel')
        self.assertEqual(m.entry, {'loader': 'loadHardwareMonitor'})
        self.assertIn('/api/shell/system/metrics', m.apis)

    def test_matches_search_by_name(self):
        self.assertTrue(RUSTDESK.matches_search('rust'))
        self.assertTrue(RUSTDESK.matches_search('RustDesk'))
        self.assertFalse(RUSTDESK.matches_search('zzz_nomatch'))

    def test_matches_search_by_tag(self):
        self.assertTrue(RUSTDESK.matches_search('vnc'))
        self.assertTrue(RUSTDESK.matches_search('remote'))

    def test_matches_search_by_description(self):
        self.assertTrue(RUSTDESK.matches_search('open-source'))

    def test_matches_search_by_group(self):
        self.assertTrue(RUSTDESK.matches_search('remote'))


class TestAppType(unittest.TestCase):
    """AppType enum."""

    def test_all_types_exist(self):
        types = [t.value for t in AppType]
        self.assertIn('nunba_panel', types)
        self.assertIn('system_panel', types)
        self.assertIn('desktop_app', types)
        self.assertIn('service', types)
        self.assertIn('agent', types)
        self.assertIn('mcp_server', types)
        self.assertIn('channel', types)
        self.assertIn('extension', types)


class TestAppRegistryBasic(unittest.TestCase):
    """Register, get, unregister."""

    def setUp(self):
        self.reg = AppRegistry()

    def test_register_and_get(self):
        self.reg.register(FEED_PANEL)
        m = self.reg.get('feed')
        self.assertIsNotNone(m)
        self.assertEqual(m.name, 'Feed')

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.reg.get('nonexistent'))

    def test_duplicate_raises(self):
        self.reg.register(FEED_PANEL)
        with self.assertRaises(ValueError):
            self.reg.register(FEED_PANEL)

    def test_unregister(self):
        self.reg.register(FEED_PANEL)
        self.reg.unregister('feed')
        self.assertIsNone(self.reg.get('feed'))

    def test_unregister_missing_raises(self):
        with self.assertRaises(KeyError):
            self.reg.unregister('ghost')

    def test_count(self):
        self.assertEqual(self.reg.count(), 0)
        self.reg.register(FEED_PANEL)
        self.reg.register(RUSTDESK)
        self.assertEqual(self.reg.count(), 2)

    def test_list_all(self):
        self.reg.register(FEED_PANEL)
        self.reg.register(RUSTDESK)
        all_apps = self.reg.list_all()
        self.assertEqual(len(all_apps), 2)
        ids = {m.id for m in all_apps}
        self.assertEqual(ids, {'feed', 'rustdesk'})


class TestAppRegistryFiltering(unittest.TestCase):
    """list_by_type, list_by_group, groups."""

    def setUp(self):
        self.reg = AppRegistry()
        for m in [FEED_PANEL, CODING_PANEL, HARDWARE_MONITOR, RUSTDESK, LLAMA_CPP]:
            self.reg.register(m)

    def test_list_by_type_nunba(self):
        panels = self.reg.list_by_type('nunba_panel')
        self.assertEqual(len(panels), 2)
        ids = {m.id for m in panels}
        self.assertEqual(ids, {'feed', 'coding'})

    def test_list_by_type_desktop(self):
        apps = self.reg.list_by_type('desktop_app')
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0].id, 'rustdesk')

    def test_list_by_group(self):
        discover = self.reg.list_by_group('Discover')
        self.assertEqual(len(discover), 1)
        self.assertEqual(discover[0].id, 'feed')

    def test_list_by_group_case_insensitive(self):
        system = self.reg.list_by_group('system')
        self.assertEqual(len(system), 1)

    def test_groups(self):
        groups = self.reg.groups()
        self.assertIn('Create', groups)
        self.assertIn('Discover', groups)
        self.assertIn('Remote', groups)
        self.assertIn('System', groups)


class TestAppRegistrySearch(unittest.TestCase):
    """Search functionality."""

    def setUp(self):
        self.reg = AppRegistry()
        for m in [FEED_PANEL, CODING_PANEL, HARDWARE_MONITOR, RUSTDESK, LLAMA_CPP]:
            self.reg.register(m)

    def test_search_by_name(self):
        results = self.reg.search('RustDesk')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, 'rustdesk')

    def test_search_by_tag(self):
        results = self.reg.search('code')
        ids = {m.id for m in results}
        self.assertIn('coding', ids)

    def test_search_empty_returns_all(self):
        results = self.reg.search('')
        self.assertEqual(len(results), 5)

    def test_search_no_match(self):
        results = self.reg.search('xyznonexistent')
        self.assertEqual(len(results), 0)

    def test_search_exact_id_first(self):
        results = self.reg.search('feed')
        self.assertEqual(results[0].id, 'feed')


class TestShellManifestCompat(unittest.TestCase):
    """Backward compatibility with shell_manifest.py."""

    def test_to_shell_manifest(self):
        reg = AppRegistry()
        reg.register(FEED_PANEL)
        reg.register(RUSTDESK)  # desktop_app — excluded from shell manifest
        sm = reg.to_shell_manifest()
        self.assertIn('feed', sm)
        self.assertNotIn('rustdesk', sm)  # not a panel
        self.assertEqual(sm['feed']['title'], 'Feed')
        self.assertEqual(sm['feed']['route'], '/social')

    def test_load_panel_manifest(self):
        reg = AppRegistry()
        panels = {
            'feed': {'title': 'Feed', 'icon': 'rss_feed',
                     'route': '/social', 'group': 'Discover',
                     'default_size': [800, 600]},
            'search': {'title': 'Search', 'icon': 'search',
                       'route': '/social/search', 'group': 'Discover',
                       'default_size': [600, 500]},
        }
        count = reg.load_panel_manifest(panels)
        self.assertEqual(count, 2)
        self.assertEqual(reg.count(), 2)
        m = reg.get('feed')
        self.assertEqual(m.type, 'nunba_panel')

    def test_load_system_panels(self):
        reg = AppRegistry()
        panels = {
            'hardware_monitor': {
                'title': 'Hardware Monitor', 'icon': 'memory',
                'loader': 'loadHardwareMonitor', 'group': 'System',
                'default_size': [700, 500],
                'apis': ['/api/shell/system/metrics'],
            },
        }
        count = reg.load_system_panels(panels)
        self.assertEqual(count, 1)
        m = reg.get('hardware_monitor')
        self.assertEqual(m.type, 'system_panel')


class TestAppRegistryEvents(unittest.TestCase):
    """Event emission on register/unregister."""

    def test_emits_registered_event(self):
        events = []
        reg = AppRegistry(event_emitter=lambda t, d: events.append((t, d)))
        reg.register(FEED_PANEL)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], 'app.registered')
        self.assertEqual(events[0][1]['app_id'], 'feed')

    def test_emits_unregistered_event(self):
        events = []
        reg = AppRegistry(event_emitter=lambda t, d: events.append((t, d)))
        reg.register(FEED_PANEL)
        reg.unregister('feed')
        self.assertEqual(events[1][0], 'app.unregistered')


class TestAppRegistryHealth(unittest.TestCase):
    """Health report."""

    def test_health(self):
        reg = AppRegistry()
        reg.register(FEED_PANEL)
        reg.register(RUSTDESK)
        h = reg.health()
        self.assertEqual(h['status'], 'ok')
        self.assertEqual(h['total_apps'], 2)
        self.assertIn('nunba_panel', h['types'])
        self.assertIn('desktop_app', h['types'])


if __name__ == '__main__':
    unittest.main()
