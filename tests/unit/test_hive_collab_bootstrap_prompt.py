"""Fidelity gate for docs/architecture/HIVE_COLLAB_BOOTSTRAP.md (the prompt
every Claude Code session loads to collaborate with the HARTOS hive).

The prompt is a semantic spec: every entry point, symbol, env knob, and trap
it names must exist in the deployed code EXACTLY as described, or a future
agent gets steered into a wall. Behavioural: real imports, real attributes,
real defaults -- when a rename/refactor breaks the prompt, THIS fails naming
the drifted reference (never a grep of prose)."""
import importlib
import os

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _mod(name):
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    return importlib.import_module(name)


def test_prompt_files_exist():
    """Every file the prompt points a future agent at must exist."""
    for rel in (
        'docs/architecture/HIVE_COLLAB_BOOTSTRAP.md',
        'hart_intelligence_entry.py',
        'scripts/run_flywheel_dev.py',
        'nixos/modules/hart-agent.nix',
        'hartos_bootstrap.py',
        'integrations/agent_engine/goal_manager.py',
        'create_recipe.py',
        'lifecycle_hooks.py',
        'docs/design/HOME_DESKTOP_DESIGN_CHECKLIST.md',
    ):
        assert os.path.exists(os.path.join(ROOT, rel)), 'prompt names missing file: ' + rel


def test_dispatch_surface_matches_prompt():
    """dispatch_goal is the flywheel's route into /chat; should_yield_to_user
    is the foreground-yield contract the prompt tells agents to respect."""
    d = _mod('integrations.agent_engine.dispatch')
    assert callable(d.dispatch_goal)
    assert callable(d.should_yield_to_user)


def test_world_model_bridge_surface_matches_prompt():
    """The ONE HevolveAI bridge: record_interaction + skill packets, and the
    50-batch flush default the prompt warns about."""
    w = _mod('integrations.agent_engine.world_model_bridge')
    b = w.WorldModelBridge
    assert callable(getattr(b, 'record_interaction'))
    assert callable(getattr(b, 'distribute_skill_packet'))
    assert callable(getattr(b, 'ingest_skill_packet'))
    src = open(os.path.join(ROOT, 'integrations/agent_engine/world_model_bridge.py'),
               encoding='utf-8').read()
    assert 'HEVOLVE_WM_FLUSH_BATCH' in src, \
        'the flush-batch knob the prompt documents is gone -- update the prompt'


def test_goal_seeding_and_daemon_match_prompt():
    g = _mod('integrations.agent_engine.goal_seeding')
    assert getattr(g, 'SEED_BOOTSTRAP_GOALS'), 'bootstrap goals list is empty/missing'
    a = _mod('integrations.agent_engine.agent_daemon')
    assert hasattr(a, 'AgentDaemon')


def test_pacing_knobs_the_prompt_documents_exist():
    """The env knobs (and the defaults the timing table is computed from) must
    still be the ones the code reads."""
    import re
    src = open(os.path.join(ROOT, 'integrations/agent_engine/agent_daemon.py'),
               encoding='utf-8').read()
    m = re.search(r"HEVOLVE_DAEMON_BOOT_DELAY['\"]\s*,\s*['\"]?(\d+)", src)
    assert m and m.group(1) == '300', 'boot-grace default drifted from the prompt (300s)'
    m = re.search(r"HEVOLVE_AGENT_POLL_INTERVAL['\"]\s*,\s*['\"]?(\d+)", src)
    assert m and m.group(1) == '30', 'poll-interval default drifted from the prompt (30s)'
    dsrc = open(os.path.join(ROOT, 'integrations/agent_engine/dispatch.py'),
                encoding='utf-8').read()
    assert 'HEVOLVE_LOCAL_LLM_MAX_CONCURRENT' in dsrc


def test_standalone_entry_gap_still_holds():
    """The prompt's 'know who starts the daemon' claim: init_agent_engine must
    NOT be called from hart_intelligence_entry's own source (only Nunba
    bootstrap, the NixOS unit, and run_flywheel_dev start the AgentDaemon).
    If someone wires it in, the prompt's instruction is stale -- update it."""
    src = open(os.path.join(ROOT, 'hart_intelligence_entry.py'), encoding='utf-8').read()
    assert 'init_agent_engine(' not in src, \
        'hart_intelligence_entry now starts the agent engine -- update HIVE_COLLAB_BOOTSTRAP.md'
    fly = open(os.path.join(ROOT, 'scripts/run_flywheel_dev.py'), encoding='utf-8').read()
    assert 'init_agent_engine' in fly, \
        'run_flywheel_dev no longer starts the engine -- update HIVE_COLLAB_BOOTSTRAP.md'
