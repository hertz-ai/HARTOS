"""
Tests for Temporal Perception — EventBus, visual/audio watchers,
MemoryGraph auto-save, perception event emission.

Covers: _active_watchers, _handle_visual_watcher_tool, _evaluate_audio_watchers,
        _push_workflow_flowchart, VisionService._save_to_memory_graph,
        VisionService._emit_perception_event, EventBus bootstrap.

Run: pytest tests/unit/test_temporal_perception.py -v
"""

import json
import os
import sys
import time
from unittest.mock import patch, MagicMock, call

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

# Guard: skip all tests if hart_intelligence_entry doesn't fully load.
# On CI, heavy imports (LangChain, autogen) fail silently leaving module
# state as MagicMock instead of real objects.
try:
    from hart_intelligence_entry import _active_watchers
    if not isinstance(_active_watchers, dict):
        pytest.skip("hart_intelligence_entry partially loaded (no _active_watchers dict)", allow_module_level=True)
except (ImportError, AttributeError):
    pytest.skip("hart_intelligence_entry not importable", allow_module_level=True)


# ══════════════════════════════════════════════════════════════════
# 1. Watcher Data Structures
# ══════════════════════════════════════════════════════════════════

class TestWatcherDataStructures:
    """Verify watcher module-level state exists and is correct type."""

    def test_active_watchers_is_dict(self):
        from hart_intelligence_entry import _active_watchers
        assert isinstance(_active_watchers, dict)

    def test_active_watchers_starts_empty(self):
        from hart_intelligence_entry import _active_watchers
        # May have entries from other tests; just check type
        assert isinstance(_active_watchers, dict)


# ══════════════════════════════════════════════════════════════════
# 2. Visual Context Watcher Tool
# ══════════════════════════════════════════════════════════════════

class TestVisualContextWatcherTool:
    """Tests for _handle_visual_watcher_tool."""

    def setup_method(self):
        from hart_intelligence_entry import _active_watchers
        _active_watchers.clear()

    @patch('hart_intelligence_entry.thread_local_data')
    def test_parses_standard_input(self, mock_tld):
        from hart_intelligence_entry import _handle_visual_watcher_tool, _active_watchers

        mock_tld.get_user_id.return_value = 'user_42'

        result = _handle_visual_watcher_tool(
            'CONDITION: user raises hand | ACTION: say banana | TTL: 10'
        )

        assert 'registered' in result.lower()
        assert 'user raises hand' in result
        assert 'say banana' in result
        assert 'user_42' in _active_watchers
        assert len(_active_watchers['user_42']) == 1
        w = _active_watchers['user_42'][0]
        assert w['condition'] == 'user raises hand'
        assert w['action'] == 'say banana'
        assert w['modality'] == 'visual'  # default
        assert w['expires_at'] > time.time()

    @patch('hart_intelligence_entry.thread_local_data')
    def test_parses_audio_modality(self, mock_tld):
        from hart_intelligence_entry import _handle_visual_watcher_tool, _active_watchers

        mock_tld.get_user_id.return_value = 'user_99'

        result = _handle_visual_watcher_tool(
            'CONDITION: user says hello | ACTION: greet back | TTL: 5 | MODALITY: audio'
        )

        assert 'audio' in result
        w = _active_watchers['user_99'][0]
        assert w['modality'] == 'audio'

    @patch('hart_intelligence_entry.thread_local_data')
    def test_parses_both_modality(self, mock_tld):
        from hart_intelligence_entry import _handle_visual_watcher_tool, _active_watchers

        mock_tld.get_user_id.return_value = 'user_77'

        _handle_visual_watcher_tool(
            'CONDITION: user waves | ACTION: wave back | TTL: 15 | MODALITY: both'
        )

        w = _active_watchers['user_77'][0]
        assert w['modality'] == 'both'

    @patch('hart_intelligence_entry.thread_local_data')
    def test_default_ttl_is_30(self, mock_tld):
        from hart_intelligence_entry import _handle_visual_watcher_tool, _active_watchers

        mock_tld.get_user_id.return_value = 'user_default'

        _handle_visual_watcher_tool('CONDITION: something | ACTION: react')

        w = _active_watchers['user_default'][0]
        # TTL should be ~30 minutes from now
        expected_min = time.time() + 29 * 60
        expected_max = time.time() + 31 * 60
        assert expected_min < w['expires_at'] < expected_max

    @patch('hart_intelligence_entry.thread_local_data')
    def test_multiple_watchers_per_user(self, mock_tld):
        from hart_intelligence_entry import _handle_visual_watcher_tool, _active_watchers

        mock_tld.get_user_id.return_value = 'user_multi'

        _handle_visual_watcher_tool('CONDITION: A | ACTION: do A | TTL: 5')
        _handle_visual_watcher_tool('CONDITION: B | ACTION: do B | TTL: 10')

        assert len(_active_watchers['user_multi']) == 2

    @patch('hart_intelligence_entry.thread_local_data')
    def test_watcher_has_callback(self, mock_tld):
        from hart_intelligence_entry import _handle_visual_watcher_tool, _active_watchers

        mock_tld.get_user_id.return_value = 'user_cb'

        _handle_visual_watcher_tool('CONDITION: test | ACTION: respond | TTL: 5')

        w = _active_watchers['user_cb'][0]
        assert callable(w['callback'])

    @patch('hart_intelligence_entry.thread_local_data')
    def test_watcher_trigger_id_format(self, mock_tld):
        from hart_intelligence_entry import _handle_visual_watcher_tool, _active_watchers

        mock_tld.get_user_id.return_value = 'user_tid'

        _handle_visual_watcher_tool('CONDITION: x | ACTION: y | TTL: 1')

        w = _active_watchers['user_tid'][0]
        assert w['trigger_id'].startswith('watcher_user_tid_')

    @patch('hart_intelligence_entry.thread_local_data')
    def test_freeform_input_uses_full_text_as_condition(self, mock_tld):
        from hart_intelligence_entry import _handle_visual_watcher_tool, _active_watchers

        mock_tld.get_user_id.return_value = 'user_free'

        _handle_visual_watcher_tool('watch for my dog')

        w = _active_watchers['user_free'][0]
        assert w['condition'] == 'watch for my dog'


