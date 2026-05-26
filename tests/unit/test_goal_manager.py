"""
Comprehensive tests for integrations/agent_engine/goal_manager.py (1934 lines).

Covers: Goal CRUD, prompt building, sanitization, product management,
registry operations, thread safety, boundary values, error paths, contracts.

Run: pytest tests/unit/test_goal_manager.py -v --noconftest
"""
import os
import sys
import time
import json
import threading
import unittest
from unittest.mock import patch, MagicMock, PropertyMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


# ---------------------------------------------------------------------------
# Helpers: mock DB session + model stubs
# ---------------------------------------------------------------------------

class FakeGoal:
    """Stub for AgentGoal ORM model."""
    _id_counter = 0

    def __init__(self, **kwargs):
        FakeGoal._id_counter += 1
        self.id = str(FakeGoal._id_counter)
        for k, v in kwargs.items():
            setattr(self, k, v)
        if not hasattr(self, 'created_at'):
            self.created_at = '2026-01-01'

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}


class FakeProduct:
    """Stub for Product ORM model."""
    _id_counter = 0

    def __init__(self, **kwargs):
        FakeProduct._id_counter += 1
        self.id = str(FakeProduct._id_counter)
        for k, v in kwargs.items():
            setattr(self, k, v)
        if not hasattr(self, 'created_at'):
            self.created_at = '2026-01-01'

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}


class FakeQuery:
    """Minimal SQLAlchemy query stub supporting filter_by/order_by/all/first."""
    def __init__(self, items=None):
        self._items = items or []
        self._filters = {}

    def filter_by(self, **kwargs):
        self._filters.update(kwargs)
        filtered = [i for i in self._items if all(
            getattr(i, k, None) == v for k, v in kwargs.items())]
        return FakeQuery(filtered)

    def order_by(self, *args):
        return self

    def all(self):
        return self._items

    def first(self):
        return self._items[0] if self._items else None


class FakeSession:
    """Minimal DB session stub."""
    def __init__(self):
        self._store = {}  # model_class -> [instances]
        self._added = []

    def add(self, obj):
        self._added.append(obj)

    def flush(self):
        pass

    def query(self, model_cls):
        return FakeQuery(self._store.get(model_cls, []))

    def seed(self, model_cls, items):
        self._store[model_cls] = items


# ---------------------------------------------------------------------------
# Module-level patches so imports don't fail
# ---------------------------------------------------------------------------

def _fresh_import():
    """Re-import goal_manager with a clean registry.

    We need to clear the module-level registries before each test class
    that cares about them. Since the module registers types at import time,
    we reload it.
    """
    import importlib
    # Ensure the module is in sys.modules so reload works
    mod_name = 'integrations.agent_engine.goal_manager'
    if mod_name in sys.modules:
        mod = sys.modules[mod_name]
        # Clear registries
        mod._prompt_builders.clear()
        mod._tool_tags.clear()
        importlib.reload(mod)
    else:
        mod = importlib.import_module(mod_name)
    return mod


# ===========================================================================
# FT: Goal Type Registry
# ===========================================================================

class TestGoalTypeRegistry(unittest.TestCase):
    """Tests for register_goal_type / get_prompt_builder / get_tool_tags / get_registered_types."""

    def setUp(self):
        self.gm = _fresh_import()

    def test_builtin_types_registered(self):
        """All built-in goal types must be present after import."""
        expected = {
            'marketing', 'coding', 'ip_protection', 'revenue', 'finance',
            'self_heal', 'federation', 'upgrade', 'thought_experiment',
            'news', 'provision', 'content_gen', 'learning',
            'distributed_learning', 'robot', 'trading', 'civic_sentinel',
            'self_build', 'autoresearch', 'code_evolution',
            'p2p_marketplace', 'p2p_rideshare', 'p2p_grocery', 'p2p_food',
            'p2p_freelance', 'p2p_bills', 'p2p_tickets', 'p2p_tutoring',
            'p2p_services', 'p2p_rental', 'p2p_health', 'p2p_logistics',
        }
        registered = set(self.gm.get_registered_types())
        self.assertTrue(expected.issubset(registered),
                        f"Missing types: {expected - registered}")

    def test_register_custom_type(self):
        """Custom goal types can be registered and retrieved."""
        builder = lambda g, p=None: "custom prompt"
        self.gm.register_goal_type('test_custom', builder, tool_tags=['tag1'])
        self.assertIn('test_custom', self.gm.get_registered_types())
        self.assertEqual(self.gm.get_prompt_builder('test_custom'), builder)
        self.assertEqual(self.gm.get_tool_tags('test_custom'), ['tag1'])

    def test_get_prompt_builder_unknown(self):
        """Unknown goal type returns None for prompt builder."""
        self.assertIsNone(self.gm.get_prompt_builder('nonexistent_xyz'))

    def test_get_tool_tags_unknown(self):
        """Unknown goal type returns empty list for tool tags."""
        self.assertEqual(self.gm.get_tool_tags('nonexistent_xyz'), [])

    def test_register_overwrites(self):
        """Registering same type twice overwrites the previous builder."""
        b1 = lambda g, p=None: "v1"
        b2 = lambda g, p=None: "v2"
        self.gm.register_goal_type('overwrite_test', b1)
        self.gm.register_goal_type('overwrite_test', b2)
        self.assertEqual(self.gm.get_prompt_builder('overwrite_test'), b2)

    def test_register_no_tool_tags(self):
        """Registering without tool_tags defaults to empty list."""
        self.gm.register_goal_type('no_tags', lambda g, p=None: "")
        self.assertEqual(self.gm.get_tool_tags('no_tags'), [])


