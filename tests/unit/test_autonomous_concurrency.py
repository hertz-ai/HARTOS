"""max_autonomous_concurrency — bound the autonomous dispatch swarm so it
leaves CPU headroom for the user + UI.

The 2026-06-13 "machine pegs ~5 min after boot" incident (#145): once the
300s boot-grace ended, agent_daemon AND coding_daemon each dispatched up to
HEVOLVE_*_MAX_CONCURRENT (default 10) concurrent CREATE pipelines while the
user was away. On a 16-core desktop that + the UI webview pinned every core
for minutes; the user had to kill Nunba to recover. should_yield_to_user()
reason #3 is (by the #60 external-CPU design) deliberately blind to our OWN
cpu, so it never bounds the swarm — this cap does.
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.agent_engine.dispatch import (  # noqa: E402
    max_autonomous_concurrency)


class TestMaxAutonomousConcurrency:
    def test_desktop_16_cores_bounded_to_leave_headroom(self):
        # (16-6)//4 = 2; an env cap of 10 is bounded down to 2 on a desktop.
        assert max_autonomous_concurrency(10, cores=16, reserve=6) == 2

    def test_small_8_core_box_floored_at_one(self):
        # (8-6)//4 = 0 -> floored to 1 (the flywheel must still make progress).
        assert max_autonomous_concurrency(10, cores=8, reserve=6) == 1

    def test_many_core_server_keeps_full_env_cap(self):
        # (64-6)//4 = 14; the env cap of 10 wins -> server throughput preserved.
        assert max_autonomous_concurrency(10, cores=64, reserve=6) == 10

    def test_operator_env_cap_lower_than_headroom_is_respected(self):
        # An operator who sets the cap to 2 is never exceeded, huge box or not.
        assert max_autonomous_concurrency(2, cores=64, reserve=6) == 2

    def test_tiny_box_never_zero_or_negative(self):
        # 4 cores, reserve 6 -> negative raw headroom -> floored at 1, not 0/neg.
        assert max_autonomous_concurrency(10, cores=4, reserve=6) == 1

    def test_env_cap_of_one_stays_one(self):
        assert max_autonomous_concurrency(1, cores=64, reserve=6) == 1

    def test_reserve_read_from_env_when_arg_omitted(self, monkeypatch):
        monkeypatch.setenv('HEVOLVE_AUTONOMOUS_CORE_RESERVE', '8')
        assert max_autonomous_concurrency(10, cores=16) == 2   # (16-8)//4
        monkeypatch.setenv('HEVOLVE_AUTONOMOUS_CORE_RESERVE', '0')
        assert max_autonomous_concurrency(10, cores=16) == 4   # (16-0)//4

    def test_malformed_env_cap_does_not_raise(self):
        # Defensive: a bad env value must never crash a daemon tick.
        assert max_autonomous_concurrency(10, cores=None, reserve='oops') >= 1
