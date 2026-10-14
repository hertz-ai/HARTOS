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
import sys
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
    # Our files are copied; Python files and caches never are.  The list of
    # what was copied stays beside them for the next setup's mirror.
    assert sorted(p.name for p in tool_dir.iterdir()
                  if p.is_file() and p.name != model_storage.NPM_COPIED_LIST) == [
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


def test_a_file_the_project_no_longer_has_is_gone_after_re_setup(env):
    """A HARTOS update that deletes or renames a project file: the re-setup
    mirrors the source, so the old file cannot linger and be bundled.  What
    npm and the prepare script put there (node_modules, the downloaded
    browser, npm's lock file) is left alone."""
    legacy = env.project / 'templates' / 'legacy'
    legacy.mkdir(parents=True)
    (legacy / 'old.tsx').write_text('old')
    (env.project / 'templates' / 'kept.tsx').write_text('kept')
    assert env.rtm.setup_tool(TOOL).get('ready') is True
    tool_dir = env.storage.get_tool_dir(TOOL)
    assert (tool_dir / 'templates' / 'legacy' / 'old.tsx').is_file()
    chrome = (tool_dir / 'node_modules' / '.remotion' / 'chrome-headless-shell' / 'win64'
              / 'chrome-headless-shell.exe')
    chrome.parent.mkdir(parents=True)
    chrome.write_bytes(b'chrome')
    (tool_dir / 'package-lock.json').write_text('{}')

    (legacy / 'old.tsx').unlink()
    legacy.rmdir()
    (env.project / 'index.js').rename(env.project / 'main.js')
    result = env.rtm.setup_tool(TOOL)

    assert result.get('ready') is True, result
    assert not (tool_dir / 'templates' / 'legacy').exists(), 'removed with its folder'
    assert not (tool_dir / 'index.js').exists(), 'a renamed file leaves no old copy'
    assert (tool_dir / 'main.js').read_text() == 'module.exports = 1;\n'
    assert (tool_dir / 'templates' / 'kept.tsx').read_text() == 'kept'
    assert chrome.read_bytes() == b'chrome'
    assert (tool_dir / 'node_modules' / 'remotion' / 'index.js').is_file()
    assert (tool_dir / 'package-lock.json').is_file()
    assert env.rtm.tool_ready(TOOL) is True


def test_the_mirror_never_removes_outside_the_projects_own_files(env):
    """A copied-files list naming node_modules or a path outside the tool
    dir (hand-edited, or corrupt) removes nothing there."""
    env.rtm.setup_tool(TOOL)
    tool_dir = env.storage.get_tool_dir(TOOL)
    outside = env.tmp / 'models' / 'outside.txt'
    outside.write_text('keep')
    listing = tool_dir / model_storage.NPM_COPIED_LIST
    listing.write_text(json.dumps(['node_modules/remotion/index.js', '../outside.txt',
                                   'index.js', 42]))
    (env.project / 'index.js').write_text('module.exports = 3;\n')
    assert env.rtm.setup_tool(TOOL).get('ready') is True
    assert outside.read_text() == 'keep'
    assert (tool_dir / 'node_modules' / 'remotion' / 'index.js').is_file()
    assert (tool_dir / 'index.js').read_text() == 'module.exports = 3;\n'


def test_an_absolute_or_other_drive_list_entry_is_skipped_not_raised(env):
    """A23a N2: the copied-files list is data on disk.  An entry naming an
    absolute path (on Windows, on another drive: os.path.relpath raises
    ValueError there, not OSError) is skipped, the file it names is left
    alone, and the setup still completes instead of setup_tool raising."""
    import sys
    env.rtm.setup_tool(TOOL)
    tool_dir = env.storage.get_tool_dir(TOOL)
    outside = env.tmp / 'abs_outside.txt'
    outside.write_text('keep')
    drive = os.path.splitdrive(str(tool_dir))[0]
    other = ('Q:' if drive.upper() != 'Q:' else 'R:') + '/elsewhere/x.txt'
    entries = [str(outside), outside.as_posix(), other, '/rooted/x.txt']
    (tool_dir / model_storage.NPM_COPIED_LIST).write_text(json.dumps(entries))
    (env.project / 'index.js').write_text('module.exports = 4;\n')
    result = env.rtm.setup_tool(TOOL)
    assert result.get('ready') is True, result
    assert outside.read_text() == 'keep'
    assert (tool_dir / 'index.js').read_text() == 'module.exports = 4;\n'
    if sys.platform == 'win32':
        # The other-drive entry is the one that used to raise ValueError.
        assert os.path.splitdrive(other)[0] != drive


# ── the copied-files list is data on disk (amendment A24a) ───────────────
#
# Review of a47b8c883, measured on D: against the real mirror: its guard
# compared lexical paths, case-sensitively.  The entry 'Node_Modules/x/keep.js'
# removed node_modules/x/keep.js and then the folders that emptied; the entry
# 'link/victim.txt', through a junction, removed a file outside the tool dir;
# the entry '.' raised PermissionError out of the mirror, and since the list
# is rewritten only after the removals, every later setup failed the same
# way.  An entry is now removed only when it resolves (links, junctions, 8.3
# names, case) to a FILE strictly inside the tool dir and outside
# node_modules; anything else is skipped, and an entry that cannot be
# removed is logged and skipped.

def _relist(env, entries):
    """Make ``entries`` the copied-files list, and change the project so the
    next setup mirrors again (a complete setup of the same project returns
    at once)."""
    tool_dir = env.storage.get_tool_dir(TOOL)
    (tool_dir / model_storage.NPM_COPIED_LIST).write_text(json.dumps(entries))
    with open(env.project / 'index.js', 'a') as fh:
        fh.write('// changed\n')
    return tool_dir


def _npm_owned(tool_dir):
    """A file npm put in node_modules, in a folder of its own."""
    keep = tool_dir / 'node_modules' / 'x' / 'keep.js'
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text('npm')
    return keep


@pytest.mark.parametrize('entry', ['Node_Modules/x/keep.js', 'NODE_MODULES/x/keep.js',
                                   './node_modules/x/keep.js', 'node_modules/x/../x/keep.js'])
def test_no_spelling_of_node_modules_reaches_it(env, entry):
    env.rtm.setup_tool(TOOL)
    keep = _npm_owned(env.storage.get_tool_dir(TOOL))
    _relist(env, [entry])
    result = env.rtm.setup_tool(TOOL)
    assert result.get('ready') is True, result
    assert keep.read_text() == 'npm' and keep.parent.is_dir()


def _short_name(path):
    """The 8.3 short name Windows keeps for ``path``, or None (another OS,
    or a volume with short names off, as D: on the dev box is)."""
    if sys.platform != 'win32':
        return None
    import ctypes
    buf = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, len(buf)):
        return None
    short = os.path.basename(buf.value)
    return short if short.lower() != os.path.basename(str(path)).lower() else None