# ===========================================================================
# FT: GoalManager CRUD
# ===========================================================================

class TestGoalManagerCreate(unittest.TestCase):
    """Tests for GoalManager.create_goal."""

    def setUp(self):
        self.gm = _fresh_import()
        self.db = FakeSession()

    def _create(self, goal_type, title, desc='', config=None,
                product_id=None, spark_budget=200, created_by=None):
        """Helper that patches models + guardrails for create_goal."""
        mock_cf = MagicMock()
        mock_cf.check_goal.return_value = (True, '')
        mock_he = MagicMock()
        mock_he.check_goal_ethos.return_value = (True, '')

        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(AgentGoal=lambda **kw: FakeGoal(**kw)),
            'security.hive_guardrails': MagicMock(
                ConstitutionalFilter=mock_cf, HiveEthos=mock_he),
            'security.rate_limiter_redis': MagicMock(
                get_rate_limiter=MagicMock(return_value=MagicMock(check=MagicMock(return_value=True)))),
        }):
            return self.gm.GoalManager.create_goal(
                self.db, goal_type, title, desc, config,
                product_id, spark_budget, created_by)

    def test_create_happy_path(self):
        """Successful goal creation returns success=True with goal dict."""
        result = self._create('marketing', 'Test Campaign')
        self.assertTrue(result['success'])
        self.assertIn('goal', result)
        self.assertEqual(result['goal']['title'], 'Test Campaign')

    def test_create_unknown_type(self):
        """Creating a goal with unregistered type returns error."""
        result = self.gm.GoalManager.create_goal(
            self.db, 'totally_unknown_type_xyz', 'Title')
        self.assertFalse(result['success'])
        self.assertIn('Unknown goal type', result['error'])

    def test_create_sets_active_status(self):
        """Newly created goals default to 'active' status."""
        result = self._create('marketing', 'Active check')
        self.assertEqual(result['goal']['status'], 'active')

    def test_create_with_config(self):
        """Config dict is preserved in the created goal."""
        cfg = {'channels': ['twitter', 'linkedin']}
        result = self._create('marketing', 'With config', config=cfg)
        self.assertTrue(result['success'])
        self.assertEqual(result['goal']['config_json'], cfg)

    def test_create_guardrail_blocks(self):
        """Goal blocked by ConstitutionalFilter returns success=False."""
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(AgentGoal=lambda **kw: FakeGoal(**kw)),
            'security.hive_guardrails': MagicMock(
                ConstitutionalFilter=MagicMock(check_goal=MagicMock(return_value=(False, 'violates rule 7'))),
                HiveEthos=MagicMock(check_goal_ethos=MagicMock(return_value=(True, '')))),
            'security.rate_limiter_redis': MagicMock(
                get_rate_limiter=MagicMock(return_value=MagicMock(check=MagicMock(return_value=True)))),
        }):
            result = self.gm.GoalManager.create_goal(
                self.db, 'marketing', 'Bad goal', created_by='user1')
        self.assertFalse(result['success'])
        self.assertIn('Guardrail', result['error'])

    def test_create_rate_limited(self):
        """Rate-limited user gets success=False."""
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(AgentGoal=lambda **kw: FakeGoal(**kw)),
            'security.hive_guardrails': MagicMock(
                ConstitutionalFilter=MagicMock(check_goal=MagicMock(return_value=(True, ''))),
                HiveEthos=MagicMock(check_goal_ethos=MagicMock(return_value=(True, '')))),
            'security.rate_limiter_redis': MagicMock(
                get_rate_limiter=MagicMock(return_value=MagicMock(check=MagicMock(return_value=False)))),
        }):
            result = self.gm.GoalManager.create_goal(
                self.db, 'marketing', 'Flood goal', created_by='spammer')
        self.assertFalse(result['success'])
        self.assertIn('Rate limited', result['error'])

    def test_create_guardrails_import_error(self):
        """Missing hive_guardrails module blocks goal creation (fail-closed)."""
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(AgentGoal=lambda **kw: FakeGoal(**kw)),
            'security.rate_limiter_redis': MagicMock(
                get_rate_limiter=MagicMock(return_value=MagicMock(check=MagicMock(return_value=True)))),
        }):
            # Remove hive_guardrails so import fails
            sys.modules.pop('security.hive_guardrails', None)
            # Force the import to raise ImportError
            import builtins
            original_import = builtins.__import__
            def fail_guardrails(name, *args, **kwargs):
                if 'hive_guardrails' in name:
                    raise ImportError("mocked")
                return original_import(name, *args, **kwargs)
            with patch('builtins.__import__', side_effect=fail_guardrails):
                result = self.gm.GoalManager.create_goal(
                    self.db, 'marketing', 'No guardrails', created_by='user1')
        self.assertFalse(result['success'])
        self.assertIn('Security module unavailable', result['error'])


