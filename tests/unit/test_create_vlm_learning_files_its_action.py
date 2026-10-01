"""CREATE files a computer-use learning under the action that ran it.

Behavioural twin of the source guard 5d6343409 shipped alone (review of
5d6343409, rejected: "its only test is a source guard", and two mutants,
filing under ``action_id + 1`` among them, survived all 53 related tests).

This drives CREATE's REAL ``execute_windows_or_android_command``: the agents
are built by ``create_agents`` (scripted, no LLM, no network, the fixture
test_create_loop_end_to_end.py already uses), the tool is taken from the
Assistant's function map, and only the two boundaries are replaced: the
owner-consent check (answered "allowed") and the VLM itself (a scripted
successful run).  Then the prompts dir is read back.

What is asserted is what REUSE depends on:

- the learning lands at ``<agent>_<flow>_<action>_vlm_agent.json`` for the
  action that was running (1-based ``current_action``), never the next free
  slot and never a neighbour's id;
- it carries the provenance the loader now requires (``learned_for``), so
  ``helper.load_vlm_agent_files`` hands it back under the same id;
- two computer-use calls inside ONE execution of an action keep both calls'
  steps, in order (before this change the second call's write replaced the
  first's, so an action that opened an app and then typed into it replayed
  only the typing);
- a later execution re-learns the action from scratch: its steps replace the
  earlier run's, they do not pile up across runs;
- a file on disk that cannot prove it belongs to the running action is not
  injected as "steps from a previous successful execution".

    python -m pytest tests/unit/test_create_vlm_learning_files_its_action.py -q -p no:cacheprovider
"""
import asyncio
import json
import os

import pytest

from tests.unit.test_create_loop_end_to_end import (  # noqa: F401  (fixture)
    PROMPT_ID, UP, USER_ID, create_env)

_SUFFIX = '_vlm_agent.json'


def _vlm_run(*commands):
    """A successful local VLM run, in the shape integrations.vlm.local_loop
    reports it (``extracted_responses`` of type 'action')."""
    return {
        'status': 'success', 'total_messages': len(commands),
        'extracted_responses': [
            {'type': 'action', 'iteration': i + 1,
             'content': {'action': 'shell', 'reasoning': c, 'result': 'ok',
                         'ok': True}}
            for i, c in enumerate(commands)]}


def _vlm_files(env):
    return sorted(p.name for p in env.prompts.iterdir()
                  if p.name.endswith(_SUFFIX))


def _read(env, action_id):
    p = env.prompts / f'{PROMPT_ID}_0_{action_id}{_SUFFIX}'
    return json.loads(p.read_text(encoding='utf-8'))


def _steps(record):
    return [s.get('steps', '') for s in record.get('recipe') or []]


@pytest.fixture
def computer(create_env, monkeypatch):
    """(env, run, sent): ``run(instruction, *commands, action=, new_run=)``
    calls CREATE's real tool while ``action`` is the running action."""
    env = create_env
    import integrations.vlm.safety as safety
    import integrations.vlm.vlm_adapter as adapter
    from hartos.helper import Action

    # The owner's consent is a boundary: answered "allowed".
    monkeypatch.setattr(safety, 'computer_control_block', lambda *a, **k: None)
    queued, sent = [], []

    def _vlm(message):
        sent.append(dict(message))
        return queued.pop(0)
    monkeypatch.setattr(adapter, 'execute_vlm_instruction', _vlm)

    def _new_execution():
        """One execution of the flow's actions, as CREATE starts one."""
        env.cr.user_tasks[UP] = Action(
            [{'action_id': i, 'action': f'step {i}'} for i in (1, 2, 3)])

    _new_execution()   # create_agents reads the session's actions
    with env.app.app_context():
        built = env.cr.create_agents(USER_ID, 'Build the agent now', PROMPT_ID)
    tool = built[6]['assistant']._function_map[
        'execute_windows_or_android_command']

    def run(instruction, *commands, action=2, new_run=False):
        if new_run or UP not in env.cr.user_tasks:
            _new_execution()
        env.cr.user_tasks[UP].current_action = action
        env.cr.request_id_list[UP] = 'req_vlm_learning'
        queued.append(_vlm_run(*(commands or (instruction,))))
        with env.app.app_context():
            out = tool(instruction, 'windows')
            if asyncio.iscoroutine(out):
                out = asyncio.run(out)
        assert not queued, 'the VLM was never called: %r' % (out,)
        return out

    return env, run, sent


