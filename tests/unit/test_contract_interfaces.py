"""
test_contract_interfaces.py - Cross-module contract tests for HARTOS

Verifies that the interfaces between HARTOS modules match what callers expect.
Nunba imports from HARTOS via pip — changing these interfaces breaks the desktop app:

FT: GoalManager registry, dispatch function signatures, agent_daemon lifecycle,
    model_catalog API shape, agent_ledger SmartLedger interface.
NFT: Import path stability, enum value stability, singleton behavior.
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ============================================================
# Import path stability — Nunba imports these exact paths
# ============================================================

class TestImportPaths:
    """Nunba's hartos_backend_adapter imports these — changing them breaks the desktop app."""

    def test_hart_intelligence_importable(self):
        import hart_intelligence
        assert hasattr(hart_intelligence, 'publish_async') or hasattr(hart_intelligence, 'app')

    def test_helper_action_class_importable(self):
        from helper import Action
        assert Action is not None

    def test_helper_retrieve_json_importable(self):
        from helper import retrieve_json
        assert callable(retrieve_json)

    def test_helper_prompts_dir_importable(self):
        from helper import PROMPTS_DIR
        assert isinstance(PROMPTS_DIR, str)
        assert os.path.isabs(PROMPTS_DIR)

    def test_cultural_wisdom_importable(self):
        from cultural_wisdom import get_cultural_prompt, CULTURAL_TRAITS
        assert callable(get_cultural_prompt)
        assert len(CULTURAL_TRAITS) >= 25

    def test_threadlocal_importable(self):
        from threadlocal import thread_local_data
        assert thread_local_data is not None

    def test_lifecycle_hooks_importable(self):
        from lifecycle_hooks import set_action_state, get_action_state
        assert callable(set_action_state)
        assert callable(get_action_state)


# ============================================================
# GoalManager — registry consumed by agent_daemon
# ============================================================

class TestGoalManagerContract:
    """GoalManager.build_prompt is called by agent_daemon on every tick."""

    def test_build_prompt_is_callable(self):
        from integrations.agent_engine.goal_manager import GoalManager
        assert callable(GoalManager.build_prompt)

    def test_register_goal_type_is_callable(self):
        from integrations.agent_engine.goal_manager import register_goal_type
        assert callable(register_goal_type)

    def test_create_goal_is_callable(self):
        from integrations.agent_engine.goal_manager import GoalManager
        assert callable(GoalManager.create_goal)


# ============================================================
# Dispatch — consumed by agent_daemon and API
# ============================================================

class TestDispatchContract:
    """dispatch_goal is the bridge between daemon goals and /chat execution."""

    def test_dispatch_goal_importable(self):
        from integrations.agent_engine.dispatch import dispatch_goal
        assert callable(dispatch_goal)

    def test_mark_user_chat_activity_importable(self):
        from integrations.agent_engine.dispatch import mark_user_chat_activity
        assert callable(mark_user_chat_activity)

    def test_is_user_recently_active_importable(self):
        from integrations.agent_engine.dispatch import is_user_recently_active
        assert callable(is_user_recently_active)
        result = is_user_recently_active()
        assert isinstance(result, bool)


# ============================================================
# AgentDaemon — lifecycle managed by app startup
# ============================================================

class TestAgentDaemonContract:
    """AgentDaemon is started by init_agent_engine — interface must be stable."""

    def test_agent_daemon_singleton(self):
        from integrations.agent_engine.agent_daemon import agent_daemon
        assert agent_daemon is not None

    def test_has_start_method(self):
        from integrations.agent_engine.agent_daemon import AgentDaemon
        daemon = AgentDaemon()
        assert callable(daemon.start)

    def test_has_stop_method(self):
        from integrations.agent_engine.agent_daemon import AgentDaemon
        daemon = AgentDaemon()
        assert callable(daemon.stop)


# ============================================================
# ModelCatalog — consumed by Nunba's models/catalog.py shim
# ============================================================

class TestModelCatalogHARTOSContract:
    """HARTOS ModelCatalog is the canonical source — Nunba re-exports it."""

    def test_model_catalog_importable(self):
        from integrations.service_tools.model_catalog import ModelCatalog
        assert ModelCatalog is not None

    def test_model_entry_importable(self):
        from integrations.service_tools.model_catalog import ModelEntry
        assert ModelEntry is not None

    def test_model_type_enum_importable(self):
        from integrations.service_tools.model_catalog import ModelType
        assert hasattr(ModelType, 'LLM')
        assert hasattr(ModelType, 'TTS')
        assert hasattr(ModelType, 'STT')
        assert hasattr(ModelType, 'VLM')

    def test_model_orchestrator_importable(self):
        from integrations.service_tools.model_orchestrator import ModelOrchestrator
        assert ModelOrchestrator is not None

    def test_vram_manager_importable(self):
        from integrations.service_tools.vram_manager import vram_manager
        assert vram_manager is not None
        assert callable(vram_manager.detect_gpu)


# ============================================================
# SmartLedger — consumed by create_recipe and reuse_recipe
# ============================================================

class TestSmartLedgerContract:
    """SmartLedger tracks task state across agent recipe execution."""

    def test_smart_ledger_importable(self):
        from agent_ledger.core import SmartLedger
        assert SmartLedger is not None

    def test_task_status_enum(self):
        from agent_ledger.core import TaskStatus
        assert hasattr(TaskStatus, 'PENDING')
        assert hasattr(TaskStatus, 'IN_PROGRESS')
        assert hasattr(TaskStatus, 'COMPLETED')

    def test_create_ledger_from_actions_importable(self):
        from agent_ledger.core import create_ledger_from_actions
        assert callable(create_ledger_from_actions)


# ============================================================
# Session cache — TTLCache consumed everywhere
# ============================================================

class TestSessionCacheContract:
    """TTLCache is the backbone of in-memory agent state."""

    def test_ttl_cache_importable(self):
        from core.session_cache import TTLCache
        assert TTLCache is not None

    def test_ttl_cache_has_dict_interface(self):
        from core.session_cache import TTLCache
        cache = TTLCache(ttl_seconds=60, max_size=10, name='test')
        cache['key'] = 'val'
        assert cache['key'] == 'val'
        assert cache.get('missing') is None
        assert 'key' in cache
        assert len(cache) == 1