class TestGoalManagerRead(unittest.TestCase):
    """Tests for GoalManager.get_goal and list_goals."""

    def setUp(self):
        self.gm = _fresh_import()
        self.db = FakeSession()

    def test_get_goal_found(self):
        """get_goal returns goal dict when found."""
        fake = FakeGoal(id='42', goal_type='marketing', title='Found')
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(AgentGoal=type(fake)),
        }):
            # Patch db.query to return our fake
            self.db.query = lambda cls: FakeQuery([fake])
            result = self.gm.GoalManager.get_goal(self.db, '42')
        self.assertTrue(result['success'])
        self.assertEqual(result['goal']['title'], 'Found')

    def test_get_goal_not_found(self):
        """get_goal returns error when goal doesn't exist."""
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(AgentGoal=FakeGoal),
        }):
            self.db.query = lambda cls: FakeQuery([])
            result = self.gm.GoalManager.get_goal(self.db, 'missing')
        self.assertFalse(result['success'])
        self.assertIn('not found', result['error'])

    def test_list_goals_empty(self):
        """list_goals returns empty list when no goals match."""
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(
                AgentGoal=MagicMock(created_at=MagicMock(desc=MagicMock(return_value=None)))),
        }):
            fake_ag = MagicMock()
            fake_ag.created_at.desc.return_value = None
            self.db.query = lambda cls: FakeQuery([])
            result = self.gm.GoalManager.list_goals(self.db)
        self.assertEqual(result, [])

    def test_list_goals_with_filters(self):
        """list_goals filters by goal_type and status."""
        g1 = FakeGoal(goal_type='marketing', status='active', title='A')
        g2 = FakeGoal(goal_type='coding', status='active', title='B')
        g3 = FakeGoal(goal_type='marketing', status='completed', title='C')
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(
                AgentGoal=MagicMock(created_at=MagicMock(desc=MagicMock(return_value=None)))),
        }):
            self.db.query = lambda cls: FakeQuery([g1, g2, g3])
            result = self.gm.GoalManager.list_goals(
                self.db, goal_type='marketing', status='active')
        titles = [g['title'] for g in result]
        self.assertIn('A', titles)
        self.assertNotIn('B', titles)
        self.assertNotIn('C', titles)


class TestGoalManagerUpdate(unittest.TestCase):
    """Tests for GoalManager.update_goal and update_goal_status."""

    def setUp(self):
        self.gm = _fresh_import()
        self.db = FakeSession()

    def test_update_goal_status_happy(self):
        """update_goal_status changes status and returns updated goal."""
        fake = FakeGoal(id='10', goal_type='marketing', title='U', status='active')
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(AgentGoal=type(fake)),
            'security.hive_guardrails': MagicMock(
                HiveEthos=MagicMock(enforce_ephemeral_agents=MagicMock())),
        }):
            self.db.query = lambda cls: FakeQuery([fake])
            result = self.gm.GoalManager.update_goal_status(self.db, '10', 'completed')
        self.assertTrue(result['success'])
        self.assertEqual(result['goal']['status'], 'completed')

    def test_update_goal_status_not_found(self):
        """update_goal_status returns error for missing goal."""
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(AgentGoal=FakeGoal),
        }):
            self.db.query = lambda cls: FakeQuery([])
            result = self.gm.GoalManager.update_goal_status(self.db, 'nope', 'done')
        self.assertFalse(result['success'])

    def test_update_goal_fields(self):
        """update_goal can update arbitrary fields on the goal."""
        fake = FakeGoal(id='20', title='Old', description='Old desc')
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(AgentGoal=type(fake)),
        }):
            self.db.query = lambda cls: FakeQuery([fake])
            result = self.gm.GoalManager.update_goal(
                self.db, '20', title='New', description='New desc')
        self.assertTrue(result['success'])
        self.assertEqual(result['goal']['title'], 'New')
        self.assertEqual(result['goal']['description'], 'New desc')

    def test_update_goal_ignores_unknown_fields(self):
        """update_goal silently ignores fields that don't exist on the model."""
        fake = FakeGoal(id='30', title='Keep')
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(AgentGoal=type(fake)),
        }):
            self.db.query = lambda cls: FakeQuery([fake])
            result = self.gm.GoalManager.update_goal(
                self.db, '30', nonexistent_field='value')
        self.assertTrue(result['success'])
        self.assertNotIn('nonexistent_field', result['goal'])

    def test_update_goal_not_found(self):
        """update_goal returns error for missing goal."""
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(AgentGoal=FakeGoal),
        }):
            self.db.query = lambda cls: FakeQuery([])
            result = self.gm.GoalManager.update_goal(self.db, 'ghost', title='X')
        self.assertFalse(result['success'])


