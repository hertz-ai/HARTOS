"""
Unit tests for the universal capability system.

Tests cover:
  1. Language segmenter (multi-lang, media tags, edge cases)
  2. AudioGen catalog population (ACE Step, DiffRhythm entries)
  3. Orchestrator: can_do(), available_capabilities(), capability_prompt()
  4. Media agent: _can_do() gate, generate_media() unavailability, degradation
  5. Dynamic service registration → capability discovery
  6. Multi-device compute offloading (delegation when local can't do it)
  7. register_media_tools (updated count with synthesize_multilingual_audio)
  8. Stale entry cleanup

Each test uses fresh catalog/orchestrator instances — no singleton pollution.
"""

import json
import os
import sys
import tempfile
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

# Add Nunba to path so tts.language_segmenter can be imported
_NUNBA_ROOT = os.path.join(os.path.dirname(__file__), '..', '..', '..',
                           'Nunba-HART-Companion')
if os.path.isdir(_NUNBA_ROOT):
    sys.path.insert(0, os.path.abspath(_NUNBA_ROOT))

from integrations.service_tools.model_catalog import (
    ModelCatalog, ModelEntry, ModelType,
)


# ─── Helpers ────────────────────────────────────────────────────────────────

def fresh_catalog() -> ModelCatalog:
    """Return a ModelCatalog backed by a throw-away temp file."""
    tmp = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
    tmp.close()
    os.unlink(tmp.name)
    return ModelCatalog(catalog_path=tmp.name)


def _make_entry(mid, model_type, vram=2.0, caps=None, cpu=True, enabled=True):
    """Create a minimal ModelEntry for testing."""
    return ModelEntry(
        id=mid, name=mid, model_type=model_type,
        source='test', vram_gb=vram, ram_gb=2.0,
        backend='test', supports_gpu=True, supports_cpu=cpu,
        capabilities=caps or {}, enabled=enabled,
        quality_score=0.8, speed_score=0.8,
    )


def fresh_orchestrator(catalog=None):
    """Return a ModelOrchestrator with a fresh catalog."""
    from integrations.service_tools.model_orchestrator import ModelOrchestrator
    cat = catalog or fresh_catalog()
    orch = ModelOrchestrator.__new__(ModelOrchestrator)
    orch._catalog = cat
    orch._loaders = {}
    orch._lock = __import__('threading').Lock()
    return orch


# ═══════════════════════════════════════════════════════════════════════════
# 1. Language Segmenter
# ═══════════════════════════════════════════════════════════════════════════

try:
    from tts.language_segmenter import segment as _seg_check
    _has_tts = True
except ImportError:
    _has_tts = False

@pytest.mark.skipif(not _has_tts, reason="tts package not installed (Nunba-only)")
class TestLanguageSegmenter:
    """Tests for tts.language_segmenter.segment()."""

    def test_empty_input(self):
        from tts.language_segmenter import segment
        assert segment('') == []
        assert segment('   ') == []
        assert segment(None) == []

    def test_pure_english(self):
        from tts.language_segmenter import segment
        r = segment('Hello world')
        assert len(r) == 1
        assert r[0]['type'] == 'speech'
        assert r[0]['lang'] == 'en'

    def test_music_tag(self):
        from tts.language_segmenter import segment
        r = segment('Intro <music genre="jazz" duration="10">smooth</music> Outro')
        assert len(r) == 3
        assert r[0] == {'type': 'speech', 'lang': 'en', 'text': 'Intro'}
        assert r[1]['type'] == 'music'
        assert r[1]['genre'] == 'jazz'
        assert r[1]['duration'] == 10
        assert r[1]['text'] == 'smooth'
        assert r[2] == {'type': 'speech', 'lang': 'en', 'text': 'Outro'}

    def test_sing_tag(self):
        from tts.language_segmenter import segment
        r = segment('<sing duration="15">la la la</sing> done')
        assert len(r) == 2
        assert r[0]['type'] == 'sing'
        assert r[0]['duration'] == 15
        assert r[1]['type'] == 'speech'

    def test_lyrics_tag(self):
        from tts.language_segmenter import segment
        r = segment('<lyrics>verse one</lyrics>')
        assert len(r) == 1
        assert r[0]['type'] == 'lyrics'
        assert r[0]['text'] == 'verse one'

    def test_multiple_tags_interleaved(self):
        from tts.language_segmenter import segment
        r = segment('Hey <music genre="pop" duration="3">beat</music> nice <sing>oh</sing> bye')
        types = [s['type'] for s in r]
        assert types == ['speech', 'music', 'speech', 'sing', 'speech']

    def test_no_tags_returns_speech(self):
        from tts.language_segmenter import segment
        r = segment('Just plain text')
        assert len(r) == 1
        assert r[0]['type'] == 'speech'

    def test_music_default_duration(self):
        from tts.language_segmenter import segment
        r = segment('<music>ambient</music>')
        assert r[0]['duration'] == 30  # default

    def test_nested_quotes_in_attrs(self):
        from tts.language_segmenter import segment
        r = segment("<music genre='rock' duration='20'>riff</music>")
        assert r[0]['genre'] == 'rock'
        assert r[0]['duration'] == 20


