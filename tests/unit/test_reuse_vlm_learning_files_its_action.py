"""REUSE files a computer-use re-learning under the action that ran it, with
the provenance the loader requires.

The REUSE twin of test_create_vlm_learning_files_its_action.py.  The agents
are built by the REAL ``reuse_recipe.create_agents_for_user`` on a saved flow
recipe (scripted, no LLM, no network: the create-loop fixture's patches are
reused), the tool is taken from the Assistant's ``_hart_core_tools`` (where
REUSE hands it to the named attach), and only the owner-consent check and the
VLM itself are replaced.

Why it matters: the loader now refuses a learning whose ``learned_for`` stamp
does not name its own action.  A REUSE writer that stamped nothing, or the
wrong id, would have every re-learning refused (or filed under a neighbour),
and nothing else in the suite would notice.

    python -m pytest tests/unit/test_reuse_vlm_learning_files_its_action.py -q -p no:cacheprovider
"""
import asyncio
import json

import pytest

from tests.unit.test_create_loop_end_to_end import (  # noqa: F401  (fixture)
    ACTIONS, PROMPT_ID, UP, USER_ID, create_env)

_SUFFIX = '_vlm_agent.json'


def _vlm_run(*commands):
    return {
        'status': 'success', 'total_messages': len(commands),
        'extracted_responses': [
            {'type': 'action', 'iteration': i + 1,
             'content': {'action': 'shell', 'reasoning': c, 'result': 'ok',
                         'ok': True}}
            for i, c in enumerate(commands)]}


def _steps(record):
    return [s.get('steps', '') for s in record.get('recipe') or []]


@pytest.fixture
def reuse_tool(create_env, monkeypatch):
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
    # The saved flow recipe REUSE replays: the fixture's three actions.
    (env.prompts / f'{PROMPT_ID}_0_recipe.json').write_text(json.dumps({
        'status': 'done', 'actions': [
            {'action_id': i, 'action': text, 'persona': 'Researcher',
             'can_perform_without_user_input': 'yes',
             'recipe': [{'steps': 'authored step %d' % i,
                         'tool_name': 'google_search'}]}
            for i, text in enumerate(ACTIONS, 1)]}), encoding='utf-8')

    monkeypatch.setattr(safety, 'computer_control_block', lambda *a, **k: None)
    queued = []
    monkeypatch.setattr(adapter, 'execute_vlm_instruction',
                        lambda message: queued.pop(0))

    with env.app.app_context():
        built = rr.create_agents_for_user(USER_ID, PROMPT_ID)
    tool = next(f for n, _d, f in built[0]._hart_core_tools
                if n == 'execute_windows_or_android_command')

    def run(instruction, *commands, action=2):
        rr.user_tasks[UP].current_action = action
        rr.request_id_list[UP] = 'req_reuse_vlm'
        queued.append(_vlm_run(*(commands or (instruction,))))
        with env.app.app_context():
            out = tool(instruction, 'windows')
            if asyncio.iscoroutine(out):
                out = asyncio.run(out)
        assert not queued, 'the VLM was never called: %r' % (out,)
        return out

    return env, rr, run


def test_a_relearning_is_filed_and_stamped_as_the_running_action(reuse_tool):
    """The instruction paraphrases action 2 ('Summarise the three sources in
    five lines'): 0.67 against it, above REUSE's identity guard (0.45) and
    below the replay bar (0.8), so it is re-learned, not replayed."""
    env, rr, run = reuse_tool
    run('Summarise the three sources into five lines now please', 'open editor',
        action=2)
    names = sorted(p.name for p in env.prompts.iterdir()
                   if p.name.endswith(_SUFFIX))
    assert names == [f'{PROMPT_ID}_0_2{_SUFFIX}'], names
    rec = json.loads((env.prompts / names[0]).read_text(encoding='utf-8'))
    assert rec['learned_for']['action_id'] == 2
    assert rec['learned_for']['prompt_id'] == str(PROMPT_ID)
    with env.app.app_context():
        loaded = rr.load_vlm_agent_files(PROMPT_ID, 0)
    assert [v['action_id'] for v in loaded] == [2], (
        'REUSE wrote a learning the loader refuses: every REUSE re-learning '
        'would be lost')


def test_two_calls_in_one_reuse_execution_keep_both(reuse_tool):
    env, rr, run = reuse_tool
    run('Summarise the three sources into five lines now please', 'open editor',
        action=2)
    run('Summarise the sources in five lines and save them', 'type lines',
        action=2)
    rec = json.loads((env.prompts / f'{PROMPT_ID}_0_2{_SUFFIX}').read_text(
        encoding='utf-8'))
    steps = _steps(rec)
    assert len(steps) == 2 and 'open editor' in steps[0] \
        and 'type lines' in steps[1], steps
