"""The coding goal's prompt is a whole programmer's workflow.

Owner, 2026-11-21: "be very detailed, preempting all possible coding use
cases, and create the workflow.  We are creating a coding harness from a
generic meta-harness via meta-prompting."  The generic /chat CREATE/REUSE
pipeline becomes a coding harness through ONE prompt, the coding goal's
(integrations.agent_engine.goal_manager._build_coding_prompt), which the
coding daemon dispatches for every active coding goal.  Until this test it
said only "Clone the repo, analyze the codebase, and make improvements".

Pinned here, each by calling the real builder:
  1. the default prompt (no kind) carries steps 0 to 11 in order, the
     completion rule, the kind index and step 1's classify instruction, and
     it puts the goal before the workflow before the platform identity: a
     CREATE step carries this prompt as its "Latest User message" cut to its
     first AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE tokens;
  2. a goal naming its kind carries that kind's playbook and no other's; a
     word that is not a kind is classified like no kind;
  3. every tool the prompt tells the agent to call (a backticked name) is in
     the tool set a coding goal's agents get, resolved through the real
     builders: build_core_tool_closures, register_request_tools,
     CREATE_ADVERTISED_TOOLS (execute_windows_or_android_command, inline in
     both legs), and the service tools the coding goal TYPE's tool tags
     unlock (get_tool_tags -> resolve_goal_tags -> filter_service_tools, the
     path goal_manager.daemon_turn_stored_tags feeds both builders);
  4. a goal naming no repository is still not sent (the 2026-10-05 strangers'
     clones, test_coding_goal_names_its_repository.py); a new project names
     ONE plain folder of the coding workspace and forbids cloning;
  5. the size: the planning call that turns the prompt into the agent's
     recipe (hartos.gather_agentdetails' autonomous plan writer, which
     carries the whole prompt in its system message AND as its user turn)
     leaves room for the plan in the 4B's window.

MEASURED 2026-11-21 (core.token_utils counts): the old prompt was 2,845
tokens, 849 of them the platform identity and 1,617 the hive SDK block, both
kept.  The new one is 3,867-3,945 tokens for a short goal.  The plan writer's
own system text is 2,031 tokens, so its call holds 2,031 + 2 x prompt: at most
9,918 for a short goal, leaving 1,858 of a 12,288 window (less the 512 the
server adds) for the plan; with the title and description at build_prompt's
caps (200 and 2,000 characters) it leaves 758.  At the 8,192 and 4,096 tiers
(core.llama_geometry.CTX_TIERS) the identity and hive blocks alone
(2 x 2,466) do not fit beside the plan writer's text; the old prompt did not
fit there either (2 x 2,845 + 2,031 + 512 = 8,233).
"""
import re

import pytest

from tests.unit.test_a_node_without_a_coding_tool_is_not_offered_one import _ctx

#: The steps, in order, as the prompt numbers them.
STEPS = ('0. Scope and safety', '1. Requirements', '2. Survey', '3. Environment',
         '4. Baseline', '5. Plan', '6. Test first', '7. Implement', '8. Verify',
         '9. Self-review', '10. Deliver', '11. Remember')
#: A backticked bare identifier is a tool the prompt tells the agent to call.
TOOL_REF = re.compile(r'`([A-Za-z_][A-Za-z0-9_]*)`')


def _goal(title='Fix the login timeout bug',
          description='Sign-in hangs for 30 s when the auth server is slow.',
          **config):
    return {'id': 'g-prompt', 'goal_type': 'coding', 'title': title,
            'description': description, 'config_json': config}


def _prompt(goal):
    from integrations.agent_engine.goal_manager import GoalManager
    return GoalManager.build_prompt(goal)


