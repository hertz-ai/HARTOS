"""A VLM learning replaces an action's steps only if it proves it is that
action's learning.

MEASURED 2026-09-28 against the live prompts dir (read-only, through the real
loader and merge): 96 in-range ``*_vlm_agent.json`` files, every one of them
REPLACES its flow action's steps, and 77 of the 96 score below
``_RELEARN_IDENTITY_THRESHOLD`` against the action they replace.  Agent
18088688973 (a research agent):

    action 3  'Create a fallback strategy for Action 3 ...'   -> 'ls -la /data'
    action 6  "save_to_long_term_memory key 'auto_research_..." ->
              'Create a temporary directory at C:\\Users\\testuser\\...'
    action 4  'Autonomous Deep Research Technology 2024'      -> steps that
              click 'Allow' on a Windows Security network-access dialog

So a REUSE replay of that agent runs OS jobs its owner never authored for it.
3d008fc9e stopped files PAST the end of a flow from adding actions; these sit
INSIDE the id range, where the merge replaces.

WHY SIMILARITY IS NOT THE LOAD-TIME RULE: the 0.45 identity threshold was
tuned for the WRITER, where the instruction comes from the running action.
Against the files on disk it lets through 0.5 (the 'Allow' dialog above),
0.556 and 0.625 (a LinkedIn action re-learned as Twitter and back).  The
walker that wrote these files is proven to have misfiled (23 orphans are its
signature), and nothing in a legacy file says which action it ran under.

THE RULE: a learning carries ``learned_for`` = {prompt_id, flow, action_id},
stamped by the one writer (``helper.bank_vlm_learning``) from the action that
was running, and the loader accepts a file only when that stamp names the
file's own coordinates.  A file without it is refused, logged once, and
quarantined once (moved aside, never deleted).  Refusing costs nothing the
action needs: the action keeps its CREATE-authored steps.

    python -m pytest tests/unit/test_vlm_learning_must_prove_its_action.py -q -p no:cacheprovider
"""
import json
import logging
import os
import types

import pytest

import hartos.helper as helper
from hartos.reuse_recipe import _vlm_merged_actions

PID = '18088688973'

# The live flow's action texts (18088688973_0_recipe.json), verbatim.
_FLOW_TEXT = {
    1: 'Generate detailed recipe with necessary steps',
    2: 'Search for latest advancements in autonomous deep research technology',
    3: 'Create a fallback strategy for Action 3: Define a recovery protocol',
    4: 'Autonomous Deep Research Technology 2024',
    5: 'Define fallback strategy for autonomous research actions in case of failure',
    6: "save_to_long_term_memory key 'auto_research_findings' value: JSON object",
}
# What the legacy files at those ids hold, verbatim.
_LEGACY = {
    3: ('ls -la /data', 'ls -la /data'),
    4: ('Execute the autonomous deep research technology 2024 task from the recipe',
        "left_click - A Windows Security dialog has appeared blocking the "
        "application from accessing public/private networks. ... clicking "
        "the 'Allow' button."),
    6: ('Create a temporary directory at C:\\Users\\testuser\\AppData\\Local\\Temp '
        'and list its contents',
        'Create a temporary directory at C:\\Users\\testuser\\AppData\\Local\\Temp'),
}


def _flow():
    return [{'action_id': i, 'action': t, 'persona': 'Executor',
             'can_perform_without_user_input': 'yes',
             'recipe': [{'steps': 'authored step %d' % i}]}
            for i, t in sorted(_FLOW_TEXT.items())]


@pytest.fixture
def prompts(tmp_path, monkeypatch):
    monkeypatch.setattr(helper, 'PROMPTS_DIR', str(tmp_path))
    monkeypatch.setattr(helper, 'current_app', types.SimpleNamespace(
        logger=logging.getLogger('test_vlm_provenance')))
    return tmp_path


def _write_legacy(dirpath, aid, action, steps, pid=PID):
    p = dirpath / f'{pid}_0_{aid}_vlm_agent.json'
    p.write_text(json.dumps({
        'status': 'done', 'action': action, 'action_id': aid,
        'persona': 'user6c2dc0fc', 'recipe': [{'steps': steps}],
        'can_perform_without_user_input': 'no'}), encoding='utf-8')
    return p


def _learning(action, *steps):
    return {'status': 'done', 'action': action, 'persona': 'user1',
            'recipe': [{'steps': s, 'tool_name': 'execute_windows_or_android_command'}
                       for s in steps],
            'can_perform_without_user_input': 'no'}


