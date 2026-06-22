"""Regression: the adopted external main LLM must be PINNED so the
model_lifecycle eviction ladder never falsely UNLOADs it under CPU pressure.

Live incident (Nunba, latest build): daemon-driven CPU pressure made
``_respond_to_cpu_pressure`` evict the (non-pinned) main ``'llm'`` ->
false ``device=UNLOADED`` -> the user's foreground "hi" bounced to the
LangChain-local "Loading tools..." rung AND piper TTS was evicted in the same
sweep ("both via ladder demotion").  The lifecycle CANNOT actually unload an
EXTERNAL llama-server, so the eviction is always a harmful no-op; the #125
heal (_record_llm_alive) papered over the churn on the next tick instead
of pinning to PREVENT it.

Behavioural: real ModelLifecycleManager, real _record_llm_alive /
_respond_to_cpu_pressure; only _do_unload (the side effect) is observed.

    python -m pytest tests/unit/test_model_lifecycle_main_llm_pin.py --noconftest -q
"""
import unittest
from unittest.mock import patch

from integrations.service_tools.model_lifecycle import (
    ModelLifecycleManager, ModelDevice, ModelPriority)


class TestAdoptedMainLLMPinned(unittest.TestCase):
    def setUp(self):
        # __init__ does NOT start the daemon loop (start() does), so a bare
        # instance is a thread-free unit under test.
        self.mgr = ModelLifecycleManager()

    def test__record_llm_alive_pins_the_engine(self):
        self.mgr._record_llm_alive()
        st = self.mgr._models['llm']
        self.assertTrue(st.pinned, 'adopted main LLM must be pinned')
        self.assertEqual(st.device, ModelDevice.GPU)

    def test_heal_branch_also_pins_a_stale_unloaded_state(self):
        self.mgr._record_llm_alive()
        st = self.mgr._models['llm']
        # simulate the pre-fix stale adopted-server state (#125)
        st.device = ModelDevice.UNLOADED
        st.pinned = False
        self.mgr._record_llm_alive()  # heal again
        self.assertTrue(st.pinned)
        self.assertEqual(st.device, ModelDevice.GPU)

    def test_cpu_pressure_never_evicts_the_pinned_main_llm(self):
        self.mgr._record_llm_alive()
        st = self.mgr._models['llm']
        # worst case: priority drifted to IDLE, no active inference
        st.priority = ModelPriority.IDLE
        st.active_inference_count = 0
        self.assertTrue(st.pinned)
        with patch.object(self.mgr, '_do_unload') as unload:
            self.mgr._respond_to_cpu_pressure()
        unload.assert_not_called()  # pinned => the ladder skips it

    def test_negative_control_unpinned_idle_llm_WOULD_be_evicted(self):
        # Proves the PIN (not some other guard) is what protects the engine:
        # the identical IDLE state, unpinned, IS evicted — that was the bug.
        self.mgr._record_llm_alive()
        st = self.mgr._models['llm']
        st.priority = ModelPriority.IDLE
        st.active_inference_count = 0
        st.pinned = False
        with patch.object(self.mgr, '_do_unload') as unload:
            self.mgr._respond_to_cpu_pressure()
        unload.assert_called_once_with('llm')


if __name__ == '__main__':
    unittest.main()