def _cases(title=None, description=None):
    """(name, goal) for every shape of coding goal: no kind (the index), each
    kind, a new project, and a local repository with a readable folder."""
    from integrations.agent_engine.goal_manager import CODING_PLAYBOOKS
    text = {}
    if title is not None:
        text = {'title': title, 'description': description}
    out = [('default', _goal(**text, repo_url='https://github.com/owner/app.git'))]
    out += [(kind, _goal(**text, repo_url='https://github.com/owner/app',
                         coding_kind=kind))
            for kind in CODING_PLAYBOOKS if kind != 'new_project']
    out.append(('new_project', _goal(**text, coding_kind='new_project',
                                     project_dir='todo_api')))
    out.append(('repo_path+files_root', _goal(**text, repo_path='C:/work/app',
                                              files_root='C:/work/app')))
    return out


def _own_text(prompt):
    """The prompt without the two blocks it carries from elsewhere: the hive
    SDK instructions and the platform identity."""
    from integrations.agent_engine.goal_manager import _get_platform_identity
    from integrations.agent_engine.hive_sdk_spec import get_hive_embedding_instructions
    return (prompt.replace(get_hive_embedding_instructions(), '')
            .replace(_get_platform_identity(), ''))


# ── 1. the workflow, the completion rule, the index ──────────────────────

def test_the_default_prompt_carries_the_steps_in_order_and_the_done_rule():
    prompt = _prompt(_goal(repo_url='https://github.com/owner/app.git'))
    at = [prompt.find(step) for step in STEPS]
    assert -1 not in at, [s for s, i in zip(STEPS, at) if i < 0]
    assert at == sorted(at), list(zip(STEPS, at))
    assert 'An applied edit is not done' in prompt
    from integrations.agent_engine.goal_manager import (
        CODING_CLASSIFY_INSTRUCTION, CODING_DONE_RULE, CODING_PLAYBOOKS)
    assert CODING_DONE_RULE in prompt
    # The index names every kind, and step 1 starts by classifying.
    index = prompt[prompt.index('KINDS (index)'):]
    assert all(kind in index for kind in CODING_PLAYBOOKS), index[:400]
    step1 = prompt[prompt.index('1. Requirements'):prompt.index('2. Survey')]
    assert step1.startswith('1. Requirements. ' + CODING_CLASSIFY_INSTRUCTION), step1[:200]


def test_the_goal_comes_before_the_workflow_and_the_identity_comes_last():
    """A CREATE step keeps only the head of this prompt (hartos.helper's
    TokenLimiter, AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE): the identity used to
    come first and filled it."""
    prompt = _prompt(_goal(repo_url='owner/repo'))
    goal_at = prompt.index('Goal: Fix the login timeout bug')
    assert goal_at < prompt.index('0. Scope and safety') < prompt.index('WHO WE ARE')
    assert prompt.index('HIVE INTELLIGENCE EMBEDDING') < prompt.index('WHO WE ARE')


def test_trueflow_guidance_lives_in_the_verify_step():
    prompt = _prompt(_goal(repo_url='owner/repo'))
    verify = prompt[prompt.index('8. Verify'):prompt.index('9. Self-review')]
    assert 'TrueflowPlugin' in verify and 'pytest --cov' in verify, verify


# ── 2. a named kind carries its own playbook ─────────────────────────────

def test_a_bug_fix_goal_carries_its_playbook_and_no_other():
    prompt = _prompt(_goal(repo_url='owner/repo', coding_kind='bug_fix'))
    assert 'Reproduce it first' in prompt, prompt[:300]
    from integrations.agent_engine.goal_manager import (
        CODING_CLASSIFY_INSTRUCTION, CODING_PLAYBOOKS)
    assert CODING_PLAYBOOKS['bug_fix'] in prompt
    others = [k for k, text in CODING_PLAYBOOKS.items()
              if text != CODING_PLAYBOOKS['bug_fix'] and text in prompt]
    assert not others, others
    assert CODING_CLASSIFY_INSTRUCTION not in prompt
    assert 'KINDS (index)' not in prompt


