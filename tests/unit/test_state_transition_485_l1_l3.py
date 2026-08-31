"""Drift-guard tests for #485 L1 + L3."""
import ast
import os

HARTOS_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(rel):
    with open(os.path.join(HARTOS_ROOT, rel), 'r', encoding='utf-8') as f:
        return f.read()


def _module_assigns(src):
    out = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out[node.target.id] = node
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node
    return out


# ─── L1: chat() honors UI's create_agent flag ──────────────────────────

def test_l1_gates_on_ui_create_agent_flag():
    src = _read('hart_intelligence_entry.py')
    assert 'L1 (#485)' in src
    assert 'if create_agent or autonomous:' in src


def test_l1_dispatcher_post_block_intact():
    src = _read('hart_intelligence_entry.py')
    assert "result.get('is_create_agent')" in src
    assert "preferred_lang = _lang_change" in src
    assert "_persist_language(_lang_change)" in src


def test_dispatcher_has_no_parallel_classifier():
    src = _read('integrations/agent_engine/speculative_dispatcher.py')
    assert 'def classify_intent_only' not in src


# ─── L3: Assistant-streak escalation to Helper ─────────────────────────

def test_l3_module_state_present():
    a = _module_assigns(_read('hartos/create_recipe.py'))
    assert '_ASSISTANT_STREAK_STATE' in a
    assert '_ASSISTANT_STREAK_THRESHOLD' in a


def test_l3_threshold_sane():
    a = _module_assigns(_read('hartos/create_recipe.py'))
    th = a['_ASSISTANT_STREAK_THRESHOLD'].value.value
    assert 2 <= th <= 5


def test_l3_escalation_wired():
    src = _read('hartos/create_recipe.py')
    assert 'ASSISTANT-STREAK-ESCALATE' in src
    assert '_ASSISTANT_STREAK_STATE.get(user_prompt' in src
    assert '_ASSISTANT_STREAK_STATE.pop(user_prompt' in src


def test_l3_no_name_enumeration():
    src = _read('hartos/create_recipe.py')
    assert "'Helper', 'Executor', 'StatusVerifier', 'ChatInstructor'" not in src


def test_existing_loop_guard_unchanged():
    a = _module_assigns(_read('hartos/create_recipe.py'))
    assert a['_STATE_TRANSITION_LOOP_THRESHOLD'].value.value == 5


# ─── EARLY-TERMINATE guard (2026-05-12 c38e8b7c-... regression) ───────
#
# When ChatInstructor (UserProxyAgent) emits its
# ``default_auto_reply='TERMINATE'``, state_transition's speaker-routing
# branch ("last_speaker.name == 'ChatInstructor' → return assistant")
# fires before the original ``'TERMINATE' in messages[-1]['content']``
# check at the bottom of the function.  The Assistant LLM then sees
# "TERMINATE\nMetadata/skeleton..." as user input and emits ~5 identical
# replies until the STUCK-LOOP guard rescues the turn ~3 minutes later.
#
# Fix: a TERMINATE early-exit BEFORE the speaker-routing branch ends
# the GroupChat round whenever any agent's last content contains
# TERMINATE — matching the per-agent ``is_termination_msg`` behaviour
# autogen's GroupChatManager already honours between rounds.

def test_early_terminate_guard_present():
    src = _read('hartos/create_recipe.py')
    assert '[EARLY-TERMINATE]' in src, (
        "EARLY-TERMINATE guard missing — state_transition would loop "
        "on ChatInstructor's default_auto_reply='TERMINATE' for ~3min"
    )


def test_early_terminate_runs_before_chat_instructor_routing():
    """The TERMINATE check must POSITIONALLY precede the speaker-
    routing branch that forwards ChatInstructor messages to Assistant.
    If the order flips, the routing branch fires first and the
    TERMINATE message gets ferried to Assistant — the exact regression
    this guard exists to prevent."""
    src = _read('hartos/create_recipe.py')
    early_marker = src.find('[EARLY-TERMINATE]')
    # The speaker-routing line is the long ``or last_speaker.name == 'ChatInstructor'``
    # disjunction at ~line 2321.
    chat_instructor_branch = src.find(
        "last_speaker.name == 'ChatInstructor'"
    )
    assert early_marker > 0, "EARLY-TERMINATE marker not found"
    assert chat_instructor_branch > 0, "ChatInstructor routing branch not found"
    assert early_marker < chat_instructor_branch, (
        "EARLY-TERMINATE check must precede the ChatInstructor → "
        "Assistant routing branch.  Reversing the order re-introduces "
        "the c38e8b7c-... 3-minute loop on trivial bound-agent input."
    )
