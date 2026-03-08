"""Tests for GPU TTS tool stubs (chatterbox, cosyvoice, indic_parler, f5_tts).

Each stub follows the luxtts_tool pattern: lazy import, JSON return, singleton
cache, VRAM claim/release, ServiceToolInfo registration.

All tests mock the actual GPU packages (chatterbox, cosyvoice, parler_tts, f5_tts)
since they're not installed in the test environment.
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# ═══════════════════════════════════════════════════════════════
# Chatterbox Turbo
# ═══════════════════════════════════════════════════════════════

class TestChatterboxSynthesize:

    def test_empty_text_returns_error(self):
        from integrations.service_tools.chatterbox_tool import chatterbox_synthesize
        result = json.loads(chatterbox_synthesize(""))
        assert "error" in result

    def test_whitespace_returns_error(self):
        from integrations.service_tools.chatterbox_tool import chatterbox_synthesize
        result = json.loads(chatterbox_synthesize("   "))
        assert "error" in result

    def test_not_installed_returns_error(self):
        with patch.dict('sys.modules', {'chatterbox': None, 'chatterbox.tts': None}):
            from integrations.service_tools.chatterbox_tool import chatterbox_synthesize
            result = json.loads(chatterbox_synthesize("Hello"))
            assert "error" in result
            assert "not installed" in result["error"]

    def test_registration(self):
        from integrations.service_tools.chatterbox_tool import ChatterboxTool
        from integrations.service_tools.registry import service_tool_registry
        result = ChatterboxTool.register_functions()
        assert result is True
        assert 'chatterbox' in service_tool_registry._tools
        tool = service_tool_registry._tools['chatterbox']
        assert 'synthesize' in tool.endpoints
        assert 'synthesize_ml' in tool.endpoints
        assert 'tts' in tool.tags
        assert 'gpu' in tool.tags


class TestChatterboxMLSynthesize:

    def test_empty_text_returns_error(self):
        from integrations.service_tools.chatterbox_tool import chatterbox_ml_synthesize
        result = json.loads(chatterbox_ml_synthesize(""))
        assert "error" in result

    def test_not_installed_returns_error(self):
        with patch.dict('sys.modules', {'chatterbox': None, 'chatterbox.tts': None}):
            from integrations.service_tools.chatterbox_tool import chatterbox_ml_synthesize
            result = json.loads(chatterbox_ml_synthesize("Hello", language='zh'))
            assert "error" in result
            assert "not installed" in result["error"]


class TestChatterboxUnload:

    def test_unload_clears_models(self):
        import integrations.service_tools.chatterbox_tool as mod
        mod._turbo_model = MagicMock()
        mod._ml_model = MagicMock()
        mod.unload_chatterbox()
        assert mod._turbo_model is None
        assert mod._ml_model is None


# ═══════════════════════════════════════════════════════════════
# CosyVoice 3
# ═══════════════════════════════════════════════════════════════

class TestCosyVoiceSynthesize:

    def test_empty_text_returns_error(self):
        from integrations.service_tools.cosyvoice_tool import cosyvoice_synthesize
        result = json.loads(cosyvoice_synthesize(""))
        assert "error" in result

    def test_not_installed_returns_error(self):
        with patch.dict('sys.modules', {
            'cosyvoice': None,
            'cosyvoice.cli': None,
            'cosyvoice.cli.cosyvoice': None,
        }):
            from integrations.service_tools.cosyvoice_tool import cosyvoice_synthesize
            result = json.loads(cosyvoice_synthesize("你好"))
            assert "error" in result
            assert "not installed" in result["error"]

    def test_registration(self):
        from integrations.service_tools.cosyvoice_tool import CosyVoiceTool
        from integrations.service_tools.registry import service_tool_registry
        result = CosyVoiceTool.register_functions()
        assert result is True
        assert 'cosyvoice' in service_tool_registry._tools
        tool = service_tool_registry._tools['cosyvoice']
        assert 'synthesize' in tool.endpoints
        assert 'multilingual' in tool.tags


class TestCosyVoiceUnload:

    def test_unload_clears_model(self):
        import integrations.service_tools.cosyvoice_tool as mod
        mod._model = MagicMock()
        mod.unload_cosyvoice()
        assert mod._model is None


# ═══════════════════════════════════════════════════════════════
# Indic Parler TTS
# ═══════════════════════════════════════════════════════════════

class TestIndicParlerSynthesize:

    def test_empty_text_returns_error(self):
        from integrations.service_tools.indic_parler_tool import indic_parler_synthesize
        result = json.loads(indic_parler_synthesize(""))
        assert "error" in result

    def test_not_installed_returns_error(self):
        with patch.dict('sys.modules', {
            'parler_tts': None,
            'transformers': None,
        }):
            from integrations.service_tools.indic_parler_tool import indic_parler_synthesize
            result = json.loads(indic_parler_synthesize("नमस्ते"))
            assert "error" in result
            assert "not installed" in result["error"]

    def test_registration(self):
        from integrations.service_tools.indic_parler_tool import IndicParlerTool
        from integrations.service_tools.registry import service_tool_registry
        result = IndicParlerTool.register_functions()
        assert result is True
        assert 'indic_parler' in service_tool_registry._tools
        tool = service_tool_registry._tools['indic_parler']
        assert 'synthesize' in tool.endpoints
        assert 'indic' in tool.tags


class TestIndicParlerUnload:

    def test_unload_clears_model(self):
        import integrations.service_tools.indic_parler_tool as mod
        mod._model = MagicMock()
        mod._tokenizer = MagicMock()
        mod.unload_indic_parler()
        assert mod._model is None
        assert mod._tokenizer is None


# ═══════════════════════════════════════════════════════════════
# F5-TTS
# ═══════════════════════════════════════════════════════════════

class TestF5Synthesize:

    def test_empty_text_returns_error(self):
        from integrations.service_tools.f5_tts_tool import f5_synthesize
        result = json.loads(f5_synthesize(""))
        assert "error" in result

    def test_not_installed_returns_error(self):
        with patch.dict('sys.modules', {
            'f5_tts': None,
            'f5_tts.api': None,
        }):
            from integrations.service_tools.f5_tts_tool import f5_synthesize
            result = json.loads(f5_synthesize("Hello"))
            assert "error" in result
            assert "not installed" in result["error"]

    def test_registration(self):
        from integrations.service_tools.f5_tts_tool import F5TTSTool
        from integrations.service_tools.registry import service_tool_registry
        result = F5TTSTool.register_functions()
        assert result is True
        assert 'f5_tts' in service_tool_registry._tools
        tool = service_tool_registry._tools['f5_tts']
        assert 'synthesize' in tool.endpoints
        assert 'voice-cloning' in tool.tags


class TestF5Unload:

    def test_unload_clears_model(self):
        import integrations.service_tools.f5_tts_tool as mod
        mod._model = MagicMock()
        mod.unload_f5_tts()
        assert mod._model is None


# ═══════════════════════════════════════════════════════════════
# Cross-tool consistency
# ═══════════════════════════════════════════════════════════════

class TestToolConsistency:
    """All GPU stubs follow the same pattern."""

    def test_all_have_unload(self):
        from integrations.service_tools import chatterbox_tool, cosyvoice_tool
        from integrations.service_tools import indic_parler_tool, f5_tts_tool
        assert callable(chatterbox_tool.unload_chatterbox)
        assert callable(cosyvoice_tool.unload_cosyvoice)
        assert callable(indic_parler_tool.unload_indic_parler)
        assert callable(f5_tts_tool.unload_f5_tts)

    def test_all_return_json_strings(self):
        """Empty text should return valid JSON with error key."""
        from integrations.service_tools.chatterbox_tool import chatterbox_synthesize
        from integrations.service_tools.cosyvoice_tool import cosyvoice_synthesize
        from integrations.service_tools.indic_parler_tool import indic_parler_synthesize
        from integrations.service_tools.f5_tts_tool import f5_synthesize

        for fn in [chatterbox_synthesize, cosyvoice_synthesize,
                   indic_parler_synthesize, f5_synthesize]:
            result = fn("")
            assert isinstance(result, str)
            parsed = json.loads(result)
            assert "error" in parsed