def test_a_word_that_is_not_a_kind_is_classified_like_no_kind():
    prompt = _prompt(_goal(repo_url='owner/repo', coding_kind='make it better'))
    assert 'Classify this goal into exactly one kind' in prompt, prompt[:300]
    assert 'make it better' not in prompt
    from integrations.agent_engine.goal_manager import CODING_CLASSIFY_INSTRUCTION
    assert CODING_CLASSIFY_INSTRUCTION in prompt


# ── 3. every tool it names is one the agents have ────────────────────────

class _Seat:
    """Records the names a registration puts on an agent's schema."""

    def __init__(self):
        self.names = []

    def register_for_llm(self, name=None, description=None, **_):
        self.names.append(name)
        return lambda func: func

    def register_for_execution(self, name=None, **_):
        return lambda func: func


@pytest.fixture
def coding_goal_tools(monkeypatch):
    """The tool names a coding goal's agents get on a node that can run
    coding tools, through the builders create_recipe and reuse_recipe use."""
    from integrations.coding_agent import orchestrator
    monkeypatch.setattr(orchestrator, 'coding_capabilities',
                        lambda *a, **k: {'runs_tasks': True, 'maps_repos': True})
    from core.agent_tools import build_core_tool_closures
    from core.agent_tool_menu import (
        CREATE_ADVERTISED_TOOLS, filter_service_tools, register_request_tools)
    names = {n for n, _d, _f in build_core_tool_closures(_ctx())}
    names |= set(CREATE_ADVERTISED_TOOLS)

    from integrations.service_tools import (
        AceStepTool, Crawl4AITool, GhPrTool, SeoAuditTool,
        service_tool_registry as registry)
    from integrations.service_tools.registry import sole_endpoint_function
    # The registry is process-wide: register into a copy, and never probe a
    # service over the network.
    monkeypatch.setattr(registry, '_tools', dict(registry._tools))
    monkeypatch.setattr(registry, '_health_check', lambda info: False)
    seat = _Seat()
    register_request_tools(seat, _Seat(), registry, set())
    names |= set(seat.names)
    # The service tools create_recipe and reuse_recipe register...
    from integrations.agent_engine.thought_experiment_tools import ExperimentVoteTool
    from integrations.coding_agent.backend_repair_tools import BackendRepairTool
    for tool in (Crawl4AITool, AceStepTool, SeoAuditTool, GhPrTool,
                 ExperimentVoteTool, BackendRepairTool):
        tool.register()
    # ...kept by the coding goal type's tool tags, the way a daemon turn's
    # are resolved (daemon_turn_stored_tags -> resolve_goal_tags).
    from integrations.agent_engine.goal_manager import get_tool_tags
    from integrations.agent_engine.marketing_tools import resolve_goal_tags
    kept = filter_service_tools(resolve_goal_tags(get_tool_tags('coding'), ''),
                                registry.get_all_tool_functions(),
                                registry.get_tool_definitions(), registry)
    names |= set(kept)
    # A step may name a one-endpoint service tool by its tool name.
    names |= {name for name, tool in registry._tools.items()
              if sole_endpoint_function(name, tool) in kept}
    return names


def test_every_tool_the_prompt_names_is_one_a_coding_goal_agent_has(coding_goal_tools):
    first = _prompt(_goal(repo_url='owner/repo'))
    assert '`execute_coding_task`' in first, 'the prompt names no tool to call'
    for name, goal in _cases():
        prompt = _prompt(goal)
        named = set(TOOL_REF.findall(_own_text(prompt)))
        assert {'execute_coding_task', 'get_repository_map', 'ask_person',
                'execute_windows_or_android_command'} <= named, (name, named)
        missing = named - coding_goal_tools
        assert not missing, (name, sorted(missing))
        # The shell tool of the LangChain path is no autogen agent's tool.
        assert 'Shell_Command' not in prompt, name


