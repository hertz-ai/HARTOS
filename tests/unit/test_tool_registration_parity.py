"""Drift-guard tests for #510 tool-name unification.

Pins the contracts shipped in this batch:

  T1   reuse_recipe.py inner tools have @log_tool_execution as the
       INNERMOST decorator (closest to def).  If autogen's
       register_for_execution / register_for_llm decorator runs above
       @log_tool_execution, the wrapper gets registered in autogen's
       function_map but the unwrapped fn is stored — wrapper is dead
       at runtime.  This pins the fix from Phase 0.
  T2   Persona registry TTLCaches are single-writer:
       agents_session / agents_roles / chat_joinees may only be
       ASSIGNED with `TTLCache(...)` in core/persona_registry.py.
       reuse_recipe.py and create_recipe.py must IMPORT, not redeclare.
  T3   Alias identity: build_core_tool_closures(ctx) returns the
       SAME closure object under both name + alias for the pairs we
       aliased in Phase 1.1 + 4.1 + 4.2 (`get_data_by_key` ↔
       `get_data_from_memory`, `text_2_image` ↔ `txt2img`,
       `get_text_from_image` ↔ `img2txt`).
  T4   register_dual `name=` override matches `func.__name__` for the
       `connect_time_main` rename in Phase 2.1.
  T5   Persona broadcast behavior: with a populated registry,
       _send_message_to_roles_impl publishes to MULTICHAT_TOPIC with
       caller metadata and returns "Message sent Successfully".
       With an empty registry, returns a helpful-error string and
       logs a warning (per "no silent gulps" directive).
  T6   No phantom tool names in helper system prompts — every
       tool-name-shaped token in reuse_recipe.py Helper / Executor /
       Helper1 / Executor1 system_message strings resolves to a
       registered name in build_core_tool_closures(ctx).
"""
from __future__ import annotations

import ast
import io
import logging
import os
import re
from unittest.mock import MagicMock

import pytest


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _read(path: str) -> str:
    return io.open(path, encoding='utf-8').read()


def _make_mock_ctx() -> dict:
    """Minimal ctx for build_core_tool_closures — same shape both flows pass."""
    return {
        'user_id': 1,
        'prompt_id': 1,
        'agent_data': {},
        'helper_fun': MagicMock(),
        'user_prompt': '1_1',
        'request_id_list': {},
        'recent_file_id': {},
        'scheduler': MagicMock(),
        'simplemem_store': None,
        'memory_graph': None,
        'log_tool_execution': lambda f: f,
        'send_message_to_user1': MagicMock(),
        'retrieve_json': MagicMock(),
        'strip_json_values': MagicMock(),
        'save_conversation_db': MagicMock(),
    }


# ─── T1: Decorator order — @log_tool_execution INNERMOST ────────────

def test_log_tool_execution_is_innermost_in_reuse_recipe():
    """Every inner function in reuse_recipe.py decorated with both
    @log_tool_execution AND an autogen register_for_execution /
    register_for_llm decorator MUST have @log_tool_execution as the
    INNERMOST decorator (closest to `def`).  Otherwise the registered
    function is unwrapped at runtime and the wrapper is dead code."""
    path = os.path.join(REPO_ROOT, 'reuse_recipe.py')
    tree = ast.parse(_read(path))
    broken = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decos = node.decorator_list
        if not decos:
            continue
        deco_names = []
        for d in decos:
            if isinstance(d, ast.Name):
                deco_names.append(d.id)
            elif isinstance(d, ast.Call):
                f = d.func
                if isinstance(f, ast.Attribute): deco_names.append(f.attr)
                elif isinstance(f, ast.Name): deco_names.append(f.id)
        if 'log_tool_execution' not in deco_names:
            continue
        has_reg = any('register_for_execution' in x or 'register_for_llm' in x
                      for x in deco_names)
        if not has_reg:
            continue
        last = decos[-1]
        last_name = last.id if isinstance(last, ast.Name) else None
        if last_name != 'log_tool_execution':
            broken.append((node.name, node.lineno))
    assert not broken, (
        f"@log_tool_execution must be INNERMOST in reuse_recipe.py — "
        f"these are still broken: {broken}.  Move the decorator to be "
        f"closest to `def` so autogen registers the wrapped function.")


# ─── T2: Persona registry single-writer ─────────────────────────────

