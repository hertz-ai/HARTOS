"""A coding run on this computer passes the computer-use gates that fit it.

A coding run writes files (aider_native) and, through an agentic CLI, runs
commands: computer use.  Like the shell tool and the VLM loop it (1) needs
the owner's computer_control permission (computer_control_block), (2) is
announced on the AI-control ribbon and the companion window
(activity_stream), and (3) works in a resolved workspace, never the process
cwd (vlm_adapter.resolve_task_workspace).  The orchestrator did none of it.
Measured 2026-10-05: with no working_dir, aider_native used '.', the
installed app's own folder, leaving a 14 MB .aider.tags.cache.v4 in
C:\\Program Files (x86)\\HevolveAI\\Nunba (updated 11:03) and creating the
folders model_registry.py\\ and Nunba-HART-Companion\\ inside the install.

Reworked per the review of cd99c540d (2026-10-05 08:00Z, REJECTED):
  * the desktop-action hard deny (computer_operation_refusal) judged the
    TEXT of coding tasks: 6 of 12 plausible tasks ran on the parent and were
    refused ('Add a system shutdown hook...', 'Implement Windows restart
    detection...'), and it guarded nothing, because the CLI's real commands
    are never checked.  A coding run no longer asks it;
  * the agent came from the thread when the caller named none, and a
    request thread still carries the last chat's prompt and user: a route
    ran a job in another agent's goal repo and announced it as another
    user.  Only the caller names the agent now; the hive fallbacks forward
    it; POST /coding/execute runs detached from the thread's last turn and
    does not hold the request open for the owner's answer;
  * `hart code` is the person at their own terminal: it is not an agent
    asking for their computer, so it runs without the ask (it refused when
    no owner was set: 'Not run: nobody is signed in').

    python -m pytest tests/unit/test_coding_runs_are_computer_use.py -q
"""
import ast
import io
import logging
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from hartos.threadlocal import thread_local_data as tld
from tests.unit.module_swap import swap_modules

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


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


@pytest.fixture
def stale_thread():
    """A request thread that still carries the last chat's turn: /chat sets
    this state and never clears it (threadlocal.detached's docstring)."""
    prev = (tld.get_prompt_id(), tld.get_user_id())
    tld.set_prompt_id('4242')
    tld.set_user_id(user_id='olduser')
    yield
    tld.set_prompt_id(prev[0])
    tld.set_user_id(user_id=prev[1])


def test_without_the_owners_permission_nothing_runs(orchestrated):
    refusal = 'Not run: the owner has not allowed agents to control this computer.'
    orchestrated['allow'].return_value = refusal
    result = orchestrated['orch']._execute_local(
        'fix the bug in t.py', 'bug_fix', '', 'u1', '', '', prompt_id='77')
    assert orchestrated['backend'].calls == []
    assert result['success'] is False and result['error'] == refusal
    assert orchestrated['allow'].call_args.args == ('77',)
    assert orchestrated['allow'].call_args.kwargs == {'wait': True}


def test_a_caller_that_cannot_wait_is_asked_without_being_held(orchestrated):
    """The orchestrator hands the caller's answer to the gate: an HTTP caller
    is asked for the owner's permission but not held for the answer."""
    orchestrated['orch']._execute_local(
        'fix the bug in t.py', 'bug_fix', '', 'u1', '', '', prompt_id='77',
        wait_for_owner=False)
    assert orchestrated['allow'].call_args.args == ('77',)
    assert orchestrated['allow'].call_args.kwargs == {'wait': False}


@pytest.mark.parametrize('task', [
    'Halt the retry loop when the queue is empty',
    'Reboot logic: retry the job after the worker crashes',
    'Implement Windows restart detection in the updater',
    'Fix device sleep handling in the power monitor',
    'Add a system shutdown hook that flushes the log',
    'Add a factory reset button to the settings page',
    'Kill the worker process with .terminate() when its heartbeat stops',
])
def test_a_task_that_describes_restart_or_shutdown_code_runs(orchestrated, task):
    """Review of cd99c540d, blocking (1), measured: these ran on the parent
    and the desktop-action filter refused them for their words."""
    result = orchestrated['orch']._execute_local(
        task, 'feature', '', 'u1', '', '', prompt_id='77')
    assert [t for t, _ctx in orchestrated['backend'].calls] == [task]
    assert result['success'] is True
    orchestrated['allow'].assert_called_once()


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


