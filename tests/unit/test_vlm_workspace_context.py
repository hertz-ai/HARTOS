"""A computer-use action resolves bare filenames in the workspace its task
was ASSIGNED, never in the service's launch directory.

Live on 2026-09-19 22:07 the loop told the VLM
``Declared task workspace: C:\\Program Files (x86)\\HevolveAI\\Nunba``: the
frozen install's cwd, stamped by both entry points as ``os.getcwd()``.  The
resolver (integrations.vlm.vlm_adapter.resolve_task_workspace) is the ONE
source both entry points now use.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest

from integrations.social.models import AgentGoal, Base, get_db, get_engine
from integrations.vlm.vlm_adapter import resolve_task_workspace


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def db():
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


def test_an_explicit_workspace_wins(db, tmp_path):
    # An absolute path on every OS: a relative one now means "inside the
    # coding workspace" (test_a_relative_explicit_path_... below).
    repo = str(tmp_path / 'work' / 'repo')
    assert resolve_task_workspace(prompt_id='777', explicit=f'  {repo} ') == repo


def test_the_goal_repo_path_is_the_workspace(db, tmp_path):
    session = get_db()
    try:
        session.add(AgentGoal(id='g-ws', goal_type='coding', title='fix it',
                              prompt_id='777',
                              config_json={'repo_path': str(tmp_path)}))
        session.commit()
    finally:
        session.close()
    assert resolve_task_workspace(prompt_id=777) == str(tmp_path)


def test_without_a_goal_the_workspace_is_never_the_process_cwd(db, tmp_path, monkeypatch):
    install_dir = tmp_path / 'Program Files (x86)' / 'HevolveAI' / 'Nunba'
    install_dir.mkdir(parents=True)
    monkeypatch.chdir(install_dir)
    resolved = resolve_task_workspace(prompt_id='no-such-prompt')
    assert Path(resolved).is_dir()
    assert Path(resolved).resolve() != install_dir.resolve()
    assert resolve_task_workspace(prompt_id=None) == resolved
    assert resolve_task_workspace(prompt_id=0) == resolved


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """The user-data coding workspace, and a process cwd that is an install
    folder (the frozen desktop's launch directory).  Faked at the
    boundaries only: the workspace path and the install folder."""
    ws = tmp_path / 'data' / 'coding'
    ws.mkdir(parents=True)
    monkeypatch.setattr('core.platform_paths.get_coding_workspace_dir',
                        lambda: str(ws))
    install = tmp_path / 'Program Files (x86)' / 'HevolveAI' / 'Nunba'
    install.mkdir(parents=True)
    monkeypatch.chdir(install)
    return {'ws': str(ws), 'install': str(install)}


def test_a_relative_explicit_path_is_inside_the_workspace_never_the_cwd(db, workspace):
    """Review of c49271ba8 (11:39Z): '.' and 'tts' came back unchanged, so the
    coding tools mapped and edited the process cwd (the install folder on the
    desktop, the clone in a dev run)."""
    assert resolve_task_workspace(prompt_id='777', explicit='.') == workspace['ws']
    assert resolve_task_workspace(prompt_id='777', explicit='tts') == \
        os.path.join(workspace['ws'], 'tts')


def test_a_relative_goal_repo_path_is_inside_the_workspace(db, workspace):
    session = get_db()
    try:
        session.add(AgentGoal(id='g-rel', goal_type='coding', title='fix it',
                              prompt_id='778', config_json={'repo_path': '.'}))
        session.commit()
    finally:
        session.close()
    assert resolve_task_workspace(prompt_id='778') == workspace['ws']


def test_an_absolute_path_elsewhere_is_kept(db, workspace, tmp_path):
    repo = str(tmp_path / 'owner_clone')
    assert resolve_task_workspace(prompt_id='777', explicit=repo) == repo


def test_the_installed_apps_own_folder_is_refused(db, workspace, monkeypatch, caplog):
    """#137 promised a coding run never works in the install folder; an
    agent-supplied absolute path still reached it.  Refused with a warning
    that names it, and the next source answers."""
    import logging
    monkeypatch.setattr('core.platform_paths.get_install_dir',
                        lambda: workspace['install'])
    with caplog.at_level(logging.WARNING):
        assert resolve_task_workspace(prompt_id='777',
                                      explicit=workspace['install']) == workspace['ws']
        inside = os.path.join(workspace['install'], 'lib')
        assert resolve_task_workspace(prompt_id='777', explicit=inside) == workspace['ws']
    said = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(workspace['install'] in m and 'install' in m.lower() for m in said), said


def test_a_goal_repo_inside_the_installed_app_is_refused(db, workspace, monkeypatch):
    monkeypatch.setattr('core.platform_paths.get_install_dir',
                        lambda: workspace['install'])
    session = get_db()
    try:
        session.add(AgentGoal(id='g-inst', goal_type='coding', title='fix it',
                              prompt_id='779',
                              config_json={'repo_path': workspace['install']}))
        session.commit()
    finally:
        session.close()
    assert resolve_task_workspace(prompt_id='779') == workspace['ws']


def test_a_source_run_has_no_install_folder(monkeypatch):
    """Running from source (no sys.frozen) there is no installed app folder
    to refuse; a frozen build's is the executable's folder."""
    import sys as _sys
    from core import platform_paths
    monkeypatch.delattr(_sys, 'frozen', raising=False)
    assert platform_paths.get_install_dir() is None
    monkeypatch.setattr(_sys, 'frozen', True, raising=False)
    monkeypatch.setattr(_sys, 'executable', os.path.join('X:', os.sep, 'Apps', 'Nunba', 'Nunba.exe'))
    assert platform_paths.get_install_dir() == os.path.join('X:', os.sep, 'Apps', 'Nunba')


def test_source_guard_every_computer_use_entrypoint_uses_the_one_resolver():
    """DRY guard across two files (the behaviour is covered above): both
    entry points that build a VLM message resolve the workspace through
    resolve_task_workspace and neither stamps the process cwd."""
    for rel in ('hart_intelligence_entry.py', 'hartos/reuse_recipe.py'):
        source = ROOT.joinpath(rel).read_text(encoding='utf-8')
        assert "'workspace_root': resolve_task_workspace(prompt_id=prompt_id)," in source, rel
        assert "'workspace_root': os.getcwd()" not in source, rel