def test_an_8dot3_short_name_never_reaches_node_modules(env):
    env.rtm.setup_tool(TOOL)
    tool_dir = env.storage.get_tool_dir(TOOL)
    keep = _npm_owned(tool_dir)
    short = _short_name(tool_dir / 'node_modules')
    if not short:
        pytest.skip('no 8.3 short name for node_modules on this volume (%s)' % tool_dir)
    _relist(env, [short + '/x/keep.js'])
    assert env.rtm.setup_tool(TOOL).get('ready') is True
    assert keep.read_text() == 'npm'


@pytest.mark.parametrize('entry', ['.', '', './', 'templates', 'templates/', 'node_modules'])
def test_a_folder_entry_removes_nothing_and_every_setup_completes(env, entry):
    (env.project / 'templates').mkdir()
    (env.project / 'templates' / 'kept.tsx').write_text('kept')
    env.rtm.setup_tool(TOOL)
    tool_dir = _relist(env, [entry])
    for _ in range(2):
        result = env.rtm.setup_tool(TOOL)
        assert result.get('ready') is True, result
        _relist(env, [entry])
    assert (tool_dir / 'templates' / 'kept.tsx').read_text() == 'kept'
    assert (tool_dir / 'node_modules' / 'remotion' / 'index.js').is_file()


