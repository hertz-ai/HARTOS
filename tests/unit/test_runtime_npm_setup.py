"""RuntimeToolManager sets up a Node project on demand (amendment A16).

A16: every optional capability is set up at runtime through
RuntimeToolManager.setup_tool, into the data dir, never packaged; a missing
mechanism is added to RuntimeToolManager, not as a second installer.  It
had git and Hugging Face downloads only.  An ``npm`` row now copies a
project HARTOS ships as source (package.json + its scripts) into the
tool's storage dir, runs ``npm install`` there and then the project's
prepare script with Node, and records a receipt naming the project's
fingerprint.  Such a row is ``on_demand``: no sidecar, no VRAM, and never
set up by setup_available_tools -- only when a task asks.

The boundary is core.subprocess_safe.run_bounded (npm and node never run
here) and the Node locator; the storage, the manifest and the copy are real.
"""
import importlib
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from core.subprocess_safe import BoundedResult

# By module path: the package re-exports the storage SINGLETON under the
# module's own name (integrations/service_tools/__init__.py), so
# ``from integrations.service_tools import model_storage`` is the instance.
model_storage = importlib.import_module('integrations.service_tools.model_storage')
runtime_manager = importlib.import_module('integrations.service_tools.runtime_manager')

TOOL = 'demo_npm'


