"""list_agents/list_recipes must resolve prompts/ via the canonical resolver.

Task #692: both MCP tool impls resolved prompts as
os.path.join(os.path.dirname(__file__), '..', '..', 'prompts') — a
module-relative path that does not exist in the frozen desktop layout
(site-packages/prompts), so installed builds scanned 0 recipes while the
repo held 2,633.  Worse, on bundled desktops recipes are SAVED to the
user data dir (core.platform_paths.get_recipe_prompts_dir), so even an
existing module-relative dir would be the WRONG folder.

The canonical resolver is core.platform_paths.get_recipe_prompts_dir —
the single dir shared by recipe SAVE (helper/create_recipe), REUSE read
(cache_loaders) and the daemon reuse-CHECK.
"""

import json
import os

import pytest


def test_list_recipes_uses_canonical_prompts_dir(monkeypatch, tmp_path):
    from integrations.mcp import _tool_impls

    recipe = {"prompt_id": "99999", "agent_name": "canon-probe",
              "agent_status": "completed"}
    (tmp_path / "99999_test_recipe.json").write_text(json.dumps(recipe))

    monkeypatch.setattr(_tool_impls, 'get_recipe_prompts_dir',
                        lambda: str(tmp_path), raising=False)
    out = json.loads(_tool_impls.list_recipes())
    names = [r.get("agent_name") for r in out.get("recipes", [])]
    assert "canon-probe" in names, (
        "list_recipes did not scan the canonical recipe dir — "
        "module-relative prompts/ path regressed (#692)")


def test_no_module_relative_prompts_path():
    """No integrations file may resolve prompts/ module-relatively.

    Sweep caught 4 production copies of the pattern on 2026-08-24:
    mcp/_tool_impls.py (x2), agent_engine/agent_baseline_service.py,
    agent_engine/window_layout_recipe.py, social/recipe_sharing.py.
    """
    import glob
    integ = os.path.join(os.path.dirname(__file__), '..', 'integrations')
    offenders = []
    for src_path in glob.glob(os.path.join(integ, '**', '*.py'),
                              recursive=True):
        with open(src_path, encoding='utf-8', errors='replace') as fh:
            if "'..', '..', 'prompts'" in fh.read():
                offenders.append(os.path.relpath(src_path, integ))
    assert not offenders, (
        f"module-relative prompts/ resolution regressed in {offenders} — "
        "use core.platform_paths.get_recipe_prompts_dir or "
        "core.cache_loaders.PROMPTS_DIR (#692)")