def test_the_goal_folder_reader_is_named_only_when_the_goal_names_its_folder():
    """get_user_uploaded_file opens a file by name only inside the goal's
    config files_root (integrations.agent_engine.goal_files)."""
    with_folder = _prompt(_goal(repo_path='C:/work/app', files_root='C:/work/app'))
    without = _prompt(_goal(repo_path='C:/work/app'))
    assert '`get_user_uploaded_file`' in with_folder
    assert 'get_user_uploaded_file' not in without


# ── 4. no repository: not sent; a new project: its own folder ────────────

@pytest.mark.parametrize('config', [
    {'repo_url': ''},
    {'coding_kind': 'feature'},
    {'coding_kind': 'new_project'},
    {'coding_kind': 'new_project', 'project_dir': ''},
    {'coding_kind': 'new_project', 'project_dir': '../outside'},
    {'coding_kind': 'new_project', 'project_dir': 'C:/Windows'},
    {'coding_kind': 'new_project', 'project_dir': 'a/b'},
    {'coding_kind': 'new_project', 'project_dir': '..'},
    {'coding_kind': 'feature', 'project_dir': 'todo_api'},
])
def test_a_goal_that_names_no_workspace_is_not_sent(config):
    assert _prompt(_goal(**config)) is None


def test_a_new_project_names_its_folder_and_forbids_cloning():
    prompt = _prompt(_goal(coding_kind='new_project', project_dir='todo_api'))
    assert prompt is not None, 'a new project with its folder is not sent'
    head = prompt[:prompt.index('TOOLS')]
    assert 'todo_api' in head and 'coding workspace' in head, head
    assert 'Never clone any repository' in head, head
    assert 'Clone the repo' not in prompt
    from integrations.agent_engine.goal_manager import CODING_PLAYBOOKS
    assert CODING_PLAYBOOKS['new_project'] in prompt


# ── 5. it fits the planning call in the 4B's window ──────────────────────

def _longest_shipped_coding_goal():
    """(title, description) of the coding goal HARTOS ships with the longest
    description: the seeds and the loophole remediation templates."""
    from integrations.agent_engine.goal_seeding import (
        LOOPHOLE_REMEDIATION_MAP, SEED_BOOTSTRAP_GOALS)
    shipped = [g for g in list(SEED_BOOTSTRAP_GOALS) + list(LOOPHOLE_REMEDIATION_MAP.values())
               if g['goal_type'] == 'coding']
    longest = max(shipped, key=lambda g: len(g['description']))
    return longest['title'], longest['description']


def test_the_planning_call_leaves_room_for_the_plan_in_the_4b_window():
    """The plan writer (gather_agentdetails.create_agents_for_user, autonomous)
    carries the whole prompt in its system message and again as its user
    turn.  At core.llama_geometry.ctx_cap() -- 12,288, the window the 4B runs
    agent turns in (n_ctx_slot measured on the owner's desktop) and the cap
    every tier is clamped to -- that call, plus what llama-server adds to the
    wire's count, leaves at least one message's worth
    (AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE) for the plan it writes."""
    from core.constants import AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE
    from core.llama_geometry import ctx_cap
    from core.llm_outbound_logger import _PROMPT_OVERHEAD_TOKENS
    from core.token_utils import count_tokens_for_text
    from hartos.gather_agentdetails import create_agents_for_user
    from integrations.agent_engine.goal_manager import get_tool_tags
    title, description = _longest_shipped_coding_goal()
    short = []
    for name, goal in _cases(title, description):
        prompt = _prompt(goal)
        writer, _proxy = create_agents_for_user(
            f'size-{name}', autonomous=True, initial_description=prompt,
            goal_tags=get_tool_tags('coding'))
        assert prompt in writer.system_message
        used = (count_tokens_for_text(writer.system_message)
                + count_tokens_for_text(prompt) + _PROMPT_OVERHEAD_TOKENS)
        if ctx_cap() - used < AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE:
            short.append((name, used, ctx_cap() - used))
    assert not short, short
