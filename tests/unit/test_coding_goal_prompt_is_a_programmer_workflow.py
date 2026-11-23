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

@pytest.fixture(autouse=True)
def coding_workspace(tmp_path, monkeypatch):
    """The node's coding workspace, empty: a new project's folder is looked
    for there before the goal's first turn (_coding_workspace), never in the
    machine's own."""
    import core.platform_paths as paths
    monkeypatch.setattr(paths, 'get_coding_workspace_dir', lambda: str(tmp_path))
    return tmp_path


def _coding_caps(monkeypatch, runs_tasks=True, maps_repos=True, runs_commands=True):
    """What the orchestrator's last background check found on this node
    (coding_capabilities), the one input of the withheld rule."""
    from integrations.coding_agent import orchestrator
    monkeypatch.setattr(orchestrator, 'coding_capabilities',
                        lambda *a, **k: {'runs_tasks': runs_tasks,
                                         'maps_repos': maps_repos,
                                         'runs_commands': runs_commands})


@pytest.fixture(autouse=True)
def every_coding_tool_runs_here(monkeypatch):
    """A node that can run every coding tool, unless a test says otherwise."""
    _coding_caps(monkeypatch)


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
    _coding_caps(monkeypatch)
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
                'execute_windows_or_android_command', 'Shell_Command'} <= named, (name, named)
        missing = named - coding_goal_tools
        assert not missing, (name, sorted(missing))


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
    # Windows device names, with or without an extension, any case, and a
    # name Windows shortens (a trailing dot or space): review of c461aaacc.
    {'coding_kind': 'new_project', 'project_dir': 'CON'},
    {'coding_kind': 'new_project', 'project_dir': 'nul'},
    {'coding_kind': 'new_project', 'project_dir': 'Nul.txt'},
    {'coding_kind': 'new_project', 'project_dir': 'aux.tar.gz'},
    {'coding_kind': 'new_project', 'project_dir': 'PRN'},
    {'coding_kind': 'new_project', 'project_dir': 'com1'},
    {'coding_kind': 'new_project', 'project_dir': 'COM9.log'},
    {'coding_kind': 'new_project', 'project_dir': 'lpt1'},
    {'coding_kind': 'new_project', 'project_dir': 'LPT9'},
    {'coding_kind': 'new_project', 'project_dir': 'todo.'},
    {'coding_kind': 'new_project', 'project_dir': 'todo '},
    {'coding_kind': 'new_project', 'project_dir': 'CON '},
    {'coding_kind': 'new_project', 'project_dir': 'nul.'},
])
def test_a_goal_that_names_no_workspace_is_not_sent(config):
    assert _prompt(_goal(**config)) is None


@pytest.mark.parametrize('name', ['console', 'com10', 'lpt0', 'nullable',
                                  'auxiliary.py', 'todo_api', 'my.app'])
def test_a_plain_name_that_only_looks_reserved_is_a_new_project(name):
    """Only the device names themselves are refused, not names that start
    with one."""
    assert _prompt(_goal(coding_kind='new_project', project_dir=name)) is not None


def test_a_new_project_never_starts_in_a_folder_that_holds_files(coding_workspace):
    """Before its first turn a new project's folder must be missing or empty:
    scaffolding into someone's folder writes over their files (review of
    c461aaacc).  Once the goal has run, the files are its own."""
    folder = coding_workspace / 'todo_api'
    folder.mkdir()
    assert _prompt(_goal(coding_kind='new_project', project_dir='todo_api')) is not None
    (folder / 'notes.txt').write_text('mine', encoding='utf-8')
    assert _prompt(_goal(coding_kind='new_project', project_dir='todo_api')) is None
    ran = dict(_goal(coding_kind='new_project', project_dir='todo_api'),
               last_dispatched_at='2026-11-22T01:00:00')
    assert _prompt(ran) is not None
    # A repository goal is never held by a folder of the same name.
    assert _prompt(_goal(repo_url='https://github.com/owner/todo_api.git')) is not None


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


