"""Behavioural test for the REUSE flow-recipe shape normalization
(2026-05-31, evidence-driven — NOT a hypothesis).

LIVE EVIDENCE (frozen_debug.log + ~/Documents/Nunba/data/prompts):
- The local model DOES emit recipes and they ARE saved (135 flow recipes on
  disk). The earlier "model can't emit / 0 recipes" diagnosis was wrong —
  it checked the dev repo dir, not the build's data dir.
- The real flywheel leak: some flow-recipe files ({prompt_id}_{flow}_recipe.json)
  hold a PER-ACTION recipe shape ({status, action, recipe, action_id, persona})
  with NO top-level 'actions' key (verified: 71294888731_0_recipe.json).
- reuse_recipe did recipes[user_prompt]['actions'] → KeyError 'actions' →
  "Some ERROR IN REUSE RECIPE 'actions'" → fell back to expensive CREATE, so
  REUSE never engaged for those agents.

FIX: _normalize_flow_recipe() wraps a per-action recipe in the flow slot as a
one-element actions list at LOAD time, so recipes already on disk reuse without
a rewrite.  These tests pin that normalization directly.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# reuse_recipe type-annotates module-level caches with autogen.AssistantAgent
# (evaluated at import time), so importing it crashes when autogen is absent
# (CI). Skip cleanly, matching the suite-wide pattern.
pytest.importorskip('autogen', reason='autogen not installed')

from reuse_recipe import _normalize_flow_recipe  # noqa: E402


def test_per_action_recipe_in_flow_slot_gets_wrapped():
    """The exact shape that crashed reuse (71294888731_0_recipe.json)."""
    per_action = {
        'status': 'done',
        'action': 'Post the Show HN draft',
        'fallback_action': '',
        'persona': 'social_media',
        'action_id': 1,
        'recipe': [{'steps': 'open browser', 'tool_name': 'execute_windows_or_android_command'}],
        'can_perform_without_user_input': 'yes',
        'scheduled_tasks': [{'cron_expression': '0 9 * * *', 'job_description': 'daily'}],
    }
    norm = _normalize_flow_recipe(per_action)
    assert isinstance(norm.get('actions'), list), "must now have an 'actions' list"
    assert len(norm['actions']) == 1
    assert norm['actions'][0]['action'] == 'Post the Show HN draft'
    # reuse reads action['persona'] + action['action'] — must survive
    assert norm['actions'][0]['persona'] == 'social_media'
    # flow-level scheduling preserved (reuse_recipe reads config['scheduled_tasks'])
    assert norm['scheduled_tasks'] == per_action['scheduled_tasks']
    # the access that used to KeyError now works
    _ = norm['actions']  # no raise


def test_correct_flow_recipe_passes_through_unchanged():
    flow = {'status': 'completed', 'actions': [
        {'action_id': 1, 'persona': 'p', 'action': 'a', 'recipe': []},
        {'action_id': 2, 'persona': 'p', 'action': 'b', 'recipe': []},
    ]}
    norm = _normalize_flow_recipe(flow)
    assert norm is flow, "a well-formed flow recipe must not be copied/altered"
    assert len(norm['actions']) == 2


def test_empty_actions_list_is_respected():
    """A flow recipe with an (empty but present) actions list is valid — not
    treated as a per-action recipe."""
    flow = {'status': 'completed', 'actions': []}
    norm = _normalize_flow_recipe(flow)
    assert norm is flow
    assert norm['actions'] == []


def test_unknown_shape_degrades_to_empty_actions_no_crash():
    weird = {'status': 'completed', 'foo': 'bar'}
    norm = _normalize_flow_recipe(weird)
    assert norm.get('actions') == [], "unknown shape gets empty actions, not KeyError"


def test_non_dict_returned_as_is():
    assert _normalize_flow_recipe(None) is None
    assert _normalize_flow_recipe(['x']) == ['x']
