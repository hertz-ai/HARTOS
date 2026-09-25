"""The session slice outranks the agent slice, and the shell server lives in it.

Measured 2026-09-22 on the Samsung box (docs/architecture/NATIVE_OS_PROGRAM.md
section 1): press p50 122 ms against a 25 ms budget with the daemons running,
12 ms with them paused.  Measured 2026-09-23 with `systemctl show`: hart.slice
held one child, hart-agents.slice at CPUWeight 100, and hart-liquid-ui, the
server behind every click's HTTP round trip, sat inside it at 80 of the slice's
380 shares, entitled to no more than any agent under contention.

hart-kernel.nix now defines hart-session.slice above hart-agents.slice and
hart-liquid-ui.nix moves the shell server into it.  These tests read the two
weights from the source and compare them, so a future edit that flips the
ratio, or drops the shell server back into the agent slice, fails here rather
than on the box.  CPUWeight is a relative share (see TestBackgroundAgentBlastRadius
in test_nixos_configs.py), so the number that matters is the ratio, not either
value alone.
"""
from __future__ import annotations

import os
import re

MODULES_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'nixos', 'modules')


def _read(name):
    with open(os.path.join(MODULES_DIR, name), encoding='utf-8') as fh:
        return fh.read()


def _slice_weight(src, slice_name):
    """CPUWeight declared inside `systemd.slices.<slice_name> = { ... };`."""
    m = re.search(
        rf'systemd\.slices\.{re.escape(slice_name)}\s*=\s*\{{(.*?)\n\s*\}};',
        src, re.S)
    assert m, f"systemd.slices.{slice_name} not defined in hart-kernel.nix"
    w = re.search(r'CPUWeight\s*=\s*(\d+)\s*;', m.group(1))
    assert w, f"systemd.slices.{slice_name} declares no CPUWeight"
    return int(w.group(1))


def test_session_slice_outranks_agent_slice():
    kernel = _read('hart-kernel.nix')
    session = _slice_weight(kernel, 'hart-session')
    agents = _slice_weight(kernel, 'hart-agents')
    assert session > agents, (
        f"hart-session.slice CPUWeight {session} must be ABOVE hart-agents.slice "
        f"{agents}: the person at the desk wins a contended core, agents get the rest")
    # A ratio that only nominally favours the session would not have moved the
    # measured numbers; keep the session at least twice the agents' share.
    assert session >= 2 * agents, (
        f"hart-session {session} vs hart-agents {agents}: the session should hold "
        f"at least twice the agents' share, or contention still lands on the click")


def test_both_slices_nest_under_hart_slice_by_name():
    """systemd derives a slice's parent from its name (hart-session.slice is a
    child of hart.slice), so the two weights are only comparable because both
    names start with `hart-`.  Renaming one silently breaks the ratio."""
    kernel = _read('hart-kernel.nix')
    for name in ('hart-session', 'hart-agents'):
        assert re.search(rf'systemd\.slices\.{name}\s*=', kernel)
        assert name.startswith('hart-')


def test_shell_server_runs_in_the_session_slice():
    ui = _read('hart-liquid-ui.nix')
    assert 'Slice = "hart-session.slice";' in ui, (
        "hart-liquid-ui must run in hart-session.slice: it serves every click's "
        "HTTP round trip and must not compete as an agent")
    assert 'Slice = "hart-agents.slice";' not in ui, (
        "hart-liquid-ui was dropped back into the agent slice, where it shared "
        "80 of 380 shares with the agents under contention (measured 2026-09-23)")


def test_goal_engine_runs_in_the_agent_slice():
    """hart-agent-daemon is the process whose forced ticks drive llama-server.
    Measured 2026-09-23 it sat in system.slice, outside the ratio the kernel
    module defines; the coordinator moved it under the agent slice with
    hart-llm (whose placement tests/unit/test_nixos_hart_llm_gpu.py pins)."""
    agent = _read('hart-agent.nix')
    code = "\n".join(line.split('#')[0] for line in agent.splitlines())
    assert 'Slice = "hart-agents.slice";' in code, (
        "hart-agent-daemon must run in hart-agents.slice so the session-over-"
        "agents ratio binds against the goal engine, not only against the "
        "units that happened to be in the slice already")


def test_kernel_comment_records_where_inference_and_the_engine_live():
    """The WHY beside the weights names hart-llm and hart-agent-daemon and
    the day they moved under the agent slice, or the next reader has to
    rediscover on the box which units the ratio actually binds."""
    kernel = _read('hart-kernel.nix')
    for name in ('hart-llm', 'hart-agent-daemon', '2026-09-23'):
        assert name in kernel, (
            f"the slice comment in hart-kernel.nix must mention {name!r}")
