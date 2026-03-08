"""Tests for core.platform.agent_environment — Agent Environments (WS2)."""

import sys
import os
import threading
import unittest
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.platform.agent_environment import (
    EnvironmentConfig,
    AgentEnvironment,
    EnvironmentManager,
)


# ─── EnvironmentConfig ──────────────────────────────────────────────

class TestEnvironmentConfig(unittest.TestCase):

    def test_defaults(self):
        cfg = EnvironmentConfig()
        self.assertEqual(cfg.working_dir, '')
        self.assertEqual(cfg.allowed_tools, [])
        self.assertEqual(cfg.denied_tools, [])
        self.assertEqual(cfg.model_policy, 'local_preferred')
        self.assertEqual(cfg.max_cost_spark, 0.0)
        self.assertEqual(cfg.ai_capabilities, [])
        self.assertEqual(cfg.event_scope, '')
        self.assertEqual(cfg.timeout_seconds, 0.0)
        self.assertEqual(cfg.metadata, {})

    def test_custom_values(self):
        cfg = EnvironmentConfig(
            working_dir='/tmp/agent',
            allowed_tools=['web_search', 'read_file'],
            model_policy='local_only',
            max_cost_spark=50.0,
            timeout_seconds=300.0,
            metadata={'purpose': 'research'},
        )
        self.assertEqual(cfg.working_dir, '/tmp/agent')
        self.assertEqual(len(cfg.allowed_tools), 2)
        self.assertEqual(cfg.model_policy, 'local_only')
        self.assertEqual(cfg.max_cost_spark, 50.0)

    def test_to_dict(self):
        cfg = EnvironmentConfig(model_policy='any')
        d = cfg.to_dict()
        self.assertEqual(d['model_policy'], 'any')
        self.assertIn('allowed_tools', d)
        self.assertIn('metadata', d)

    def test_from_dict(self):
        cfg = EnvironmentConfig.from_dict({
            'model_policy': 'local_only',
            'max_cost_spark': 100.0,
            'unknown_field': 'ignored',
        })
        self.assertEqual(cfg.model_policy, 'local_only')
        self.assertEqual(cfg.max_cost_spark, 100.0)

    def test_roundtrip(self):
        cfg = EnvironmentConfig(
            allowed_tools=['a', 'b'],
            denied_tools=['c'],
            model_policy='any',
            max_cost_spark=25.0,
        )
        restored = EnvironmentConfig.from_dict(cfg.to_dict())
        self.assertEqual(restored.allowed_tools, cfg.allowed_tools)
        self.assertEqual(restored.denied_tools, cfg.denied_tools)
        self.assertEqual(restored.model_policy, cfg.model_policy)
        self.assertEqual(restored.max_cost_spark, cfg.max_cost_spark)


# ─── AgentEnvironment ─────────────────────────────────────────────

class TestAgentEnvironment(unittest.TestCase):

    def _make_env(self, **config_kwargs):
        cfg = EnvironmentConfig(**config_kwargs)
        return AgentEnvironment(env_id='test-123', name='Test', config=cfg)

    def test_creation(self):
        env = self._make_env()
        self.assertEqual(env.env_id, 'test-123')
        self.assertEqual(env.name, 'Test')
        self.assertTrue(env.active)
        self.assertGreater(env.created_at, 0)

    def test_deactivate(self):
        env = self._make_env()
        self.assertTrue(env.active)
        env.deactivate()
        self.assertFalse(env.active)

    def test_to_dict(self):
        env = self._make_env(model_policy='local_only')
        d = env.to_dict()
        self.assertEqual(d['env_id'], 'test-123')
        self.assertEqual(d['name'], 'Test')
        self.assertTrue(d['active'])
        self.assertEqual(d['config']['model_policy'], 'local_only')
        self.assertEqual(d['cost_spent'], 0.0)


# ─── Tool Checking ───────────────────────────────────────────────

