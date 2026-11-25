"""Tests for GPU TTS tool modules (chatterbox, cosyvoice, indic_parler, f5_tts).

These tools were migrated to the subprocess-isolation pattern
(see integrations.service_tools.gpu_worker). The actual model runs
inside a dedicated worker process spawned on first synthesize() call,
so these tests focus on the parent-side contract:

  * Empty / whitespace input is validated synchronously, with no
    subprocess spawn — it must return {"error": "Text is required"}.
  * The return value is always a JSON string with an expected shape.
  * ServiceToolInfo registration is unchanged from the pre-refactor API.
  * unload() is callable and idempotent (even if the worker never started).
  * Each module exposes a ToolWorker instance wired to the correct
    worker module path so the parent can spawn it.
  * The ToolWorker instance is the single source of truth for the
    subprocess lifecycle (no leftover in-process globals).

Crash-isolation behavior itself is covered in test_gpu_worker.py using
a no-GPU echo worker. These tests do NOT spawn the real GPU workers
because they'd need f5_tts / chatterbox / cosyvoice / parler_tts pip
packages installed, which CI doesn't have.
"""
import json
import os
import sys

import pytest

# Make HARTOS importable
_HARTOS_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _HARTOS_ROOT not in sys.path:
    sys.path.insert(0, _HARTOS_ROOT)


# ═══════════════════════════════════════════════════════════════
# Chatterbox Turbo (English, 3.8GB VRAM, worker variant='turbo')
# ═══════════════════════════════════════════════════════════════

class TestChatterboxSynthesize:

    def test_empty_text_returns_error(self):
        from integrations.service_tools.chatterbox_tool import chatterbox_synthesize
        result = json.loads(chatterbox_synthesize(""))
        assert "error" in result
        assert "Text is required" in result["error"]

    def test_whitespace_returns_error(self):
        from integrations.service_tools.chatterbox_tool import chatterbox_synthesize
        result = json.loads(chatterbox_synthesize("   "))
        assert "error" in result

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

    def test_tool_worker_is_present(self):
        """Parent-side ToolWorker singleton must exist and be wired to
        the correct worker module path with the 'turbo' variant arg."""
        from integrations.service_tools.chatterbox_tool import _turbo
        assert _turbo.tool_name == 'chatterbox_turbo'
        assert _turbo.worker_module == 'integrations.service_tools.chatterbox_tool'
        assert _turbo.worker_args == ['turbo']
        assert _turbo.vram_budget == 'tts_chatterbox_turbo'


class TestChatterboxMLSynthesize:

    def test_empty_text_returns_error(self):
        from integrations.service_tools.chatterbox_tool import chatterbox_ml_synthesize
        result = json.loads(chatterbox_ml_synthesize(""))
        assert "error" in result

    def test_tool_worker_variant_is_ml(self):
        from integrations.service_tools.chatterbox_tool import _ml
        assert _ml.tool_name == 'chatterbox_ml'
        assert _ml.worker_args == ['ml']
        assert _ml.vram_budget == 'tts_chatterbox_ml'


class TestChatterboxUnload:

    def test_unload_is_idempotent(self):
        """unload_chatterbox() must be safe to call when no worker has
        ever started (the workers are lazy — subprocesses only spawn on
        first synthesize call)."""
        from integrations.service_tools.chatterbox_tool import unload_chatterbox
        # Should not raise regardless of internal state
        unload_chatterbox()
        unload_chatterbox()  # idempotent — second call still OK


# ═══════════════════════════════════════════════════════════════
# CosyVoice 3 (9 languages, 3.5GB VRAM)
# ═══════════════════════════════════════════════════════════════

class TestCosyVoiceSynthesize:

    def test_empty_text_returns_error(self):
        from integrations.service_tools.cosyvoice_tool import cosyvoice_synthesize
        result = json.loads(cosyvoice_synthesize(""))
        assert "error" in result

    def test_registration(self):
        from integrations.service_tools.cosyvoice_tool import CosyVoiceTool
        from integrations.service_tools.registry import service_tool_registry
        result = CosyVoiceTool.register_functions()
        assert result is True
        assert 'cosyvoice' in service_tool_registry._tools
        tool = service_tool_registry._tools['cosyvoice']
        assert 'synthesize' in tool.endpoints
        assert 'multilingual' in tool.tags

    def test_tool_worker_is_present(self):
        from integrations.service_tools.cosyvoice_tool import _tool
        assert _tool.tool_name == 'cosyvoice3'
        assert _tool.worker_module == 'integrations.service_tools.cosyvoice_tool'
        assert _tool.vram_budget == 'tts_cosyvoice3'


