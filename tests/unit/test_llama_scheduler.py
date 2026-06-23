"""Behavioural tests for the slot-aware priority admission controller
(core.llama_scheduler) — #162.

Real scheduler, real threads; verifies: slot cap, user-preempts-daemon (fires
the daemon's cancel_fn + takes the slot now), user-does-NOT-preempt-user,
priority ordering (user dequeued before an earlier-arriving daemon), the
visible in-flight registry (rid + kind), token-keying (empty rids don't
collide), and fail-open timeout.

    python -m pytest tests/unit/test_llama_scheduler.py --noconftest -q
"""
import threading
import time
import unittest
from unittest.mock import patch

from core.llama_scheduler import LlamaScheduler


class TestLlamaScheduler(unittest.TestCase):
    def test_slot_cap_blocks_until_release(self):
        s = LlamaScheduler(n_slots=1)
        t1 = s.acquire('u1', 'user')
        self.assertIsNotNone(t1)
        got = []
        th = threading.Thread(target=lambda: got.append(s.acquire('d1', 'daemon', timeout=2)))
        th.start()
        time.sleep(0.25)
        self.assertEqual(got, [])          # full → blocked
        s.release(t1)
        th.join(2)
        self.assertTrue(got and got[0] is not None)   # freed → granted

    def test_user_preempts_in_flight_daemon(self):
        s = LlamaScheduler(n_slots=1)
        canceled = []
        td = s.acquire('d1', 'daemon', cancel_fn=lambda: canceled.append('d1'))
        self.assertIsNotNone(td)
        tu = s.acquire('u1', 'user', timeout=2)
        self.assertIsNotNone(tu)                       # user granted immediately
        self.assertEqual(canceled, ['d1'])             # daemon's call aborted
        self.assertEqual(s.inflight(), [('u1', 'user')])  # user now holds the slot
        s.release(td)                                  # daemon's late release = no-op
        self.assertEqual(s.inflight(), [('u1', 'user')])

    def test_user_does_NOT_preempt_user(self):
        s = LlamaScheduler(n_slots=1)
        canceled = []
        tu1 = s.acquire('u1', 'user', cancel_fn=lambda: canceled.append('u1'))
        got = []
        th = threading.Thread(target=lambda: got.append(s.acquire('u2', 'user', timeout=1)))
        th.start()
        time.sleep(0.3)
        self.assertEqual(got, [])          # u2 waits — does not preempt u1
        self.assertEqual(canceled, [])     # u1 untouched
        s.release(tu1)
        th.join(2)
        self.assertTrue(got and got[0] is not None)

    def test_user_dequeued_before_earlier_daemon(self):
        s = LlamaScheduler(n_slots=1)
        hold = s.acquire('hold', 'user')
        order = []

        def acq(rid, kind):
            tok = s.acquire(rid, kind, timeout=4)
            order.append(rid)
            time.sleep(0.05)
            s.release(tok)

        td = threading.Thread(target=acq, args=('d1', 'daemon')); td.start()
        time.sleep(0.15)                   # daemon queues FIRST
        tu = threading.Thread(target=acq, args=('u2', 'user')); tu.start()
        time.sleep(0.15)                   # user queues SECOND
        s.release(hold)
        td.join(5); tu.join(5)
        self.assertEqual(order, ['u2', 'd1'])   # user wins despite arriving later

    def test_inflight_registry_is_visible_with_kind(self):
        s = LlamaScheduler(n_slots=2)
        s.acquire('u1', 'user')
        s.acquire('daemon_goal_9', 'daemon')
        infl = dict(s.inflight())
        self.assertEqual(infl.get('u1'), 'user')
        self.assertEqual(infl.get('daemon_goal_9'), 'daemon')
        st = s.stats()
        self.assertEqual(st['n_slots'], 2)
        self.assertEqual(st['in_flight'], 2)

    def test_empty_rid_daemons_do_not_collide(self):
        s = LlamaScheduler(n_slots=2)
        t1 = s.acquire('', 'daemon')
        t2 = s.acquire('', 'daemon')
        self.assertIsNotNone(t1)
        self.assertIsNotNone(t2)
        self.assertEqual(len(s.inflight()), 2)      # two distinct slots, same rid

    def test_timeout_returns_none_fail_open(self):
        s = LlamaScheduler(n_slots=1)
        s.acquire('u1', 'user')               # users fill the slot
        tok = s.acquire('u2', 'user', timeout=0.3)
        self.assertIsNone(tok)                # no daemon to preempt → timeout

    def test_set_slots_promotes_waiters(self):
        s = LlamaScheduler(n_slots=1)
        s.acquire('u1', 'user')
        got = []
        th = threading.Thread(target=lambda: got.append(s.acquire('u2', 'user', timeout=2)))
        th.start()
        time.sleep(0.2)
        self.assertEqual(got, [])             # blocked at n_slots=1
        s.set_slots(2)                        # grow → promote the waiter
        th.join(2)
        self.assertTrue(got and got[0] is not None)


class TestSlotAutoDetect(unittest.TestCase):
    def test_refresh_from_config_field_first(self):
        s = LlamaScheduler(n_slots=2)
        with patch('core.llama_scheduler._read_config_slots', return_value=4), \
             patch('core.llama_scheduler._read_server_slots', return_value=99):
            s.refresh_slots()
        self.assertEqual(s.n_slots, 4)          # config field wins over /props

    def test_refresh_from_server_props_when_config_absent(self):
        s = LlamaScheduler(n_slots=2)
        with patch('core.llama_scheduler._read_config_slots', return_value=None), \
             patch('core.llama_scheduler._read_server_slots', return_value=3):
            s.refresh_slots()
        self.assertEqual(s.n_slots, 3)          # falls back to live /props

    def test_refresh_keeps_current_when_both_unavailable(self):
        s = LlamaScheduler(n_slots=2)
        with patch('core.llama_scheduler._read_config_slots', return_value=None), \
             patch('core.llama_scheduler._read_server_slots', return_value=None):
            s.refresh_slots()
        self.assertEqual(s.n_slots, 2)          # fail-open: keep the default


if __name__ == '__main__':
    unittest.main()
