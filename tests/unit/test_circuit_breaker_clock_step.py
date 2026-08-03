"""A backwards clock step must not strand the breaker or the backoff (#24).

THE REAL INCIDENT
─────────────────
On a Windows dual-boot node the RTC holds LOCAL time while NixOS reads it as
UTC, so the box ran +5:30 wrong until wifi came up — at which point NTP yanked
the wall clock BACKWARDS by the whole 19800s, immediately before the desktop
hung (hart-installer.nix:117 records this; the install-time fix writes
time.hardwareClockInLocalTime when a Windows bootloader is present).

That fix stops the STEP on installed dual-boot machines. It does not make the
runtime survive a step, and steps have other causes: a live-USB boot on the
same hardware, a dead CMOS battery, a VM restored from a snapshot, or any
first NTP sync after a long power-off.

WHAT BREAKS WITHOUT MONOTONIC TIME
──────────────────────────────────
`CircuitBreaker._get_state` computed `elapsed = time.time() - self._opened_at`.
After a -19800s step, `elapsed` is about -19800, so `elapsed > cooldown` is
False and the breaker reports OPEN — and keeps reporting OPEN until the wall
clock climbs back, i.e. for the WHOLE OFFSET. A 60-second cooldown becomes a
five-and-a-half-hour outage of whatever that breaker guards.

`PeerBackoff` had the same shape from the other side: it stores
`time.time() + delay` as a deadline, so a backwards step leaves every key
backed off for the offset instead of its delay.

Both are pure elapsed-time arithmetic, never displayed and never persisted
(`get_stats` does not expose them), so `time.monotonic()` is the correct clock:
it cannot be stepped by NTP or by the RTC being misread.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.circuit_breaker import (  # noqa: E402
    CircuitBreaker, CircuitState, PeerBackoff)

#: The steward's actual offset: IST is UTC+5:30, so the step was 5.5 hours.
IST_STEP = 5.5 * 3600


class _FakeClock:
    """A monotonic-looking clock the test can advance — and try to rewind."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class BackwardsClockStep(unittest.TestCase):

    def _tripped(self, breaker):
        for _ in range(breaker.threshold):
            breaker.record_failure()

    def test_breaker_reopens_on_cooldown_and_is_not_stranded(self):
        """The core guarantee: cooldown is measured on a clock that cannot go back."""
        clock = _FakeClock()
        with patch('core.circuit_breaker.time.monotonic', clock):
            cb = CircuitBreaker(name='t', threshold=2, cooldown=60.0)
            self._tripped(cb)
            self.assertEqual(cb.state, CircuitState.OPEN)

            # A wall clock would jump back here. Monotonic does not, so the
            # breaker's own timeline is untouched and the cooldown still ends.
            clock.t += 61
            self.assertEqual(cb.state, CircuitState.HALF_OPEN,
                             "breaker did not leave OPEN after its cooldown")

    def test_wall_clock_would_have_stranded_it_for_the_whole_offset(self):
        """Demonstrates the ORIGINAL bug, so the fix is not taken on faith.

        Drives the same arithmetic the old code used and shows the verdict it
        produced: after -19800s, `elapsed > cooldown` is False, i.e. OPEN.
        """
        opened_at = 1_000_000.0
        cooldown = 60.0
        stepped_now = opened_at + 5 - IST_STEP     # 5s in, then NTP steps back
        elapsed = stepped_now - opened_at
        self.assertLess(elapsed, 0, "precondition: the step makes elapsed negative")
        self.assertFalse(elapsed > cooldown,
                         "the old wall-clock check reported OPEN — this is the bug")
        # And it stays wrong until the wall clock climbs all the way back.
        self.assertGreater(IST_STEP, cooldown * 300)

    def test_backoff_deadline_is_not_pushed_into_the_far_future(self):
        clock = _FakeClock()
        with patch('core.circuit_breaker.time.monotonic', clock):
            bo = PeerBackoff(initial=1.0, maximum=30.0)
            bo.record_failure('peer-a')
            self.assertTrue(bo.is_backed_off('peer-a'))

            clock.t += 2                      # past the 1s initial delay
            self.assertFalse(bo.is_backed_off('peer-a'),
                             "key still backed off after its delay elapsed")

    def test_backoff_prune_uses_the_same_clock_as_the_deadline(self):
        """A prune on a different clock than the deadline would never expire."""
        clock = _FakeClock()
        with patch('core.circuit_breaker.time.monotonic', clock):
            bo = PeerBackoff(initial=1.0, maximum=30.0)
            bo.record_failure('peer-b')
            clock.t += 5
            bo.prune_expired()
            self.assertFalse(bo.is_backed_off('peer-b'))

    def test_module_uses_no_wall_clock_at_all(self):
        """Guard the guard, and the whole module: one missed call re-opens this.

        Labelled source check, paired with the behavioural tests above — the
        bug is an ABSENCE (a call that should not exist), and no single
        behavioural test can prove absence across the file.
        """
        import inspect
        import core.circuit_breaker as mod
        src = inspect.getsource(mod)
        offenders = [ln.strip() for ln in src.splitlines()
                     if 'time.time()' in ln and not ln.strip().startswith('#')]
        self.assertEqual(offenders, [], (
            f"wall-clock time is back in circuit_breaker.py: {offenders}. "
            f"An NTP step (or a misread RTC on a dual-boot box) makes elapsed "
            f"negative and strands the breaker for the whole offset."))


if __name__ == '__main__':
    unittest.main()
