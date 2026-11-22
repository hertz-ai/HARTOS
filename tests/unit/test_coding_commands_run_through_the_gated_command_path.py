"""A coding goal's commands run through the one gated command path.

Until 2026-11-22 a coding goal ran its commands through
execute_windows_or_android_command, the computer-use loop: a screen, 30 s
and the first 2,000 characters of each stream.  0f6f65e7e moved them to
Executor code blocks, which skipped every gate (review rv a3a8836d and
hartos-52's rv_ac3a, both measured): no owner's permission, no hard-deny
list, no indicator, a timeout that stopped nothing.  It was reverted
(c5fd6f3c2), and the owner ruled the fix goes forward through the existing
mechanism (2026-11-22).

The existing mechanism is hart_intelligence_entry._handle_shell_command_tool,
the chat's Shell_Command and the computer-use loop's shell step.  A coding
goal's agents now get it as the autogen tool Shell_Command
(core.agent_tools.build_core_tool_closures), with a coding command's bounds
(CODING_COMMAND_TIMEOUT_S, CODING_COMMAND_OUTPUT_CHARS) and its workspace.

Pinned here through the real tool and the real handler, mocking only the
boundaries (the owner's consent store, the process runner where a test
must not run anything):
  * the owner says no: nothing runs;
  * a hard-denied command is refused before anyone is asked;
  * a command that hangs is stopped at its limit (a real process);
  * a failing command's exit code and summary reach the agent, inside one
    seat message;
  * the activity indicator is lit for the run;
  * it runs in the coding workspace.
"""
import asyncio
import os
import sys
import time

import pytest

from tests.unit.test_a_node_without_a_coding_tool_is_not_offered_one import _ctx

pytest.importorskip('hart_intelligence_entry')

PROMPT_ID = '8888'


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """The coding workspace the command runs in, and this turn's agent on
    the thread (the /chat handler stamps it; the handler's consent and
    indicator read it)."""
    import core.platform_paths as paths
    from hartos.threadlocal import thread_local_data
    monkeypatch.setattr(paths, 'get_coding_workspace_dir', lambda: str(tmp_path))
    monkeypatch.delenv('HEVOLVE_CODING_WORKDIR', raising=False)
    before = thread_local_data.get_prompt_id()
    thread_local_data.set_prompt_id(PROMPT_ID)
    yield tmp_path
    thread_local_data.set_prompt_id(before)


@pytest.fixture
def allowed(monkeypatch):
    """The owner has allowed agents to control this computer."""
    from integrations.vlm import safety
    monkeypatch.setattr(safety, 'computer_control_block',
                        lambda agent_id, **_kw: None)


@pytest.fixture
def indicator(monkeypatch):
    """Records what the activity indicator was told
    (integrations.vlm.activity_stream.current_run, the ribbon the owner
    sees during computer use)."""
    import integrations.vlm.activity_stream as act
    seen = []

    class Run:
        def __init__(self, **ids):
            seen.append(('open', ids))

        def step(self, **kw):
            seen.append(('step', kw))

        def finish(self, **kw):
            seen.append(('finish', kw))

    monkeypatch.setattr(act, 'current_run', lambda **ids: Run(**ids))
    return seen


def _tool(monkeypatch):
    """The coding leg's Shell_Command, built the way create_recipe and
    reuse_recipe build their tools, on a node where it is offered."""
    from integrations.coding_agent import orchestrator
    monkeypatch.setattr(orchestrator, 'coding_capabilities',
                        lambda *a, **k: {'runs_tasks': True, 'maps_repos': True,
                                         'runs_commands': True})
    from core.agent_tools import build_core_tool_closures
    tools = {n: f for n, _d, f in build_core_tool_closures(_ctx())}
    assert 'Shell_Command' in tools, sorted(tools)
    return lambda command, **kw: asyncio.run(tools['Shell_Command'](command, **kw))


def _script(folder, name, body):
    path = folder / name
    path.write_text(body, encoding='utf-8')
    return f'{sys.executable} {name}'


def test_the_owner_says_no_and_nothing_runs(workspace, monkeypatch):
    """The real consent gate (computer_control_block) with the owner's
    answer read as "Don't allow" from the consent store."""
    import hart_intelligence_entry as hie
    from integrations.vlm import safety
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-1')
    monkeypatch.setattr(safety, '_computer_control_answer',
                        lambda owner, agent, reason: False)
    monkeypatch.setattr(safety, '_computer_control_reason', lambda agent: 'r')
    ran = []
    monkeypatch.setattr(hie, 'run_bounded', lambda *a, **k: ran.append(a))
    marker = workspace / 'ran.txt'
    reply = _tool(monkeypatch)(_script(workspace, 'w.py',
                                       f"open(r'{marker}', 'w').write('x')\n"))
    assert reply.startswith('Shell_Command not run:'), reply
    assert 'said no' in reply, reply
    assert ran == [] and not marker.exists()


def test_a_hard_denied_command_is_refused_before_anyone_is_asked(workspace, monkeypatch):
    import hart_intelligence_entry as hie
    from integrations.vlm import safety
    asked, ran = [], []
    monkeypatch.setattr(safety, 'computer_control_block',
                        lambda agent_id, **kw: asked.append(agent_id))
    monkeypatch.setattr(hie, 'run_bounded', lambda *a, **k: ran.append(a))
    tool = _tool(monkeypatch)
    for command in ('shutdown /r /t 0', 'rm -rf /', 'format c:',
                    'taskkill /im python.exe /f'):
        reply = tool(command)
        assert reply.startswith('Shell_Command refused'), (command, reply)
    assert asked == [] and ran == []


