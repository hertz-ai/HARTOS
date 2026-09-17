"""A computer-use action must receive the workspace its task was assigned.

The live failure on 2026-09-17 opened ``revenue_aggregator.py`` as a bare
filename.  Windows resolved it against the service launch directory, while the
only similarly named file was in an unrelated, older build checkout.  The VLM
must receive one declared root and must not guess among checkouts.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _source(*parts):
    return (ROOT.joinpath(*parts)).read_text(encoding='utf-8')


def test_vlm_loop_injects_the_declared_workspace_into_the_task():
    source = _source('integrations', 'vlm', 'local_loop.py')
    assert "workspace_root = str(message.get('workspace_root') or '').strip()" in source
    assert 'Declared task workspace: {workspace_root}' in source
    assert 'resolve it within this workspace before opening it' in source


def test_every_local_computer_use_entrypoint_declares_its_workspace():
    direct = _source('hart_intelligence_entry.py')
    reused = _source('hartos', 'reuse_recipe.py')
    assert "'workspace_root': os.getcwd()," in direct
    assert "'workspace_root': os.getcwd()," in reused