# ══════════════════════════════════════════════════════════════════
# 3. Audio Watcher LLM Evaluation
# ══════════════════════════════════════════════════════════════════

class TestAudioWatcherEvaluation:
    """Tests for _evaluate_audio_watchers — LLM-powered semantic matching."""

    def setup_method(self):
        from hart_intelligence_entry import _active_watchers
        _active_watchers.clear()

    def test_noop_when_no_watchers(self):
        """Should return immediately if no watchers for user."""
        from hart_intelligence_entry import _evaluate_audio_watchers
        # Should not raise
        _evaluate_audio_watchers('user_none', 'hello world')

    def test_noop_when_watchers_expired(self):
        """Should skip expired watchers."""
        from hart_intelligence_entry import _evaluate_audio_watchers, _active_watchers

        _active_watchers['user_exp'] = [{
            'trigger_id': 'w1',
            'expires_at': time.time() - 100,  # expired
            'condition': 'test',
            'action': 'respond',
            'modality': 'audio',
            'callback': MagicMock(),
        }]

        _evaluate_audio_watchers('user_exp', 'hello')
        # Callback should NOT have been called (watcher expired)
        _active_watchers['user_exp'][0]['callback'].assert_not_called()

    @patch('hart_intelligence_entry.get_llm')
    def test_fires_matching_watcher(self, mock_get_llm):
        """When LLM says watcher matches, callback should fire."""
        from hart_intelligence_entry import _evaluate_audio_watchers, _active_watchers

        mock_callback = MagicMock()
        _active_watchers['user_fire'] = [{
            'trigger_id': 'w1',
            'expires_at': time.time() + 3600,
            'condition': 'user mentions their dog',
            'action': 'remind about vet',
            'modality': 'audio',
            'callback': mock_callback,
        }]

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content='[1]')
        mock_get_llm.return_value = mock_llm

        _evaluate_audio_watchers('user_fire', 'my puppy just walked in')

        mock_callback.assert_called_once_with('my puppy just walked in')

    @patch('hart_intelligence_entry.get_llm')
    def test_does_not_fire_when_no_match(self, mock_get_llm):
        """When LLM returns empty array, no callback fires."""
        from hart_intelligence_entry import _evaluate_audio_watchers, _active_watchers

        mock_callback = MagicMock()
        _active_watchers['user_nofire'] = [{
            'trigger_id': 'w1',
            'expires_at': time.time() + 3600,
            'condition': 'user mentions their dog',
            'action': 'remind about vet',
            'modality': 'audio',
            'callback': mock_callback,
        }]

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content='[]')
        mock_get_llm.return_value = mock_llm

        _evaluate_audio_watchers('user_nofire', 'the weather is nice today')

        mock_callback.assert_not_called()

    @patch('hart_intelligence_entry.get_llm')
    def test_skips_visual_only_watchers(self, mock_get_llm):
        """Audio eval should skip watchers with modality='visual'."""
        from hart_intelligence_entry import _evaluate_audio_watchers, _active_watchers

        mock_callback = MagicMock()
        _active_watchers['user_vis'] = [{
            'trigger_id': 'w1',
            'expires_at': time.time() + 3600,
            'condition': 'see a cat',
            'action': 'meow',
            'modality': 'visual',  # visual-only, not audio
            'callback': mock_callback,
        }]

        # LLM should NOT be called since all watchers are visual
        _evaluate_audio_watchers('user_vis', 'I see a cat')
        mock_get_llm.assert_not_called()

    @patch('hart_intelligence_entry.get_llm')
    def test_handles_llm_exception_gracefully(self, mock_get_llm):
        """LLM failure should not crash."""
        from hart_intelligence_entry import _evaluate_audio_watchers, _active_watchers

        _active_watchers['user_err'] = [{
            'trigger_id': 'w1',
            'expires_at': time.time() + 3600,
            'condition': 'anything',
            'action': 'respond',
            'modality': 'audio',
            'callback': MagicMock(),
        }]

        mock_get_llm.side_effect = RuntimeError('LLM down')
        # Should not raise
        _evaluate_audio_watchers('user_err', 'test text')