def _link_dir(link, target):
    """A folder link: an NTFS junction on Windows (what mklink /J makes, no
    privilege needed), a symlink elsewhere."""
    if sys.platform == 'win32':
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(str(target), str(link), target_is_directory=True)


def test_a_link_out_of_the_tool_dir_is_never_followed(env):
    env.rtm.setup_tool(TOOL)
    tool_dir = env.storage.get_tool_dir(TOOL)
    elsewhere = env.tmp / 'elsewhere'
    (elsewhere / 'sub').mkdir(parents=True)
    victim = elsewhere / 'sub' / 'victim.txt'
    victim.write_text('keep')
    _link_dir(tool_dir / 'link', elsewhere)
    try:
        _relist(env, ['link/sub/victim.txt'])
        assert env.rtm.setup_tool(TOOL).get('ready') is True
        assert victim.read_text() == 'keep' and (elsewhere / 'sub').is_dir()
    finally:
        # The link itself, never what it points at.
        link = str(tool_dir / 'link')
        if os.path.lexists(link):
            os.rmdir(link) if sys.platform == 'win32' else os.unlink(link)


def test_an_entry_that_cannot_be_removed_is_logged_and_skipped(env, monkeypatch, caplog):
    (env.project / 'a.js').write_text('a')
    (env.project / 'b.js').write_text('b')
    env.rtm.setup_tool(TOOL)
    tool_dir = env.storage.get_tool_dir(TOOL)
    (env.project / 'a.js').unlink()
    (env.project / 'b.js').unlink()
    real_remove = os.remove

    def remove(path, *a, **k):
        if os.path.basename(str(path)) == 'a.js':
            raise PermissionError(13, 'Access is denied', str(path))
        return real_remove(path, *a, **k)
    monkeypatch.setattr(os, 'remove', remove)
    with caplog.at_level('WARNING', logger=model_storage.logger.name):
        result = env.rtm.setup_tool(TOOL)
    assert result.get('ready') is True, result
    assert (tool_dir / 'a.js').is_file() and not (tool_dir / 'b.js').exists()
    assert any('a.js' in r.getMessage() and 'Access is denied' in r.getMessage()
               for r in caplog.records)


def test_a_project_without_package_json_is_refused_before_npm_runs(env):
    """npm install in a folder with no package.json walks up to the nearest
    ancestor that has one and installs THERE: such a project is refused, and
    npm never runs."""
    (env.project / 'package.json').unlink()
    result = env.rtm.setup_tool(TOOL)
    assert 'package.json' in result.get('error', ''), result
    assert env.runs == []
    assert TOOL not in env.storage.get_manifest().get('tools', {})


def test_a_project_with_no_dependencies_is_complete_without_node_modules(env, monkeypatch):
    """npm makes no node_modules for a project with no dependencies, so the
    receipt alone says it is set up: a second setup installs nothing."""
    def run_bounded(cmd, timeout=5.0, **kw):          # npm that creates nothing
        env.runs.append(SimpleNamespace(cmd=list(cmd), kind='npm' if 'install' in cmd else 'node'))
        return BoundedResult(0, 'up to date', '')
    monkeypatch.setattr(model_storage, 'run_bounded', run_bounded)
    assert env.rtm.setup_tool(TOOL).get('ready') is True
    assert not (env.storage.get_tool_dir(TOOL) / 'node_modules').exists()
    env.runs.clear()
    assert env.rtm.tool_ready(TOOL) is True
    assert env.rtm.setup_tool(TOOL).get('ready') is True and env.runs == []
    # A project that names dependencies still needs its node_modules: an npm
    # that exits 0 without making it is a failed setup, never 'ready'.
    (env.project / 'package.json').write_text('{"name": "demo", "dependencies": {"a": "1.0.0"}}')
    result = env.rtm.setup_tool(TOOL)
    assert 'node_modules' in result.get('error', '') and not result.get('ready'), result
    assert env.rtm.tool_ready(TOOL) is False, 'dependencies named, none installed'