# ── 6. commands run on the gated command path, in the background ─────────

def _tools_block(prompt):
    return prompt[prompt.index('TOOLS'):prompt.index('WORKFLOW')]


def _line(tools, start):
    return next(line for line in tools.splitlines() if line.startswith(start))


def test_a_command_runs_on_the_gated_command_tool_never_the_screen():
    """Shell_Command (core.agent_tools -> _handle_shell_command_tool: the
    owner's permission, the safety list, the indicator, run_bounded) runs a
    command with no window; the screen tool is for looking at an app."""
    tools = _tools_block(_prompt(_goal(repo_url='https://github.com/owner/app.git')))
    command = _line(tools, '- `Shell_Command`')
    assert 'background with no window' in command and "owner's permission" in command, command
    assert 'in app,' in command, command
    screen = _line(tools, '- `execute_windows_or_android_command`')
    assert 'never for commands' in screen, screen
    assert 'run a command' not in screen, screen


def test_the_limits_the_prompt_quotes_are_the_ones_the_tool_passes(monkeypatch):
    """The prompt reads CODING_COMMAND_TIMEOUT_S and
    CODING_COMMAND_OUTPUT_CHARS, the bounds core.agent_tools' Shell_Command
    hands the handler, never a number of its own."""
    import core.constants as constants
    monkeypatch.setattr(constants, 'CODING_COMMAND_TIMEOUT_S', 77)
    monkeypatch.setattr(constants, 'CODING_COMMAND_OUTPUT_CHARS', 1234)
    command = _line(_tools_block(_prompt(_goal(repo_url='owner/repo'))), '- `Shell_Command`')
    assert '77 s each' in command and '(1234 characters)' in command, command
    assert '30 s' not in command and '2000' not in command, command


def test_python_is_the_interpreter_this_process_runs_workers_with(monkeypatch):
    """The installed app's sys.executable is the app itself; the bundled
    python-embed is what core.venv_paths.venv_creator_python names."""
    import core.venv_paths as venv_paths
    # Quoted: cmd.exe splits an unquoted "Program Files (x86)" at its spaces
    # (review of 2982773f7).
    bundled = r'C:\Program Files (x86)\HevolveAI\Nunba\python-embed\python.exe'
    monkeypatch.setattr(venv_paths, 'venv_creator_python', lambda: bundled)
    command = _line(_tools_block(_prompt(_goal(repo_url='owner/repo'))), '- `Shell_Command`')
    assert f'Python here is "{bundled}"' in command, command
    monkeypatch.setattr(venv_paths, 'venv_creator_python', lambda: None)
    command = _line(_tools_block(_prompt(_goal(repo_url='owner/repo'))), '- `Shell_Command`')
    assert 'Python here' not in command, command


def test_the_clone_is_made_with_working_dir_empty():
    """The clone folder is the workspace the prompt names, and it does not
    exist until the clone: the command that makes it runs with working_dir
    empty (a command in a missing folder is refused, never run in the
    process folder; review of 2982773f7)."""
    command = _line(_tools_block(_prompt(_goal(repo_url='https://github.com/owner/app.git'))),
                    '- `Shell_Command`')
    assert 'missing folder (the clone, a new project) with working_dir empty' in command, command


def test_a_failed_capability_read_still_builds_the_prompt(monkeypatch):
    """The other two readers of the withheld rule (build_core_tool_closures,
    executor_refusal) treat a failed read as nothing withheld; the prompt
    raised instead (review of 2982773f7)."""
    from integrations.coding_agent import orchestrator

    def broken(*a, **k):
        raise RuntimeError('capability check broke')
    monkeypatch.setattr(orchestrator, 'coding_capabilities', broken)
    prompt = _prompt(_goal(repo_url='owner/repo'))
    assert prompt and '- `Shell_Command`' in _tools_block(prompt), prompt


