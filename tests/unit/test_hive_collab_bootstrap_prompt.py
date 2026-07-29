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


def test_cluster_fabric_references_exist():
    """The prompt's 20-node section names the real fabric files -- discovery,
    gossip, PeerLink, coordinator backends, recipe sync, aggregator. A rename
    breaks the prompt's instructions, so it breaks here first."""
    for rel in (
        'integrations/social/peer_discovery.py',
        'core/peer_link/link_manager.py',
        'core/peer_link/channels.py',
        'core/recipe_sync.py',
        'integrations/distributed_agent/coordinator_backends.py',
        'integrations/distributed_agent/worker_loop.py',
    ):
        assert os.path.exists(os.path.join(ROOT, rel)), 'cluster prompt names missing file: ' + rel


def test_cluster_gaps_the_prompt_documents_still_hold():
    """SOURCE-SHAPE GUARDS (labelled per feedback_no_grep_tests: these check
    the prompt's NEGATIVE claims -- things the code must NOT yet contain.
    When someone lands the missing link, this fails, pointing at the exact
    prompt line to update rather than letting the doc go stale)."""
    entry = open(os.path.join(ROOT, 'hart_intelligence_entry.py'), encoding='utf-8').read()
    lm = open(os.path.join(ROOT, 'core/peer_link/link_manager.py'), encoding='utf-8').read()
    assert '/api/peer-link/message' not in entry, \
        'the inbound PeerLink route now EXISTS -- update the cluster section (dial-only claim is stale)'
    assert '/api/peer-link/message' in lm, \
        'link_manager fallback contract moved -- update hook 5 in the prompt'
    guard = open(os.path.join(ROOT, 'security/hive_guardrails.py'), encoding='utf-8').read()
    assert 'MAX_SKILL_PACKETS_PER_HOUR' in guard and 'MIN_WITNESS_COUNT_FOR_RALT' in guard, \
        'skill-packet gate constants renamed -- update the prompt'
    disp = open(os.path.join(ROOT, 'integrations/agent_engine/dispatch.py'), encoding='utf-8').read()
    assert 'dispatch_goal_distributed' in disp and '_has_hive_peers' in disp, \
        'distributed dispatch surface renamed -- update the prompt'


def test_cluster_recipe_reuse_over_a2a_matches_prompt():
    """The prompt's POSITIVE claim (flipped 2026-07-17 from 'NEVER as
    coded'): cross-node recipe REUSE works over the A2A surface. The
    outbound client exists with the exact surface the doc names, the
    daemon wires it before the CREATE-vs-REUSE split, and banking rides
    the ONE recipe_sync envelope writer."""
    p = _mod('integrations.google_a2a.peer_reuse')
    for fn in ('discover_peer_agent', 'pull_recipe', 'invoke_peer_agent',
               'try_peer_recipe_reuse', 'build_agent_directory',
               'export_allowed', 'peer_reuse_enabled'):
        assert callable(getattr(p, fn)), \
            'peer_reuse surface drifted from the prompt: ' + fn
    assert p.PEER_REUSE_ENV == 'HEVOLVE_A2A_PEER_REUSE', \
        'the gate knob the prompt documents was renamed -- update the prompt'
    a = _mod('integrations.agent_engine.agent_daemon')
    assert callable(getattr(a, '_try_peer_recipe_reuse')), \
        'daemon peer-reuse hook gone -- update the cluster section'
    rs = _mod('core.recipe_sync')
    assert callable(getattr(rs, 'write_envelope_files')), \
        'the ONE envelope writer moved -- update the prompt'
    assert callable(getattr(rs, 'envelope_checksum')), \
        'peer pull integrity helper moved -- update the prompt'


def test_capability_mesh_surface_matches_prompt():
    """The proactive advert layer the cluster section now claims: the recipe
    topic const lives in the canonical registry, is aliased into TOPIC_MAP for
    WAMP-readiness, and peer_reuse exposes the four mesh functions. A rename
    breaks the prompt's 'publish on bank -> cache advert -> consult before
    sweep' story, so it breaks here first."""
    c = _mod('core.constants')
    assert getattr(c, 'RECIPE_AVAILABLE_TOPIC', '') == 'com.hertzai.hevolve.recipe.available'
    assert hasattr(c, 'RECIPE_SEMANTIC_TOPIC')
    mb = open(os.path.join(ROOT, 'core/peer_link/message_bus.py'), encoding='utf-8').read()
    assert "'recipe.available'" in mb and 'RECIPE_AVAILABLE_TOPIC' in mb, \
        'the recipe topic is no longer aliased into TOPIC_MAP (WAMP-readiness lost)'
    pr = _mod('integrations.google_a2a.peer_reuse')
    for fn in ('announce_recipe_available', 'on_recipe_available_advert',
               'advert_for', 'consume_advert'):
        assert callable(getattr(pr, fn, None)), 'peer_reuse.%s (mesh function) is gone' % fn


def test_standalone_entry_starts_the_engine():
    """The prompt's 'know who starts the daemon' claim.

    This used to assert the OPPOSITE -- that hart_intelligence_entry does NOT
    start the engine -- and named that a 'gap'.  The gap was real and load
    bearing: `init_social` delegates engine init to its caller, Nunba's
    bootstrap picked it up, and this standalone launcher never did.  Every
    Docker / OS deployment therefore booted with no daemon supervisor and no
    Phase-2 goal bootstrap, so seeded goals sat `active` and nothing ever
    dispatched.  Wired in 2026-07-21; this test now guards the fix.
    """
    src = open(os.path.join(ROOT, 'hart_intelligence_entry.py'), encoding='utf-8').read()
    assert 'init_agent_engine(app)' in src, \
        ('hart_intelligence_entry no longer starts the agent engine -- Docker '
         'and OS nodes will seed goals that never dispatch')
    fly = open(os.path.join(ROOT, 'scripts/run_flywheel_dev.py'), encoding='utf-8').read()
    assert 'init_agent_engine' in fly, \
        'run_flywheel_dev no longer starts the engine -- update HIVE_COLLAB_BOOTSTRAP.md'
