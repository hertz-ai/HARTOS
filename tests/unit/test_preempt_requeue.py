"""#162 — "queue the canceled daemon alone": a daemon goal whose in-flight LLM
call is PREEMPTED for a live user turn must be treated as a TRANSIENT defer
(re-queued next tick), not counted toward the 5-strike auto-pause.

Verifies the explicit preempt signal: core.foreground.note_preempt /
preempted_recently, that the llama scheduler's preempt records it, and that
dispatch.is_transient_deferral returns True on a recent preempt even when the
user is no longer "recently active".

    python -m pytest tests/unit/test_preempt_requeue.py --noconftest -q
"""
import unittest
from unittest.mock import patch

import core.foreground as F
from core.llama_scheduler import LlamaScheduler


class TestPreemptRequeueSignal(unittest.TestCase):
    def setUp(self):
        F._last_preempt_at = float('-inf')   # no preempt yet

    def test_preempted_recently_window(self):
        self.assertFalse(F.preempted_recently(30))     # baseline
        F.note_preempt()
        self.assertTrue(F.preempted_recently(30))       # just fired
        self.assertFalse(F.preempted_recently(0.0))     # zero window → never recent

    def test_scheduler_preempt_records_the_signal(self):
        s = LlamaScheduler(n_slots=1)
        s.acquire('d1', 'daemon', cancel_fn=lambda: None)   # daemon holds the slot
        s.acquire('u1', 'user', timeout=1)                   # user preempts it
        self.assertTrue(F.preempted_recently(30))            # preempt was recorded

    def test_is_transient_deferral_true_on_recent_preempt(self):
        from integrations.agent_engine import dispatch as D
        F.note_preempt()
        # user NOT recently active and breaker closed — only the preempt signal
        # should make this a transient defer.
        with patch.object(D, 'is_user_recently_active', return_value=False), \
             patch.object(D, '_cb_is_open', return_value=False):
            self.assertTrue(D.is_transient_deferral())

    def test_is_transient_deferral_false_without_preempt_or_activity(self):
        from integrations.agent_engine import dispatch as D
        F._last_preempt_at = float('-inf')      # no preempt
        with patch.object(D, 'is_user_recently_active', return_value=False), \
             patch.object(D, '_cb_is_open', return_value=False):
            self.assertFalse(D.is_transient_deferral())   # genuine failure → counts


if __name__ == '__main__':
    unittest.main()