def test_persona_caches_assigned_only_in_core_persona_registry():
    """The persona TTLCaches must be ASSIGNED only in
    core/persona_registry.py.  Drift-guard against accidental
    redeclaration in reuse_recipe.py or create_recipe.py — would
    break the single-source-of-truth invariant."""
    cache_names = ('agents_session', 'agents_roles', 'chat_joinees')
    files_to_check = ('reuse_recipe.py', 'create_recipe.py')
    for filename in files_to_check:
        path = os.path.join(REPO_ROOT, filename)
        src = _read(path)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id in cache_names:
                    # An assignment found — only allowed if it's actually an
                    # import alias (which doesn't show up as ast.Assign anyway,
                    # so any Assign hit is a redeclaration).
                    pytest.fail(
                        f"{filename}:{node.lineno} redeclares persona cache "
                        f"`{tgt.id}` — must import from core.persona_registry "
                        f"instead.  Single-writer invariant violated.")

    # Positive check: the canonical home actually has the assignments
    canon = _read(os.path.join(REPO_ROOT, 'core', 'persona_registry.py'))
    canon_tree = ast.parse(canon)
    found = set()
    for node in ast.walk(canon_tree):
        if not isinstance(node, ast.AnnAssign) and not isinstance(node, ast.Assign):
            continue
        if isinstance(node, ast.AnnAssign):
            tgt = node.target
        else:
            tgt = node.targets[0] if node.targets else None
        if isinstance(tgt, ast.Name) and tgt.id in cache_names:
            found.add(tgt.id)
    assert found == set(cache_names), (
        f"core/persona_registry.py missing canonical TTLCache assignments — "
        f"found {found}, expected {set(cache_names)}")


def test_persona_caches_are_identical_objects_across_imports():
    """When reuse_recipe and core.persona_registry are both imported,
    their `agents_session` (etc.) references must be the SAME object.
    Otherwise we have parallel caches and the single-writer invariant
    is broken at runtime."""
    from core.persona_registry import (
        agents_session as canon_session,
        agents_roles as canon_roles,
        chat_joinees as canon_joinees,
    )
    from reuse_recipe import (
        agents_session as reuse_session,
        agents_roles as reuse_roles,
        chat_joinees as reuse_joinees,
    )
    assert canon_session is reuse_session
    assert canon_roles is reuse_roles
    assert canon_joinees is reuse_joinees


# ─── T3: Alias identity — same closure object ───────────────────────

@pytest.mark.parametrize('canonical,alias', [
    ('get_data_by_key',       'get_data_from_memory'),
    ('text_2_image',          'txt2img'),
    ('get_text_from_image',   'img2txt'),
])
def test_alias_points_to_same_closure(canonical, alias):
    """Phase 1.1 + 4.1 + 4.2 aliases must register the SAME closure
    object under both names — proves behavior is identical, no
    parallel impl drift introduced."""
    from core.agent_tools import build_core_tool_closures
    tools = build_core_tool_closures(_make_mock_ctx())
    by_name = {n: f for n, _, f in tools}
    assert canonical in by_name, (
        f"canonical `{canonical}` not registered in core.agent_tools")
    assert alias in by_name, (
        f"alias `{alias}` not registered — phantom tool / drift risk")
    assert by_name[canonical] is by_name[alias], (
        f"alias `{alias}` does NOT share closure identity with "
        f"`{canonical}` — parallel path would let them drift.")


# ─── T4: register_dual name matches func.__name__ (Phase 2.1) ───────

def test_connect_time_main_register_dual_name_matches_func():
    """Phase 2.1 rename: register_dual at reuse_recipe.py:~2293 must
    use `name='connect_time_main'` (matching the function name AND the
    LLM prompt at create_recipe.py:2823).  Pre-fix it was
    `'Connect_to_main_agent'` — LLM 404'd because the prompt advertised
    the wrong name."""
    path = os.path.join(REPO_ROOT, 'reuse_recipe.py')
    src = _read(path)
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        fn_name = (f.id if isinstance(f, ast.Name)
                   else f.attr if isinstance(f, ast.Attribute)
                   else None)
        if fn_name != 'register_dual':
            continue
        # Look for the connect_time_main register call
        if len(node.args) < 4:
            continue
        third_arg = node.args[2]
        if isinstance(third_arg, ast.Name) and third_arg.id == 'connect_time_main':
            name_arg = node.args[3]
            if isinstance(name_arg, ast.Constant):
                assert name_arg.value == 'connect_time_main', (
                    f"register_dual for connect_time_main has wrong name "
                    f"override: {name_arg.value!r} (should be 'connect_time_main')")
                found = True
    assert found, (
        "register_dual(helper1, time_agent, connect_time_main, ...) not "
        "found in reuse_recipe.py")


