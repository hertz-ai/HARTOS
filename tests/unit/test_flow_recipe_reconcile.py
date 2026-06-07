"""Functional tests for the orphaned flow-recipe reconciler (#85, Gate 1).

These write REAL action-recipe + config files into a tmp prompts dir, call the
REAL reconciler, and assert on the REAL flow-recipe file it writes (content +
return value). The whole point of the fix is "never synthesize a partial flow",
so the safety tests (short count / gap / non-terminal / no-overwrite) are the
load-bearing ones.

Imports core.flow_recipe_reconcile directly — it is deliberately autogen/Flask
free, so this suite runs in the autogen-less CI env (unlike create_recipe).
No grep tests: every assertion observes a file the function actually produced.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.flow_recipe_reconcile import reconcile_orphaned_flow_recipes


def _write(d, name, obj):
    with open(os.path.join(d, name), 'w', encoding='utf-8') as f:
        json.dump(obj, f)


def _read(d, name):
    with open(os.path.join(d, name), 'r', encoding='utf-8') as f:
        return json.load(f)


def _config(n_actions, n_flows=1):
    """A prompt config whose flow 0 declares n_actions planned actions."""
    flows = [{'persona': 'Assistant',
              'actions': [f'action {i}' for i in range(1, n_actions + 1)]}]
    for _ in range(n_flows - 1):
        flows.append({'persona': 'Assistant', 'actions': ['x']})
    return {'flows': flows}


def _action(action_id, status='done'):
    return {
        'status': status,
        'action': f'do thing {action_id}',
        'fallback_action': 'ask the user',
        'persona': 'Assistant',
        'action_id': action_id,
        'recipe': [{'steps': f'step for {action_id}', 'tool_name': 'None',
                    'generalized_functions': '',
                    'agent_to_perform_this_action': 'Helper'}],
    }


def test_reconciles_a_complete_orphaned_flow(tmp_path):
    d = str(tmp_path)
    _write(d, '777.json', _config(2))          # config: 2 planned actions
    _write(d, '777_0_1.json', _action(1))       # both action recipes present
    _write(d, '777_0_2.json', _action(2))       # ...but NO flow recipe yet

    out = reconcile_orphaned_flow_recipes('777', prompts_dir=d)

    assert ('777', 0) in out
    fr = _read(d, '777_0_recipe.json')
    assert fr['status'] == 'completed'
    # Assembled from the on-disk action recipes, in action-id order.
    assert [a['action_id'] for a in fr['actions']] == [1, 2]
    assert fr['actions'][0]['recipe'][0]['steps'] == 'step for 1'


def test_skips_short_count_never_truncates(tmp_path):
    d = str(tmp_path)
    _write(d, '778.json', _config(3))           # config says 3 actions
    _write(d, '778_0_1.json', _action(1))
    _write(d, '778_0_2.json', _action(2))       # only 2 of 3 on disk

    out = reconcile_orphaned_flow_recipes('778', prompts_dir=d)

    assert out == []                            # refused to assemble a partial
    assert not os.path.exists(os.path.join(d, '778_0_recipe.json'))


def test_skips_gap_missing_first_action(tmp_path):
    d = str(tmp_path)
    _write(d, '779.json', _config(2))
    _write(d, '779_0_2.json', _action(2))       # action 1 MISSING (gap)

    out = reconcile_orphaned_flow_recipes('779', prompts_dir=d)

    assert out == []
    assert not os.path.exists(os.path.join(d, '779_0_recipe.json'))


def test_skips_non_terminal_action_status(tmp_path):
    d = str(tmp_path)
    _write(d, '780.json', _config(1))
    _write(d, '780_0_1.json', _action(1, status='in_progress'))  # not done

    out = reconcile_orphaned_flow_recipes('780', prompts_dir=d)

    assert out == []
    assert not os.path.exists(os.path.join(d, '780_0_recipe.json'))


def test_never_overwrites_existing_flow_recipe(tmp_path):
    d = str(tmp_path)
    _write(d, '781.json', _config(1))
    _write(d, '781_0_1.json', _action(1))
    pre = {'status': 'completed', 'actions': ['PRE-EXISTING-SENTINEL']}
    _write(d, '781_0_recipe.json', pre)

    out = reconcile_orphaned_flow_recipes('781', prompts_dir=d)

    assert out == []                            # already present, untouched
    assert _read(d, '781_0_recipe.json')['actions'] == ['PRE-EXISTING-SENTINEL']


def test_accepts_completed_status_too(tmp_path):
    d = str(tmp_path)
    _write(d, '783.json', _config(1))
    _write(d, '783_0_1.json', _action(1, status='completed'))

    out = reconcile_orphaned_flow_recipes('783', prompts_dir=d)

    assert ('783', 0) in out
    assert _read(d, '783_0_recipe.json')['status'] == 'completed'


def test_sweep_mode_discovers_orphans_without_prompt_id(tmp_path):
    d = str(tmp_path)
    # one reconcilable prompt + one that must be left alone (short count)
    _write(d, '782.json', _config(1))
    _write(d, '782_0_1.json', _action(1))
    _write(d, '790.json', _config(2))
    _write(d, '790_0_1.json', _action(1))       # only 1 of 2 → must skip

    out = reconcile_orphaned_flow_recipes(prompts_dir=d)  # sweep, no prompt_id

    assert ('782', 0) in out
    assert ('790', 0) not in out
    assert os.path.exists(os.path.join(d, '782_0_recipe.json'))
    assert not os.path.exists(os.path.join(d, '790_0_recipe.json'))


def test_does_not_resurrect_an_optimizer_archived_flow(tmp_path):
    # #111: the optimizer retired this flow's recipe to .optbak so the daemon
    # re-CREATEs it WITH experience hints. The action recipes still exist, so the
    # naive reconciler would rebuild the flow recipe and silently no-op Gate-4.
    d = str(tmp_path)
    _write(d, '785.json', _config(1))
    _write(d, '785_0_1.json', _action(1))             # action recipe present...
    # ...but the optimizer has archived the flow recipe (the daemon owns re-CREATE)
    _write(d, '785_0_recipe.json.optbak', {'status': 'completed', 'actions': []})
    _write(d, '785_0_recipe.json.optbak.meta.json',
           {'archived_reward': 0.3, 'prompt_id': '785', 'flow_id': 0})

    out = reconcile_orphaned_flow_recipes('785', prompts_dir=d)

    assert out == []                                  # left for the daemon re-CREATE
    assert not os.path.exists(os.path.join(d, '785_0_recipe.json'))


def test_multiple_flows_per_prompt(tmp_path):
    d = str(tmp_path)
    _write(d, '784.json', _config(1, n_flows=2))  # flow 0 + flow 1
    _write(d, '784_0_1.json', _action(1))          # flow 0 complete
    _write(d, '784_1_1.json', _action(1))          # flow 1 complete

    out = reconcile_orphaned_flow_recipes('784', prompts_dir=d)

    assert ('784', 0) in out and ('784', 1) in out
    assert os.path.exists(os.path.join(d, '784_0_recipe.json'))
    assert os.path.exists(os.path.join(d, '784_1_recipe.json'))
