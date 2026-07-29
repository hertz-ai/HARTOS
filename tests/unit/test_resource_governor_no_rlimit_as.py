"""The governor must NEVER fall back to RLIMIT_AS on Linux.

Root cause of the 2026-07-28 "can't start new thread" backend crash-loop in every
nixosTest VM (task #15, runs 30404684254/30406523354/30407872610/30409652771):

  _enforce_ram_linux -> cgroup memory.max write fails (User=hart has no cgroup
  delegation inside the systemd unit) -> falls back to RLIMIT_AS = 0.75 x total
  RAM. On a 2 GB VM that is ~1.5 GB of VIRTUAL ADDRESS SPACE, which a fully
  imported CPython exhausts before RSS is even 110 MB; the next mmap -- the 8 MB
  thread stack at hart_intelligence_entry.py:1909 -- fails EAGAIN and CPython
  reports "can't start new thread", deterministically at the same line, five
  restarts to start-limit, on both server and edge variants.

Why nothing else caught it: fresh processes are unlimited (the limit is
self-inflicted per-process AFTER the governor runs), Windows takes the
Job-object path (dev box never sees it), and real 8 GB hardware gets a 6 GB VA
cap it happens to fit under -- a silent landmine, not a pass.

RLIMIT_AS is the wrong tool: address space is not RAM (untouched arenas, shared
library maps and guard pages all count), which is exactly why cgroup memory.max
exists. On HART OS the systemd unit ALREADY enforces MemoryMax at the cgroup
level -- the in-process fallback was a second, parallel enforcement path that
contradicted the canonical one (Gate 4).

Behavioural: drives the REAL _enforce_ram_linux with a fake `resource` module
installed in sys.modules (so the test runs on the Windows dev box, where the
real module does not exist) and asserts the fallback is never taken.

Run (dev box):
    python -m pytest tests/unit/test_resource_governor_no_rlimit_as.py -v \
        --noconftest -p no:cacheprovider
"""
import sys
import types
from unittest.mock import MagicMock

import pytest

from core.resource_governor import ResourceEnforcer


@pytest.fixture
def fake_resource(monkeypatch):
    """A stand-in for the POSIX-only `resource` module, visible to the governor."""
    mod = types.ModuleType("resource")
    mod.RLIMIT_AS = 9
    mod.getrlimit = MagicMock(return_value=(-1, -1))
    mod.setrlimit = MagicMock()
    monkeypatch.setitem(sys.modules, "resource", mod)
    return mod


def _enforcer_without_cgroup():
    e = ResourceEnforcer.__new__(ResourceEnforcer)  # no platform side-effects
    e._cgroup_path = None                            # the VM/unit reality: no delegation
    return e


def test_no_cgroup_never_falls_back_to_rlimit_as(fake_resource):
    # THE bug: cgroup unavailable used to mean setrlimit(RLIMIT_AS, ...).
    _enforcer_without_cgroup()._enforce_ram_linux(int(1.5 * 1024**3))
    fake_resource.setrlimit.assert_not_called()


def test_cgroup_failure_also_never_falls_back(fake_resource, tmp_path):
    # A cgroup path that exists but is not writable (EACCES inside the unit)
    # must ALSO not degrade into the address-space cap.
    e = ResourceEnforcer.__new__(ResourceEnforcer)
    e._cgroup_path = str(tmp_path / "nonexistent-cgroup-dir")
    e._enforce_ram_linux(int(1.5 * 1024**3))
    fake_resource.setrlimit.assert_not_called()


def test_posix_rlimit_helper_still_exists_for_non_linux():
    # macOS still routes through _enforce_ram_rlimit directly (its dispatch arm
    # in _enforce_ram); only the LINUX fallback is removed. Guard the method's
    # existence so the mac arm cannot silently break.
    assert callable(getattr(ResourceEnforcer, "_enforce_ram_rlimit", None))
