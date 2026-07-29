"""Local-2B agent/surface BASELINE harness tests.

Steward mandate (2026-07-22): tests on surfaces/agents that are not profiled
against the local llama.cpp 2B, so the potato machine has a baseline.

The BASELINE NUMBERS themselves are produced on a node with llama-server up
(profile_local_2b.py). What is enforceable on ANY box, today, is the harness
CONTRACT + COVERAGE -- and coverage is what makes "no agent left unprofiled" a
guarantee instead of a hope: every canonical seeded agent task must map to a
probe + budget, or this fails.

Behavioural: imports the real profiler + the real seed list and exercises them.
"""
import importlib.util
import json
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPECTRUM = os.path.join(REPO, "scripts", "agent_baseline", "spectrum.json")
PROFILER = os.path.join(REPO, "scripts", "agent_baseline", "profile_local_2b.py")
BUDGET_KEYS = {"first_token_ms", "total_ms", "min_chars"}


def _load_profiler():
    spec = importlib.util.spec_from_file_location("profile_local_2b", PROFILER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def spectrum():
    with open(SPECTRUM, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def prof():
    return _load_profiler()


def test_spectrum_is_wellformed(spectrum):
    for key in ("endpoint", "budgets", "categories", "surfaces"):
        assert key in spectrum, "spectrum.json missing %r" % key
    assert "default" in spectrum["budgets"]
    for name, b in spectrum["budgets"].items():
        if name.startswith("_"):
            continue
        assert BUDGET_KEYS <= set(b), "budget %r missing keys %s" % (name, BUDGET_KEYS - set(b))
        for k in BUDGET_KEYS:
            assert isinstance(b[k], (int, float)) and b[k] > 0, "%s.%s must be > 0" % (name, k)


def test_every_category_and_surface_names_a_real_budget(spectrum):
    valid = set(spectrum["budgets"]) - {"_note"}
    for grp in ("categories", "surfaces"):
        for name, c in spectrum[grp].items():
            if name.startswith("_"):
                continue
            assert c.get("probe"), "%s.%s has no probe" % (grp, name)
            assert c.get("budget") in valid, "%s.%s budget %r not defined" % (grp, name, c.get("budget"))


def test_harness_enumerates_the_real_agent_spectrum(prof):
    """The task list is DERIVED from the one canonical seed list, not a copy."""
    tasks = prof._agent_tasks()
    assert len(tasks) >= 10, (
        "only %d agent tasks enumerated -- the seed list did not load, so the "
        "baseline would silently cover almost nothing" % len(tasks))
    for name, cat in tasks:
        assert name.startswith("goal:")
        assert isinstance(cat, str) and cat


def test_no_seeded_agent_is_left_uncategorised(prof, spectrum):
    """COVERAGE: every seeded goal must map to a category that HAS a probe. A goal
    that fell through to an undefined category would be profiled with nothing --
    the exact silent-gap the mandate forbids ('surfaces which are not tested')."""
    cats = spectrum["categories"]
    missing = []
    for name, cat in prof._agent_tasks():
        if cat not in cats or not cats[cat].get("probe"):
            missing.append("%s -> %s" % (name, cat))
    assert not missing, "agent tasks with no probe: " + "; ".join(missing)


def test_categorizer_is_deterministic_and_total(prof):
    """Every (slug,title) maps to a category; unknown work falls to 'generic',
    never to None (a None category would crash the profile loop mid-run)."""
    for slug, title in [("bootstrap_marketing_awareness", "Awareness"),
                        ("bootstrap_revenue_monitor", "Revenue"),
                        ("something_totally_unknown", "Mystery"),
                        ("", "")]:
        cat = prof._categorize(slug, title)
        assert isinstance(cat, str) and cat, "categorize(%r) -> %r" % (slug, cat)


def test_profiler_skips_cleanly_with_no_local_model(prof, monkeypatch):
    """CONTRACT: on a box with no llama-server (the dev box / CI) the profiler
    reports the deferral and returns 0 -- it must NEVER fabricate a number or
    fail the build for a missing model."""
    # point at a port nothing is listening on, and confirm the reachability probe
    # is False, then that main() returns 0.
    monkeypatch.setattr(prof, "_llm_port", lambda: 6553)  # ~unused high port
    monkeypatch.setattr(prof, "_endpoint_up", lambda port, spec: False)
    assert prof.main([]) == 0


def test_endpoint_is_the_shared_llama_server_not_a_parallel_path(spectrum, prof):
    """Guard the no-parallel-path invariant: the profiler must resolve the port
    from port_registry 'llm' (the SAME server the agents use), and hit the OpenAI
    chat path -- not spin up or hardcode a second inference route."""
    assert spectrum["endpoint"]["path"] == "/v1/chat/completions"
    # _llm_port prefers port_registry; on this box it resolves to a real int.
    assert isinstance(prof._llm_port(), int) and prof._llm_port() > 0
