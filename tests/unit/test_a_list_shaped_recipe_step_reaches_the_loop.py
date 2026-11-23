"""A recipe step whose text is a list reaches the VLM as lines, on both legs.

Live 2026-11-21, agent 15004929489 (the news-curation agent): CREATE wrote
action 2's recipe step as a list of three sentences

    "steps": ["Open a default web browser and navigate to ...",
              "Locate and select the 'General News' ... filter ...",
              "Extract the URLs of the top 10 most recent articles ..."]

and both command tools read a matched step with ``step.get('steps', '')
.strip()``.  Every time the model's instruction matched action 2 the tool
raised AttributeError("'list' object has no attribute 'strip'") and returned
``{'error': ...}`` in about 200 ms, before the VLM ran (09:18:25 and
13:31:21; frozen_debug.log traceback at reuse_recipe.py:2739).  The same
reader turned a list-shaped tool argument into its repr "['a', 'b']".

The tools here are the REAL ones, built by the real agent factories with the
create-loop fixture's patches; only the owner-consent check and the VLM are
replaced.

    python -m pytest tests/unit/test_a_list_shaped_recipe_step_reaches_the_loop.py -q -p no:cacheprovider
"""
import asyncio
import json

import pytest

from tests.unit.test_create_loop_end_to_end import (  # noqa: F401  (fixture)
    ACTIONS, PROMPT_ID, UP, USER_ID, create_env)
from tests.unit.test_create_vlm_learning_files_its_action import (  # noqa: F401
    computer)

STEPS = ['Open the sources folder in the file manager',
         'Select the three newest files',
         'Read each file and write five lines']


def _done():
    return {'status': 'success', 'exit_reason': 'done',
            'extracted_responses': [
                {'type': 'completion', 'iteration': 1, 'content': 'done'}]}


@pytest.fixture
def reuse_sent(create_env, monkeypatch):
    """REUSE's real command tool on a saved flow whose action 2 has a
    list-shaped step.  Returns (run, sent): run(instruction) calls the tool
    while action 2 runs; sent holds every message the VLM was handed."""
    env = create_env
    import hartos.reuse_recipe as rr
    import integrations.vlm.safety as safety
    import integrations.vlm.vlm_adapter as adapter

    monkeypatch.setattr(rr, 'PROMPTS_DIR', str(env.prompts))
    monkeypatch.setattr(rr, 'HAS_SIMPLEMEM', False)
    for name in ('user_tasks', 'recipes', 'final_recipe', 'request_id_list',
                 'user_agents', 'time_actions', 'user_ledgers'):
        obj = getattr(rr, name, None)
        if obj is not None and hasattr(obj, '_loader'):
            monkeypatch.setattr(obj, '_loader', None)
        if obj is not None and hasattr(obj, 'clear'):
            obj.clear()
    if getattr(rr, 'get_production_backend', None) is not None:
        from agent_ledger.backends import InMemoryBackend
        monkeypatch.setattr(rr, 'get_production_backend',
                            lambda *a, **k: InMemoryBackend())
    (env.prompts / f'{PROMPT_ID}_0_recipe.json').write_text(json.dumps({
        'status': 'done', 'actions': [
            {'action_id': i, 'action': text, 'persona': 'Researcher',
             'can_perform_without_user_input': 'yes',
             'recipe': [{'steps': (STEPS if i == 2 else 'authored step %d' % i),
                         'tool_name': 'execute_windows_or_android_command'}]}
            for i, text in enumerate(ACTIONS, 1)]}), encoding='utf-8')
    monkeypatch.setattr(safety, 'computer_control_block', lambda *a, **k: None)
    sent = []

    def _vlm(message):
        sent.append(dict(message))
        return _done()
    monkeypatch.setattr(adapter, 'execute_vlm_instruction', _vlm)

    with env.app.app_context():
        built = rr.create_agents_for_user(USER_ID, PROMPT_ID)
    tool = next(f for n, _d, f in built[0]._hart_core_tools
                if n == 'execute_windows_or_android_command')

    def run(instruction, action=2):
        rr.user_tasks[UP].current_action = action
        rr.request_id_list[UP] = 'req_list_steps'
        with env.app.app_context():
            out = tool(instruction, 'windows')
            if asyncio.iscoroutine(out):
                out = asyncio.run(out)
        return out

    return run, sent


def test_a_list_is_one_text_of_lines():
    from hartos.reuse_recipe import _coerce_instruction_text
    assert _coerce_instruction_text(STEPS) == '\n'.join(STEPS)
    assert _coerce_instruction_text(
        [' a ', None, '', {'text': 'b'}, ['c', 'd']]) == 'a\nb\nc\nd'
    assert _coerce_instruction_text([]) == ''


def test_reuse_hands_a_list_step_to_the_vlm_instead_of_raising(reuse_sent):
    run, sent = reuse_sent
    out = run(ACTIONS[1])           # action 2's own words: the recipe matches
    assert sent, ('the VLM was never called; the tool answered %r -- a '
                  'list-shaped step raised before the loop started' % (out,))
    enhanced = sent[0].get('enhanced_instruction') or ''
    assert 'Follow these steps from a previous successful execution' in enhanced
    for line in STEPS:
        assert line in enhanced, (line, enhanced)
    assert "['" not in enhanced, 'the step reached the VLM as a list repr'


def test_reuse_reads_a_list_argument_as_lines(reuse_sent):
    """The existing caller of the coercer (the tool's own argument)."""
    run, sent = reuse_sent
    run(['Open Notepad', 'type the five lines'], action=3)
    assert sent, 'the VLM was never called'
    assert sent[0]['instruction_to_vlm_agent'] == \
        'Open Notepad\ntype the five lines'


def test_create_hands_a_list_step_to_the_vlm_instead_of_raising(computer):
    env, run, sent = computer
    env.cr.vlm_recipes[UP] = {'actions': [
        {'action_id': 2, 'action': 'Summarise the sources folder files',
         'recipe': [{'steps': STEPS,
                     'tool_name': 'execute_windows_or_android_command'}]}]}
    out = run('Summarise the sources folder files', 'read files', action=2)
    assert sent, 'the VLM was never called: %r' % (out,)
    enhanced = sent[0].get('enhanced_instruction') or ''
    for line in STEPS:
        assert line in enhanced, (line, enhanced)