def test_a_caller_that_names_no_agent_never_borrows_the_threads_last_turn(
        orchestrated, stale_thread, monkeypatch):
    """Review of cd99c540d, blocking (2), measured: a thread still carrying
    agent 4242 and user olduser ran the job in 4242's goal repo and
    announced it as olduser."""
    seen = {}
    monkeypatch.setattr('integrations.vlm.vlm_adapter.resolve_task_workspace',
                        lambda prompt_id=None, explicit='': (
                            seen.setdefault('workspace_for', prompt_id),
                            orchestrated['workspace'])[1])
    orchestrated['orch']._execute_local(
        'fix the bug in t.py', 'bug_fix', '', '', '', '')
    assert orchestrated['allow'].call_args.args == (None,)
    assert seen['workspace_for'] is None
    opened = orchestrated['log'][0][1]
    assert opened['prompt_id'] == '' and opened['user_id'] == ''


def test_the_hive_fallbacks_keep_the_asking_agent(orchestrated, monkeypatch):
    """Review of cd99c540d: a task the hive could not take ran here under the
    thread's identity, and consent was asked for None instead of '8888'."""
    orch = orchestrated['orch']
    monkeypatch.setattr(orch, '_can_run_locally', lambda: False)
    shard_engine = types.ModuleType('integrations.agent_engine.shard_engine')

    class _NoShards:
        def __init__(self, *a, **k):
            pass

        def decompose_task(self, *a, **k):
            return []

    shard_engine.ShardEngine = _NoShards
    shard_engine.ShardScope = MagicMock()
    mesh = types.ModuleType('integrations.agent_engine.compute_mesh_service')
    mesh.get_compute_mesh = lambda: MagicMock(get_available_peers=lambda: [])
    with swap_modules({
            'integrations.agent_engine.shard_engine': shard_engine,
            'integrations.agent_engine.compute_mesh_service': mesh}):
        orch.execute('fix the bug in t.py', 'bug_fix', user_id='u1',
                     data_scope='trusted_peer', prompt_id='8888')
    assert orchestrated['allow'].call_args.args == ('8888',)
    assert len(orchestrated['backend'].calls) == 1


@pytest.mark.parametrize('scope', ['edge_only', ''], ids=['private', 'shareable'])
def test_execute_hands_the_asker_to_the_local_run(orchestrated, monkeypatch, scope):
    """execute() is the door every caller uses.  On both local branches (a
    private task, and a shareable one this computer can run) the agent it
    names and whether it can wait reach the owner's gate."""
    orch = orchestrated['orch']
    monkeypatch.setattr(orch, '_can_run_locally', lambda: True)
    orch.execute('fix the bug in t.py', 'bug_fix', user_id='u1',
                 data_scope=scope, prompt_id='77', wait_for_owner=False)
    assert orchestrated['allow'].call_args.args == ('77',)
    assert orchestrated['allow'].call_args.kwargs == {'wait': False}
    assert len(orchestrated['backend'].calls) == 1


@pytest.mark.parametrize('scope', ['edge_only', ''], ids=['private', 'shareable'])
def test_execute_runs_the_persons_own_task_unasked(orchestrated, monkeypatch, scope):
    orch = orchestrated['orch']
    monkeypatch.setattr(orch, '_can_run_locally', lambda: True)
    orch.execute('fix the bug in t.py', 'bug_fix', user_id='u1',
                 data_scope=scope, requested_by_person=True)
    orchestrated['allow'].assert_not_called()
    assert len(orchestrated['backend'].calls) == 1


# Every place a hive path falls back to running here, it runs for the agent
# that asked: the review measured consent asked for None instead of '8888'.

_WHO = {'prompt_id': '8888', 'requested_by_person': False,
        'wait_for_owner': False}