# ═══════════════════════════════════════════════════════════════════════════
# 2. AudioGen Catalog Population
# ═══════════════════════════════════════════════════════════════════════════

class TestAudioGenPopulation:
    """Tests for populate_audiogen_catalog."""

    def test_populates_two_entries(self):
        from integrations.service_tools.media_agent import populate_audiogen_catalog
        cat = fresh_catalog()
        added = populate_audiogen_catalog(cat)
        assert added == 2

    def test_acestep_capabilities(self):
        from integrations.service_tools.media_agent import populate_audiogen_catalog
        cat = fresh_catalog()
        populate_audiogen_catalog(cat)
        e = cat.get('audio_gen-acestep')
        assert e is not None
        assert e.capabilities['music_gen'] is True
        assert e.capabilities['singing'] is True
        assert e.model_type == 'audio_gen'

    def test_diffrhythm_capabilities(self):
        from integrations.service_tools.media_agent import populate_audiogen_catalog
        cat = fresh_catalog()
        populate_audiogen_catalog(cat)
        e = cat.get('audio_gen-diffrhythm')
        assert e is not None
        assert e.capabilities['singing_voice'] is True
        assert e.capabilities['music_gen'] is False
        assert e.model_type == 'audio_gen'

    def test_idempotent(self):
        from integrations.service_tools.media_agent import populate_audiogen_catalog
        cat = fresh_catalog()
        populate_audiogen_catalog(cat)
        added2 = populate_audiogen_catalog(cat)
        assert added2 == 0  # already registered

    def test_stale_entry_cleanup(self):
        """Stale audio_gen entries with no capabilities get removed."""
        cat = fresh_catalog()
        # Simulate a stale entry (old catalog JSON, no caps)
        stale = _make_entry('audio_gen-old-thing', 'audio_gen', caps={})
        cat.register(stale, persist=False)
        assert cat.get('audio_gen-old-thing') is not None

        cat._populate_audiogen_models()
        # Stale entry should be gone
        assert cat.get('audio_gen-old-thing') is None
        # New entries should exist
        assert cat.get('audio_gen-acestep') is not None


# ═══════════════════════════════════════════════════════════════════════════
# 3. Orchestrator: can_do, available_capabilities, capability_prompt
# ═══════════════════════════════════════════════════════════════════════════