def test_a_hung_command_is_stopped_at_its_limit(workspace, allowed, monkeypatch):
    """A real child process: run_bounded kills it at CODING_COMMAND_TIMEOUT_S
    (here 2 s), not when it would have ended (60 s)."""
    import core.constants as constants
    monkeypatch.setattr(constants, 'CODING_COMMAND_TIMEOUT_S', 2)
    started = time.monotonic()
    reply = _tool(monkeypatch)(_script(workspace, 'hang.py',
                                       'import time\ntime.sleep(60)\n'))
    took = time.monotonic() - started
    assert 'timed out after 2s' in reply, reply
    assert took < 20, took


def test_a_failing_run_brings_its_exit_code_and_summary(workspace, allowed, monkeypatch):
    """A real test-runner-shaped run: 2,000 lines, then the summary, exit 1.
    The agent gets the exit code first and the summary last, both within one
    seat message (AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE)."""
    from core.constants import AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE
    from core.token_utils import count_tokens_for_text
    summary = '=========== 3 failed, 1997 passed in 41.20s ==========='
    body = ("import sys\n"
            "for i in range(2000):\n"
            "    print(f'tests/unit/test_mod_{i:04d}.py::test_case PASSED [{i % 100:3d}%]')\n"
            "print('E   AssertionError: boom', file=sys.stderr)\n"
            f"print({summary!r})\n"
            "sys.exit(1)\n")
    reply = _tool(monkeypatch)(_script(workspace, 'suite.py', body))
    assert reply.startswith('Exit code: 1\n'), reply[:80]
    assert 'tests/unit/test_mod_0000.py' in reply, reply[:300]
    assert summary in reply, reply[-400:]
    assert '[stderr]' in reply and 'AssertionError: boom' in reply, reply[-400:]
    assert count_tokens_for_text(reply) <= AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE


def test_the_indicator_is_lit_for_the_run(workspace, allowed, indicator, monkeypatch):
    reply = _tool(monkeypatch)(_script(workspace, 'ok.py', "print('done')\n"))
    assert reply.startswith('Exit code: 0'), reply
    opened = [ids for kind, ids in indicator if kind == 'open']
    assert opened and opened[0]['prompt_id'] == PROMPT_ID, indicator
    phases = [kw.get('phase') for kind, kw in indicator if kind == 'step']
    assert phases[:1] == ['executing'] and phases[-1] == 'completed', indicator
    assert any(kind == 'finish' for kind, _ in indicator), indicator


def test_it_runs_in_the_coding_workspace_with_the_coding_bounds(workspace, allowed, monkeypatch):
    import hart_intelligence_entry as hie
    from core.constants import CODING_COMMAND_TIMEOUT_S
    seen = {}
    real = hie.run_bounded

    def spy(argv, **kw):
        seen.update(kw)
        return real(argv, **kw)
    monkeypatch.setattr(hie, 'run_bounded', spy)
    reply = _tool(monkeypatch)(_script(workspace, 'where.py',
                                       'import os\nprint(os.getcwd())\n'))
    assert os.path.normcase(str(workspace)) in os.path.normcase(reply), reply
    assert seen['timeout'] == CODING_COMMAND_TIMEOUT_S
    assert os.path.normcase(seen['cwd']) == os.path.normcase(str(workspace))


# ── one audit line per command (security.audit_trail) ─────────────────────

@pytest.fixture
def audit(monkeypatch):
    """The audit trail's one record API, as the path calls it."""
    import security.audit_trail as trail
    lines = []
    monkeypatch.setattr(trail, 'record', lambda kind, **f: lines.append((kind, f)))
    return lines


def test_every_command_leaves_one_audit_line_with_its_outcome(workspace, allowed, audit, monkeypatch):
    """Owner 2026-11-20: every action of an agent on the record.  A run, a
    refusal and a failure each leave exactly one 'action' line naming the
    command and how it ended, attributed to the turn's agent."""
    tool = _tool(monkeypatch)
    ran = _script(workspace, 'ok.py', "print('done')\n")
    assert tool(ran).startswith('Exit code: 0')
    assert tool('shutdown /r /t 0').startswith('Shell_Command refused')
    failed = _script(workspace, 'bad.py', 'import sys\nsys.exit(3)\n')
    assert tool(failed).startswith('Exit code: 3')
    assert [kind for kind, _ in audit] == ['action', 'action', 'action'], audit
    texts = [f['text'] for _, f in audit]
    assert texts == [ran, 'shutdown /r /t 0', failed], texts
    outcomes = [f['outcome'] for _, f in audit]
    assert outcomes[0] == 'Exit code: 0' and outcomes[2] == 'Exit code: 3', outcomes
    assert outcomes[1].startswith('Shell_Command refused'), outcomes
    assert {f['agent_id'] for _, f in audit} == {PROMPT_ID}
    assert {f['via'] for _, f in audit} == {'Shell_Command'}


def test_a_command_the_owner_did_not_allow_is_on_the_record_too(workspace, audit, monkeypatch):
    from integrations.vlm import safety
    monkeypatch.setattr(safety, 'computer_control_block',
                        lambda agent_id, **kw: 'Not run: the owner said no.')
    reply = _tool(monkeypatch)('git status')
    assert reply.startswith('Shell_Command not run')
    assert len(audit) == 1 and audit[0][1]['text'] == 'git status', audit
    assert audit[0][1]['outcome'].startswith('Shell_Command not run'), audit