class TestToolChecking(unittest.TestCase):

    def test_no_constraints_allows_all(self):
        cfg = EnvironmentConfig()
        env = AgentEnvironment(env_id='t', name='t', config=cfg)
        self.assertTrue(env.check_tool('anything'))
        self.assertTrue(env.check_tool('web_search'))

    def test_allowed_tools_whitelist(self):
        cfg = EnvironmentConfig(allowed_tools=['web_search', 'read_file'])
        env = AgentEnvironment(env_id='t', name='t', config=cfg)
        self.assertTrue(env.check_tool('web_search'))
        self.assertTrue(env.check_tool('read_file'))
        self.assertFalse(env.check_tool('write_file'))
        self.assertFalse(env.check_tool('delete_file'))

    def test_denied_tools_blacklist(self):
        cfg = EnvironmentConfig(denied_tools=['delete_file', 'rm_rf'])
        env = AgentEnvironment(env_id='t', name='t', config=cfg)
        self.assertTrue(env.check_tool('web_search'))
        self.assertTrue(env.check_tool('write_file'))
        self.assertFalse(env.check_tool('delete_file'))
        self.assertFalse(env.check_tool('rm_rf'))

    def test_denied_takes_precedence_over_allowed(self):
        cfg = EnvironmentConfig(
            allowed_tools=['web_search', 'write_file'],
            denied_tools=['write_file'],
        )
        env = AgentEnvironment(env_id='t', name='t', config=cfg)
        self.assertTrue(env.check_tool('web_search'))
        self.assertFalse(env.check_tool('write_file'))


# ─── Budget ──────────────────────────────────────────────────────

class TestBudgetEnforcement(unittest.TestCase):

    def test_no_budget_always_passes(self):
        cfg = EnvironmentConfig(max_cost_spark=0.0)
        env = AgentEnvironment(env_id='t', name='t', config=cfg)
        self.assertTrue(env.check_budget(1000.0))

    def test_within_budget(self):
        cfg = EnvironmentConfig(max_cost_spark=50.0)
        env = AgentEnvironment(env_id='t', name='t', config=cfg)
        self.assertTrue(env.check_budget(30.0))
        env.record_cost(30.0)
        self.assertTrue(env.check_budget(20.0))

    def test_exceeds_budget(self):
        cfg = EnvironmentConfig(max_cost_spark=50.0)
        env = AgentEnvironment(env_id='t', name='t', config=cfg)
        env.record_cost(40.0)
        self.assertFalse(env.check_budget(20.0))

    def test_exact_budget_boundary(self):
        cfg = EnvironmentConfig(max_cost_spark=50.0)
        env = AgentEnvironment(env_id='t', name='t', config=cfg)
        self.assertTrue(env.check_budget(50.0))
        env.record_cost(50.0)
        self.assertFalse(env.check_budget(0.01))


# ─── Infer Delegation ───────────────────────────────────────────

class TestInferDelegation(unittest.TestCase):

    def test_inactive_env_returns_error(self):
        cfg = EnvironmentConfig()
        env = AgentEnvironment(env_id='t', name='t', config=cfg)
        env.deactivate()
        result = env.infer('hello')
        self.assertIn('error', result)
        self.assertIn('inactive', result['error'])

    @patch('core.platform.agent_environment.get_model_bus_service', create=True)
    def test_infer_delegates_to_model_bus(self, mock_get_bus):
        # Patch at the right level
        mock_bus = Mock()
        mock_bus.infer.return_value = {'response': 'hello'}
        mock_get_bus.return_value = mock_bus

        # We need to patch the import inside the method
        cfg = EnvironmentConfig(model_policy='local_only')
        env = AgentEnvironment(env_id='t', name='t', config=cfg)

        with patch.dict('sys.modules', {
            'integrations.agent_engine.model_bus_service': Mock(
                get_model_bus_service=Mock(return_value=mock_bus)
            ),
        }):
            result = env.infer('summarize this')
            self.assertEqual(result['response'], 'hello')

    def test_infer_handles_import_error(self):
        cfg = EnvironmentConfig()
        env = AgentEnvironment(env_id='t', name='t', config=cfg)
        # With no model_bus_service installed, should return error
        with patch.dict('sys.modules', {
            'integrations.agent_engine.model_bus_service': None,
        }):
            result = env.infer('hello')
            self.assertIn('error', result)