class TestCosyVoiceUnload:

    def test_unload_is_idempotent(self):
        from integrations.service_tools.cosyvoice_tool import unload_cosyvoice
        unload_cosyvoice()
        unload_cosyvoice()


# ═══════════════════════════════════════════════════════════════
# Indic Parler TTS (22 Indian languages + English, 1.8GB VRAM)
# ═══════════════════════════════════════════════════════════════

class TestIndicParlerSynthesize:

    def test_empty_text_returns_error(self):
        from integrations.service_tools.indic_parler_tool import indic_parler_synthesize
        result = json.loads(indic_parler_synthesize(""))
        assert "error" in result

    def test_registration(self):
        from integrations.service_tools.indic_parler_tool import IndicParlerTool
        from integrations.service_tools.registry import service_tool_registry
        result = IndicParlerTool.register_functions()
        assert result is True
        assert 'indic_parler' in service_tool_registry._tools
        tool = service_tool_registry._tools['indic_parler']
        assert 'synthesize' in tool.endpoints
        assert 'indic' in tool.tags

    def test_tool_worker_is_present(self):
        from integrations.service_tools.indic_parler_tool import _tool
        assert _tool.tool_name == 'indic_parler'
        assert _tool.worker_module == 'integrations.service_tools.indic_parler_tool'
        assert _tool.vram_budget == 'tts_indic_parler'


class TestIndicParlerUnload:

    def test_unload_is_idempotent(self):
        from integrations.service_tools.indic_parler_tool import unload_indic_parler
        unload_indic_parler()
        unload_indic_parler()


# ═══════════════════════════════════════════════════════════════
# F5-TTS (English + Chinese, 1.3GB VRAM)
# ═══════════════════════════════════════════════════════════════

class TestF5Synthesize:

    def test_empty_text_returns_error(self):
        from integrations.service_tools.f5_tts_tool import f5_synthesize
        result = json.loads(f5_synthesize(""))
        assert "error" in result

    def test_registration(self):
        from integrations.service_tools.f5_tts_tool import F5TTSTool
        from integrations.service_tools.registry import service_tool_registry
        result = F5TTSTool.register_functions()
        assert result is True
        assert 'f5_tts' in service_tool_registry._tools
        tool = service_tool_registry._tools['f5_tts']
        assert 'synthesize' in tool.endpoints
        assert 'voice-cloning' in tool.tags

    def test_tool_worker_is_present(self):
        from integrations.service_tools.f5_tts_tool import _tool
        assert _tool.tool_name == 'f5_tts'
        assert _tool.worker_module == 'integrations.service_tools.f5_tts_tool'
        assert _tool.vram_budget == 'tts_f5'


class TestF5Unload:

    def test_unload_is_idempotent(self):
        from integrations.service_tools.f5_tts_tool import unload_f5_tts
        unload_f5_tts()
        unload_f5_tts()


# ═══════════════════════════════════════════════════════════════
# Kokoro 82M (English, CPU/GPU, ~200MB)
# ═══════════════════════════════════════════════════════════════

class TestKokoroSynthesize:

    def test_empty_text_returns_error(self):
        from integrations.service_tools.kokoro_tool import kokoro_synthesize
        result = json.loads(kokoro_synthesize(""))
        assert "error" in result

    def test_registration(self):
        from integrations.service_tools.kokoro_tool import KokoroTool
        from integrations.service_tools.registry import service_tool_registry
        result = KokoroTool.register_functions()
        assert result is True
        assert 'kokoro' in service_tool_registry._tools
        tool = service_tool_registry._tools['kokoro']
        assert 'synthesize' in tool.endpoints
        assert 'kokoro' in tool.tags

    def test_tool_worker_is_present(self):
        from integrations.service_tools.kokoro_tool import _tool
        assert _tool.tool_name == 'kokoro'
        assert _tool.worker_module == 'integrations.service_tools.kokoro_tool'
        assert _tool.vram_budget == 'tts_kokoro'


