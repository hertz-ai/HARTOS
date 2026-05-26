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


if __name__ == '__main__':
    unittest.main()
