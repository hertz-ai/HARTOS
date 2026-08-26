"""The baseline unit's timeout must be a function of the WORK, not a constant.

MEASURED on the fleet box 2026-08-26:
  hart-agent-baseline.service: Main process exited, code=killed, status=15/TERM
  hart-agent-baseline.service: Failed with result 'timeout'.

The unit shipped TimeoutStartSec=300. The profiler enumerates 72 real agent
tasks + 3 surfaces = 75 probes, and spectrum.json gives each probe a 25-45s
budget, so a complete pass legitimately needs ~2630s (43.8 min). systemd was
killing it after roughly seven probes EVERY run, and because SIGTERM lands
mid-run the node wrote no baseline at all -- losing the single artifact the
whole exercise exists to produce.

It only surfaced once llama actually answered on :808. Before that the profiler
self-deferred in under a second, so a 300s limit was never approached and the
bug sat invisible behind a broken dependency.

Two properties are pinned here:
  * the profiler derives its own deadline from the same per-probe budgets it
    judges verdicts against, and degrades to a PARTIAL baseline instead of
    being killed;
  * the unit's outer bound is computed from those same budgets and stays
    comfortably above the plan, so it can never be tighter than the work.

Run:
  pytest tests/unit/test_agent_baseline_timeout.py -v --noconftest
"""

import importlib.util
import json
import os
import re
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPECTRUM = os.path.join(REPO, "scripts", "agent_baseline", "spectrum.json")
PROFILER = os.path.join(REPO, "scripts", "agent_baseline", "profile_local_2b.py")
UNIT = os.path.join(REPO, "nixos", "modules", "hart-agent-baseline.nix")


def _profiler():
    spec = importlib.util.spec_from_file_location("profile_local_2b", PROFILER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _spectrum():
    with open(SPECTRUM, encoding="utf-8") as f:
        return json.load(f)


def _nix_bound():
    """Recompute the module's derived timeout the way Nix does."""
    src = open(UNIT, encoding="utf-8").read()
    ceiling = int(re.search(r"probeCeiling\s*=\s*(\d+)", src).group(1))
    margin = int(re.search(r"probeCeiling\s*\+\s*(\d+)", src).group(1))
    worst = max(v.get("total_ms", 0)
                for v in _spectrum()["budgets"].values() if isinstance(v, dict))
    return (worst // 1000) * ceiling + margin


def test_unit_no_longer_hardcodes_a_constant_timeout():
    src = open(UNIT, encoding="utf-8").read()
    assert 'TimeoutStartSec = "300"' not in src, (
        "the 300s constant is the bug: it bounded a ~2630s job and killed it "
        "after about seven of 75 probes on every run")
    assert "baselineTimeoutSec" in src, (
        "the timeout must be derived from the probe budgets, not written as a "
        "literal")


def test_plan_is_derived_from_the_per_probe_budgets():
    mod = _profiler()
    spec = _spectrum()
    items = mod._items(spec)
    expected = sum(mod._budget_for(spec, b)["total_ms"]
                   for (_n, _c, _p, b) in items) / 1000.0

    assert mod.plan_seconds(spec, items) == expected
    assert len(items) > 1, "the work list should not be empty"
    assert expected > 300, (
        "if the real plan were under 300s the old constant would have been "
        "fine; it is not, which is the whole point (%.0fs)" % expected)


def test_unit_bound_exceeds_the_plan_it_is_bounding():
    """The invariant that actually matters: outer bound > inner plan."""
    mod = _profiler()
    plan = mod.plan_seconds(_spectrum())
    bound = _nix_bound()

    assert bound > plan, (
        "unit TimeoutStartSec (%ds) is below the profiler's own budget-derived "
        "plan (%.0fs); systemd would kill a healthy run again" % (bound, plan))


def test_probe_ceiling_is_above_the_real_probe_count():
    """The ceiling is what stops the derived bound going stale as tasks grow."""
    mod = _profiler()
    src = open(UNIT, encoding="utf-8").read()
    ceiling = int(re.search(r"probeCeiling\s*=\s*(\d+)", src).group(1))
    actual = len(mod._items(_spectrum()))

    assert ceiling >= actual, (
        "probeCeiling=%d is below the %d probes actually enumerated; the "
        "derived timeout would under-bound the run" % (ceiling, actual))


def test_running_out_of_budget_yields_a_partial_baseline_not_a_kill():
    """The behaviour that turns a timeout from data-loss into data."""
    mod = _profiler()
    spec = _spectrum()
    calls = []

    def _never_called(*a, **k):
        calls.append(1)
        return (1, 1, 99, None)

    mod._run_probe = _never_called
    # A deadline already in the past: every probe must be skipped, and we must
    # still get one result row per item rather than an exception or a hang.
    results = mod.profile(spec, 8080, deadline=time.monotonic() - 1)

    assert len(results) == len(mod._items(spec))
    assert all(r["verdict"] == "SKIPPED" for r in results)
    assert not calls, "no probe should run once the deadline has passed"
    assert all(r["error"] for r in results), "a skip must say why"


def test_plan_flag_prints_an_integer_a_unit_could_consume():
    """--plan exists so the bound can be sourced from the work, by anything."""
    mod = _profiler()
    plan = mod.plan_seconds(_spectrum())
    assert int(plan) > 0
    src = open(PROFILER, encoding="utf-8").read()
    assert '"--plan"' in src or "'--plan'" in src