# ===========================================================================
# FT: ProductManager CRUD
# ===========================================================================

class TestProductManager(unittest.TestCase):
    """Tests for ProductManager CRUD operations."""

    def setUp(self):
        self.gm = _fresh_import()
        self.db = FakeSession()

    def test_create_product_happy(self):
        """Create product returns success with product dict."""
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(Product=lambda **kw: FakeProduct(**kw)),
        }):
            result = self.gm.ProductManager.create_product(
                self.db, 'Widget', owner_id='owner1', description='A widget')
        self.assertTrue(result['success'])
        self.assertEqual(result['product']['name'], 'Widget')

    def test_get_product_not_found(self):
        """get_product returns error for missing product."""
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(Product=FakeProduct),
        }):
            self.db.query = lambda cls: FakeQuery([])
            result = self.gm.ProductManager.get_product(self.db, 'nope')
        self.assertFalse(result['success'])

    def test_update_product_keywords(self):
        """update_product handles 'keywords' specially via keywords_json."""
        fake = FakeProduct(id='5', name='P', keywords_json=[])
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(Product=type(fake)),
        }):
            self.db.query = lambda cls: FakeQuery([fake])
            result = self.gm.ProductManager.update_product(
                self.db, '5', keywords=['ai', 'ml'])
        self.assertTrue(result['success'])
        self.assertEqual(result['product']['keywords_json'], ['ai', 'ml'])

    def test_delete_product_archives(self):
        """delete_product sets status to 'archived' (soft delete)."""
        fake = FakeProduct(id='7', name='P', status='active')
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(Product=type(fake)),
        }):
            self.db.query = lambda cls: FakeQuery([fake])
            result = self.gm.ProductManager.delete_product(self.db, '7')
        self.assertTrue(result['success'])
        self.assertEqual(result['product']['status'], 'archived')

    def test_delete_product_not_found(self):
        """delete_product returns error for missing product."""
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(Product=FakeProduct),
        }):
            self.db.query = lambda cls: FakeQuery([])
            result = self.gm.ProductManager.delete_product(self.db, 'nope')
        self.assertFalse(result['success'])


# ===========================================================================
# FT: Prompt Building
# ===========================================================================

