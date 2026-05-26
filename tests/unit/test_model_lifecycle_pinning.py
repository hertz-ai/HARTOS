"""Tests for the pinned / pressure_evict_only flags on ModelState.

Regression guard for the 2026-04-11 incident where the 4B main LLM
got passively evicted every 5 minutes mid-session because
_update_priorities unconditionally demoted ANY idle model past its
timeout to EVICTABLE, and _evict_idle_models then unloaded it.

The fix adds two orthogonal policy flags:

  - ``pinned`` — model is always ACTIVE, never evicted regardless of
    idle time or pressure. Used for the draft 0.8B classifier.
  - ``pressure_evict_only`` — model can be evicted under VRAM /
    RAM / CPU pressure but NEVER by the passive idle sweep. Used
    for main chat LLMs (2B / 4B).

Default-flag models keep the legacy passive idle eviction behavior.
"""
import time
import unittest

from integrations.service_tools.model_lifecycle import (
    ModelDevice,
    ModelLifecycleManager,
    ModelPriority,
    ModelState,
)


class TestPinnedModelEvictionPolicy(unittest.TestCase):
    """Pinned models must never change priority and never evict."""

    def _fresh_manager(self):
        """Construct a new manager with an empty model dict so tests
        don't pollute the global singleton."""
        return ModelLifecycleManager()

    def _long_idle(self):
        """Return a last_access_time 10000s in the past (far past any
        reasonable idle timeout)."""
        return time.time() - 10000

    def test_pinned_model_stays_active_after_long_idle(self):
        mlm = self._fresh_manager()
        mlm._models['llm-0.8b-draft'] = ModelState(
            name='llm-0.8b-draft',
            device=ModelDevice.GPU,
            priority=ModelPriority.WARM,
            last_access_time=self._long_idle(),
            idle_timeout_s=300.0,
            pinned=True,
        )

        mlm._update_priorities()

        state = mlm._models['llm-0.8b-draft']
        self.assertEqual(
            state.priority, ModelPriority.ACTIVE,
            f"pinned model was demoted to {state.priority} — pinning broken",
        )

    def test_pinned_model_excluded_from_evict_candidates(self):
        mlm = self._fresh_manager()
        mlm._models['pinned-thing'] = ModelState(
            name='pinned-thing',
            device=ModelDevice.GPU,
            priority=ModelPriority.EVICTABLE,  # manually forced EVICTABLE
            last_access_time=self._long_idle(),
            idle_timeout_s=300.0,
            pinned=True,
        )

        with mlm._lock:
            candidates = [
                s.name for s in mlm._models.values()
                if s.priority == ModelPriority.EVICTABLE
                and s.device != ModelDevice.UNLOADED
                and s.active_inference_count == 0
                and not s.pinned
            ]

        self.assertNotIn(
            'pinned-thing', candidates,
            "pinned model appeared in idle eviction candidates — "
            "the belt-and-suspenders guard in _evict_idle_models is broken",
        )

    def test_pinned_model_excluded_from_vram_pressure_candidates(self):
        """Pinned models must survive even VRAM pressure responses."""
        mlm = self._fresh_manager()
        mlm._models['pinned'] = ModelState(
            name='pinned',
            device=ModelDevice.GPU,
            priority=ModelPriority.WARM,
            pinned=True,
        )
        mlm._models['normal'] = ModelState(
            name='normal',
            device=ModelDevice.GPU,
            priority=ModelPriority.IDLE,
        )

        with mlm._lock:
            candidates = [
                s.name for s in mlm._models.values()
                if s.device in (ModelDevice.GPU, ModelDevice.CPU_OFFLOAD)
                and s.priority != ModelPriority.ACTIVE
                and not s.pinned
            ]

        self.assertNotIn('pinned', candidates)
        self.assertIn('normal', candidates)