class TestAnUnprovenFileReplacesNothing:

    def test_the_live_foreign_jobs_do_not_reach_the_flow(self, prompts):
        for aid, (action, steps) in _LEGACY.items():
            _write_legacy(prompts, aid, action, steps)
        loaded = helper.load_vlm_agent_files(PID, 0)
        assert loaded == [], (
            'a file that does not say which action it was learned for was '
            'handed to the merge: %r' % [v.get('action') for v in loaded])
        out = _vlm_merged_actions(_flow(), loaded)
        assert out == _flow(), (
            "an unrelated OS job replaced a real action's steps")

    def test_a_file_learned_for_this_action_still_applies(self, prompts):
        helper.bank_vlm_learning(PID, 0, 3, 'run-a',
                                 _learning('Write the recovery protocol',
                                           'open editor', 'save protocol'))
        loaded = helper.load_vlm_agent_files(PID, 0)
        assert [v['action_id'] for v in loaded] == [3]
        out = _vlm_merged_actions(_flow(), loaded)
        a3 = next(a for a in out if a['action_id'] == 3)
        assert [s['steps'] for s in a3['recipe']] == ['open editor', 'save protocol']
        assert a3['action'] == _FLOW_TEXT[3]          # the goal is kept
        assert [a['recipe'] for a in out if a['action_id'] != 3] == [
            a['recipe'] for a in _flow() if a['action_id'] != 3]

    def test_a_file_moved_under_another_actions_id_is_refused(self, prompts):
        """The walker's signature: the content says action 2, the filename
        says 3.  The filename is what the merge would apply it to."""
        path = helper.bank_vlm_learning(PID, 0, 2, 'run-a',
                                        _learning('Search the web', 'search'))
        os.replace(path, str(prompts / f'{PID}_0_3_vlm_agent.json'))
        assert helper.load_vlm_agent_files(PID, 0) == []

    @pytest.mark.parametrize('stamp', [
        {'prompt_id': '99999999999', 'flow': 0, 'action_id': 3},   # other agent
        {'prompt_id': PID, 'flow': 1, 'action_id': 3},             # other flow
        {'prompt_id': PID, 'flow': 0},                             # no action id
        {'prompt_id': PID, 'flow': 'x', 'action_id': 3},           # junk
        'not a dict',
    ])
    def test_a_stamp_for_other_coordinates_is_refused(self, prompts, stamp):
        rec = dict(_learning('x', 'y'), action_id=3, learned_for=stamp)
        (prompts / f'{PID}_0_3_vlm_agent.json').write_text(
            json.dumps(rec), encoding='utf-8')
        assert helper.load_vlm_agent_files(PID, 0) == []

    def test_the_direct_read_applies_the_same_rule(self, prompts):
        """Both tools read '<agent>_<flow>_<action>_vlm_agent.json' directly to
        inject 'steps from a previous successful execution'."""
        _write_legacy(prompts, 3, *_LEGACY[3])
        assert helper.read_vlm_learning(PID, 0, 3) is None
        helper.bank_vlm_learning(PID, 0, 3, 'r', _learning('ls', 'list'))
        assert helper.read_vlm_learning(PID, 0, 3)['recipe'][0]['steps'] == 'list'

    @pytest.mark.parametrize('record', [None, [], 'x', 3, {}, {'learned_for': None}])
    def test_the_rule_never_raises(self, record):
        assert helper.vlm_learning_refusal(record, PID, 0, 3)


class TestOneLearningPerExecution:

    def test_calls_in_one_execution_append_in_order(self, prompts):
        helper.bank_vlm_learning(PID, 0, 2, 'run-a', _learning('open', 'open app'))
        helper.bank_vlm_learning(PID, 0, 2, 'run-a', _learning('type', 'type text'))
        rec = helper.read_vlm_learning(PID, 0, 2)
        assert [s['steps'] for s in rec['recipe']] == ['open app', 'type text']
        assert rec['action'] == 'type'

    def test_a_new_execution_replaces_the_old_one(self, prompts):
        helper.bank_vlm_learning(PID, 0, 2, 'run-a', _learning('open', 'open app'))
        helper.bank_vlm_learning(PID, 0, 2, 'run-b', _learning('launch', 'launch'))
        rec = helper.read_vlm_learning(PID, 0, 2)
        assert [s['steps'] for s in rec['recipe']] == ['launch']

    def test_no_execution_id_never_appends(self, prompts):
        """An unknown execution cannot claim the earlier steps as its own."""
        helper.bank_vlm_learning(PID, 0, 2, None, _learning('open', 'open app'))
        helper.bank_vlm_learning(PID, 0, 2, None, _learning('type', 'type text'))
        rec = helper.read_vlm_learning(PID, 0, 2)
        assert [s['steps'] for s in rec['recipe']] == ['type text']

    def test_a_legacy_file_at_the_path_is_replaced_not_extended(self, prompts):
        _write_legacy(prompts, 2, 'ls -la /data', 'ls -la /data')
        helper.bank_vlm_learning(PID, 0, 2, 'run-a', _learning('open', 'open app'))
        rec = helper.read_vlm_learning(PID, 0, 2)
        assert [s['steps'] for s in rec['recipe']] == ['open app']

    def test_it_writes_exactly_the_actions_path(self, prompts):
        path = helper.bank_vlm_learning(PID, 0, 5, 'r', _learning('a', 'b'))
        assert os.path.basename(path) == f'{PID}_0_5_vlm_agent.json'
        assert sorted(os.listdir(prompts)) == [f'{PID}_0_5_vlm_agent.json']
        assert json.loads(open(path, encoding='utf-8').read())['action_id'] == 5

    def test_every_execution_gets_its_own_id(self):
        assert helper.Action([]).run_id != helper.Action([]).run_id