# ── 7. a node that cannot run a coding tool is not told to use it ────────

@pytest.mark.parametrize('caps', [
    dict(runs_tasks=False, maps_repos=False, runs_commands=False),
    dict(runs_tasks=False, maps_repos=True, runs_commands=True),
    dict(runs_tasks=True, maps_repos=False, runs_commands=True),
    dict(runs_tasks=True, maps_repos=True, runs_commands=False),
    dict(runs_tasks=False, maps_repos=False, runs_commands=True),
], ids=['none', 'no-edit', 'no-map', 'no-command', 'command-only'])
def test_a_tool_this_node_cannot_run_is_not_named(monkeypatch, caps):
    """core.agent_tool_menu.withheld_coding_tools, the rule every tool build
    reads: execute_coding_task needs 'runs_tasks', get_repository_map
    'maps_repos', Shell_Command 'runs_commands'.  No leg offers a withheld
    one, so no prompt names it (no dead step), the script playbook and
    steps 2 and 7 included; a command then goes to a tool that is offered."""
    _coding_caps(monkeypatch, **caps)
    goals = _cases() + [('script', _goal(repo_url='owner/repo',
                                         coding_kind='script_or_data_task'))]
    for name, goal in goals:
        named = set(TOOL_REF.findall(_own_text(_prompt(goal))))
        assert ('execute_coding_task' in named) == caps['runs_tasks'], (name, named)
        assert ('get_repository_map' in named) == caps['maps_repos'], (name, named)
        assert ('Shell_Command' in named) == caps['runs_commands'], (name, named)
        assert 'ask_person' in named, (name, named)
    prompt = _prompt(_goal(repo_url='owner/repo'))
    step7 = prompt[prompt.index('7. Implement'):prompt.index('8. Verify')]
    if not caps['runs_tasks']:
        assert 'execute_coding_task' not in step7, step7
    if not caps['runs_commands']:
        tools = _tools_block(prompt)
        command = _line(tools, '- Commands')
        want = '`execute_coding_task`' if caps['runs_tasks'] else '`ask_person`'
        assert want in command, command


# ── 8. read-only kinds install nothing and scaffold nothing ──────────────

def _steps_3_and_4(prompt):
    return prompt[prompt.index('3. Environment'):prompt.index('5. Plan')]


@pytest.mark.parametrize('kind', ['code_review', 'explain_or_explore'])
def test_a_read_only_kind_installs_nothing_and_adds_no_framework(kind):
    steps = _steps_3_and_4(_prompt(_goal(repo_url='owner/repo', coding_kind=kind)))
    assert 'install nothing' in steps, steps
    assert 'install declared dependencies' not in steps, steps
    assert 'standard framework' not in steps, steps


def test_a_writing_kind_keeps_its_environment_and_baseline():
    steps = _steps_3_and_4(_prompt(_goal(repo_url='owner/repo', coding_kind='feature')))
    assert 'install declared dependencies' in steps
    assert "add the stack's standard framework and a smoke test" in steps
    assert 'only reads' not in steps


@pytest.mark.parametrize('kind', ['', 'script_or_data_task'])
def test_a_goal_that_may_only_read_is_told_when_to_skip(kind):
    config = {'coding_kind': kind} if kind else {}
    steps = _steps_3_and_4(_prompt(_goal(repo_url='owner/repo', **config)))
    assert 'install declared dependencies' in steps
    assert 'only reads or reports' in steps and 'skip this step' in steps, steps


# ── 9. commits take only the agent's own files ───────────────────────────

def test_the_agent_commits_only_its_own_files_by_path():
    prompt = _prompt(_goal(repo_url='owner/repo'))
    deliver = prompt[prompt.index('10. Deliver'):prompt.index('11. Remember')]
    assert 'only your own files, each staged by path' in deliver, deliver
    assert 'never git add -A' in deliver, deliver