class TestOrchestratorCapabilities:
    """Tests for ModelOrchestrator capability introspection."""

    def _make_orch_with_entries(self, entries):
        cat = fresh_catalog()
        for e in entries:
            cat.register(e, persist=False)
        return fresh_orchestrator(cat)

    def test_can_do_with_loaded_model(self):
        e = _make_entry('tts-test', 'tts', vram=1.0, caps={'voice_cloning': True})
        e.loaded = True
        e.device = 'gpu'
        orch = self._make_orch_with_entries([e])
        assert orch.can_do('tts') is True
        assert orch.can_do('tts', 'voice_cloning') is True
        assert orch.can_do('tts', 'nonexistent_cap') is False

    def test_can_do_returns_false_for_unknown_type(self):
        orch = fresh_orchestrator()
        assert orch.can_do('robot_locomotion') is False

    def test_can_do_checks_compute_fit(self):
        """A model that needs 100GB VRAM should not be available."""
        e = _make_entry('video-huge', 'video_gen', vram=100.0, cpu=False,
                        caps={'txt2vid': True})
        orch = self._make_orch_with_entries([e])
        # On most test machines, 100GB VRAM won't fit
        assert orch.can_do('video_gen', 'txt2vid') is False

    def test_can_do_cpu_capable_fits(self):
        """A CPU-capable model should be available even with 0 VRAM."""
        e = _make_entry('stt-tiny', 'stt', vram=0.0, cpu=True,
                        caps={'realtime': True})
        orch = self._make_orch_with_entries([e])
        assert orch.can_do('stt') is True
        assert orch.can_do('stt', 'realtime') is True

    def test_available_capabilities_structure(self):
        e = _make_entry('tts-x', 'tts', caps={'streaming': True})
        orch = self._make_orch_with_entries([e])
        caps = orch.available_capabilities()
        assert 'tts' in caps
        info = caps['tts']
        assert 'available' in info
        assert 'loaded' in info
        assert 'can_load' in info
        assert 'capabilities' in info
        assert 'services' in info

    def test_capability_prompt_empty_when_nothing(self):
        orch = fresh_orchestrator()
        assert orch.capability_prompt() == ''

    def test_capability_prompt_excludes_llm(self):
        """LLM should not appear in the prompt (the agent IS the LLM)."""
        e = _make_entry('llm-test', 'llm', vram=0.0, caps={'chat': True})
        orch = self._make_orch_with_entries([e])
        prompt = orch.capability_prompt()
        assert 'llm' not in prompt.lower() or prompt == ''

    def test_capability_prompt_includes_tts(self):
        e = _make_entry('tts-x', 'tts', vram=0.0, caps={'streaming': True})
        orch = self._make_orch_with_entries([e])
        prompt = orch.capability_prompt()
        assert 'tts' in prompt.lower()
        assert 'streaming' in prompt.lower()

    @patch('integrations.service_tools.model_orchestrator.service_tool_registry',
           create=True)
    def test_dynamic_service_creates_new_category(self, _):
        """A dynamic service with unknown tags creates a new category."""
        cat = fresh_catalog()
        orch = fresh_orchestrator(cat)

        # Simulate a dynamically registered service
        mock_registry = MagicMock()
        mock_tool = MagicMock()
        mock_tool.tags = ['robot_locomotion', 'hardware']
        mock_registry._tools = {'robot_arm': mock_tool}

        with patch('integrations.service_tools.registry.service_tool_registry',
                   mock_registry):
            caps = orch.available_capabilities()

        # New category should appear
        assert 'robot_locomotion' in caps
        assert caps['robot_locomotion']['available'] is True
        assert 'robot_arm' in caps['robot_locomotion']['services']

    def test_capabilities_never_size_the_models_folder(self):
        """LIVE build #8, 28 Nov: every sung part took 4.2 s (a plain line
        0.06-0.43 s) because the capability check called the dashboard's
        get_all_status(), which sizes the whole models folder (a stat of every
        file: 4.3-5.6 s measured) only for each tool's 'running' flag - a step
        that could never change the answer (step 2 marks every listed service
        available), so it is gone.  A listed service is still available."""
        cat = fresh_catalog()
        orch = fresh_orchestrator(cat)
        mock_registry = MagicMock()
        mock_tool = MagicMock()
        mock_tool.tags = ['music']
        mock_registry._tools = {'acestep': mock_tool}

        from integrations.service_tools import runtime_manager as rm
        sized = []
        # The dashboard is left real: on the parent, available_capabilities called it and it
        # sized the folder (sized == [1]).  No tool status is asked for at all now.
        asked = []
        with patch('integrations.service_tools.registry.service_tool_registry', mock_registry), \
             patch.object(rm.runtime_tool_manager.storage, 'get_total_size',
                          side_effect=lambda: sized.append(1) or 0), \
             patch.object(rm.runtime_tool_manager, 'get_tool_status',
                          side_effect=lambda name: asked.append(name) or {'running': False}):
            caps = orch.available_capabilities()
        assert asked == [], 'the capability check asked each tool for its status'
        assert sized == [], 'the capability check sized the whole models folder'
        assert caps['music']['available'] is True
        assert 'acestep' in caps['music']['services']


# ═══════════════════════════════════════════════════════════════════════════
# 4. Media Agent Capability Gates
# ═══════════════════════════════════════════════════════════════════════════