def test_the_install_bound_is_the_callers(env):
    """install_npm_project takes the npm install bound from its caller (the
    WhatsApp gateway keeps its 180 s); the default stays NPM_INSTALL_TIMEOUT_S."""
    env.storage.install_npm_project(TOOL, str(env.project), timeout_s=180.0)
    assert [r.timeout for r in env.runs if r.kind == 'npm'] == [180.0]
    other = model_storage.ModelStorageManager(base_dir=env.tmp / 'models2')
    env.runs.clear()
    other.install_npm_project(TOOL, str(env.project))
    assert [r.timeout for r in env.runs if r.kind == 'npm'] == [model_storage.NPM_INSTALL_TIMEOUT_S]


def test_npm_is_asked_to_prefer_its_cache(env):
    env.rtm.setup_tool(TOOL)
    npm = next(r for r in env.runs if r.kind == 'npm')
    assert '--prefer-offline' in npm.cmd


def test_a_failed_npm_logs_its_output_and_where(env, caplog):
    """The person is told a short reason; the log keeps npm's own words and
    the folder it ran in (what the WhatsApp gateway's own log kept, for the
    EPERM-under-Program-Files diagnosis)."""
    env.answers['npm'] = BoundedResult(1, 'x' * 1500 + ' EPERM mkdir node_modules', '')
    with caplog.at_level('WARNING', logger=model_storage.logger.name):
        result = env.rtm.setup_tool(TOOL)
    assert 'npm install failed' in result['error'] and len(result['error']) < 600
    logged = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'EPERM mkdir node_modules' in logged and 'x' * 1400 in logged
    assert str(env.storage.get_tool_dir(TOOL)) in logged


# ── a row that is off by a flag, needs the owner's yes and free disk ─────
#
# Amendment A14/A16: Remotion's row ships OFF behind one flag until the owner
# confirms its license, is set up only after the one consent card says yes,
# and checks free disk first.  The gate sits in setup_tool itself, so every
# caller -- a reel, the reel_motion tool, POST /api/tools/<name>/setup -- meets
# it (review of 38a220495: the route calls setup_tool for any row).

GATED = 'demo_gated'
FLAG = 'HEVOLVE_DEMO_GATED_TOOL'


@pytest.fixture
def gated(env, monkeypatch):
    monkeypatch.setitem(runtime_manager.TOOL_CONFIGS, GATED, {
        'download_type': 'npm', 'npm_project': str(env.project), 'npm_prepare': 'prepare.js',
        'on_demand': True, 'catalog_id': None, 'enabled_by': FLAG,
        'consent': 'demo:gated', 'consent_reason': 'Set the demo up? It downloads 1 GB.',
        'npm_min_free_bytes': 1 << 30})
    monkeypatch.delenv(FLAG, raising=False)
    asks = []
    answer = {'value': 'granted'}

    def ask_owner_for_setup(capability, *, reason):
        asks.append((capability, reason))
        return answer['value']
    import integrations.agent_engine.capability_setup as capability_setup
    monkeypatch.setattr(capability_setup, 'ask_owner_for_setup', ask_owner_for_setup)
    free = {'bytes': 5 << 30}
    measured = []

    def free_bytes(path):
        measured.append(str(path))
        return free['bytes']
    monkeypatch.setattr(model_storage, 'free_bytes', free_bytes)
    return SimpleNamespace(asks=asks, answer=answer, free=free, measured=measured)


