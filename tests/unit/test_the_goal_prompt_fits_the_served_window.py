"""A goal's prompt, and the seat that carries it, fit the served model's window.

Owner, 2026-11-21, on c461aaacc's prompt size: "Auto compact and make the
agent harness context window aware of the model being served."

MEASURED 2026-11-21 (core.token_utils counts), the coding goal's prompt
(goal_manager._build_coding_prompt) at build_prompt's caps (title 200,
description 2,000 characters): 4,364 tokens, 849 of them the platform
identity and 1,617 the hive SDK block.

  * The CREATE plan writer (hartos.gather_agentdetails.create_agents_for_user,
    autonomous) carries it twice beside ~2,000 tokens of its own; at the 4B's
    12,288 window about 1,000 tokens were left for the plan it writes.
  * A CREATE step carries it as the person's words of its order ("Execute
    Action N: <step> ,Latest User message: <prompt>") and the seat's token
    limiter kept the first AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE (1,000) tokens:
    the cut ends inside workflow step 5, so steps 6-11 never reached a step.

The served window is core.llm_outbound_logger.turn_message_room: the
messages' floor of the live llama-server window (12,288 -> 3,072) for a turn
on the local llama-server, None for a hosted model (whose window nothing on
the node reads, as for core.agent_tool_menu.fit_schema_to_ctx).

Pinned here, each by calling the real code with the window mocked at its
boundary (_get_budget_per_slot, the turn's config list):
  1. on a 12,288 window a coding goal at the caps leaves the plan writer at
     least 1,500 tokens for the plan;
  2. on a large window (200,000) or a hosted model the full identity and hive
     text are in the prompt;
  3. a CREATE step on a 12,288 window receives the whole workflow, steps 0 to
     11 in order and the done rule; other messages keep the configured cap;
  4. on a large window or a hosted model every goal type's prompt is its
     builder's own text, byte for byte.
"""
import os
import sys

import pytest

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if _HARTOS not in sys.path:
    sys.path.insert(0, _HARTOS)

import core.llm_outbound_logger as wire  # noqa: E402

#: The 4B's agent-turn window (n_ctx_slot measured on the owner's desktop).
SMALL = 12288
LARGE = 200000
#: What the plan writer must have left for the plan it writes.
PLAN_ROOM = 1500
STEPS = ('0. Scope and safety', '1. Requirements', '2. Survey', '3. Environment',
         '4. Baseline', '5. Plan', '6. Test first', '7. Implement', '8. Verify',
         '9. Self-review', '10. Deliver', '11. Remember')
_ENV_KEYS = ('HEVOLVE_NODE_TIER', 'HEVOLVE_ACTIVE_CLOUD_PROVIDER',
             'HEVOLVE_LLM_ENDPOINT_URL', 'HEVOLVE_LLM_MODEL_NAME',
             'HEVOLVE_LLM_API_KEY', 'HEVOLVE_LLAMA_CTX_SIZE')
_HOSTED = [{'model': 'claude-haiku-5-5', 'base_url': 'https://api.anthropic.com/v1',
            'api_key': 'placeholder'}]


@pytest.fixture
def served(monkeypatch):
    """``served(window)`` serves this node's goal turns on the local
    llama-server with ``window`` tokens per slot; ``served(None)`` on a
    hosted model.  Both readers see it: build_prompt (the node's own list,
    core.autogen_config.get_autogen_config_list) and the seats' limiter (the
    turn's list, hartos.helper.get_llm_config)."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    import core.autogen_config as autogen_config
    import hartos.helper as helper

    def serve(window):
        if window is None:
            config_list = _HOSTED
        else:
            kind, entry = autogen_config.resolve_llm_backend()
            assert kind == 'local', entry.get('base_url')
            config_list = [entry]
            monkeypatch.setattr(wire, '_get_budget_per_slot', lambda: window)
        monkeypatch.setattr(autogen_config, 'get_autogen_config_list',
                            lambda: list(config_list))
        monkeypatch.setattr(helper, 'config_list', list(config_list))
        return config_list
    return serve


def _coding_goal_at_caps():
    """The longest coding goal HARTOS ships (seeds and loophole remediation
    templates), its title and description repeated to build_prompt's caps."""
    from integrations.agent_engine.goal_seeding import (
        LOOPHOLE_REMEDIATION_MAP, SEED_BOOTSTRAP_GOALS)
    shipped = [g for g in list(SEED_BOOTSTRAP_GOALS)
               + list(LOOPHOLE_REMEDIATION_MAP.values())
               if g['goal_type'] == 'coding']
    longest = max(shipped, key=lambda g: len(g['description']))
    return {'id': 'g-caps', 'goal_type': 'coding',
            'title': ((longest['title'] + ' ') * 10)[:200],
            'description': ((longest['description'] + ' ') * 10)[:2000],
            'config_json': {'repo_url': 'https://github.com/owner/app.git'}}