# ─── T5: Persona broadcast behavior ──────────────────────────────────

def test_send_message_to_roles_publishes_with_role_match():
    """With a populated registry, _send_message_to_roles_impl looks up
    the persona by role, publishes to the per-user multichat topic
    (com.hertzai.hevolve.agent.multichat.{target_user_id}), and returns
    the success string."""
    from core.persona_registry import (
        register_persona_for_session,
        _send_message_to_roles_impl,
        MULTICHAT_TOPIC_BASE,
        multichat_topic_for,
    )
    register_persona_for_session('u_t5', 'p_t5', [
        {'name': 'student'}, {'name': 'parent'}, {'name': 'teacher'},
    ])
    captured = []

    def fake_publish(topic, message, *args, **kwargs):
        captured.append((topic, message))

    result = _send_message_to_roles_impl(
        'u_t5', 'p_t5', 'teacher',
        'Please review the homework', publish_fn=fake_publish)
    assert result == 'Message sent Successfully'
    assert len(captured) == 1
    expected_topic = multichat_topic_for('u_t5')
    assert captured[0][0] == expected_topic, (
        f"Expected per-user topic {expected_topic}, got {captured[0][0]}.  "
        f"Multichat topic must be suffixed with the target persona's "
        f"user_id to match HARTOS's chat/action/vision/etc. convention "
        f"and let WAMP router ACL gate cross-tenant subscription.")
    assert expected_topic.startswith(MULTICHAT_TOPIC_BASE + '.')
    payload = captured[0][1]
    assert payload['role'] == 'teacher'
    assert payload['message'] == 'Please review the homework'
    assert payload['caller_user_id'] == 'u_t5'
    assert payload['caller_prompt_id'] == 'p_t5'


def test_multichat_topic_is_per_user():
    """Direct contract test for the topic builder."""
    from core.persona_registry import multichat_topic_for, MULTICHAT_TOPIC_BASE
    assert multichat_topic_for('alice') == f'{MULTICHAT_TOPIC_BASE}.alice'
    assert multichat_topic_for(42) == f'{MULTICHAT_TOPIC_BASE}.42'
    # Different users → different topics (no cross-tenant collision)
    assert multichat_topic_for('alice') != multichat_topic_for('bob')


def test_multichat_in_message_bus_topic_map_is_templated():
    """core.peer_link.message_bus.TOPIC_MAP['agent.multichat'] must use
    the per-user template so bus.publish('agent.multichat', user_id=X)
    resolves to the per-user topic, matching the persona_registry direct-
    publish path.  Single source of truth for the wire topic."""
    from core.peer_link.message_bus import TOPIC_MAP
    template = TOPIC_MAP.get('agent.multichat')
    assert template is not None, "agent.multichat missing from TOPIC_MAP"
    assert '{user_id}' in template, (
        f"agent.multichat template {template!r} must contain {{user_id}} "
        f"placeholder so bus.publish substitutes the per-user suffix.")
    assert template.startswith('com.hertzai.hevolve.agent.multichat')


def test_send_message_to_roles_helpful_error_when_no_personas(caplog):
    """Empty registry → returns helpful error string + logs a warning.
    Per 'no silent gulps' directive: the failure path must appear in
    logs, not just return silently."""
    from core.persona_registry import _send_message_to_roles_impl
    captured = []

    def fake_publish(topic, message, *a, **kw):
        captured.append((topic, message))

    with caplog.at_level(logging.WARNING, logger='core.persona_registry'):
        # Use a key we know is not in the registry
        result = _send_message_to_roles_impl(
            'u_t5_missing', 'p_t5_missing', 'student',
            'Test', publish_fn=fake_publish)

    assert 'No personas registered' in result
    assert not captured, "publish must not fire when no personas"
    assert any('no agent_session' in r.message for r in caplog.records), (
        "expected warning log when persona session missing")


def test_send_message_to_roles_no_match_returns_helpful_error():
    """Populated registry but no persona with matching role → helpful error."""
    from core.persona_registry import (
        register_persona_for_session, _send_message_to_roles_impl,
    )
    register_persona_for_session('u_t5b', 'p_t5b', [{'name': 'student'}])
    result = _send_message_to_roles_impl(
        'u_t5b', 'p_t5b', 'principal',
        'Test', publish_fn=lambda *a, **kw: None)
    assert "No persona with role='principal'" in result