def test_a_flagged_row_is_off_by_default_and_setup_refuses(env, gated):
    assert env.rtm.tool_enabled(GATED) is False
    result = env.rtm.setup_tool(GATED)
    assert result.get('disabled') is True and FLAG in result.get('error', ''), result
    assert env.runs == [] and gated.asks == [], 'off: nobody is asked, nothing runs'
    assert env.rtm.get_tool_status(GATED)['enabled'] is False
    assert env.rtm.tool_ready(GATED) is False


@pytest.mark.parametrize('value', ['1', 'true', 'yes', 'on'])
def test_the_flag_switches_it_on(env, gated, monkeypatch, value):
    monkeypatch.setenv(FLAG, value)
    assert env.rtm.tool_enabled(GATED) is True
    assert env.rtm.setup_tool(GATED).get('ready') is True
    assert gated.asks == [('demo:gated', 'Set the demo up? It downloads 1 GB.')]


@pytest.mark.parametrize('answer', ['asked', 'declined', 'unavailable'])
def test_without_the_owners_yes_setup_runs_nothing(env, gated, monkeypatch, answer):
    monkeypatch.setenv(FLAG, '1')
    gated.answer['value'] = answer
    result = env.rtm.setup_tool(GATED)
    assert result.get('consent') == answer and 'error' in result, result
    assert env.runs == []
    assert GATED not in env.storage.get_manifest().get('tools', {})


def test_a_row_without_consent_or_flag_is_unchanged(env, gated):
    """Existing rows name neither: they are on and need no card."""
    assert env.rtm.tool_enabled(TOOL) is True
    assert env.rtm.ask_tool_consent(TOOL) == 'granted'
    assert env.rtm.setup_tool(TOOL).get('ready') is True and gated.asks == []


def test_too_little_free_disk_refuses_before_anything_is_copied(env, gated, monkeypatch):
    monkeypatch.setenv(FLAG, '1')
    gated.free['bytes'] = (1 << 30) - 1
    result = env.rtm.setup_tool(GATED)
    assert 'disk' in result.get('error', '') and 'GB' in result['error'], result
    assert env.runs == []
    assert not env.storage.get_tool_dir(GATED).exists(), 'nothing copied'
    assert gated.measured and os.path.normcase(gated.measured[0]) == \
        os.path.normcase(str(env.storage.get_tool_dir(GATED)))
    gated.free['bytes'] = 1 << 30
    assert env.rtm.setup_tool(GATED).get('ready') is True


def test_an_unreadable_disk_is_a_failed_setup(env, gated, monkeypatch):
    monkeypatch.setenv(FLAG, '1')

    def no_drive(path):
        raise FileNotFoundError(2, 'The system cannot find the path specified', str(path))
    monkeypatch.setattr(model_storage, 'free_bytes', no_drive)
    result = env.rtm.setup_tool(GATED)
    assert 'disk' in result.get('error', '') and env.runs == [], result


def test_a_complete_setup_is_not_measured_again(env, gated, monkeypatch):
    monkeypatch.setenv(FLAG, '1')
    env.rtm.setup_tool(GATED)
    gated.free['bytes'] = 0
    env.runs.clear()
    assert env.rtm.setup_tool(GATED).get('ready') is True and env.runs == []


def test_setup_in_the_background_is_one_setup_however_often_asked(env, gated, monkeypatch):
    """setup_tool_async: a caller waits as long as it can afford
    (Future.result(timeout)); asking again while it runs joins the same
    setup."""
    monkeypatch.setenv(FLAG, '1')
    release = threading.Event()
    real = model_storage.run_bounded

    def slow(cmd, timeout=5.0, **kw):
        release.wait(10)
        return real(cmd, timeout=timeout, **kw)
    monkeypatch.setattr(model_storage, 'run_bounded', slow)
    first = env.rtm.setup_tool_async(GATED)
    second = env.rtm.setup_tool_async(GATED)
    assert first is second
    import concurrent.futures
    with pytest.raises(concurrent.futures.TimeoutError):
        first.result(timeout=0.2)
    release.set()
    assert first.result(timeout=10).get('ready') is True
    assert [r.kind for r in env.runs] == ['npm', 'node']
    again = env.rtm.setup_tool_async(GATED)
    assert again is not first and again.result(timeout=10).get('ready') is True