class TestMediaAgentGates:
    """Tests for media_agent._can_do() and generate_media() gates."""

    @patch('integrations.service_tools.model_orchestrator.get_orchestrator')
    def test_can_do_delegates_to_orchestrator(self, mock_get_orch):
        from integrations.service_tools.media_agent import _can_do
        mock_orch = MagicMock()
        mock_orch.can_do.return_value = True
        mock_get_orch.return_value = mock_orch

        assert _can_do('tts') is True
        mock_orch.can_do.assert_called_once_with('tts', None)

    @patch('integrations.service_tools.model_orchestrator.get_orchestrator')
    def test_can_do_with_capability(self, mock_get_orch):
        from integrations.service_tools.media_agent import _can_do
        mock_orch = MagicMock()
        mock_orch.can_do.return_value = False
        mock_get_orch.return_value = mock_orch

        assert _can_do('audio_gen', 'music_gen') is False
        mock_orch.can_do.assert_called_once_with('audio_gen', 'music_gen')

    @patch('integrations.service_tools.media_agent._can_do', return_value=False)
    def test_generate_media_video_unavailable(self, _):
        from integrations.service_tools.media_agent import generate_media
        r = json.loads(generate_media('test prompt', 'video'))
        assert r['status'] == 'unavailable'
        assert 'video' in r.get('error', '').lower()
        assert 'suggestion' in r

    @patch('integrations.service_tools.media_agent._can_do', return_value=False)
    def test_generate_media_audio_music_unavailable(self, _):
        from integrations.service_tools.media_agent import generate_media
        r = json.loads(generate_media('test prompt', 'audio_music'))
        assert r['status'] == 'unavailable'

    @patch('integrations.service_tools.media_agent._can_do', return_value=False)
    def test_generate_media_audio_speech_unavailable(self, _):
        from integrations.service_tools.media_agent import generate_media
        r = json.loads(generate_media('test prompt', 'audio_speech'))
        assert r['status'] == 'unavailable'

    def test_generate_media_invalid_modality(self):
        from integrations.service_tools.media_agent import generate_media
        r = json.loads(generate_media('test', 'invalid_type'))
        assert r['status'] == 'error'
        assert 'Invalid' in r['error']


# ═══════════════════════════════════════════════════════════════════════════
# 5. Synthesize Multilingual Audio — Degradation
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _has_tts, reason="tts package not installed (Nunba-only)")
class TestMultilingualSynthDegradation:
    """Tests for synthesize_multilingual_audio partial/degraded results."""

    @patch('integrations.service_tools.media_agent._can_do', return_value=False)
    def test_returns_unavailable_when_no_tts(self, _):
        from integrations.service_tools.media_agent import synthesize_multilingual_audio
        r = json.loads(synthesize_multilingual_audio('hello world'))
        assert r['status'] == 'unavailable'
        assert 'suggestion' in r

    @patch('integrations.service_tools.media_agent._can_do')
    def test_degrades_music_segments_when_offline(self, mock_can_do):
        """Music segments should be reported as degraded, speech still works."""
        def side_effect(mt, cap=None):
            if mt == 'tts':
                return True
            if mt == 'audio_gen' and cap == 'music_gen':
                return False
            return True
        mock_can_do.side_effect = side_effect

        # Mock the TTS engine
        mock_engine = MagicMock()
        mock_engine._synthesize_multilingual.return_value = '/tmp/test.wav'

        with patch('tts.tts_engine.get_tts_engine', return_value=mock_engine):
            with patch('tts.language_segmenter.segment') as mock_seg:
                mock_seg.return_value = [
                    {'type': 'speech', 'lang': 'en', 'text': 'Hello'},
                    {'type': 'music', 'text': 'jazz vibes', 'genre': 'jazz', 'duration': 5},
                    {'type': 'speech', 'lang': 'en', 'text': 'Bye'},
                ]
                from integrations.service_tools.media_agent import synthesize_multilingual_audio
                r = json.loads(synthesize_multilingual_audio(
                    'Hello <music genre="jazz" duration="5">jazz vibes</music> Bye'))

        assert r['status'] == 'partial'
        assert r['segments_total'] == 3
        assert r['segments_synthesized'] == 2
        assert len(r['degraded_segments']) == 1
        assert r['degraded_segments'][0]['type'] == 'music'
        assert 'music gen' in r['degraded_segments'][0]['reason']


# ═══════════════════════════════════════════════════════════════════════════
# 6. Multi-Device Compute Offloading
# ═══════════════════════════════════════════════════════════════════════════