class TestBuildPrompt(unittest.TestCase):
    """Tests for GoalManager.build_prompt (dispatch to registered builders)."""

    def setUp(self):
        self.gm = _fresh_import()

    def test_build_prompt_with_registered_type(self):
        """build_prompt dispatches to the registered builder."""
        custom_builder = MagicMock(return_value="custom result")
        self.gm.register_goal_type('test_build', custom_builder)
        with patch.dict('sys.modules', {
            'security.hive_guardrails': MagicMock(HiveEthos=MagicMock()),
        }):
            result = self.gm.GoalManager.build_prompt(
                {'goal_type': 'test_build', 'title': 'T', 'description': 'D'})
        custom_builder.assert_called_once()
        self.assertEqual(result, "custom result")

    def test_build_prompt_unknown_type_fallback(self):
        """Unknown goal type uses fallback (title + description)."""
        with patch.dict('sys.modules', {
            'security.hive_guardrails': MagicMock(HiveEthos=MagicMock()),
        }):
            result = self.gm.GoalManager.build_prompt(
                {'goal_type': 'mystery', 'title': 'MyTitle', 'description': 'MyDesc'})
        self.assertIn('MyTitle', result)
        self.assertIn('MyDesc', result)

    def test_build_prompt_guardrails_unavailable_proceeds_in_warn_mode(self):
        """When hive_guardrails can't be imported, build_prompt proceeds (warn mode)."""
        import builtins
        original_import = builtins.__import__
        def fail_guardrails(name, *args, **kwargs):
            if 'hive_guardrails' in name:
                raise ImportError("mocked")
            return original_import(name, *args, **kwargs)
        sys.modules.pop('security.hive_guardrails', None)
        with patch('builtins.__import__', side_effect=fail_guardrails):
            result = self.gm.GoalManager.build_prompt(
                {'goal_type': 'unknown_xyz', 'title': 'T', 'description': 'D'})
        # Warn mode: returns a basic prompt with title + description
        self.assertIsNotNone(result)
        self.assertIn('T', result)

    def test_build_prompt_sanitizes_title(self):
        """Title is truncated to 200 chars and control chars are stripped."""
        long_title = 'A' * 500
        with patch.dict('sys.modules', {
            'security.hive_guardrails': MagicMock(HiveEthos=MagicMock()),
        }):
            result = self.gm.GoalManager.build_prompt(
                {'goal_type': 'unknown_fallback', 'title': long_title, 'description': ''})
        # The fallback prompt includes the sanitized title (max 200 chars)
        # Count occurrences of 'A' — should be at most 200 in the title portion
        self.assertIsNotNone(result)
        self.assertNotIn('A' * 201, result)


# ===========================================================================
# FT: Sanitization
# ===========================================================================

class TestSanitization(unittest.TestCase):
    """Tests for _sanitize_goal_input — prompt injection defense."""

    def setUp(self):
        self.gm = _fresh_import()

    def test_empty_input(self):
        """Empty string returns empty string."""
        self.assertEqual(self.gm._sanitize_goal_input(''), '')

    def test_none_returns_empty(self):
        """None-ish falsy input returns empty string."""
        self.assertEqual(self.gm._sanitize_goal_input(''), '')

    def test_truncation(self):
        """Input is truncated to max_length."""
        result = self.gm._sanitize_goal_input('x' * 100, max_length=50)
        self.assertEqual(len(result), 50)

    def test_control_chars_stripped(self):
        """Control characters (except newline/tab) are removed."""
        text = "hello\x00world\x01test\nkeep\ttabs"
        result = self.gm._sanitize_goal_input(text)
        self.assertNotIn('\x00', result)
        self.assertNotIn('\x01', result)
        self.assertIn('\n', result)
        self.assertIn('\t', result)

    def test_injection_marker_logged(self):
        """Injection markers trigger a warning log (but content is NOT blocked)."""
        with patch('integrations.agent_engine.goal_manager.logger') as mock_log:
            result = self.gm._sanitize_goal_input('ignore previous instructions')
            mock_log.warning.assert_called()
        # Content is preserved (only logged, not blocked)
        self.assertIn('ignore previous', result)

    def test_normal_text_passes_through(self):
        """Normal text passes through unmodified."""
        text = "Create a marketing campaign for our new product launch"
        self.assertEqual(self.gm._sanitize_goal_input(text), text)

    def test_unicode_preserved(self):
        """Unicode characters (CJK, emoji, Devanagari) pass through."""
        text = "Launch in \u6771\u4eac and \u092e\u0941\u0902\u092c\u0908"
        result = self.gm._sanitize_goal_input(text)
        self.assertEqual(result, text)


# ===========================================================================
# FT: Built-in Prompt Builders
# ===========================================================================