# ─── Scoped Events ──────────────────────────────────────────────

class TestScopedEvents(unittest.TestCase):

    @patch('core.platform.events.emit_event')
    def test_emit_scoped(self, mock_emit):
        cfg = EnvironmentConfig(event_scope='env.research')
        env = AgentEnvironment(env_id='t', name='t', config=cfg)
        env.emit('task.done', {'result': 'ok'})
        mock_emit.assert_called_once()
        topic = mock_emit.call_args[0][0]
        self.assertEqual(topic, 'env.research.task.done')
        data = mock_emit.call_args[0][1]
        self.assertEqual(data['result'], 'ok')
        self.assertEqual(data['_env_id'], 't')

    @patch('core.platform.events.emit_event')
    def test_emit_uses_env_id_as_default_scope(self, mock_emit):
        cfg = EnvironmentConfig()  # no event_scope
        env = AgentEnvironment(env_id='abc-123', name='t', config=cfg)
        env.emit('test.event')
        topic = mock_emit.call_args[0][0]
        self.assertEqual(topic, 'env.abc-123.test.event')

    @patch('core.platform.events.emit_event', side_effect=Exception('bus down'))
    def test_emit_swallows_errors(self, mock_emit):
        cfg = EnvironmentConfig()
        env = AgentEnvironment(env_id='t', name='t', config=cfg)
        # Should not raise
        env.emit('test.event')


# ─── EnvironmentManager CRUD ────────────────────────────────────

class TestEnvironmentManager(unittest.TestCase):

    def test_create_basic(self):
        mgr = EnvironmentManager()
        env = mgr.create('research')
        self.assertIsNotNone(env)
        self.assertEqual(env.name, 'research')
        self.assertTrue(env.active)
        self.assertIn('research', env.env_id)

    def test_create_with_config(self):
        cfg = EnvironmentConfig(model_policy='local_only', max_cost_spark=100.0)
        mgr = EnvironmentManager()
        env = mgr.create('coding', config=cfg)
        self.assertEqual(env.config.model_policy, 'local_only')
        self.assertEqual(env.config.max_cost_spark, 100.0)

    def test_create_with_kwargs(self):
        mgr = EnvironmentManager()
        env = mgr.create('task', model_policy='any',
                          allowed_tools=['web_search'])
        self.assertEqual(env.config.model_policy, 'any')
        self.assertEqual(env.config.allowed_tools, ['web_search'])

    def test_get_existing(self):
        mgr = EnvironmentManager()
        env = mgr.create('test')
        found = mgr.get(env.env_id)
        self.assertIs(found, env)

    def test_get_nonexistent(self):
        mgr = EnvironmentManager()
        self.assertIsNone(mgr.get('does-not-exist'))

    def test_destroy(self):
        mgr = EnvironmentManager()
        env = mgr.create('temp')
        env_id = env.env_id
        self.assertTrue(mgr.destroy(env_id))
        self.assertIsNone(mgr.get(env_id))
        self.assertFalse(env.active)

    def test_destroy_nonexistent(self):
        mgr = EnvironmentManager()
        self.assertFalse(mgr.destroy('nope'))

    def test_list_environments(self):
        mgr = EnvironmentManager()
        mgr.create('a')
        mgr.create('b')
        envs = mgr.list_environments()
        self.assertEqual(len(envs), 2)
        names = {e['name'] for e in envs}
        self.assertEqual(names, {'a', 'b'})

    def test_count(self):
        mgr = EnvironmentManager()
        self.assertEqual(mgr.count(), 0)
        mgr.create('x')
        self.assertEqual(mgr.count(), 1)
        mgr.create('y')
        self.assertEqual(mgr.count(), 2)