class TestKokoroUnload:

    def test_unload_is_idempotent(self):
        """unload_kokoro() must be safe to call when no worker has ever
        started — matches the idempotency guarantee of the other four
        engines so the admin Model Management unload path treats them
        uniformly. Regression guard: before this commit there was no
        test for Kokoro's unload path while every other engine had one."""
        from integrations.service_tools.kokoro_tool import unload_kokoro
        unload_kokoro()
        unload_kokoro()


# ═══════════════════════════════════════════════════════════════
# Cross-tool consistency
# ═══════════════════════════════════════════════════════════════

class TestToolConsistency:
    """All GPU tool modules follow the same subprocess-isolation pattern."""

    def test_all_have_unload(self):
        from integrations.service_tools import chatterbox_tool, cosyvoice_tool
        from integrations.service_tools import indic_parler_tool, f5_tts_tool
        from integrations.service_tools import kokoro_tool
        assert callable(chatterbox_tool.unload_chatterbox)
        assert callable(cosyvoice_tool.unload_cosyvoice)
        assert callable(indic_parler_tool.unload_indic_parler)
        assert callable(f5_tts_tool.unload_f5_tts)
        assert callable(kokoro_tool.unload_kokoro)

    def test_all_return_json_strings_on_empty_input(self):
        """Empty text must be rejected synchronously (no subprocess spawn)
        with a JSON error string."""
        from integrations.service_tools.chatterbox_tool import (
            chatterbox_synthesize, chatterbox_ml_synthesize,
        )
        from integrations.service_tools.cosyvoice_tool import cosyvoice_synthesize
        from integrations.service_tools.indic_parler_tool import indic_parler_synthesize
        from integrations.service_tools.f5_tts_tool import f5_synthesize
        from integrations.service_tools.kokoro_tool import kokoro_synthesize

        fns = [
            chatterbox_synthesize,
            chatterbox_ml_synthesize,
            cosyvoice_synthesize,
            indic_parler_synthesize,
            f5_synthesize,
            kokoro_synthesize,
        ]
        for fn in fns:
            result = fn("")
            assert isinstance(result, str), f'{fn.__name__} did not return str'
            parsed = json.loads(result)
            assert "error" in parsed, f'{fn.__name__} did not return error JSON'

    def test_all_tool_modules_are_their_own_worker_entry(self):
        """Every migrated tool module uses ITSELF as the worker entry
        (DRY: no separate *_worker.py file). worker_module must match
        the tool module's own __name__."""
        from integrations.service_tools.chatterbox_tool import _turbo, _ml
        from integrations.service_tools.cosyvoice_tool import _tool as _cosy_tool
        from integrations.service_tools.indic_parler_tool import _tool as _parler_tool
        from integrations.service_tools.f5_tts_tool import _tool as _f5_tool
        from integrations.service_tools.kokoro_tool import _tool as _kokoro_tool

        assert _turbo.worker_module.endswith('chatterbox_tool')
        assert _ml.worker_module.endswith('chatterbox_tool')
        assert _cosy_tool.worker_module.endswith('cosyvoice_tool')
        assert _parler_tool.worker_module.endswith('indic_parler_tool')
        assert _f5_tool.worker_module.endswith('f5_tts_tool')
        assert _kokoro_tool.worker_module.endswith('kokoro_tool')

    def test_none_alive_before_first_call(self):
        """Importing a tool module must NOT spawn any subprocesses —
        workers are lazy-started only when a synthesize call actually
        needs the GPU."""
        from integrations.service_tools.chatterbox_tool import _turbo, _ml
        from integrations.service_tools.cosyvoice_tool import _tool as _cosy_tool
        from integrations.service_tools.indic_parler_tool import _tool as _parler_tool
        from integrations.service_tools.f5_tts_tool import _tool as _f5_tool
        from integrations.service_tools.kokoro_tool import _tool as _kokoro_tool

        # Ensure teardown from other tests didn't leave workers alive
        for t in (_turbo, _ml, _cosy_tool, _parler_tool, _f5_tool, _kokoro_tool):
            t.stop()

        assert not _turbo.is_alive()
        assert not _ml.is_alive()
        assert not _cosy_tool.is_alive()
        assert not _parler_tool.is_alive()
        assert not _f5_tool.is_alive()
        assert not _kokoro_tool.is_alive()