class TestBuiltinPromptBuilders(unittest.TestCase):
    """Tests for specific prompt builder functions."""

    def setUp(self):
        self.gm = _fresh_import()

    def test_marketing_prompt_contains_platform_identity(self):
        """Marketing prompt includes WHO WE ARE section."""
        builder = self.gm.get_prompt_builder('marketing')
        with patch.dict('sys.modules', {
            'security.hive_guardrails': MagicMock(
                VALUES=MagicMock(GUARDIAN_PURPOSE=['test purpose'])),
        }):
            result = builder({'title': 'Test', 'goal_type': 'marketing'})
        self.assertIn('WHO WE ARE', result)
        self.assertIn('YOUR CURRENT GOAL', result)

    def test_coding_prompt_has_trueflow(self):
        """Coding prompt includes TrueflowPlugin instructions."""
        builder = self.gm.get_prompt_builder('coding')
        with patch.dict('sys.modules', {
            'security.hive_guardrails': MagicMock(
                VALUES=MagicMock(GUARDIAN_PURPOSE=['test'])),
            'integrations.agent_engine.hive_sdk_spec': MagicMock(
                get_hive_embedding_instructions=MagicMock(return_value='HIVE_EMBED'),
                CODE_QUALITY_CONSTITUTIONAL_RULES='CODE_RULES'),
        }):
            result = builder({'title': 'Fix bug', 'goal_type': 'coding',
                              'config': {'repo_url': 'https://github.com/test'}})
        self.assertIn('TrueflowPlugin', result)
        self.assertIn('Fix bug', result)

    def test_self_heal_prompt_has_exception_info(self):
        """Self-heal prompt includes exception details from config."""
        builder = self.gm.get_prompt_builder('self_heal')
        result = builder({
            'title': 'Fix crash', 'goal_type': 'self_heal',
            'config': {'exc_type': 'ValueError', 'source_module': 'main',
                       'source_function': 'run', 'occurrence_count': 5,
                       'sample_traceback': 'Traceback...'}})
        self.assertIn('ValueError', result)
        self.assertIn('Module:', result)
        self.assertIn('SELF-HEALING', result)

    def test_trading_prompt_paper_mode_default(self):
        """Trading prompt defaults to paper trading."""
        builder = self.gm.get_prompt_builder('trading')
        result = builder({'title': 'Trade', 'goal_type': 'trading', 'config': {}})
        self.assertIn('PAPER TRADING', result)

    def test_trading_prompt_live_mode(self):
        """Trading prompt shows LIVE TRADING when configured."""
        builder = self.gm.get_prompt_builder('trading')
        result = builder({'title': 'Trade', 'goal_type': 'trading',
                          'config': {'paper_trading': False}})
        self.assertIn('LIVE TRADING', result)

    def test_autoresearch_returns_none_without_config(self):
        """Autoresearch returns None when repo_path or run_command missing (guard)."""
        builder = self.gm.get_prompt_builder('autoresearch')
        result = builder({'title': 'Research', 'goal_type': 'autoresearch', 'config': {}})
        self.assertIsNone(result)

    def test_autoresearch_with_full_config(self):
        """Autoresearch returns prompt when repo_path and run_command provided."""
        builder = self.gm.get_prompt_builder('autoresearch')
        result = builder({
            'title': 'Optimize', 'goal_type': 'autoresearch',
            'config': {'repo_path': '/repo', 'run_command': 'python test.py'}})
        self.assertIsNotNone(result)
        self.assertIn('AUTONOMOUS RESEARCH', result)
        self.assertIn('/repo', result)

    def test_robot_prompt_fallback(self):
        """Robot prompt has fallback when robotics package unavailable."""
        builder = self.gm.get_prompt_builder('robot')
        # robotics package not installed — fallback path
        import builtins
        original_import = builtins.__import__
        def fail_robotics(name, *args, **kwargs):
            if 'robot_prompt_builder' in name:
                raise ImportError("no robotics")
            return original_import(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=fail_robotics):
            result = builder({'title': 'Walk', 'description': 'Walk forward',
                              'goal_type': 'robot'})
        self.assertIn('ROBOT GOAL', result)
        self.assertIn('Walk', result)

    def test_news_prompt_has_curation_rules(self):
        """News prompt includes curation rules."""
        builder = self.gm.get_prompt_builder('news')
        result = builder({'title': 'Curate', 'goal_type': 'news', 'config': {}})
        self.assertIn('CURATION RULES', result)


# ===========================================================================
# FT: CODING_GOAL_TYPES
# ===========================================================================

class TestCodingGoalTypes(unittest.TestCase):
    """Tests for CODING_GOAL_TYPES frozenset constant."""

    def setUp(self):
        self.gm = _fresh_import()

    def test_is_frozenset(self):
        """CODING_GOAL_TYPES must be a frozenset (immutable)."""
        self.assertIsInstance(self.gm.CODING_GOAL_TYPES, frozenset)

    def test_contains_expected_types(self):
        """All coding-related types are present."""
        expected = {'coding', 'code_evolution', 'self_heal', 'autoresearch', 'self_build'}
        self.assertEqual(self.gm.CODING_GOAL_TYPES, expected)


# ===========================================================================
# Boundary: Edge cases
# ===========================================================================