class _Vram:
    """Records every VRAM question: an npm tool must ask none."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*a, **k):
            self.calls.append(name)
            return True if name in ('can_fit', 'allocate') else 'gpu'
        return record


@pytest.fixture
def env(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'package.json').write_text('{"name": "demo", "dependencies": {}}')
    (project / 'index.js').write_text('module.exports = 1;\n')
    (project / 'prepare.js').write_text('console.log("ready");\n')
    (project / '__init__.py').write_text('')
    (project / '__pycache__').mkdir()
    (project / '__pycache__' / 'x.pyc').write_bytes(b'\x00')
    monkeypatch.setitem(runtime_manager.TOOL_CONFIGS, TOOL, {
        'download_type': 'npm', 'npm_project': str(project), 'npm_prepare': 'prepare.js',
        'on_demand': True, 'catalog_id': None})
    monkeypatch.setattr(runtime_manager, 'STATE_FILE', tmp_path / 'tool_state.json')
    monkeypatch.setattr(model_storage, 'find_node', lambda beside=None: '/opt/node/bin/node')
    monkeypatch.setattr(model_storage, 'find_npm', lambda: '/opt/node/bin/npm')
    runs = []
    answers = {'npm': BoundedResult(0, 'added 3 packages', ''),
               'node': BoundedResult(0, '{"event":"ready"}', '')}

    def run_bounded(cmd, timeout=5.0, **kw):
        kind = 'npm' if 'install' in cmd else 'node'
        runs.append(SimpleNamespace(cmd=list(cmd), timeout=timeout, cwd=kw.get('cwd'), kind=kind))
        answer = answers[kind]
        if kind == 'npm' and answer.returncode == 0 and not answer.timed_out:
            os.makedirs(os.path.join(kw['cwd'], 'node_modules', 'remotion'), exist_ok=True)
            with open(os.path.join(kw['cwd'], 'node_modules', 'remotion', 'index.js'), 'w') as f:
                f.write('x')
        return answer
    monkeypatch.setattr(model_storage, 'run_bounded', run_bounded)
    storage = model_storage.ModelStorageManager(base_dir=tmp_path / 'models')
    vram = _Vram()
    rtm = runtime_manager.RuntimeToolManager(storage=storage, vram=vram)
    popens = []

    def no_sidecar(self, tool_name, *a, **k):
        popens.append(tool_name)
        return {'error': 'a sidecar start was attempted'}
    monkeypatch.setattr(runtime_manager.RuntimeToolManager, '_start_sidecar', no_sidecar)
    monkeypatch.setattr(runtime_manager.RuntimeToolManager, '_start_inprocess', no_sidecar)
    return SimpleNamespace(rtm=rtm, storage=storage, project=project, runs=runs,
                           answers=answers, vram=vram, popens=popens, tmp=tmp_path)


def test_setup_installs_the_project_into_its_storage_dir_and_prepares_it(env):
    result = env.rtm.setup_tool(TOOL)
    tool_dir = env.storage.get_tool_dir(TOOL)
    assert result.get('downloaded') is True and result.get('ready') is True, result
    assert 'error' not in result
    # Our files are copied; Python files and caches never are.
    assert sorted(p.name for p in tool_dir.iterdir() if p.is_file()) == [
        'index.js', 'package.json', 'prepare.js']
    assert not (tool_dir / '__pycache__').exists()
    npm, node = env.runs
    assert npm.kind == 'npm' and npm.cmd[0].endswith('npm') and 'install' in npm.cmd
    assert os.path.samefile(npm.cwd, tool_dir)
    assert node.cmd == ['/opt/node/bin/node', 'prepare.js'] and os.path.samefile(node.cwd, tool_dir)
    assert npm.timeout > 0 and node.timeout > 0
    # A receipt for exactly this project.
    row = env.storage.get_manifest()['tools'][TOOL]
    assert row['source_url'] == 'npm:' + env.storage.npm_project_fingerprint(str(env.project))
    # No server and no GPU: nothing is spawned, VRAM is never asked.
    assert env.popens == [] and env.vram.calls == []
    assert env.rtm.tool_ready(TOOL) is True
    assert env.rtm.get_tool_status(TOOL)['ready'] is True


def test_a_second_setup_runs_nothing(env):
    env.rtm.setup_tool(TOOL)
    env.runs.clear()
    result = env.rtm.setup_tool(TOOL)
    assert result.get('ready') is True and env.runs == []


def test_a_changed_project_is_installed_again(env):
    env.rtm.setup_tool(TOOL)
    assert env.rtm.tool_ready(TOOL)
    (env.project / 'index.js').write_text('module.exports = 2;\n')
    assert env.rtm.tool_ready(TOOL) is False, 'the receipt names the old project'
    env.runs.clear()
    assert env.rtm.setup_tool(TOOL).get('ready') is True
    assert [r.kind for r in env.runs] == ['npm', 'node']
    assert (env.storage.get_tool_dir(TOOL) / 'index.js').read_text() == 'module.exports = 2;\n'


@pytest.mark.parametrize('kind, answer, said', [
    ('npm', BoundedResult(1, '', 'ERR! 404 Not Found'), 'npm install failed'),
    ('npm', BoundedResult(-1, '', '', timed_out=True), 'timed out'),
    ('node', BoundedResult(1, '', 'chrome download failed'), 'prepare.js failed'),
])
def test_a_failed_step_leaves_no_receipt_and_says_why(env, kind, answer, said):
    env.answers[kind] = answer
    result = env.rtm.setup_tool(TOOL)
    assert said in result.get('error', ''), result
    assert not result.get('ready')
    assert TOOL not in env.storage.get_manifest().get('tools', {})
    assert env.rtm.tool_ready(TOOL) is False


def test_without_node_nothing_runs_and_the_reason_is_node(env, monkeypatch):
    monkeypatch.setattr(model_storage, 'find_node', lambda beside=None: None)
    result = env.rtm.setup_tool(TOOL)
    assert 'Node.js' in result.get('error', '') and env.runs == []


def test_bulk_setup_never_installs_an_on_demand_tool(env, monkeypatch):
    monkeypatch.setattr(runtime_manager, 'TOOL_CONFIGS',
                        {TOOL: runtime_manager.TOOL_CONFIGS[TOOL]})
    result = env.rtm.setup_available_tools()
    assert 'skipped' in result[TOOL] and env.runs == []


def test_start_tool_answers_ready_without_a_sidecar(env):
    assert 'error' in env.rtm.start_tool(TOOL)
    env.rtm.setup_tool(TOOL)
    assert env.rtm.start_tool(TOOL).get('ready') is True
    assert env.popens == []


def test_two_setups_at_once_install_once(env, monkeypatch):
    real = model_storage.run_bounded

    def slow(cmd, timeout=5.0, **kw):
        time.sleep(0.3)
        return real(cmd, timeout=timeout, **kw)
    monkeypatch.setattr(model_storage, 'run_bounded', slow)
    results = []
    threads = [threading.Thread(target=lambda: results.append(env.rtm.setup_tool(TOOL)))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert [r.get('ready') for r in results] == [True, True]
    assert [r.kind for r in env.runs] == ['npm', 'node'], 'one install, one prepare'


def test_the_receipt_survives_a_new_manager(env):
    """A restart: the manifest on disk answers, nothing is installed again."""
    env.rtm.setup_tool(TOOL)
    fresh = runtime_manager.RuntimeToolManager(
        storage=model_storage.ModelStorageManager(base_dir=env.tmp / 'models'), vram=_Vram())
    env.runs.clear()
    assert fresh.tool_ready(TOOL) is True
    assert fresh.setup_tool(TOOL).get('ready') is True and env.runs == []


def test_the_state_file_lists_it_without_running(env):
    env.rtm.setup_tool(TOOL)
    state = json.loads(runtime_manager.STATE_FILE.read_text())
    assert state['tools'][TOOL]['downloaded'] is True
    assert state['tools'][TOOL]['was_running'] is False