class _Shard:
    task_description = 'fix the bug in t.py'
    full_content = {'t.py': 'x = 1\n'}
    interface_specs = []
    target_files = ['t.py']
    scope = types.SimpleNamespace(value='full_file')


def _hive(monkeypatch, *, shards=(), engine_fails=False, egress=True,
          peers=(), post_status=500):
    """The hive's boundaries, faked: shard engine, compute mesh, egress
    guard, encryption and the POST to the peer.  Returns the orchestrator
    with its local run recorded, so a test reads who it ran for."""
    from integrations.coding_agent.orchestrator import CodingAgentOrchestrator
    shard_engine = types.ModuleType('integrations.agent_engine.shard_engine')

    class _Engine:
        def __init__(self, *a, **k):
            pass

        def decompose_task(self, *a, **k):
            if engine_fails:
                raise RuntimeError('shard engine down')
            return list(shards)

    shard_engine.ShardEngine = _Engine
    shard_engine.ShardScope = types.SimpleNamespace(
        FULL_FILE=types.SimpleNamespace(value='full_file'))
    mesh = types.ModuleType('integrations.agent_engine.compute_mesh_service')
    mesh.get_compute_mesh = lambda: types.SimpleNamespace(
        get_available_peers=lambda: list(peers), score=lambda peer: 1)
    monkeypatch.setitem(sys.modules, shard_engine.__name__, shard_engine)
    monkeypatch.setitem(sys.modules, mesh.__name__, mesh)
    monkeypatch.setattr('security.edge_privacy.ScopeGuard.check_egress',
                        lambda self, data, dest: (egress, '' if egress else 'held back'))
    monkeypatch.setattr('security.channel_encryption.encrypt_json_for_peer',
                        lambda payload, pub: 'envelope')
    monkeypatch.setattr('core.http_pool.pooled_post',
                        lambda *a, **k: types.SimpleNamespace(
                            status_code=post_status, json=lambda: {}))
    orch = CodingAgentOrchestrator()
    ran = []
    monkeypatch.setattr(orch, '_execute_local',
                        lambda *a, **kw: (ran.append(kw), {'success': True})[1])
    monkeypatch.setattr(orch, '_record_peer_trust', lambda *a, **k: None)
    return orch, ran


_TRUSTED_NO_KEY = {'node_id': 'p1', 'trust_level': 'SAME_USER'}
_TRUSTED = dict(_TRUSTED_NO_KEY, x25519_public_hex='ab', url='http://p1')


@pytest.mark.parametrize('path, hive', [
    ('distribute', dict()),                                   # no shards, no peers
    ('distribute', dict(shards=[_Shard()], egress=False)),    # egress guard holds all
    ('distribute', dict(shards=[_Shard()], peers=[{'node_id': 'p1'}])),  # none trusted
    ('distribute', dict(engine_fails=True)),                  # sharding failed
    ('offload', dict(peers=[{'node_id': 'p1'}])),             # none code-trusted
    ('offload', dict(peers=[_TRUSTED_NO_KEY])),               # peer has no key
    ('offload', dict(peers=[_TRUSTED])),                      # peer answered 500
], ids=['no-shards', 'egress-held', 'no-trusted-peer', 'sharding-failed',
        'offload-untrusted', 'offload-no-key', 'offload-peer-failed'])
def test_every_hive_fallback_runs_for_the_agent_that_asked(monkeypatch, path, hive):
    orch, ran = _hive(monkeypatch, **hive)
    if path == 'distribute':
        orch._distribute_to_hive('fix the bug in t.py', 'bug_fix', '', 'u1',
                                 '', '', 'trusted_peer', who=dict(_WHO))
    else:
        orch._offload_to_hive('fix the bug in t.py', 'bug_fix', '', 'u1',
                              '', '', who=dict(_WHO))
    assert ran == [_WHO]