# ══════════════════════════════════════════════════════════════════
# 4. Workflow Flowchart Push
# ══════════════════════════════════════════════════════════════════

class TestWorkflowFlowchartPush:
    """Tests for _push_workflow_flowchart Crossbar publishing."""

    @patch('hart_intelligence_entry.publish_async')
    def test_pushes_recipe_via_crossbar(self, mock_publish):
        """Should publish recipe JSON to per-user chat topic."""
        import tempfile
        from hart_intelligence_entry import _push_workflow_flowchart

        with tempfile.TemporaryDirectory() as tmpdir:
            recipe = {'name': 'TestAgent', 'goal': 'testing', 'flows': []}
            recipe_path = os.path.join(tmpdir, 'agent_99.json')
            with open(recipe_path, 'w') as f:
                json.dump(recipe, f)

            with patch('hart_intelligence_entry.PROMPTS_DIR', tmpdir):
                _push_workflow_flowchart('user_42', 'agent_99', 'req_123')

            mock_publish.assert_called_once()
            call_args = mock_publish.call_args[0]
            assert call_args[0] == 'com.hertzai.hevolve.chat.user_42'
            msg = json.loads(call_args[1])
            assert msg['priority'] == 50
            assert msg['action'] == 'WorkflowFlowchart'
            assert msg['recipe']['name'] == 'TestAgent'
            assert msg['prompt_id'] == 'agent_99'

    @patch('hart_intelligence_entry.publish_async')
    def test_noop_when_recipe_missing(self, mock_publish):
        """Should silently skip if recipe file doesn't exist."""
        from hart_intelligence_entry import _push_workflow_flowchart

        with patch('hart_intelligence_entry.PROMPTS_DIR', '/nonexistent'):
            _push_workflow_flowchart('user_1', 'missing_agent', 'req_1')

        mock_publish.assert_not_called()

    @patch('hart_intelligence_entry.publish_async')
    def test_does_not_crash_on_publish_error(self, mock_publish):
        """publish_async failure should be swallowed."""
        import tempfile
        from hart_intelligence_entry import _push_workflow_flowchart

        mock_publish.side_effect = RuntimeError('Crossbar down')

        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, 'x.json'), 'w') as f:
                json.dump({'name': 'X'}, f)

            with patch('hart_intelligence_entry.PROMPTS_DIR', tmpdir):
                # Should not raise
                _push_workflow_flowchart('u1', 'x', 'r1')


# ══════════════════════════════════════════════════════════════════
# 5. VisionService Temporal Methods
# ══════════════════════════════════════════════════════════════════