class TestPressureEvictOnlyPolicy(unittest.TestCase):
    """pressure_evict_only models survive the passive idle sweep
    but still appear as candidates under real VRAM pressure."""

    def test_main_llm_caps_at_idle_not_evictable(self):
        """A 4B main LLM idle for 10000s should become IDLE, not
        EVICTABLE, so the idle sweep doesn't touch it."""
        mlm = ModelLifecycleManager()
        mlm._models['llm-4b-main'] = ModelState(
            name='llm-4b-main',
            device=ModelDevice.GPU,
            priority=ModelPriority.WARM,
            last_access_time=time.time() - 10000,
            idle_timeout_s=300.0,
            pressure_evict_only=True,
        )

        mlm._update_priorities()

        state = mlm._models['llm-4b-main']
        self.assertEqual(
            state.priority, ModelPriority.IDLE,
            f"pressure_evict_only model was demoted to {state.priority} "
            f"— the policy is broken, it should cap at IDLE",
        )

    def test_main_llm_not_in_idle_eviction_candidates(self):
        """After _update_priorities runs, the main LLM's priority is
        IDLE (not EVICTABLE), so _evict_idle_models's filter excludes it."""
        mlm = ModelLifecycleManager()
        mlm._models['llm-4b-main'] = ModelState(
            name='llm-4b-main',
            device=ModelDevice.GPU,
            priority=ModelPriority.WARM,
            last_access_time=time.time() - 10000,
            idle_timeout_s=300.0,
            pressure_evict_only=True,
        )

        mlm._update_priorities()

        with mlm._lock:
            candidates = [
                s.name for s in mlm._models.values()
                if s.priority == ModelPriority.EVICTABLE
            ]

        self.assertNotIn('llm-4b-main', candidates)

    def test_main_llm_still_appears_in_vram_pressure_candidates(self):
        """pressure_evict_only != pinned. Under real VRAM pressure, the
        main LLM SHOULD be a candidate — just not the idle sweep."""
        mlm = ModelLifecycleManager()
        mlm._models['llm-4b-main'] = ModelState(
            name='llm-4b-main',
            device=ModelDevice.GPU,
            priority=ModelPriority.IDLE,  # post-_update_priorities state
            pressure_evict_only=True,
        )

        with mlm._lock:
            candidates = [
                s.name for s in mlm._models.values()
                if s.device in (ModelDevice.GPU, ModelDevice.CPU_OFFLOAD)
                and s.priority != ModelPriority.ACTIVE
                and not s.pinned
            ]

        self.assertIn(
            'llm-4b-main', candidates,
            "pressure_evict_only model MUST appear in VRAM-pressure "
            "candidates — it is not pinned, just protected from the "
            "passive idle sweep",
        )


class TestDefaultEvictionPolicyUnchanged(unittest.TestCase):
    """Default-flag models (whisper, TTS, vision) must keep the
    legacy behavior so this commit doesn't break existing tests."""

    def test_default_model_demotes_to_evictable_after_timeout(self):
        mlm = ModelLifecycleManager()
        mlm._models['whisper'] = ModelState(
            name='whisper',
            device=ModelDevice.GPU,
            priority=ModelPriority.WARM,
            last_access_time=time.time() - 10000,
            idle_timeout_s=300.0,
        )

        mlm._update_priorities()

        state = mlm._models['whisper']
        self.assertEqual(
            state.priority, ModelPriority.EVICTABLE,
            "default-flag model should still demote to EVICTABLE after "
            "idle_timeout — this commit must not change legacy behavior",
        )

    def test_default_model_appears_in_idle_candidates(self):
        mlm = ModelLifecycleManager()
        mlm._models['whisper'] = ModelState(
            name='whisper',
            device=ModelDevice.GPU,
            priority=ModelPriority.WARM,
            last_access_time=time.time() - 10000,
            idle_timeout_s=300.0,
        )

        mlm._update_priorities()

        with mlm._lock:
            candidates = [
                s.name for s in mlm._models.values()
                if s.priority == ModelPriority.EVICTABLE
                and s.device != ModelDevice.UNLOADED
                and s.active_inference_count == 0
                and not s.pinned
            ]

        self.assertIn('whisper', candidates)


class TestActiveInferenceGuardStillWins(unittest.TestCase):
    """active_inference_count > 0 always forces ACTIVE regardless of
    idle time — must still apply alongside the new flags."""

    def test_active_inference_beats_idle_timeout(self):
        mlm = ModelLifecycleManager()
        mlm._models['busy'] = ModelState(
            name='busy',
            device=ModelDevice.GPU,
            priority=ModelPriority.WARM,
            last_access_time=time.time() - 10000,
            idle_timeout_s=300.0,
            active_inference_count=1,
        )

        mlm._update_priorities()

        state = mlm._models['busy']
        self.assertEqual(state.priority, ModelPriority.ACTIVE)