class TestTheLearningIsFiledUnderItsOwnAction:

    def test_it_lands_at_the_running_actions_path(self, computer):
        env, run, _ = computer
        run('Open Notepad', 'open notepad', action=2)
        assert _vlm_files(env) == [f'{PROMPT_ID}_0_2{_SUFFIX}'], (
            'action 2 ran the command, so its learning must be filed as '
            'action 2 -- the reader takes the filename number as the id')
        rec = _read(env, 2)
        assert rec['action_id'] == 2
        assert rec['learned_for']['prompt_id'] == str(PROMPT_ID)
        assert rec['learned_for']['flow'] == 0
        assert rec['learned_for']['action_id'] == 2
        assert any('open notepad' in s for s in _steps(rec))

    def test_the_reader_returns_it_under_the_same_id(self, computer):
        env, run, _ = computer
        run('Open Notepad', 'open notepad', action=2)
        with env.app.app_context():
            loaded = env.cr.load_vlm_agent_files(PROMPT_ID, 0)
        assert [v['action_id'] for v in loaded] == [2], (
            'a learning CREATE just wrote must be accepted by the loader; a '
            'refused one would mean every CREATE-time learning is lost')
        assert any('open notepad' in s for s in _steps(loaded[0]))

    def test_each_action_gets_its_own_file(self, computer):
        env, run, _ = computer
        run('Open Notepad', 'open notepad', action=1)
        run('Save the weekly summary to disk', 'save summary', action=3)
        assert _vlm_files(env) == [f'{PROMPT_ID}_0_1{_SUFFIX}',
                                   f'{PROMPT_ID}_0_3{_SUFFIX}']
        assert any('open notepad' in s for s in _steps(_read(env, 1)))
        assert any('save summary' in s for s in _steps(_read(env, 3)))

    def test_a_file_already_there_is_overwritten_not_walked_past(
            self, computer):
        """The live shape: _1_ and _2_ already on disk, action 2 re-learns.
        The walker filed this as _3_ -- action 3's id."""
        env, run, _ = computer
        for aid in (1, 2):
            (env.prompts / f'{PROMPT_ID}_0_{aid}{_SUFFIX}').write_text(
                json.dumps({'action': 'an older chore %d' % aid,
                            'action_id': aid,
                            'recipe': [{'steps': 'old %d' % aid}]}),
                encoding='utf-8')
        run('Open Notepad', 'open notepad', action=2)
        assert _vlm_files(env) == [f'{PROMPT_ID}_0_1{_SUFFIX}',
                                   f'{PROMPT_ID}_0_2{_SUFFIX}'], (
            'a learning was filed under another action id')
        assert _steps(_read(env, 2)) != ['old 2']
        assert _read(env, 1)['recipe'] == [{'steps': 'old 1'}], (
            "action 1's file was touched by action 2's learning")


class TestOneExecutionKeepsAllItsCalls:

    def test_two_calls_in_one_action_keep_both_steps_in_order(
            self, computer):
        env, run, _ = computer
        run('Open Notepad', 'open notepad', action=2)
        run('Type the weekly summary into the editor', 'type summary',
            action=2)
        assert _vlm_files(env) == [f'{PROMPT_ID}_0_2{_SUFFIX}']
        steps = _steps(_read(env, 2))
        assert len(steps) == 2, (
            'the second call replaced the first: an action that opens an app '
            'and then types into it would replay only the typing. steps=%r'
            % (steps,))
        assert 'open notepad' in steps[0] and 'type summary' in steps[1]

    def test_a_later_execution_relearns_from_scratch(self, computer):
        env, run, _ = computer
        run('Open Notepad', 'open notepad', action=2)
        run('Launch the text editor application', 'start editor', action=2,
            new_run=True)
        steps = _steps(_read(env, 2))
        assert len(steps) == 1 and 'start editor' in steps[0], (
            'a new execution must re-learn the action, not pile its steps '
            'onto the previous run\'s: %r' % (steps,))


class TestOnlyAProvenLearningIsReplayed:

    def test_an_unproven_file_is_not_injected_as_steps(self, computer):
        """The direct read.  A legacy file at the running action's path whose
        text matches the instruction would have been injected as "steps from
        a previous successful execution" -- whatever job it really holds."""
        env, run, sent = computer
        (env.prompts / f'{PROMPT_ID}_0_2{_SUFFIX}').write_text(json.dumps({
            'action': 'Open Notepad', 'action_id': 2,
            'recipe': [{'steps': 'ls -la /data'}]}), encoding='utf-8')
        run('Open Notepad', 'open notepad', action=2)
        assert 'enhanced_instruction' not in sent[-1], sent[-1]

    def test_a_proven_file_is_still_injected(self, computer):
        """The feature keeps working: this action's own learning guides the
        next call with the same instruction."""
        env, run, sent = computer
        run('Open Notepad', 'open notepad', action=2)
        run('Open Notepad', 'open notepad again', action=2, new_run=True)
        assert 'open notepad' in sent[-1].get('enhanced_instruction', ''), (
            sent[-1])
