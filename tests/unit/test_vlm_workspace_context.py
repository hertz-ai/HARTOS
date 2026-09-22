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


def test_an_explicit_workspace_wins(db):
    assert resolve_task_workspace(prompt_id='777', explicit='  D:/work/repo ') == 'D:/work/repo'


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


def test_source_guard_every_computer_use_entrypoint_uses_the_one_resolver():
    """DRY guard across two files (the behaviour is covered above): both
    entry points that build a VLM message resolve the workspace through
    resolve_task_workspace and neither stamps the process cwd."""
    for rel in ('hart_intelligence_entry.py', 'hartos/reuse_recipe.py'):
        source = ROOT.joinpath(rel).read_text(encoding='utf-8')
        assert "'workspace_root': resolve_task_workspace(prompt_id=prompt_id)," in source, rel
        assert "'workspace_root': os.getcwd()" not in source, rel
