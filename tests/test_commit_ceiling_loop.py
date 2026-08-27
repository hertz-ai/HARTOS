"""The supervisor recycles the child before its commit leak starves the disk.

#696 stopgap, measured 2026-08-26: three consecutive hevolveai child
generations leaked ~31-33GB/h commit until the Windows pagefile ballooned
into the last free gigabytes (51GB allocation, 0.7GB disk free at the
worst).  run_commit_ceiling_loop is the automated form of the controlled
child kill run by hand at 04:19 and 05:26 that night.  Same injected-
callback style as run_learning_yield_loop's tests — no threads, no psutil.
"""
from integrations.agent_engine.hevolveai_supervisor import (
    run_commit_ceiling_loop,
)

GB = 1024 ** 3


def _drive(commits, ceiling_gb):
    """Run the loop over a fixed commit sequence; return recycle calls."""
    seq = iter(commits)
    recycled = []
    ticks = {'n': 0}

    def get_commit():
        return next(seq)

    def recycle(commit):
        recycled.append(commit)

    def stop():
        ticks['n'] += 1
        return ticks['n'] > len(commits)

    run_commit_ceiling_loop(get_commit, recycle, ceiling_gb * GB,
                            sleep=lambda: None, stop=stop)
    return recycled


def test_recycles_when_commit_crosses_ceiling():
    recycled = _drive([10 * GB, 25 * GB], ceiling_gb=20)
    assert recycled == [25 * GB]


def test_below_ceiling_never_recycles():
    assert _drive([5 * GB, 12 * GB, 19 * GB], ceiling_gb=20) == []


def test_none_commit_child_down_is_skipped():
    assert _drive([None, None], ceiling_gb=20) == []


def test_raising_get_commit_survives_to_next_tick():
    calls = {'n': 0}

    def get_commit():
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('child mid-restart')
        return 30 * GB

    recycled = []
    run_commit_ceiling_loop(
        get_commit, recycled.append, 20 * GB,
        sleep=lambda: None, stop=lambda: calls['n'] >= 2)
    assert recycled == [30 * GB]