def test_a_background_setup_that_raises_hands_the_error_to_its_waiter(env, gated, monkeypatch):
    monkeypatch.setenv(FLAG, '1')

    def boom(*a, **k):
        raise RuntimeError('a bug in setup_tool')
    monkeypatch.setattr(runtime_manager.RuntimeToolManager, 'setup_tool', boom)
    with pytest.raises(RuntimeError):
        env.rtm.setup_tool_async(GATED).result(timeout=10)


# ── setup_tool's npm branch never raises (amendment A24b ii) ─────────────
#
# Review of 38a220495: the npm branch caught only NpmSetupFailed.  With
# manifest.json unwritable the receipt write raised PermissionError out of
# setup_tool, so POST /api/tools/<name>/setup answered from the exception and
# a reel's background setup handed it to its waiter.

def test_a_receipt_that_cannot_be_written_is_a_failed_setup_not_a_raise(env):
    env.storage.base_dir.mkdir(parents=True, exist_ok=True)
    env.storage.manifest_file.mkdir()       # a folder where the file goes: never writable
    result = env.rtm.setup_tool(TOOL)
    assert 'error' in result and not result.get('ready'), result
    assert str(env.storage.get_tool_dir(TOOL)) in result['error']
    assert env.rtm.tool_ready(TOOL) is False


def test_the_installer_raises_only_npm_setup_failed(env):
    env.storage.base_dir.mkdir(parents=True, exist_ok=True)
    env.storage.manifest_file.mkdir()
    with pytest.raises(model_storage.NpmSetupFailed):
        env.storage.install_npm_project(TOOL, str(env.project))


def test_a_bug_in_the_installer_is_a_failed_setup_not_a_raise(env, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError('a bug in the installer')
    monkeypatch.setattr(model_storage.ModelStorageManager, 'install_npm_project', boom)
    with caplog.at_level('ERROR', logger=runtime_manager.logger.name):
        result = env.rtm.setup_tool(TOOL)
    assert 'a bug in the installer' in result.get('error', '') and not result.get('ready'), result
    assert any(r.exc_info for r in caplog.records), 'the traceback is logged'


# ── one flag says "on demand" (amendment A24b iii) ────────────────────────
#
# Review of 38a220495: tool_ready keyed off download_type == 'npm' while
# setup_tool, start_tool, get_tool_status and setup_available_tools key off
# on_demand.  ``on_demand`` is the one answer: an npm row without it is a
# server tool to all of them, so it is ready when it serves, not because its
# packages are installed.

def test_on_demand_is_the_one_flag_tool_ready_reads(env, monkeypatch):
    row = dict(runtime_manager.TOOL_CONFIGS[TOOL])
    del row['on_demand']
    monkeypatch.setitem(runtime_manager.TOOL_CONFIGS, 'demo_npm_server', row)
    env.storage.install_npm_project('demo_npm_server', row['npm_project'])
    assert env.storage.npm_install_is_complete('demo_npm_server', row['npm_project'])
    assert env.rtm.tool_ready('demo_npm_server') is False, 'nothing serves it'
    env.rtm.start_tool('demo_npm_server')
    assert env.popens == ['demo_npm_server'], 'start_tool treats it as a server too'
    # The on-demand row is ready once set up, and nothing is started for it.
    env.rtm.setup_tool(TOOL)
    assert env.rtm.tool_ready(TOOL) is True and env.popens == ['demo_npm_server']


def test_every_on_demand_row_is_an_npm_project():
    """tool_ready answers an on-demand row from its npm setup, the one kind
    of on-demand setup there is (setup_tool's npm branch)."""
    for name, row in runtime_manager.TOOL_CONFIGS.items():
        if row.get('on_demand'):
            assert row.get('download_type') == 'npm' and row.get('npm_project'), name


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