class TestRegisterLifecycleAssignsCorrectPolicy(unittest.TestCase):
    """ModelOrchestrator._register_lifecycle is the actual decision
    site for which policy each catalog entry gets.  Regression guard
    for the 2026-05-03 incident where the 4B-VL main died silently
    (no log captured because Nunba's main llama-server uses
    subprocess.PIPE drained only during startup) and was downgraded
    from pinned to pressure_evict_only — letting phase-3 pressure
    eventually unload it."""

    def _entry(self, model_id, *, model_type='llm', purposes=None):
        from integrations.service_tools.model_catalog import ModelEntry
        return ModelEntry(
            id=model_id,
            name=model_id,
            model_type=model_type,
            purposes=list(purposes or []),
        )

    def _orchestrator(self):
        from integrations.service_tools.model_orchestrator import ModelOrchestrator
        return ModelOrchestrator()

    def _register(self, entry):
        """Drive _register_lifecycle and return the resulting ModelState
        from the lifecycle manager.  Uses the orchestrator's id→key
        mapping the same way runtime registration does."""
        from integrations.service_tools.model_lifecycle import (
            get_model_lifecycle_manager, CPU_OFFLOAD_TABLE)
        orch = self._orchestrator()
        # Fresh manager state for test isolation.
        mlm = get_model_lifecycle_manager()
        mlm._models.clear()
        offload_name = entry.id.split('-')[1] if '-' in entry.id else entry.id
        if offload_name not in CPU_OFFLOAD_TABLE:
            offload_name = entry.id
        orch._register_lifecycle(entry)
        return mlm._models.get(offload_name)

    def test_main_plus_vlm_via_purpose_is_pinned(self):
        """purposes contains 'vision' / 'caption' / 'grounding' →
        pinned (highest protection, never evicted)."""
        for purpose in ('vision', 'caption', 'grounding'):
            with self.subTest(purpose=purpose):
                state = self._register(self._entry(
                    'llm-qwen3.5-4b-vl-recommended',
                    purposes=['main', purpose]))
                self.assertIsNotNone(state, "registration failed")
                self.assertTrue(
                    state.pinned,
                    f"main+{purpose} LLM must be pinned (1 model serves "
                    f"chat AND VLM agentic loop) — losing it kills both",
                )
                self.assertFalse(
                    state.pressure_evict_only,
                    "pinned overrides pressure_evict_only",
                )

    def test_main_plus_vlm_via_id_is_pinned_when_purposes_empty(self):
        """No purposes set → fallback id pattern match for ``-vl-`` /
        ``-vlm-`` / ``mmproj`` triggers the same pin."""
        for model_id in (
            'llm-qwen3.5-4b-vl-recommended',
            'llm-qwen-7b-vlm-base',
            'llm-some-mmproj-build',
        ):
            with self.subTest(model_id=model_id):
                state = self._register(self._entry(model_id, purposes=[]))
                self.assertIsNotNone(state)
                self.assertTrue(
                    state.pinned,
                    f"id={model_id} should match VLM-main fallback pattern",
                )

    def test_chat_only_main_keeps_pressure_evict_only(self):
        """Main without any VLM purpose → pressure_evict_only (the
        2026-04-11 fix).  Chat reload is cheap, no in-flight loop."""
        state = self._register(self._entry(
            'llm-qwen3.5-4b-base', purposes=['main']))
        self.assertIsNotNone(state)
        self.assertFalse(state.pinned, "chat-only main must NOT be pinned")
        self.assertTrue(
            state.pressure_evict_only,
            "chat-only main should keep pressure_evict_only",
        )

    def test_draft_classifier_stays_pinned(self):
        """The 0.8B draft was already pinned — must stay pinned."""
        state = self._register(self._entry(
            'llm-qwen3.5-0.8b', purposes=['draft']))
        self.assertIsNotNone(state)
        self.assertTrue(state.pinned, "draft must remain pinned")

    def test_draft_via_id_fallback_stays_pinned(self):
        for model_id in (
            'llm-qwen3.5-0.8b-draft', 'llm-caption-mini'):
            with self.subTest(model_id=model_id):
                state = self._register(self._entry(model_id, purposes=[]))
                self.assertIsNotNone(state)
                self.assertTrue(state.pinned)


if __name__ == '__main__':
    unittest.main()
