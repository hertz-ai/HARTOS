"""Recipe self-optimizer file surgery (#88, Gate 4 ACT half).

archive -> (daemon re-CREATEs) -> accept | rollback. These exercise the real
functions against a tmp prompts dir and assert the on-disk outcome + the
anti-churn / rollback safety guards. No grep tests.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import flow_recipe_optimizer as opt


def _recipe(d, pid='77', flow=0, body=None):
    p = os.path.join(d, f'{pid}_{flow}_recipe.json')
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(body or {'status': 'completed', 'actions': [{'action_id': 1}]}, f)
    return p


def _read(p):
    with open(p, 'r', encoding='utf-8') as f:
        return json.load(f)


def test_archive_retires_recipe_and_records_reward(tmp_path):
    d = str(tmp_path)
    recipe = _recipe(d, body={'v': 'original'})
    assert opt.archive_recipe_for_reoptimization('77', 0, reward=0.42, prompts_dir=d) is True
    # recipe file gone, backup present, sidecar records the reward
    assert not os.path.exists(recipe)
    assert os.path.exists(recipe + '.optbak')
    assert _read(recipe + '.optbak') == {'v': 'original'}
    assert opt.has_pending_optimization('77', 0, prompts_dir=d) is True
    assert opt.archived_reward('77', 0, prompts_dir=d) == 0.42


def test_archive_noop_when_no_recipe(tmp_path):
    assert opt.archive_recipe_for_reoptimization('77', 0, reward=0.1, prompts_dir=str(tmp_path)) is False


def test_archive_noop_when_already_pending_anti_churn(tmp_path):
    d = str(tmp_path)
    _recipe(d, body={'v': 'original'})
    assert opt.archive_recipe_for_reoptimization('77', 0, 0.42, prompts_dir=d) is True
    # daemon re-CREATEs a fresh recipe in place while a .optbak is pending...
    _recipe(d, body={'v': 'recreated'})
    # ...a second archive MUST NOT stack (would re-enter #85 re-CREATE churn).
    assert opt.archive_recipe_for_reoptimization('77', 0, 0.5, prompts_dir=d) is False
    assert _read(os.path.join(d, '77_0_recipe.json')) == {'v': 'recreated'}  # untouched


def test_rollback_restores_the_proven_better_recipe(tmp_path):
    d = str(tmp_path)
    recipe = _recipe(d, body={'v': 'original'})
    opt.archive_recipe_for_reoptimization('77', 0, 0.42, prompts_dir=d)
    _recipe(d, body={'v': 'worse-recreate'})           # daemon re-CREATEd a worse one
    assert opt.rollback_recipe('77', 0, prompts_dir=d) is True
    assert _read(recipe) == {'v': 'original'}           # proven-better is live again
    assert not os.path.exists(recipe + '.optbak')       # backup consumed
    assert opt.has_pending_optimization('77', 0, prompts_dir=d) is False


def test_accept_keeps_recreate_and_drops_backup(tmp_path):
    d = str(tmp_path)
    recipe = _recipe(d, body={'v': 'original'})
    opt.archive_recipe_for_reoptimization('77', 0, 0.42, prompts_dir=d)
    _recipe(d, body={'v': 'better-recreate'})          # daemon re-CREATEd a better one
    assert opt.accept_reoptimization('77', 0, prompts_dir=d) is True
    assert _read(recipe) == {'v': 'better-recreate'}    # kept
    assert not os.path.exists(recipe + '.optbak')       # backup + sidecar gone
    assert opt.archived_reward('77', 0, prompts_dir=d) is None


def test_rollback_noop_without_backup(tmp_path):
    assert opt.rollback_recipe('77', 0, prompts_dir=str(tmp_path)) is False