# ═══════════════════════════════════════════════════════════════
# Phonetic transcriber (design 1125 C2): its own worker, fp32 only
# ═══════════════════════════════════════════════════════════════

class TestPhoneticTool:
    """The phonetic model's worker with a fake model: WavLMForCTC and
    Wav2Vec2Processor load in fp32 on the device the parent left visible, and
    the 'phones' op turns a 16 kHz mono clip into IPA.  No weights are read."""

    def _fake_transformers(self, monkeypatch, seen):
        import types
        import torch

        class _Processor:
            @classmethod
            def from_pretrained(cls, path):
                seen.append(('processor', path))
                return cls()

            def __call__(self, samples, sampling_rate, return_tensors):
                seen.append(('clip', len(samples), sampling_rate, return_tensors))
                return types.SimpleNamespace(input_values=torch.zeros(1, len(samples)))

            def batch_decode(self, ids):
                return [' a m a ']

        class _Model:
            @classmethod
            def from_pretrained(cls, path, **kw):
                seen.append(('model', path))
                return cls()

            def to(self, *args, **kw):
                seen.append(('to', kw.get('device'), kw.get('dtype')))
                return self

            def eval(self):
                return self

            def __call__(self, input_values):
                return types.SimpleNamespace(logits=torch.zeros(1, 4, 55))

        fake = types.ModuleType('transformers')
        fake.Wav2Vec2Processor = _Processor
        fake.WavLMForCTC = _Model
        monkeypatch.setitem(sys.modules, 'transformers', fake)

    def test_load_is_fp32_on_the_device_the_parent_left_visible(self, monkeypatch):
        """The parent hides the GPU from a worker the orchestrator put on the
        CPU, so the worker never picks a device of its own; and its _load never
        builds an orchestrator (lifecycle registration is the parent's)."""
        import torch
        import integrations.service_tools.model_orchestrator as mo
        from integrations.service_tools import phonetic_tool as pt
        seen, built = [], []
        self._fake_transformers(monkeypatch, seen)
        monkeypatch.setattr(mo, 'get_orchestrator', lambda: built.append(True))
        monkeypatch.setattr(pt, 'model_dir', lambda: 'phonetic-models')
        for visible, device in ((False, 'cpu'), (True, 'cuda')):
            monkeypatch.setattr(torch.cuda, 'is_available', lambda: visible)
            seen.clear()
            state = pt._load()
            assert state.device == device
            assert ('to', device, torch.float32) in seen, seen
            assert ('model', 'phonetic-models') in seen
        assert built == [], "_load built an orchestrator; registration is the parent's"

    def test_the_phones_op_turns_a_clip_into_ipa(self, monkeypatch):
        import base64
        import numpy as np
        import torch
        from integrations.service_tools import phonetic_tool as pt
        seen = []
        self._fake_transformers(monkeypatch, seen)
        monkeypatch.setattr(torch.cuda, 'is_available', lambda: False)
        monkeypatch.setattr(pt, 'model_dir', lambda: 'phonetic-models')
        state = pt._load()
        clip = (np.full(pt.SAMPLE_RATE, 0.25) * 32767).astype('<i2').tobytes()
        got = pt._synthesize(state, {'op': 'phones',
                                     'pcm16': base64.b64encode(clip).decode('ascii')})
        assert got['phones'] == 'a m a'
        assert ('clip', pt.SAMPLE_RATE, pt.SAMPLE_RATE, 'pt') in seen
        assert 'error' in pt._synthesize(state, {'op': 'transcribe'})

    def test_its_worker_is_its_own_module_under_the_row_name(self):
        from integrations.service_tools import phonetic_tool as pt
        assert pt._tool.worker_module == 'integrations.service_tools.phonetic_tool'
        assert pt._tool.tool_name == pt._tool.vram_budget == pt.PHONETIC_MODEL_ID
        assert pt._tool.run_mode is None, 'the device is set by the loader, never at import'
        assert not pt._tool.is_alive()
