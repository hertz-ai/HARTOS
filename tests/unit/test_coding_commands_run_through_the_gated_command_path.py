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


# ── the Executor seat's code blocks run through the same gate ────────────

def _executor(workspace):
    """The CREATE Executor seat, as create_agents builds it, with no LLM: only
    its code execution can answer."""
    import hartos.create_recipe as cr
    import hartos.helper as helper
    cr.autogen.ConversableAgent            # the lazy proxy's hook runs here
    from autogen.agentchat.conversable_agent import ConversableAgent
    assert ConversableAgent.run_code is helper.gated_run_code
    ex = cr.instantiate_executor_agent()
    ex._code_execution_config['work_dir'] = str(workspace)
    ex.client = None
    return ex


def _reply(seat, block):
    reply = seat.generate_reply(messages=[{'role': 'user', 'name': 'Assistant',
                                           'content': block}])
    return reply if isinstance(reply, str) else (reply or {}).get('content')


def test_the_reuse_pipeline_installs_the_same_gate():
    """REUSE's lazy autogen proxy runs the same hook, so a REUSE-only
    process gates every seat too."""
    import hartos.reuse_recipe as rr
    import hartos.helper as helper
    from autogen.agentchat.conversable_agent import ConversableAgent
    rr.autogen.ConversableAgent
    assert ConversableAgent.run_code is helper.gated_run_code


def test_with_nobody_signed_in_the_executor_runs_nothing(workspace, monkeypatch):
    """Measured on c5fd6f3c2 (rv_ac3a probe_rev): this block ran, and printed
    rv-echo-ok, with nobody signed in.  Now the owner's permission refuses."""
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)
    marker = workspace / 'ran.txt'
    reply = _reply(_executor(workspace),
                   f"```python\nopen(r'{marker}', 'w').write('x')\nprint('rv-echo-ok')\n```")
    assert reply.startswith('exitcode: 1'), reply
    assert 'nobody is signed in' in reply and 'rv-echo-ok' not in reply, reply
    assert not marker.exists()


def test_a_hard_denied_block_is_refused(workspace, allowed, monkeypatch):
    import hart_intelligence_entry as hie
    ran = []
    monkeypatch.setattr(hie, 'run_bounded', lambda *a, **k: ran.append(a))
    ex = _executor(workspace)
    for block in ("```sh\nshutdown -r now\n```",
                  "```python\nimport os\nos.system('shutdown /r /t 0')\n```"):
        reply = _reply(ex, block)
        assert reply.startswith('exitcode: 1') and 'refused' in reply, (block, reply)
    assert ran == []


def test_a_hung_block_is_stopped_at_its_limit(workspace, allowed, monkeypatch):
    """autogen's own timeout returned only when the block ended (2 s limit, 6 s
    sleep: 6.1 s); run_bounded kills it."""
    import core.constants as constants
    monkeypatch.setattr(constants, 'CODING_COMMAND_TIMEOUT_S', 2)
    started = time.monotonic()
    reply = _reply(_executor(workspace), "```python\nimport time\ntime.sleep(60)\n```")
    assert 'timed out after 2s' in reply, reply
    assert time.monotonic() - started < 20


def test_a_failing_block_keeps_its_stdout_and_its_exit_code(workspace, allowed, indicator, audit, monkeypatch):
    """autogen returned stderr only for a failing block; the path returns both
    streams, the exit code, lights the indicator and writes the audit line."""
    block = ("```python\nimport sys\nprint('out before the failure')\n"
             "print('E   boom', file=sys.stderr)\nsys.exit(4)\n```")
    reply = _reply(_executor(workspace), block)
    assert reply.startswith('exitcode: 4 (execution failed)'), reply
    assert 'out before the failure' in reply and 'E   boom' in reply, reply
    assert any(kind == 'open' for kind, _ in indicator), indicator
    assert [k for k, _ in audit] == ['action'], audit
    assert audit[0][1]['outcome'] == 'Exit code: 4', audit
    assert not list(workspace.glob('tmp_code_*.py')), 'the script is left behind'