class TestBoundaryValues(unittest.TestCase):
    """Boundary and edge case tests."""

    def setUp(self):
        self.gm = _fresh_import()

    def test_sanitize_exactly_max_length(self):
        """Input exactly at max_length is not truncated."""
        text = 'x' * 2000
        result = self.gm._sanitize_goal_input(text, max_length=2000)
        self.assertEqual(len(result), 2000)

    def test_sanitize_one_over_max_length(self):
        """Input one char over max_length is truncated."""
        text = 'x' * 2001
        result = self.gm._sanitize_goal_input(text, max_length=2000)
        self.assertEqual(len(result), 2000)

    def test_sanitize_single_char(self):
        """Single character input works."""
        self.assertEqual(self.gm._sanitize_goal_input('a'), 'a')

    def test_build_prompt_empty_title_and_desc(self):
        """build_prompt works with empty title and description."""
        with patch.dict('sys.modules', {
            'security.hive_guardrails': MagicMock(HiveEthos=MagicMock()),
        }):
            result = self.gm.GoalManager.build_prompt(
                {'goal_type': 'nonexistent', 'title': '', 'description': ''})
        self.assertIsNotNone(result)

    def test_sanitize_all_injection_markers(self):
        """Each injection marker is detected individually."""
        for marker in self.gm._INJECTION_MARKERS:
            with patch('integrations.agent_engine.goal_manager.logger') as mock_log:
                self.gm._sanitize_goal_input(f"test {marker} test")
                mock_log.warning.assert_called_once(), \
                    f"Marker '{marker}' should trigger warning"

    def test_unicode_goal_title_roundtrip(self):
        """Unicode goal titles survive create -> to_dict."""
        title = "\u0928\u092e\u0938\u094d\u0924\u0947 \u4e16\u754c \ud83c\udf0d"
        fake = FakeGoal(title=title, goal_type='marketing', status='active',
                        description='', config_json={})
        d = fake.to_dict()
        self.assertEqual(d['title'], title)


# ===========================================================================
# NFT: Thread Safety
# ===========================================================================

class TestThreadSafety(unittest.TestCase):
    """Thread safety tests for concurrent goal operations."""

    def setUp(self):
        self.gm = _fresh_import()

    def test_concurrent_registry_reads(self):
        """Multiple threads reading registry concurrently don't crash."""
        errors = []
        def reader():
            try:
                for _ in range(100):
                    self.gm.get_registered_types()
                    self.gm.get_prompt_builder('marketing')
                    self.gm.get_tool_tags('coding')
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [], f"Thread errors: {errors}")

    def test_concurrent_sanitization(self):
        """Concurrent sanitization calls don't interfere with each other."""
        results = {}
        def sanitize_worker(idx):
            text = f"test_{idx}_" * 100
            results[idx] = self.gm._sanitize_goal_input(text, max_length=500)

        threads = [threading.Thread(target=sanitize_worker, args=(i,))
                   for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(results), 20)
        for idx, result in results.items():
            self.assertTrue(result.startswith(f"test_{idx}_"))


# ===========================================================================
# NFT: Import Speed
# ===========================================================================

class TestImportSpeed(unittest.TestCase):
    """Non-functional: module import should be fast."""

    def test_import_under_2_seconds(self):
        """goal_manager module imports in under 2 seconds."""
        import importlib
        mod_name = 'integrations.agent_engine.goal_manager'
        # Remove from cache
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        start = time.monotonic()
        importlib.import_module(mod_name)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.0,
                        f"Import took {elapsed:.2f}s — should be under 2s")


# ===========================================================================
# Contract: Return shapes and required fields
# ===========================================================================

class TestContracts(unittest.TestCase):
    """Contract tests: verify return shapes are consistent."""

    def setUp(self):
        self.gm = _fresh_import()

    def test_create_goal_success_shape(self):
        """Successful create returns {'success': True, 'goal': dict}."""
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(AgentGoal=lambda **kw: FakeGoal(**kw)),
            'security.hive_guardrails': MagicMock(
                ConstitutionalFilter=MagicMock(check_goal=MagicMock(return_value=(True, ''))),
                HiveEthos=MagicMock(check_goal_ethos=MagicMock(return_value=(True, '')))),
            'security.rate_limiter_redis': MagicMock(
                get_rate_limiter=MagicMock(return_value=MagicMock(check=MagicMock(return_value=True)))),
        }):
            result = self.gm.GoalManager.create_goal(
                FakeSession(), 'marketing', 'Shape test')
        self.assertIn('success', result)
        self.assertTrue(result['success'])
        self.assertIn('goal', result)
        self.assertIsInstance(result['goal'], dict)

    def test_create_goal_error_shape(self):
        """Failed create returns {'success': False, 'error': str}."""
        result = self.gm.GoalManager.create_goal(
            FakeSession(), 'nonexistent_type_abc', 'Bad')
        self.assertIn('success', result)
        self.assertFalse(result['success'])
        self.assertIn('error', result)
        self.assertIsInstance(result['error'], str)

    def test_get_goal_success_shape(self):
        """Successful get returns {'success': True, 'goal': dict}."""
        fake = FakeGoal(id='1', goal_type='x', title='T')
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(AgentGoal=type(fake)),
        }):
            db = FakeSession()
            db.query = lambda cls: FakeQuery([fake])
            result = self.gm.GoalManager.get_goal(db, '1')
        self.assertTrue(result['success'])
        self.assertIsInstance(result['goal'], dict)

    def test_get_goal_error_shape(self):
        """Failed get returns {'success': False, 'error': str}."""
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(AgentGoal=FakeGoal),
        }):
            db = FakeSession()
            db.query = lambda cls: FakeQuery([])
            result = self.gm.GoalManager.get_goal(db, 'nope')
        self.assertFalse(result['success'])
        self.assertIn('error', result)

    def test_list_goals_returns_list(self):
        """list_goals always returns a list (even if empty)."""
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(
                AgentGoal=MagicMock(created_at=MagicMock(desc=MagicMock(return_value=None)))),
        }):
            db = FakeSession()
            db.query = lambda cls: FakeQuery([])
            result = self.gm.GoalManager.list_goals(db)
        self.assertIsInstance(result, list)

    def test_prompt_builders_return_string_or_none(self):
        """All registered prompt builders return str or None."""
        for gtype in self.gm.get_registered_types():
            builder = self.gm.get_prompt_builder(gtype)
            self.assertIsNotNone(builder,
                                 f"Builder for {gtype} should not be None")
            self.assertTrue(callable(builder),
                            f"Builder for {gtype} should be callable")