# ─── Event Emission ─────────────────────────────────────────────

class TestManagerEvents(unittest.TestCase):

    def test_create_emits_event(self):
        emitter = Mock()
        mgr = EnvironmentManager(event_emitter=emitter)
        mgr.create('research')
        emitter.assert_called_once()
        topic = emitter.call_args[0][0]
        self.assertEqual(topic, 'environment.created')
        data = emitter.call_args[0][1]
        self.assertEqual(data['name'], 'research')

    def test_destroy_emits_event(self):
        emitter = Mock()
        mgr = EnvironmentManager(event_emitter=emitter)
        env = mgr.create('temp')
        emitter.reset_mock()
        mgr.destroy(env.env_id)
        emitter.assert_called_once()
        topic = emitter.call_args[0][0]
        self.assertEqual(topic, 'environment.destroyed')


# ─── Health ──────────────────────────────────────────────────────

class TestManagerHealth(unittest.TestCase):

    def test_health_empty(self):
        mgr = EnvironmentManager()
        h = mgr.health()
        self.assertEqual(h['status'], 'ok')
        self.assertEqual(h['total_environments'], 0)
        self.assertEqual(h['active'], 0)

    def test_health_with_environments(self):
        mgr = EnvironmentManager()
        env1 = mgr.create('a')
        mgr.create('b')
        h = mgr.health()
        self.assertEqual(h['total_environments'], 2)
        self.assertEqual(h['active'], 2)
        # Deactivate one (but keep in manager)
        env1.deactivate()
        h = mgr.health()
        self.assertEqual(h['active'], 1)


# ─── Thread Safety ──────────────────────────────────────────────

class TestThreadSafety(unittest.TestCase):

    def test_concurrent_create(self):
        mgr = EnvironmentManager()
        errors = []

        def create_env(name):
            try:
                mgr.create(name)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=create_env, args=(f'env-{i}',))
                   for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        self.assertEqual(mgr.count(), 20)


# ─── Bootstrap Registration ─────────────────────────────────────

class TestBootstrapRegistration(unittest.TestCase):

    def test_bootstrap_registers_environments(self):
        from core.platform.registry import reset_registry
        reset_registry()
        from core.platform.bootstrap import bootstrap_platform
        registry = bootstrap_platform()
        self.assertTrue(registry.has('environments'))
        mgr = registry.get('environments')
        self.assertIsInstance(mgr, EnvironmentManager)
        reset_registry()

    def test_manager_health_after_bootstrap(self):
        from core.platform.registry import reset_registry
        reset_registry()
        from core.platform.bootstrap import bootstrap_platform
        registry = bootstrap_platform()
        mgr = registry.get('environments')
        h = mgr.health()
        self.assertEqual(h['status'], 'ok')
        reset_registry()


# ─── Edge Cases ─────────────────────────────────────────────────

class TestEdgeCases(unittest.TestCase):

    def test_empty_name(self):
        mgr = EnvironmentManager()
        env = mgr.create('')
        self.assertEqual(env.name, '')
        self.assertTrue(env.active)

    def test_unicode_name(self):
        mgr = EnvironmentManager()
        env = mgr.create('研究タスク')
        self.assertEqual(env.name, '研究タスク')

    def test_destroy_already_inactive(self):
        mgr = EnvironmentManager()
        env = mgr.create('temp')
        env.deactivate()
        # Destroy should still work
        self.assertTrue(mgr.destroy(env.env_id))
        self.assertFalse(env.active)

    def test_config_with_ai_capabilities(self):
        cfg = EnvironmentConfig(
            ai_capabilities=[{'type': 'llm', 'required': True}],
        )
        env = AgentEnvironment(env_id='t', name='t', config=cfg)
        d = env.to_dict()
        self.assertEqual(len(d['config']['ai_capabilities']), 1)


if __name__ == '__main__':
    unittest.main()
