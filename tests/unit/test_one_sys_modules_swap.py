"""No NEW patch.dict('sys.modules', ...): use tests.unit.module_swap.swap_modules.

patch.dict on sys.modules restores the WHOLE dict on exit and so evicts
every module first imported inside the block.  Two failures measured from
that one cause (details in tests/unit/module_swap.py):

  * torch: "function '_has_torch_function' already has a docstring" -- the
    C extension stays initialised while its Python layer re-executes (the
    test_distributed_bridge CI failures);
  * stale modules: a later test patched a stale qwen3vl_backend while the
    loop re-imported a fresh one and sent real requests to 127.0.0.1:8080
    (2026-09-27).

The 108 files that use it today (447 call sites, every spelling,
measured 2026-09-28) are FROZEN below with their site counts: they may
shrink, never grow, and no other file may start.  Migrating them is
separate work.  The detector is shown to fire on each spelling first, so
this guard cannot pass by matching nothing.
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _is_sys_modules(node):
    if isinstance(node, ast.Constant) and node.value == 'sys.modules':
        return True
    return (isinstance(node, ast.Attribute) and node.attr == 'modules'
            and isinstance(node.value, ast.Name) and node.value.id == 'sys')


def _is_patch_dict(call):
    f = call.func
    return isinstance(f, ast.Attribute) and f.attr == 'dict' and (
        (isinstance(f.value, ast.Name) and f.value.id == 'patch')
        or (isinstance(f.value, ast.Attribute) and f.value.attr == 'patch'))


def sys_modules_patch_dicts(source):
    """How many patch.dict(sys.modules / 'sys.modules', ...) calls source makes."""
    return sum(1 for n in ast.walk(ast.parse(source))
               if isinstance(n, ast.Call) and _is_patch_dict(n)
               and n.args and _is_sys_modules(n.args[0]))


#: Files allowed to keep their patch.dict(sys.modules) calls, and how many.
FROZEN = {
    'tests/e2e/test_e2e_pipelines.py': 2,
    'tests/functional/test_vlm_loop_functional.py': 3,
    'tests/test_stt_stream_window.py': 4,
    'tests/test_system_introspect_tool.py': 1,
    'tests/unit/test_a2a_admitted_peer_runs_shared_agent.py': 1,
    'tests/unit/test_agent_daemon_resume_state.py': 1,
    'tests/unit/test_agent_engine.py': 1,
    'tests/unit/test_agent_environment.py': 2,
    'tests/unit/test_agent_network_resilience.py': 1,
    'tests/unit/test_agent_voice_bridge_tick.py': 3,
    'tests/unit/test_api_dashboard.py': 8,
    'tests/unit/test_api_thought_experiments.py': 13,
    'tests/unit/test_api_tracker.py': 2,
    'tests/unit/test_ask_for_help.py': 5,
    'tests/unit/test_bind_game_sound.py': 9,
    'tests/unit/test_budget_gate.py': 5,
    'tests/unit/test_channel_adapters.py': 7,
    'tests/unit/test_chat_admits_allowed_device.py': 1,
    'tests/unit/test_chat_tts_normalization.py': 2,
    'tests/unit/test_chat_tts_voiced_reply.py': 1,
    'tests/unit/test_consent_fanout_p0.py': 4,
    'tests/unit/test_consent_fanout_p1.py': 6,
    'tests/unit/test_consent_fanout_p2.py': 3,
    'tests/unit/test_consent_fanout_p3.py': 3,
    'tests/unit/test_copilot_outbound_record.py': 1,
    'tests/unit/test_daemon_liveness_authority.py': 2,
    'tests/unit/test_deployment_scenarios.py': 3,
    'tests/unit/test_desktop_socket_gate.py': 5,
    'tests/unit/test_device_access_gate.py': 1,
    'tests/unit/test_discord_adapter.py': 1,
    'tests/unit/test_dispatch.py': 41,
    'tests/unit/test_distro_tools.py': 5,
    'tests/unit/test_elided_pointer_never_reaches_the_user.py': 1,
    'tests/unit/test_embeddings.py': 1,
    'tests/unit/test_error_advice_db_dedup.py': 1,
    'tests/unit/test_error_advice_goal_pickup.py': 2,
    'tests/unit/test_error_recovery.py': 2,
    'tests/unit/test_eventbus_wamp_tts.py': 1,
    'tests/unit/test_expert_turn.py': 1,
    'tests/unit/test_failed_turn_is_not_work.py': 2,
    'tests/unit/test_federated_aggregator.py': 3,
    'tests/unit/test_gather_turn_mirrors_user_side.py': 1,
    'tests/unit/test_goal_manager.py': 35,
    'tests/unit/test_google_chat_adapter.py': 13,
    'tests/unit/test_gossip_bandwidth.py': 1,
    'tests/unit/test_hardware_adapters.py': 6,
    'tests/unit/test_hart_onboarding.py': 6,
    'tests/unit/test_hart_sdk.py': 5,
    'tests/unit/test_hive_benchmark_prover_claim_grace.py': 2,
    'tests/unit/test_hive_guardrails.py': 6,
    'tests/unit/test_http_pool_prompt_preview.py': 3,
    'tests/unit/test_imessage_adapter.py': 17,
    'tests/unit/test_immutable_audit_log.py': 2,
    'tests/unit/test_instruction_queue.py': 3,
    'tests/unit/test_lightweight_vision.py': 3,
    'tests/unit/test_liquid_ui_backend_owner.py': 1,
    'tests/unit/test_llm_watchdog_adopt_heal.py': 4,
    'tests/unit/test_luxtts_tool.py': 3,
    'tests/unit/test_mattermost_adapter.py': 5,
    'tests/unit/test_mcp_integration.py': 1,
    'tests/unit/test_media_error_classification.py': 1,
    'tests/unit/test_mention_service_read_context.py': 2,
    'tests/unit/test_mode_aware_inference.py': 37,
    'tests/unit/test_model_lifecycle.py': 8,
    'tests/unit/test_model_override_transport.py': 5,
    'tests/unit/test_origin_attestation.py': 3,
    'tests/unit/test_ota_update.py': 2,
    'tests/unit/test_p2p_flow_fixes.py': 1,
    'tests/unit/test_parallel_dispatch.py': 1,
    'tests/unit/test_peer_link.py': 5,
    'tests/unit/test_peer_link_enforcement_default.py': 1,
    'tests/unit/test_peerlink_capabilities_gpu_keys.py': 1,
    'tests/unit/test_peerlink_capabilities_use_canonical_profile.py': 2,
    'tests/unit/test_pocket_tts.py': 1,
    'tests/unit/test_polymarket_adapter.py': 3,
    'tests/unit/test_provider_breaker.py': 1,
    'tests/unit/test_regression_session_fixes.py': 1,
    'tests/unit/test_remote_desktop_signaling.py': 1,
    'tests/unit/test_remote_desktop_window_capture.py': 1,
    'tests/unit/test_remote_executor.py': 6,
    'tests/unit/test_robotics.py': 16,
    'tests/unit/test_room_presence_service.py': 1,
    'tests/unit/test_runtime_tools.py': 4,
    'tests/unit/test_security_hardening.py': 1,
    'tests/unit/test_security_swallows_are_reported.py': 2,
    'tests/unit/test_shell_desktop_apis.py': 1,
    'tests/unit/test_shell_os_apis.py': 3,
    'tests/unit/test_shell_system_apis.py': 6,
    'tests/unit/test_sidecar_liveness_is_serving.py': 1,
    'tests/unit/test_signal_adapter.py': 12,
    'tests/unit/test_source_protection.py': 1,
    'tests/unit/test_stt_cuda_runtime_fallback.py': 1,
    'tests/unit/test_sync_post_landing.py': 2,
    'tests/unit/test_telegram_adapter.py': 1,
    'tests/unit/test_temporal_perception.py': 1,
    'tests/unit/test_theme_service.py': 1,
    'tests/unit/test_token_blocklist_does_not_block_import.py': 4,
    'tests/unit/test_trading_agents.py': 2,
    'tests/unit/test_tts_router.py': 1,
    'tests/unit/test_upgrade_pipeline.py': 3,
    'tests/unit/test_vision_sidecar.py': 1,
    'tests/unit/test_voice_speak_confines_paths.py': 3,
    'tests/unit/test_vram_budget_check.py': 1,
    'tests/unit/test_web_adapter.py': 11,
    'tests/unit/test_worker_hands_help_to_the_goal.py': 1,
    'tests/unit/test_worker_tick_claims_nothing_it_would_defer.py': 1,
    'tests/unit/test_ws11_critical_fixes.py': 2,
    'tests/unit/test_ws13_runtime_wiring.py': 2,
}


@pytest.mark.parametrize('src', [
    "from unittest.mock import patch\nwith patch.dict('sys.modules', {'x': None}):\n    pass\n",
    "import sys\nfrom unittest.mock import patch\nwith patch.dict(sys.modules, {'x': None}):\n    pass\n",
    "from unittest import mock\nwith mock.patch.dict('sys.modules', {'x': None}):\n    pass\n",
    "import unittest.mock\n@unittest.mock.patch.dict('sys.modules', {'x': None})\ndef t():\n    pass\n",
])
def test_the_detector_fires_on_each_spelling(src):
    assert sys_modules_patch_dicts(src) == 1


def test_the_detector_leaves_other_patch_dicts_alone():
    src = ("import os\nfrom unittest.mock import patch\n"
           "with patch.dict(os.environ, {'A': '1'}):\n    pass\n"
           "from tests.unit.module_swap import swap_modules\n"
           "with swap_modules({'x': None}):\n    pass\n")
    assert sys_modules_patch_dicts(src) == 0


def test_hidden_modules_hides_a_module_that_is_importable():
    """The one helper really hides.  (test_distributed_bridge's own check
    hides a Nunba module HARTOS cannot import anyway, so it cannot fail.)"""
    import importlib
    from tests.conftest import hidden_modules
    importlib.import_module('colorsys')          # importable outside
    with hidden_modules('colorsys'):
        with pytest.raises(ImportError):
            importlib.import_module('colorsys')
    assert importlib.import_module('colorsys')   # and again after


def test_swap_modules_keeps_what_was_imported_inside():
    import sys
    from tests.unit.module_swap import swap_modules
    sys.modules.pop('this', None)
    with swap_modules({'colorsys': None}):
        import this  # noqa: F401  -- first imported INSIDE the block
    assert 'this' in sys.modules, 'swap_modules evicted a module imported inside it'


def test_source_guard_no_new_sys_modules_patch_dict():
    grown = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, 'tests')):
        dirnames[:] = [d for d in dirnames if d != '__pycache__']
        for fname in filenames:
            if not fname.endswith('.py'):
                continue
            path = os.path.join(dirpath, fname)
            rel = os.path.relpath(path, ROOT).replace(os.sep, '/')
            if rel == 'tests/unit/test_one_sys_modules_swap.py':
                continue
            try:
                n = sys_modules_patch_dicts(
                    open(path, encoding='utf-8', errors='replace').read())
            except SyntaxError:
                continue
            if n > FROZEN.get(rel, 0):
                grown.append(f'{rel}: {n} (allowed {FROZEN.get(rel, 0)})')
    assert grown == [], (
        "new patch.dict('sys.modules', ...) -- use "
        "tests.unit.module_swap.swap_modules (or tests.conftest.hidden_modules "
        "to hide a module).  patch.dict restores the WHOLE dict and evicts "
        "every module imported inside it: that broke torch on CI "
        "('_has_torch_function already has a docstring') and sent a mocked VLM "
        "loop to 127.0.0.1:8080 through a stale qwen3vl_backend.\n  "
        + '\n  '.join(grown))


def test_the_allowlist_only_shrinks():
    """A migrated file is dropped from FROZEN, so it cannot drift back."""
    stale = []
    for rel, allowed in FROZEN.items():
        path = os.path.join(ROOT, *rel.split('/'))
        n = 0
        if os.path.exists(path):
            try:
                n = sys_modules_patch_dicts(
                    open(path, encoding='utf-8', errors='replace').read())
            except SyntaxError:
                continue
        if n < allowed:
            stale.append(f'{rel}: {n} now, FROZEN says {allowed} -- lower it')
    assert stale == [], '\n'.join(stale)