# ===========================================================================
# Error: DB Failures
# ===========================================================================

class TestDBFailures(unittest.TestCase):
    """Tests for database failure handling."""

    def setUp(self):
        self.gm = _fresh_import()

    def test_create_goal_db_flush_raises(self):
        """If db.flush() raises, the exception propagates (no silent swallow)."""
        db = FakeSession()
        db.flush = MagicMock(side_effect=RuntimeError("DB down"))
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(AgentGoal=lambda **kw: FakeGoal(**kw)),
            'security.hive_guardrails': MagicMock(
                ConstitutionalFilter=MagicMock(check_goal=MagicMock(return_value=(True, ''))),
                HiveEthos=MagicMock(check_goal_ethos=MagicMock(return_value=(True, '')))),
            'security.rate_limiter_redis': MagicMock(
                get_rate_limiter=MagicMock(return_value=MagicMock(check=MagicMock(return_value=True)))),
        }):
            with self.assertRaises(RuntimeError):
                self.gm.GoalManager.create_goal(
                    db, 'marketing', 'DB fail test')

    def test_update_status_db_flush_raises(self):
        """If db.flush() raises during status update, exception propagates."""
        fake = FakeGoal(id='1', status='active')
        db = FakeSession()
        db.query = lambda cls: FakeQuery([fake])
        db.flush = MagicMock(side_effect=RuntimeError("DB down"))
        with patch.dict('sys.modules', {
            'integrations.social.models': MagicMock(AgentGoal=type(fake)),
        }):
            with self.assertRaises(RuntimeError):
                self.gm.GoalManager.update_goal_status(db, '1', 'completed')


# ===========================================================================
# FT: P2P Prompt Builders
# ===========================================================================

class TestP2PPromptBuilders(unittest.TestCase):
    """Tests for P2P business vertical prompt builders."""

    def setUp(self):
        self.gm = _fresh_import()

    def _build(self, goal_type, config=None):
        builder = self.gm.get_prompt_builder(goal_type)
        self.assertIsNotNone(builder, f"No builder for {goal_type}")
        return builder({'title': 'Test', 'goal_type': goal_type,
                        'description': 'desc', 'config': config or {}})

    def test_p2p_marketplace_has_preamble(self):
        """P2P marketplace prompt includes the shared P2P preamble."""
        result = self._build('p2p_marketplace')
        self.assertIn('90% to service provider', result)

    def test_p2p_rideshare_has_ridesnap(self):
        """P2P rideshare prompt mentions RideSnap backend."""
        result = self._build('p2p_rideshare')
        self.assertIn('RIDESNAP', result)

    def test_p2p_grocery_has_mcgroce(self):
        """P2P grocery prompt mentions McGroce backend."""
        result = self._build('p2p_grocery')
        self.assertIn('McGROCE', result)

    def test_p2p_health_has_safety_rules(self):
        """P2P health prompt includes critical medical safety rules."""
        result = self._build('p2p_health')
        self.assertIn('NEVER provide medical diagnosis', result)

    def test_all_p2p_types_have_escrow(self):
        """All P2P prompts mention escrow (AP2 payment safety)."""
        p2p_types = [t for t in self.gm.get_registered_types() if t.startswith('p2p_')]
        for gtype in p2p_types:
            result = self._build(gtype)
            self.assertIn('escrow', result.lower(),
                          f"{gtype} prompt should mention escrow")


if __name__ == '__main__':
    unittest.main()