def test_a_block_runs_in_the_workspace_with_the_bundled_python(workspace, allowed, monkeypatch):
    """sys.executable is the app in the installed build; the block runs with
    core.venv_paths.venv_creator_python, in the workspace."""
    import core.venv_paths as venv_paths
    real = sys.executable
    monkeypatch.setattr(venv_paths, 'venv_creator_python', lambda: real)
    monkeypatch.setattr(sys, 'executable', str(workspace / 'Nunba.exe'))
    reply = _reply(_executor(workspace),
                   "```python\nimport os, sys\nprint(os.getcwd())\nprint(sys.executable)\n```")
    assert reply.startswith('exitcode: 0'), reply
    assert os.path.normcase(str(workspace)) in os.path.normcase(reply), reply
    assert os.path.normcase(real) in os.path.normcase(reply), reply


def test_a_block_naming_a_parent_folder_is_refused(workspace, allowed, monkeypatch):
    import hart_intelligence_entry as hie
    ran = []
    monkeypatch.setattr(hie, 'run_bounded', lambda *a, **k: ran.append(a))
    ex = _executor(workspace)
    for block in ("```python\nopen('../x', 'w').write('x')\n```",
                  "```sh\ncd .. && touch x\n```"):
        reply = _reply(ex, block)
        assert reply.startswith('exitcode: 1') and "outside the workspace" in reply, reply
    assert ran == [] and not (workspace.parent / 'x').exists()
    # An ellipsis is no parent folder.
    monkeypatch.setattr(hie, 'run_bounded', lambda *a, **k: __import__(
        'core.subprocess_safe', fromlist=['BoundedResult']).BoundedResult(
            returncode=0, stdout='Loading...', stderr='', timed_out=False))
    assert _reply(ex, "```python\nprint('Loading...')\n```").startswith('exitcode: 0')


def test_an_sh_block_runs_in_bash_never_powershell(workspace, allowed, monkeypatch):
    import hart_intelligence_entry as hie
    seen = []
    from core.subprocess_safe import BoundedResult
    monkeypatch.setattr(hie, 'run_bounded', lambda argv, **k: seen.append(argv) or
                        BoundedResult(returncode=0, stdout='ok', stderr='', timed_out=False))
    assert _reply(_executor(workspace), "```sh\necho hi\n```").startswith('exitcode: 0')
    assert seen and seen[0][0] == 'bash' and seen[0][-1] == 'echo hi', seen


# ── a refused computer-use command is on the record too (rv_abff finding 2) ──
#
# abffee892 said every caller of the gated path leaves one line.  The
# computer-use loop's hard-deny (execute_action's destructive_computer_operation
# check) returns before its shell step reaches the path, so `rm -rf /` there
# left no line in either store.  Through the REAL trail writer, a temp folder.

@pytest.fixture
def trail(tmp_path_factory, monkeypatch):
    """The audit trail itself (security.audit_trail.AuditTrail) in a temp
    folder; returns a reader of every line it has written."""
    import security.audit_trail as at
    t = at.AuditTrail(root=str(tmp_path_factory.mktemp('trail')), start=False,
                      role_lookup=lambda uid: None)
    monkeypatch.setattr(at, '_trail', t)
    return lambda: t.read(limit=1000)


@pytest.fixture
def loop_jsonl(tmp_path_factory, monkeypatch):
    """The computer-use loop's own JSONL (integrations.vlm.safety.AuditLogger)
    in a temp folder; returns its lines."""
    from integrations.vlm import safety
    folder = tmp_path_factory.mktemp('vlm_audit')
    monkeypatch.setenv('HEVOLVE_VLM_AUDIT_DIR', str(folder))
    monkeypatch.setattr(safety, '_audit_logger', None)
    monkeypatch.setattr(safety, '_session_guard', None)

    def lines():
        import json
        return [json.loads(raw) for f in sorted(folder.iterdir())
                for raw in f.read_text(encoding='utf-8').splitlines() if raw.strip()]
    return lines


class _FakeVault:
    """A credential vault with nothing in it: aliases and results pass as
    they are (fake, no value is ever held)."""

    def resolve_aliases(self, action):
        return dict(action)

    def mask_secrets(self, result):
        return result