def _prompt(goal):
    from integrations.agent_engine.goal_manager import GoalManager
    return GoalManager.build_prompt(goal)


def _full_blocks():
    from integrations.agent_engine.goal_manager import _get_platform_identity
    from integrations.agent_engine.hive_sdk_spec import get_hive_embedding_instructions
    return _get_platform_identity(), get_hive_embedding_instructions()


# ── the window ───────────────────────────────────────────────────────────

def test_the_room_is_the_wire_floor_of_the_local_window_and_none_when_hosted(served):
    local = served(SMALL)
    assert wire.turn_message_room(local) == wire._min_message_budget(SMALL) == 3072
    assert wire.turn_message_room(_HOSTED) is None
    # A list that ends at the node's own llama-server (with_local_fallback)
    # is bounded by it, as the schema fit is.
    assert wire.turn_message_room(_HOSTED + local) == 3072


# ── 1. the plan writer leaves room for the plan ──────────────────────────

def test_on_the_4b_window_a_goal_at_the_caps_leaves_the_plan_its_room(served):
    served(SMALL)
    from core.token_utils import count_tokens_for_text
    from hartos.gather_agentdetails import create_agents_for_user
    from integrations.agent_engine.goal_manager import get_tool_tags
    prompt = _prompt(_coding_goal_at_caps())
    writer, _proxy = create_agents_for_user(
        'fit-caps', autonomous=True, initial_description=prompt,
        goal_tags=get_tool_tags('coding'))
    assert prompt in writer.system_message
    # The writer's system message holds the prompt, and the prompt is its
    # user turn; llama-server adds what the wire's count never sees.
    used = (count_tokens_for_text(writer.system_message)
            + count_tokens_for_text(prompt) + wire._PROMPT_OVERHEAD_TOKENS)
    assert SMALL - used >= PLAN_ROOM, (SMALL - used, count_tokens_for_text(prompt))


def test_on_the_4b_window_the_short_forms_keep_their_rules(served):
    served(SMALL)
    from integrations.agent_engine.goal_manager import CODING_DONE_RULE
    from integrations.agent_engine.hive_sdk_spec import (
        CODE_QUALITY_CONSTITUTIONAL_RULES, HIVE_SDK_SPEC_URL)
    from security.hive_guardrails import VALUES
    prompt = _prompt(_coding_goal_at_caps())
    identity, hive = _full_blocks()
    assert identity not in prompt and hive not in prompt
    # The goal's own text is never cut: every step, the done rule, test first.
    at = [prompt.find(step) for step in STEPS]
    assert -1 not in at and at == sorted(at), list(zip(STEPS, at))
    assert CODING_DONE_RULE in prompt and 'a failing behavioural test' in prompt
    # Each short form keeps its heading, its rules and where the rest is.
    assert 'HIVE INTELLIGENCE EMBEDDING' in prompt and 'WHO WE ARE' in prompt
    assert CODE_QUALITY_CONSTITUTIONAL_RULES in prompt
    assert HIVE_SDK_SPEC_URL in prompt
    assert all(line in prompt for line in VALUES.GUARDIAN_PURPOSE)


# ── 2. a large window keeps everything ───────────────────────────────────