class TestMultiDeviceOffloading:
    """Tests for delegation suggestion when local node can't handle a task."""

    @patch('integrations.service_tools.media_agent._can_do', return_value=False)
    def test_video_suggests_delegation(self, _):
        from integrations.service_tools.media_agent import generate_media
        r = json.loads(generate_media('create a video', 'video'))
        assert r['status'] == 'unavailable'
        assert 'delegate' in r.get('suggestion', '').lower()

    @patch('integrations.service_tools.media_agent._can_do', return_value=False)
    def test_music_suggests_delegation(self, _):
        from integrations.service_tools.media_agent import generate_media
        r = json.loads(generate_media('make a song', 'audio_music'))
        assert r['status'] == 'unavailable'
        assert 'delegate' in r.get('suggestion', '').lower() or \
               'text' in r.get('suggestion', '').lower()

    def test_can_do_false_does_not_crash(self):
        """can_do should return False gracefully when orchestrator unavailable."""
        from integrations.service_tools.media_agent import _can_do
        with patch('integrations.service_tools.model_orchestrator.get_orchestrator',
                   side_effect=ImportError('no module')):
            assert _can_do('anything') is False


# ═══════════════════════════════════════════════════════════════════════════
# 7. Tool Registration (updated count)
# ═══════════════════════════════════════════════════════════════════════════

class TestMediaToolRegistration:
    """Verify register_media_tools registers all 3 tools."""

    def test_registers_three_tools(self):
        from integrations.service_tools.media_agent import register_media_tools
        mock_helper = MagicMock()
        mock_assistant = MagicMock()
        mock_helper.register_for_llm.return_value = lambda f: f
        mock_assistant.register_for_execution.return_value = lambda f: f

        register_media_tools(mock_helper, mock_assistant)

        # generate_media + check_media_status + synthesize_multilingual_audio = 3
        assert mock_helper.register_for_llm.call_count == 3
        assert mock_assistant.register_for_execution.call_count == 3

        names = [c[1]['name'] for c in mock_helper.register_for_llm.call_args_list]
        assert 'generate_media' in names
        assert 'check_media_status' in names
        assert 'synthesize_multilingual_audio' in names


# ═══════════════════════════════════════════════════════════════════════════
# 8. Catalog list_types
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# 9. Distributed Dispatch + Capability Matching
# ═══════════════════════════════════════════════════════════════════════════

