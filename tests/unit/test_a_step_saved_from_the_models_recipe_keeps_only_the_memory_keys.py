"""A step saved from the model's recipe keeps only the keys of the creator's
memory, on the real create loop.

65d65a767 made CREATE's two action-file writers save the creator's memory
outline keys-only (strip_json_values(..., keys_only=True)): REUSE shows that
file to every later user of the agent, and "<saved_str>" there told a new
person a value was saved where they had none (rv_88d7 S2).  rv_3local's
review of 65d65a767 asked for that switch to be pinned by behaviour, not by
reading the source.

This drives the REAL loop (create_recipe.recipe -> get_response_group) with
the scripted agents of tests/unit/test_create_loop_end_to_end.py, no LLM.
The trace cannot be banked here, so each finished step's recipe is asked of
the model and saved by those writers.  The creator's memory holds a saved
email draft; every action file written from the model's recipe must name its
keys and claim no value.

    python -m pytest tests/unit/test_a_step_saved_from_the_models_recipe_keeps_only_the_memory_keys.py -q -p no:cacheprovider
"""
import json

from core.agent_tools import person_store_id
from tests.unit.test_create_loop_end_to_end import (  # noqa: F401
    PROMPT_ID, UP, create_env, _run)


def _leaves(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from _leaves(v)
    elif isinstance(node, list):
        for v in node:
            yield from _leaves(v)
    else:
        yield node


def test_an_action_file_from_the_models_recipe_names_keys_and_no_values(
        create_env, monkeypatch):
    env = create_env
    monkeypatch.setattr(env.cr, '_bank_action_recipe_from_trace',
                        lambda *a, **k: False)
    env.cr.agent_data[person_store_id(UP)] = {
        'email_brief': {'recipient': 'manager@example.com',
                        'body': 'May I take Friday off?'},
        'step': 2}
    _run(env, turns=6)
    written = [json.loads(p.read_text(encoding='utf-8'))
               for p in sorted(env.prompts.glob(f'{PROMPT_ID}_0_*.json'))
               if not p.name.endswith('_recipe.json')]
    from_model = [d for d in written if d.get('recipe_source') != 'execution_trace']
    assert from_model, ('no action file was written from the model recipe; '
                        f'recipe requests: {env.script.recipe_requests}; '
                        f'files: {[sorted(d) for d in written]}')
    for data in from_model:
        meta = data.get('metadata')
        assert meta.get('email_brief') == {'recipient': None, 'body': None}, meta
        assert 'step' in meta, meta
        assert all(leaf is None for leaf in _leaves(meta)), meta


def test_a_recipe_saved_late_keeps_only_the_memory_keys_too(create_env, monkeypatch,
                                                           caplog):
    """The late-save twin: a recipe that arrives after its step has moved on
    (the recipe-completion hook did not move it to RECIPE_RECEIVED) is saved
    by the second writer, which must keep only the keys too.  Here the hook
    moves nothing, so every recipe the model returns is saved late."""
    import logging
    env = create_env
    monkeypatch.setattr(env.cr, '_bank_action_recipe_from_trace',
                        lambda *a, **k: False)
    monkeypatch.setattr(env.cr, 'lifecycle_hook_track_recipe_completion',
                        lambda *a, **k: {'action': 'allow', 'message': None})
    env.cr.agent_data[person_store_id(UP)] = {
        'email_brief': {'recipient': 'manager@example.com',
                        'body': 'May I take Friday off?'},
        'step': 2}
    with caplog.at_level(logging.INFO):
        _run(env, turns=2)
    assert any('Saved Individual recipe (late)' in r.getMessage()
               for r in caplog.records), 'the late writer never saved a step'
    written = [json.loads(p.read_text(encoding='utf-8'))
               for p in sorted(env.prompts.glob(f'{PROMPT_ID}_0_*.json'))
               if not p.name.endswith('_recipe.json')]
    late = [d for d in written if d.get('recipe_source') != 'execution_trace']
    assert late, ('no action file was saved late; recipe requests: '
                  f'{env.script.recipe_requests}')
    for data in late:
        meta = data.get('metadata')
        assert meta.get('email_brief') == {'recipient': None, 'body': None}, meta
        assert all(leaf is None for leaf in _leaves(meta)), meta