@pytest.mark.parametrize('window', [LARGE, None], ids=['200k', 'hosted'])
def test_a_large_window_carries_the_full_identity_and_hive_text(served, window):
    served(window)
    prompt = _prompt(_coding_goal_at_caps())
    identity, hive = _full_blocks()
    assert identity in prompt and hive in prompt


# ── 3. a CREATE step receives the whole workflow ─────────────────────────

def _seat_view(messages):
    """What a CREATE seat's context limiter keeps, built as create_recipe's
    context_handling builds it."""
    from core.constants import (AUTOGEN_MESSAGE_TOKEN_BUDGET,
                                AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE)
    from hartos.helper import token_limiter
    return token_limiter(max_tokens=AUTOGEN_MESSAGE_TOKEN_BUDGET,
                         max_tokens_per_message=AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE,
                         min_tokens=0, turn_window=True).apply_transform(messages)


def _create_step(prompt):
    from hartos.create_recipe import step_instruction
    return {'role': 'user', 'name': 'UserProxy', 'content': step_instruction(
        1, 'save_data_in_memory: the numbered acceptance criteria', prompt)}


def test_a_create_step_on_the_4b_window_receives_the_whole_workflow(served):
    served(SMALL)
    from integrations.agent_engine.goal_manager import CODING_DONE_RULE
    prompt = _prompt(_coding_goal_at_caps())
    step = _create_step(prompt)
    # A later turn of the walk: the step is the task turn, not the newest.
    tool_result = {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok ' * 2000}
    verdict = {'role': 'user', 'name': 'StatusVerifier', 'content': 'pending ' * 2000}
    kept = _seat_view([step, {'role': 'assistant', 'content': None,
                              'tool_calls': [{'id': 'c1', 'type': 'function',
                                              'function': {'name': 'x', 'arguments': '{}'}}]},
                       tool_result, verdict])
    seen = next(m['content'] for m in kept if m.get('name') == 'UserProxy')
    at = [seen.find(s) for s in STEPS]
    assert -1 not in at and at == sorted(at), list(zip(STEPS, at))
    assert CODING_DONE_RULE in seen
    # Every other message keeps the configured cap, the newest user turn
    # from another seat included: only the task turn is widened.
    from core.constants import AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE
    from core.token_utils import count_tokens_for_text
    for other in [m for m in kept if m.get('name') != 'UserProxy'
                  and isinstance(m.get('content'), str)]:
        assert count_tokens_for_text(other['content']) <= AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE + 8, other.get('name')


def test_a_hosted_turn_keeps_the_configured_cut(served):
    """No window is read for a hosted model: its seats keep the configured
    caps, as before (a free hosted model may have a small window)."""
    served(None)
    from core.constants import AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE
    from core.token_utils import count_tokens_for_text
    kept = _seat_view([_create_step(_prompt(_coding_goal_at_caps()))])
    assert count_tokens_for_text(kept[-1]['content']) <= AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE + 8


# ── 4. other goal types are untouched on a large window ──────────────────

def _every_goal():
    """One goal of every registered type, the late-registered sales types
    included."""
    import integrations.agent_engine.goal_manager as gm
    for module in ('integrations.agent_engine.outreach_crm_tools',
                   'integrations.agent_engine.journey_engine'):
        __import__(module)
    return [(t, {'id': f'g-{t}', 'goal_type': t,
                 'title': 'Grow the weekly sign-ups for the speech app',
                 'description': 'Find twenty speech clinics in Chennai and write '
                                'to each one about the app. ' * 3,
                 'config_json': {'repo_url': 'https://github.com/owner/app.git'}})
            for t in sorted(gm.get_registered_types())]


@pytest.mark.parametrize('window', [LARGE, None], ids=['200k', 'hosted'])
def test_on_a_large_window_every_prompt_is_its_builders_text(served, window):
    served(window)
    import integrations.agent_engine.goal_manager as gm
    for goal_type, goal in _every_goal():
        safe = dict(goal, title=gm._sanitize_goal_input(goal['title'], 200),
                    description=gm._sanitize_goal_input(goal['description'], 2000))
        assert _prompt(goal) == gm._prompt_builders[goal_type](safe, None), goal_type