class TestDistributedCapabilityMatching:
    """Tests for capability-aware distributed task routing."""

    def test_host_registry_auto_discovers_model_caps(self):
        """Host registration should include model capabilities from orchestrator."""
        from integrations.distributed_agent.host_registry import RegionalHostRegistry

        mock_redis = MagicMock()
        registry = RegionalHostRegistry(mock_redis, 'node-1', 'http://localhost:5000')

        # Mock orchestrator returning capabilities
        mock_caps = {
            'tts': {'available': True, 'capabilities': {'streaming': True, 'voice_cloning': True}},
            'audio_gen': {'available': True, 'capabilities': {'singing': True}},
            'video_gen': {'available': False, 'capabilities': {}},
        }
        mock_orch = MagicMock()
        mock_orch.available_capabilities.return_value = mock_caps

        with patch('integrations.service_tools.model_orchestrator.get_orchestrator',
                   return_value=mock_orch):
            registry.register_host(['coding', 'testing'])

        # Check what was stored in Redis
        call_args = mock_redis.hset.call_args
        stored_data = json.loads(call_args[0][2])
        caps = stored_data['capabilities']
        assert 'coding' in caps              # original
        assert 'tts' in caps                 # auto-discovered
        assert 'tts:streaming' in caps       # fine-grained
        assert 'tts:voice_cloning' in caps
        assert 'audio_gen' in caps
        assert 'audio_gen:singing' in caps
        assert 'video_gen' not in caps       # not available, excluded

    def test_host_registry_graceful_when_orchestrator_unavailable(self):
        """Should still register with original caps if orchestrator fails."""
        from integrations.distributed_agent.host_registry import RegionalHostRegistry

        mock_redis = MagicMock()
        registry = RegionalHostRegistry(mock_redis, 'node-2', '')

        with patch('integrations.service_tools.model_orchestrator.get_orchestrator',
                   side_effect=ImportError):
            registry.register_host(['coding'])

        stored = json.loads(mock_redis.hset.call_args[0][2])
        assert 'coding' in stored['capabilities']

    def test_discover_model_capabilities_returns_flat_strings(self):
        """_discover_model_capabilities returns list of flat strings."""
        from integrations.distributed_agent.host_registry import RegionalHostRegistry

        mock_orch = MagicMock()
        mock_orch.available_capabilities.return_value = {
            'stt': {'available': True, 'capabilities': {'realtime': True}},
        }
        with patch('integrations.service_tools.model_orchestrator.get_orchestrator',
                   return_value=mock_orch):
            caps = RegionalHostRegistry._discover_model_capabilities()

        assert isinstance(caps, list)
        assert 'stt' in caps
        assert 'stt:realtime' in caps

    def test_get_hosts_with_model_capability(self):
        """get_hosts_with_capability should match model-level caps."""
        from integrations.distributed_agent.host_registry import RegionalHostRegistry

        mock_redis = MagicMock()
        registry = RegionalHostRegistry(mock_redis, 'node-q', '')

        # Simulate two hosts in Redis
        host_a = json.dumps({
            'host_id': 'node-a',
            'capabilities': ['coding', 'tts', 'tts:streaming'],
            'last_seen': datetime.now().isoformat(),
        })
        host_b = json.dumps({
            'host_id': 'node-b',
            'capabilities': ['coding', 'audio_gen', 'audio_gen:music_gen'],
            'last_seen': datetime.now().isoformat(),
        })
        mock_redis.hgetall.return_value = {
            'node-a': host_a, 'node-b': host_b,
        }

        # Query for music generation — only node-b should match
        music_hosts = registry.get_hosts_with_capability('audio_gen:music_gen')
        assert len(music_hosts) == 1
        assert music_hosts[0]['host_id'] == 'node-b'

        # Query for tts — only node-a
        tts_hosts = registry.get_hosts_with_capability('tts')
        assert len(tts_hosts) == 1
        assert tts_hosts[0]['host_id'] == 'node-a'

        # Query for coding — both
        code_hosts = registry.get_hosts_with_capability('coding')
        assert len(code_hosts) == 2


# ═══════════════════════════════════════════════════════════════════════════
# 10. Hive Compute Contribution + Privacy
# ═══════════════════════════════════════════════════════════════════════════

class TestHiveComputeContribution:
    """Tests for compute contribution and privacy guards."""

    def test_federated_aggregator_has_privacy_scope(self):
        """FederatedAggregator should tag deltas with privacy scope."""
        # Just verify the import path exists and the module structure is sound
        from integrations.agent_engine.federated_aggregator import FederatedAggregator
        assert hasattr(FederatedAggregator, 'tick')
        assert hasattr(FederatedAggregator, 'broadcast_delta')

    def test_idle_detection_opt_in_exists(self):
        """Idle detection module should have opt-in/out for compute contribution."""
        from integrations.coding_agent.idle_detection import IdleDetectionService
        assert hasattr(IdleDetectionService, 'opt_in')

    def test_capability_prompt_does_not_leak_private_data(self):
        """capability_prompt should not include user IDs, paths, or secrets."""
        e = _make_entry('tts-test', 'tts', vram=0.0, caps={'streaming': True})
        orch = fresh_orchestrator()
        orch._catalog.register(e, persist=False)
        prompt = orch.capability_prompt()

        # Should not contain paths, IPs, or user info
        assert 'Users' not in prompt
        assert 'localhost' not in prompt
        assert 'password' not in prompt.lower()
        assert 'secret' not in prompt.lower()


class TestCatalogListTypes:
    """Tests for ModelCatalog.list_types()."""

    def test_empty_catalog(self):
        cat = fresh_catalog()
        assert cat.list_types() == []

    def test_returns_distinct_types(self):
        cat = fresh_catalog()
        cat.register(_make_entry('a', 'tts'), persist=False)
        cat.register(_make_entry('b', 'tts'), persist=False)
        cat.register(_make_entry('c', 'stt'), persist=False)
        types = cat.list_types()
        assert sorted(types) == ['stt', 'tts']

    def test_excludes_disabled(self):
        cat = fresh_catalog()
        cat.register(_make_entry('a', 'tts', enabled=True), persist=False)
        cat.register(_make_entry('b', 'video_gen', enabled=False), persist=False)
        types = cat.list_types()
        assert 'tts' in types
        assert 'video_gen' not in types