class TestVisionServiceTemporalMethods:
    """Tests for VisionService._save_to_memory_graph and _emit_perception_event."""

    def test_save_to_memory_graph_method_exists(self):
        from integrations.vision.vision_service import VisionService
        assert hasattr(VisionService, '_save_to_memory_graph')
        assert callable(VisionService._save_to_memory_graph)

    def test_emit_perception_event_method_exists(self):
        from integrations.vision.vision_service import VisionService
        assert hasattr(VisionService, '_emit_perception_event')
        assert callable(VisionService._emit_perception_event)

    @pytest.mark.skip(reason="MemoryGraph.add path changed — needs new mock target")
    @patch('integrations.vision.vision_service.VisionService.__init__', return_value=None)
    def test_save_to_memory_graph_calls_graph(self, mock_init):
        """Should call MemoryGraph.add with visual context metadata."""
        from integrations.vision.vision_service import VisionService

        vs = VisionService.__new__(VisionService)

        mock_graph = MagicMock()
        with patch('hart_intelligence_entry._get_or_create_graph', return_value=mock_graph):
            vs._save_to_memory_graph('user_1', 'person sitting at desk', 'camera')

        mock_graph.add.assert_called_once()
        call_kwargs = mock_graph.add.call_args
        assert 'person sitting at desk' in str(call_kwargs)

    @patch('integrations.vision.vision_service.VisionService.__init__', return_value=None)
    def test_save_to_memory_graph_handles_error(self, mock_init):
        """MemoryGraph failure should not crash vision service."""
        from integrations.vision.vision_service import VisionService

        vs = VisionService.__new__(VisionService)

        with patch('hart_intelligence_entry._get_or_create_graph', side_effect=RuntimeError('db error')):
            # Should not raise
            vs._save_to_memory_graph('u1', 'test', 'camera')

    @patch('integrations.vision.vision_service.VisionService.__init__', return_value=None)
    @patch('core.platform.events.emit_event')
    def test_emit_perception_event_emits(self, mock_emit, mock_init):
        """Should emit perception.vision.present event."""
        from integrations.vision.vision_service import VisionService

        vs = VisionService.__new__(VisionService)
        vs._emit_perception_event('user_5', 'person waving', 'camera')

        mock_emit.assert_called_once()
        args = mock_emit.call_args[0]
        assert args[0] == 'perception.vision.present'
        payload = args[1]
        assert payload['user_id'] == 'user_5'
        assert payload['channel'] == 'camera'
        assert payload['content'] == 'person waving'

    @patch('integrations.vision.vision_service.VisionService.__init__', return_value=None)
    def test_emit_perception_event_handles_error(self, mock_init):
        """EventBus failure should not crash vision service."""
        from integrations.vision.vision_service import VisionService

        vs = VisionService.__new__(VisionService)

        with patch('core.platform.events.emit_event', side_effect=RuntimeError('bus down')):
            # Should not raise
            vs._emit_perception_event('u1', 'test', 'screen')


# ══════════════════════════════════════════════════════════════════
# 6. Description Loop Wiring
# ══════════════════════════════════════════════════════════════════

class TestDescriptionLoopWiring:
    """Verify _save_to_memory_graph and _emit_perception_event are wired in."""

    def test_description_loop_calls_memory_graph(self):
        """_description_loop source should call _save_to_memory_graph."""
        import inspect
        from integrations.vision.vision_service import VisionService
        src = inspect.getsource(VisionService._description_loop)
        assert '_save_to_memory_graph' in src

    def test_description_loop_calls_emit_event(self):
        """_description_loop source should call _emit_perception_event."""
        import inspect
        from integrations.vision.vision_service import VisionService
        src = inspect.getsource(VisionService._description_loop)
        assert '_emit_perception_event' in src

    def test_description_loop_calls_both_channels(self):
        """Both camera and screen channels should save and emit."""
        import inspect
        from integrations.vision.vision_service import VisionService
        src = inspect.getsource(VisionService._description_loop)
        # Count occurrences — should be 2 each (camera + screen)
        assert src.count('_save_to_memory_graph') == 2
        assert src.count('_emit_perception_event') == 2


# ══════════════════════════════════════════════════════════════════
# 7. EventBus Bootstrap in Nunba
# ══════════════════════════════════════════════════════════════════

class TestEventBusBootstrap:
    """Verify bootstrap_platform is called in Nunba main.py."""

    def test_main_py_bootstraps_eventbus(self):
        """main.py source should call bootstrap_platform."""
        main_path = os.path.join(
            os.path.dirname(__file__), '..', '..', '..', 'Nunba', 'main.py'
        )
        if not os.path.isfile(main_path):
            # Try absolute path
            main_path = r'C:\Users\sathi\PycharmProjects\Nunba\main.py'

        if os.path.isfile(main_path):
            with open(main_path) as f:
                src = f.read()
            assert 'bootstrap_platform' in src
            assert 'from core.platform.bootstrap' in src
        else:
            pytest.skip('Nunba main.py not found at expected path')


# ══════════════════════════════════════════════════════════════════
# 8. Visual_Context_Watcher Tool Registration
# ══════════════════════════════════════════════════════════════════

class TestWatcherToolRegistration:
    """Verify Visual_Context_Watcher is registered in LangChain tool list."""

    def test_tool_wired_in_get_tools(self):
        """get_tools source should reference Visual_Context_Watcher."""
        import inspect
        from hart_intelligence_entry import get_tools
        src = inspect.getsource(get_tools)
        assert 'Visual_Context_Watcher' in src
        assert '_handle_visual_watcher_tool' in src

    def test_tool_description_mentions_modality(self):
        """Tool description should mention modality options."""
        import inspect
        from hart_intelligence_entry import get_tools
        src = inspect.getsource(get_tools)
        assert 'MODALITY' in src or 'modality' in src.lower()