def test_send_message_to_roles_empty_role_arg():
    """Empty role arg → helpful error, no publish, log warning."""
    from core.persona_registry import _send_message_to_roles_impl
    result = _send_message_to_roles_impl(
        'u', 'p', '', 'Test', publish_fn=lambda *a, **kw: None)
    assert 'role argument is empty' in result.lower()


# ─── T6: Phantom-prompt-name check (subset for now) ─────────────────

def test_get_data_from_memory_resolves():
    """Helper system prompts in reuse_recipe.py at lines 1102, 1129,
    2151, 2174 advertise `get_data_from_memory`.  After Phase 1.1 it
    must be a registered name."""
    from core.agent_tools import build_core_tool_closures
    tools = build_core_tool_closures(_make_mock_ctx())
    names = {n for n, _, _ in tools}
    assert 'get_data_from_memory' in names


def test_send_message_to_roles_registered_in_both_flows():
    """`send_message_to_roles` MUST be registered in BOTH create + reuse
    flows.  Autonomous-mode recipe creation does real multi-persona work
    while authoring (gather-requirements has already populated the
    registry by the time create-mode autogen runs).  Same canonical impl
    from core.persona_registry behind both registrations — capability
    parity is the invariant."""
    reuse_src = _read(os.path.join(REPO_ROOT, 'reuse_recipe.py'))
    create_src = _read(os.path.join(REPO_ROOT, 'create_recipe.py'))

    # Both files reference the tool name
    assert 'def send_message_to_roles' in reuse_src, (
        "send_message_to_roles missing from reuse_recipe.py")
    assert 'def send_message_to_roles' in create_src, (
        "send_message_to_roles missing from create_recipe.py — both flows "
        "must register it for autonomous-mode parity.")

    # Both files register via register_dual (or AH-stack in reuse)
    assert 'register_dual(helper, assistant, send_message_to_roles' in create_src, (
        "send_message_to_roles must be register_dual-registered in create_recipe.py")

    # Canonical impl exists in core.persona_registry
    from core.persona_registry import _send_message_to_roles_impl
    assert callable(_send_message_to_roles_impl)


def test_tier2_goal_detection_parity():
    """Both create_recipe.py and reuse_recipe.py must use
    detect_goal_tags() for Tier-2 progressive tool injection, with the
    same 5 categories (marketing / ip_protection / self_build / outreach
    / sales).  Earlier reuse used `prompt_id.startswith()` and only
    handled 2 of 5 — recipes authored under semantic tags failed to
    replay because the matching Tier-2 modules never loaded."""
    EXPECTED_CATEGORIES = {
        'marketing', 'ip_protection', 'self_build', 'outreach', 'sales',
    }
    EXPECTED_REGISTERS = {
        'marketing': 'register_marketing_tools',
        'ip_protection': 'register_ip_protection_tools',
        'self_build': 'register_self_build_tools',
        'outreach': 'register_outreach_tools',
        'sales': 'register_journey_tools',
    }
    for filename in ('create_recipe.py', 'reuse_recipe.py'):
        src = _read(os.path.join(REPO_ROOT, filename))

        # Must use detect_goal_tags() — semantic method, not prefix string
        assert 'detect_goal_tags(' in src, (
            f"{filename} must use detect_goal_tags(...) for Tier-2 "
            f"detection.  Mirrors progressive-injection design and "
            f"makes both flows route the same tags to the same modules.")

        # Old prefix-string method must be GONE from the Tier-2 block.
        # (Keep narrow — `prompt_id.startswith` could legitimately be
        # used elsewhere.  Just assert it's not in the marketing block.)
        assert "prompt_id).startswith('marketing')" not in src, (
            f"{filename}: legacy prompt_id.startswith('marketing') check "
            f"present — Tier-2 detection must use detect_goal_tags()")

        # All 5 categories must be present
        for cat in EXPECTED_CATEGORIES:
            assert f"'{cat}' in goal_tags" in src, (
                f"{filename}: Tier-2 missing detection branch for tag "
                f"`{cat}`. Categories must match between create + reuse "
                f"or recipes authored under one tag won't replay under "
                f"the other.")

        # The registration calls must point at the right module
        for cat, reg_fn in EXPECTED_REGISTERS.items():
            assert reg_fn in src, (
                f"{filename}: missing call to {reg_fn} for the "
                f"`{cat}` Tier-2 branch")


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