@pytest.fixture
def no_process(monkeypatch):
    """Nothing really runs: the gated path's process runner answers exit 0,
    and the vault is the fake one.  Returns what would have run."""
    import hart_intelligence_entry as hie
    import core.tool_logging as tool_logging
    from core.subprocess_safe import BoundedResult
    ran = []
    monkeypatch.setattr(hie, 'run_bounded', lambda argv, **k: ran.append(argv) or
                        BoundedResult(returncode=0, stdout='ok', stderr='', timed_out=False))
    monkeypatch.setattr(tool_logging, 'credential_vault', lambda: _FakeVault())
    return ran


def _loop_step(command):
    """The computer-use loop's shell step, as the loop calls it (safety on)."""
    from integrations.vlm.local_computer_tool import execute_action
    return execute_action({'action': 'shell', 'command': command}, 'inprocess',
                          safety=True)


@pytest.mark.parametrize('command', ['rm -rf /', 'shutdown /r /t 0'])
def test_a_loop_command_the_hard_deny_refuses_leaves_one_line(
        command, workspace, allowed, indicator, no_process, trail, loop_jsonl):
    from integrations.vlm.safety import destructive_computer_operation
    reason = destructive_computer_operation({'action': 'shell', 'command': command})
    result = _loop_step(command)
    assert result == {'output': '', 'status': 'safety_blocked',
                      'error': reason, 'safety_block': reason}, result
    assert no_process == []
    lines = trail()
    assert len(lines) == 1, lines
    line = lines[0]
    assert line['kind'] == 'action' and line['dir'] == 'out', line
    assert line['text'] == command and line['agent_id'] == PROMPT_ID, line
    assert line['extra'] == {'outcome': reason, 'via': 'Shell_Command'}, line
    assert 'never agent-executable' in line['extra']['outcome'], line
    # The loop's own store has it too, as it has every other refusal.
    jsonl = loop_jsonl()
    assert len(jsonl) == 1 and jsonl[0]['block_reason'] == reason, jsonl


def test_every_caller_leaves_exactly_one_line_per_command(
        workspace, allowed, indicator, no_process, trail, loop_jsonl, monkeypatch):
    """An allowed command and a refused one, through the chat's Shell_Command,
    the loop's shell step and a coding goal's Shell_Command: one line each,
    never two (the gated path does not run after the loop's refusal)."""
    import hart_intelligence_entry as hie

    def loop(command):
        result = _loop_step(command)
        return result.get('output') or result.get('error')
    callers = {'chat': hie._handle_shell_command_tool, 'loop': loop,
               'coding': _tool(monkeypatch)}
    seen = {}
    for name, call in callers.items():
        for command in ('echo hi', 'shutdown /r /t 0'):
            before = len(trail())
            reply = call(command)
            seen[(name, command)] = (len(trail()) - before, reply.split('\n', 1)[0])
    assert {n for n, _reply in seen.values()} == {1}, seen
    assert [seen[(c, 'echo hi')][1] for c in callers] == ['Exit code: 0'] * 3, seen
    assert len(no_process) == 3 and all(argv[-1] == 'echo hi' for argv in no_process), \
        no_process


def test_a_record_that_raises_is_logged_and_the_refusal_is_unchanged(
        workspace, allowed, indicator, no_process, trail, loop_jsonl, monkeypatch):
    import json
    import logging
    import security.audit_trail as at
    normal = _loop_step('rm -rf /')

    def boom(kind, **fields):
        raise RuntimeError('the queue broke')
    monkeypatch.setattr(at, 'record', boom)
    # The entry module's own logger (it does not propagate to the root).
    warned = []

    class Seen(logging.Handler):
        def emit(self, record):
            warned.append(record.getMessage())
    log = logging.getLogger('hart_intelligence_entry')
    seen = Seen(level=logging.WARNING)
    log.addHandler(seen)
    try:
        broken = _loop_step('rm -rf /')
    finally:
        log.removeHandler(seen)
    assert json.dumps(broken, sort_keys=True) == json.dumps(normal, sort_keys=True)
    assert any('not put on the audit trail' in m for m in warned), warned