class TestLoggedOncePerFile:

    def test_a_refused_file_is_logged_once_per_process(self, prompts, caplog):
        _write_legacy(prompts, 3, *_LEGACY[3])
        _write_legacy(prompts, 6, *_LEGACY[6])
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                helper.load_vlm_agent_files(PID, 0)
        refused = [r.getMessage() for r in caplog.records
                   if 'VLM-UNPROVEN' in r.getMessage()]
        assert len(refused) == 2, refused
        assert any('_3_vlm_agent' in m for m in refused)
        assert any('_6_vlm_agent' in m for m in refused)

    def test_an_orphan_is_logged_once_per_process(self, caplog):
        orphan = dict(_learning('an orphan only this test writes', 's'),
                      action_id=9,
                      learned_for={'prompt_id': 'orphan-once', 'flow': 0,
                                   'action_id': 9})
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                _vlm_merged_actions(_flow(), [dict(orphan)])
        hits = [r for r in caplog.records if 'VLM-ORPHAN' in r.getMessage()]
        assert len(hits) == 1, [r.getMessage() for r in hits]


class TestQuarantineOnce:

    def test_unproven_files_are_moved_aside_never_deleted(self, prompts):
        legacy = {aid: _write_legacy(prompts, aid, *_LEGACY[aid]).read_bytes()
                  for aid in _LEGACY}
        proven = helper.bank_vlm_learning(PID, 0, 2, 'r', _learning('a', 'b'))
        (prompts / f'{PID}_0_recipe.json').write_text('{}', encoding='utf-8')

        assert helper.quarantine_unproven_vlm_learnings_once(str(prompts)) == 'moved'

        qdir = prompts / helper.VLM_QUARANTINE_DIRNAME
        for aid, raw in legacy.items():
            name = f'{PID}_0_{aid}_vlm_agent.json'
            assert not (prompts / name).exists(), name
            assert (qdir / name).read_bytes() == raw, (
                'the quarantined copy must be the file, byte for byte')
        assert os.path.exists(proven), 'a proven learning was quarantined'
        assert (prompts / f'{PID}_0_recipe.json').exists()
        assert helper.load_vlm_agent_files(PID, 0)[0]['action_id'] == 2

    def test_it_runs_once(self, prompts):
        _write_legacy(prompts, 3, *_LEGACY[3])
        assert helper.quarantine_unproven_vlm_learnings_once(str(prompts)) == 'moved'
        late = _write_legacy(prompts, 6, *_LEGACY[6])
        assert helper.quarantine_unproven_vlm_learnings_once(str(prompts)) == 'done'
        assert late.exists(), 'a second pass ran after the marker was written'

    def test_the_marker_records_what_moved_and_why(self, prompts):
        _write_legacy(prompts, 3, *_LEGACY[3])
        helper.quarantine_unproven_vlm_learnings_once(str(prompts))
        marker = json.loads((prompts / helper.VLM_QUARANTINE_DIRNAME
                             / helper.VLM_QUARANTINE_MARKER).read_text(encoding='utf-8'))
        assert list(marker['moved']) == [f'{PID}_0_3_vlm_agent.json']
        assert 'learned_for' in marker['moved'][f'{PID}_0_3_vlm_agent.json']

    def test_nothing_to_move_still_marks_it_done(self, prompts):
        assert helper.quarantine_unproven_vlm_learnings_once(str(prompts)) == 'none'
        assert helper.quarantine_unproven_vlm_learnings_once(str(prompts)) == 'done'

    def test_a_failed_move_is_not_marked_so_the_next_boot_retries(
            self, prompts, monkeypatch):
        _write_legacy(prompts, 3, *_LEGACY[3])
        real_replace = os.replace

        def _locked(src, dst):
            raise PermissionError('file in use')
        monkeypatch.setattr(helper.os, 'replace', _locked)
        assert helper.quarantine_unproven_vlm_learnings_once(str(prompts)) == 'partial'
        monkeypatch.setattr(helper.os, 'replace', real_replace)
        assert helper.quarantine_unproven_vlm_learnings_once(str(prompts)) == 'moved'

    def test_the_marker_is_not_a_prompts_json(self, prompts):
        """Several listings glob <prompts>/*.json as agents or prompts; the
        marker and the moved files live in a subdirectory."""
        _write_legacy(prompts, 3, *_LEGACY[3])
        helper.quarantine_unproven_vlm_learnings_once(str(prompts))
        assert [p for p in os.listdir(prompts) if p.endswith('.json')] == []

    def test_a_missing_dir_does_not_raise(self, tmp_path):
        assert helper.quarantine_unproven_vlm_learnings_once(
            str(tmp_path / 'absent')) == 'none'