def test_the_owners_own_command_runs_without_the_ask(orchestrated):
    """`hart code` is the person at their terminal, not an agent asking for
    their computer; it refused whenever no owner was set."""
    orchestrated['orch']._execute_local(
        'fix the bug in t.py', 'bug_fix', '', 'u1', '', '',
        requested_by_person=True)
    orchestrated['allow'].assert_not_called()
    assert len(orchestrated['backend'].calls) == 1


def test_hart_code_says_the_person_asked(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from hartos.hart_cli import hart
    orch = MagicMock()
    orch.execute.return_value = {'success': True, 'tool': 'aider_native',
                                 'execution_time_s': 0.1, 'output': 'done'}
    with patch('integrations.coding_agent.orchestrator.get_coding_orchestrator',
               return_value=orch):
        result = CliRunner().invoke(hart, ['code', 'Add a test for t.py',
                                           '--working-dir', str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert orch.execute.call_args.kwargs['requested_by_person'] is True


def test_an_http_caller_is_asked_but_not_held(monkeypatch):
    """Review of cd99c540d: POST /coding/execute blocked 108.6 s waiting for
    the owner, past the 30-60 s its clients wait.  Asked with wait=False the
    gate asks once and answers at once."""
    from integrations.vlm import safety
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner1')
    monkeypatch.setattr(safety, '_computer_control_answer', lambda *a: None)
    slept = []
    refusal = safety.computer_control_block(None, wait=False, sleep=slept.append)
    assert slept == []
    assert refusal and 'They have been asked' in refusal


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


# ─── POST /coding/execute ─────────────────────────────────────────────────────

_HIE = os.path.join(_ROOT, 'hart_intelligence_entry.py')


def _coding_execute_route():
    """The REAL /coding/execute view, compiled from the source tree with its
    decorators left off: the module cannot be imported in the test venv."""
    tree = ast.parse(io.open(_HIE, encoding='utf-8', errors='replace').read())
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == 'coding_execute')
    node.decorator_list = []
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    return compile(module, _HIE, 'exec')


def test_the_route_runs_detached_from_the_threads_last_turn_and_does_not_wait(
        stale_thread):
    seen = {}

    class _Orch:
        def execute(self, **kw):
            seen['prompt_on_thread'] = tld.get_prompt_id()
            seen['user_on_thread'] = tld.get_user_id()
            seen['kwargs'] = kw
            return {'success': True}

    body = {'task': 'fix the bug in t.py', 'task_type': 'bug_fix'}
    ns = {'request': types.SimpleNamespace(get_json=lambda force=False: dict(body)),
          'jsonify': lambda obj: obj, 'logging': logging}
    exec(_coding_execute_route(), ns)
    with patch('integrations.coding_agent.orchestrator.get_coding_orchestrator',
               return_value=_Orch()):
        assert ns['coding_execute']() == {'success': True}
    assert seen['prompt_on_thread'] is None and seen['user_on_thread'] is None
    assert seen['kwargs']['wait_for_owner'] is False
    # The thread's own state is back once the route returns.
    assert tld.get_prompt_id() == '4242'


def test_a_peer_shard_runs_detached_from_the_threads_last_turn(stale_thread, monkeypatch):
    """The peer-shard route (coding_agent api.execute_shard) is served on a
    request thread too: the shard is the peer's, not the last chat's agent's."""
    from flask import Flask
    from integrations.coding_agent.api import coding_agent_bp
    seen = {}

    class _Orch:
        def _execute_local(self, **kw):
            seen.update(prompt=tld.get_prompt_id(), user=tld.get_user_id(), kw=kw)
            return {'success': True, 'output': 'edited'}

    monkeypatch.setattr('security.channel_encryption.decrypt_json_from_peer',
                        lambda envelope: {'task': 'fix t.py', 'file_content': {}})
    app = Flask('peer-shard')
    app.register_blueprint(coding_agent_bp)
    with patch('integrations.coding_agent.orchestrator.get_coding_orchestrator',
               return_value=_Orch()):
        resp = app.test_client().post('/coding/execute', json={'encrypted': 'envelope'})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert seen['prompt'] is None and seen['user'] is None
    assert seen['kw']['user_id'] == 'peer'
    assert tld.get_prompt_id() == '4242'
