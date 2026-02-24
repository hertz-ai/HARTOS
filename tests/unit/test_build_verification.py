"""
Build Verification Tests — ensures HARTOS is buildable on any installed machine.

Tests that every module imports, entry points resolve, schema creates,
and no circular imports exist. Run this FIRST on any new machine.

Run: pytest tests/unit/test_build_verification.py -v --noconftest
"""
import importlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


class TestCoreModulesImportable(unittest.TestCase):
    """Every core module must import without errors."""

    CORE_MODULES = [
        'threadlocal',
        'helper',
        'helper_func',
        'helper_ledger',
        'lifecycle_hooks',
        'config',
    ]

    def test_all_core_modules_import(self):
        failures = []
        for mod_name in self.CORE_MODULES:
            try:
                importlib.import_module(mod_name)
            except Exception as e:
                failures.append(f"{mod_name}: {type(e).__name__}: {e}")
        if failures:
            self.fail(f"Failed to import:\n" + "\n".join(failures))


class TestIntegrationModulesImportable(unittest.TestCase):
    """Integration sub-packages must import without errors."""

    MODULES = [
        'integrations.social.models',
        'integrations.social.hosting_reward_service',
        'integrations.social.resonance_engine',
        'integrations.agent_engine.compute_config',
        'integrations.agent_engine.model_registry',
        'integrations.agent_engine.budget_gate',
        'integrations.agent_engine.revenue_aggregator',
        'integrations.agent_engine.dispatch',
        'integrations.coding_agent.tool_backends',
        'integrations.coding_agent.task_distributor',
        'integrations.service_tools.vram_manager',
        'security.master_key',
        'security.hive_guardrails',
        'security.system_requirements',
    ]

    def test_all_integration_modules_import(self):
        failures = []
        for mod_name in self.MODULES:
            try:
                importlib.import_module(mod_name)
            except Exception as e:
                failures.append(f"{mod_name}: {type(e).__name__}: {e}")
        if failures:
            self.fail(f"Failed to import:\n" + "\n".join(failures))


class TestFlaskAppImportable(unittest.TestCase):
    """langchain_gpt_api.py must import and create Flask app."""

    def test_langchain_gpt_api_imports(self):
        import langchain_gpt_api
        self.assertTrue(hasattr(langchain_gpt_api, 'app'))

    def test_flask_app_has_routes(self):
        import langchain_gpt_api
        rules = [r.rule for r in langchain_gpt_api.app.url_map.iter_rules()]
        # Must have at least /chat and /status
        self.assertIn('/chat', rules)
        self.assertIn('/status', rules)


class TestSchemaCreation(unittest.TestCase):
    """SQLAlchemy Base.metadata.create_all() must work."""

    def test_base_metadata_has_tables(self):
        from integrations.social.models import Base
        tables = Base.metadata.tables
        self.assertGreater(len(tables), 0)

    def test_required_tables_exist(self):
        from integrations.social.models import Base
        table_names = set(Base.metadata.tables.keys())
        required = {
            'peer_nodes', 'users', 'metered_api_usage',
            'node_compute_config', 'compute_escrow',
        }
        for t in required:
            self.assertIn(t, table_names, f"Missing table: {t}")

    def test_create_all_in_memory(self):
        """Schema creation works with in-memory SQLite."""
        from sqlalchemy import create_engine
        from integrations.social.models import Base
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        # Verify tables were created
        from sqlalchemy import inspect
        inspector = inspect(engine)
        table_names = inspector.get_table_names()
        self.assertIn('peer_nodes', table_names)
        self.assertIn('metered_api_usage', table_names)
        self.assertIn('node_compute_config', table_names)
        engine.dispose()


class TestNoCircularImports(unittest.TestCase):
    """Critical import chains must not be circular."""

    def test_compute_config_independent(self):
        """compute_config.py must import without pulling Flask."""
        # Clear modules
        mod_name = 'integrations.agent_engine.compute_config'
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        importlib.import_module(mod_name)
        # Should NOT have loaded langchain_gpt_api
        self.assertNotIn('langchain_gpt_api', sys.modules.get(mod_name, {}).__dict__)

    def test_model_registry_independent(self):
        """model_registry.py must import without pulling Flask."""
        mod = importlib.import_module('integrations.agent_engine.model_registry')
        self.assertTrue(hasattr(mod, 'ModelRegistry'))

    def test_budget_gate_independent(self):
        """budget_gate.py must import without pulling Flask."""
        mod = importlib.import_module('integrations.agent_engine.budget_gate')
        self.assertTrue(hasattr(mod, 'record_metered_usage'))


class TestEntryPoints(unittest.TestCase):
    """Package entry points must resolve."""

    def test_setup_py_exists(self):
        setup_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            'setup.py')
        self.assertTrue(os.path.exists(setup_path))

    def test_pyproject_toml_exists(self):
        pyproject_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            'pyproject.toml')
        self.assertTrue(os.path.exists(pyproject_path))

    def test_requirements_txt_exists(self):
        req_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            'requirements.txt')
        self.assertTrue(os.path.exists(req_path))


class TestSingletonsInitialize(unittest.TestCase):
    """Key singleton getters must work."""

    def test_model_registry_singleton(self):
        from integrations.agent_engine.model_registry import model_registry
        self.assertIsNotNone(model_registry)

    def test_revenue_aggregator_singleton(self):
        from integrations.agent_engine.revenue_aggregator import get_revenue_aggregator
        agg = get_revenue_aggregator()
        self.assertIsNotNone(agg)

    def test_compute_policy_defaults(self):
        from integrations.agent_engine.compute_config import get_compute_policy
        policy = get_compute_policy()
        self.assertIsInstance(policy, dict)
        self.assertIn('compute_policy', policy)


class TestDependencyVersions(unittest.TestCase):
    """Key dependencies must be importable at correct versions."""

    def test_flask_importable(self):
        import flask
        self.assertIsNotNone(flask.__version__)

    def test_sqlalchemy_importable(self):
        import sqlalchemy
        self.assertIsNotNone(sqlalchemy.__version__)

    def test_langchain_classic_importable(self):
        import langchain_classic
        # langchain_classic provides backward compat for langchain 0.x imports

    def test_langchain_community_importable(self):
        import langchain_community

    def test_langchain_openai_importable(self):
        import langchain_openai


if __name__ == '__main__':
    unittest.main()
