"""A coding run on this computer passes the same gates as every computer-use run.

A coding run writes files (aider_native) and, through an agentic CLI, runs
commands: computer use.  The shell tool and the VLM loop already (1) refuse
power/reset/erase/format (computer_operation_refusal), (2) need the owner's
computer_control permission (computer_control_block), (3) announce the run
on the AI-control ribbon and the companion window (activity_stream), and
(4) work in a resolved workspace, never the process cwd
(vlm_adapter.resolve_task_workspace).  The coding orchestrator did none of
it.  Measured 2026-10-05: with no working_dir, aider_native used '.', the
installed app's own folder, leaving a 14 MB .aider.tags.cache.v4 in
C:\\Program Files (x86)\\HevolveAI\\Nunba (updated 11:03) and creating the
folders model_registry.py\\ and Nunba-HART-Companion\\ inside the install.

    python -m pytest tests/unit/test_coding_runs_are_computer_use.py -q
"""
import os
from unittest.mock import MagicMock, patch

import pytest


class _Backend:
    """A coding backend that records what it was asked to do."""

    name = 'aider_native'

    def __init__(self, success=True):
        self.calls = []
        self._success = success

    def execute(self, task, context):
        self.calls.append((task, dict(context)))
        if self._success:
            return {'success': True, 'output': 'edited', 'tool': self.name,
                    'execution_time_s': 0.1, 'files_changed': ['t.py']}
        return {'success': False, 'output': 'talk', 'tool': self.name,
                'execution_time_s': 0.1, 'error': 'No edit applied: t.py: failed.'}


class _Run:
    """Stands in for activity_stream's ActivityRun: records the announcement."""

    def __init__(self, log):
        self.log = log

    def step(self, **kw):
        self.log.append(('step', kw['phase'], kw.get('error', '')))

    def finish(self, **kw):
        self.log.append(('finish', kw['exit_reason'], kw.get('error', '')))


@pytest.fixture
def orchestrated(monkeypatch, tmp_path):
    """The orchestrator with one recorded backend, a temp workspace, the
    benchmark in a temp DB and the announcement recorded."""
    from integrations.coding_agent import benchmark_tracker as bt
    from integrations.coding_agent.orchestrator import CodingAgentOrchestrator
    from integrations.vlm import activity_stream, safety
    bench = bt.BenchmarkTracker(db_path=str(tmp_path / 'bench.db'))
    monkeypatch.setattr(bt, 'get_benchmark_tracker', lambda: bench)
    workspace = tmp_path / 'coding_workspace'
    workspace.mkdir()
    monkeypatch.setattr('core.platform_paths.get_coding_workspace_dir',
                        lambda: str(workspace))
    log = []
    monkeypatch.setattr(activity_stream, 'current_run',
                        lambda **kw: (log.append(('open', kw)), _Run(log))[1])
    allow = MagicMock(return_value=None)
    monkeypatch.setattr(safety, 'computer_control_block', allow)
    backend = _Backend()
    monkeypatch.setattr('integrations.coding_agent.tool_router.get_available_backends',
                        lambda: {'aider_native': backend})
    return {'orch': CodingAgentOrchestrator(), 'backend': backend, 'log': log,
            'allow': allow, 'workspace': str(workspace), 'safety': safety}


def test_without_the_owners_permission_nothing_runs(orchestrated, monkeypatch):
    refusal = 'Not run: the owner has not allowed agents to control this computer.'
    orchestrated['allow'].return_value = refusal
    result = orchestrated['orch']._execute_local(
        'fix the bug in t.py', 'bug_fix', '', 'u1', '', '', prompt_id='77')
    assert orchestrated['backend'].calls == []
    assert result['success'] is False and result['error'] == refusal
    orchestrated['allow'].assert_called_once_with('77')


def test_a_destructive_task_is_refused_before_anyone_is_asked(orchestrated):
    result = orchestrated['orch']._execute_local(
        'restart the computer when the build finishes', 'feature', '', 'u1', '', '',
        prompt_id='77')
    assert orchestrated['backend'].calls == []
    orchestrated['allow'].assert_not_called()
    assert result['success'] is False
    assert 'destructive' in result['error']


def test_no_working_dir_means_the_workspace_never_the_process_cwd(orchestrated):
    orchestrated['orch']._execute_local(
        'fix the bug in t.py', 'bug_fix', '', 'u1', '', '', prompt_id='no-goal')
    (_task, context), = orchestrated['backend'].calls
    assert context['working_dir'] == orchestrated['workspace']
    assert os.path.abspath(context['working_dir']) != os.path.abspath(os.getcwd())


def test_a_working_dir_the_caller_names_is_kept(orchestrated, tmp_path):
    repo = str(tmp_path / 'repo')
    orchestrated['orch']._execute_local(
        'fix the bug in t.py', 'bug_fix', '', 'u1', '', repo, prompt_id='77')
    (_task, context), = orchestrated['backend'].calls
    assert context['working_dir'] == repo


def test_the_run_is_announced_while_it_happens_and_closed(orchestrated):
    log = orchestrated['log']

    def _execute(task, context):
        log.append(('backend runs',))
        return {'success': True, 'output': 'edited', 'files_changed': ['t.py']}

    orchestrated['backend'].execute = _execute
    orchestrated['orch']._execute_local(
        'fix the bug in t.py', 'bug_fix', '', 'u1', '', '', prompt_id='77')
    opened = log[0]
    assert opened[0] == 'open' and opened[1]['prompt_id'] == '77'
    assert log[1:] == [('step', 'executing', ''), ('backend runs',),
                       ('step', 'completed', ''), ('finish', 'done', '')]


def test_a_failed_run_is_announced_as_failed(orchestrated):
    orchestrated['backend']._success = False
    orchestrated['orch']._execute_local(
        'fix the bug in t.py', 'bug_fix', '', 'u1', '', '', prompt_id='77')
    steps = [e for e in orchestrated['log'] if e[0] in ('step', 'finish')]
    assert steps[-2][:2] == ('step', 'failed') and 'No edit applied' in steps[-2][2]
    assert steps[-1][:2] == ('finish', 'action_error')


def test_the_agent_tool_names_its_agent(monkeypatch):
    """execute_coding_task holds the turn's prompt_id; it hands it over, so
    the permission and the announcement are the agent's, not anonymous."""
    import asyncio
    from core.agent_tools import build_core_tool_closures
    ctx = {
        'user_id': '999', 'prompt_id': '8888', 'agent_data': {},
        'helper_fun': MagicMock(), 'user_prompt': '999_8888',
        'request_id_list': {'999_8888': 'req1'}, 'recent_file_id': {},
        'scheduler': MagicMock(), 'send_message_to_user1': MagicMock(),
        'retrieve_json': MagicMock(return_value={}),
        'strip_json_values': MagicMock(return_value=''),
        'save_conversation_db': MagicMock(return_value='1'),
    }
    tools = {name: fn for name, _d, fn in build_core_tool_closures(ctx)}
    orch = MagicMock()
    orch.execute.return_value = {'success': False, 'error': 'x'}
    with patch('integrations.coding_agent.orchestrator.get_coding_orchestrator',
               return_value=orch):
        asyncio.run(tools['execute_coding_task']('fix t.py'))
    assert orch.execute.call_args.kwargs['prompt_id'] == '8888'
