"""A VLM re-authoring of an action must not change whose action it is.

MEASURED LIVE 2026-09-08 20:44, agent 89555447799 (24-action recipe), on the
installed build.  The agent reported success after executing 2 actions and
silently skipped 22 — and this is why:

    ON DISK   89555447799_0_recipe.json : 24 actions, persona 'Executor' x24
              89555447799.json          : personas ['Executor'], flows 1
              *_vlm_agent.json          : 22 files, persona =
                                          'usercf125371-...'  x19
                                          'userw80_89555447799' x3

    IN MEMORY (live log, "this is action persona:" x24):
              usercf125371-...      19
              userw80_89555447799    3
              Executor               2   <- the only ones the role filter keeps

`load_vlm_agent_files` returns each VLM file whole, and three verbatim copies
of the merge block replaced the flow action with it wholesale:

    recipes[user_prompt]['actions'][i] = vlm_action

so 22 of 24 actions lost persona 'Executor'.  The role filter that follows
(reuse_recipe.py:1057) keeps only `persona.lower() == role.lower()`, leaving
role_actions = 2.  `Action(role_actions)` then defines the ledger's entire
world, and the run ends with the honest-looking

    [REUSE-LEDGER] ... tasks=33 actions=2
    [REUSE] All 2 actions completed

The `if len(role_actions) == 0: role_actions = recipes[...]['actions']`
fallback at :1065 cannot rescue this: 2 is not 0.  A PARTIAL match is worse
than no match — it looks like a successful narrow instead of a failed one.

`persona` has two producers speaking different languages: the flow recipe
writes a ROLE name, the VLM writer writes a USER id.  Reconciling at LOAD
(rather than rewriting 22 files) follows the precedent `_normalize_flow_recipe`
set in this same module: "recipes ALREADY on disk reuse correctly without a
rewrite".

    python -m pytest tests/unit/test_vlm_merge_preserves_persona.py \
        --noconftest -q
"""
import pytest

from hartos.reuse_recipe import _vlm_merged_actions


def _flow(n, persona='Executor'):
    return [{'action_id': i, 'action': 'step %d' % i, 'persona': persona}
            for i in range(1, n + 1)]


def _vlm(action_id, persona='usercf125371-5b6a-4e00-beae-f42513cf47ab'):
    return {'action_id': action_id, 'action': 'vlm step %d' % action_id,
            'persona': persona, 'recipe': [{'steps': 'click'}],
            'can_perform_without_user_input': 'no'}


class TestAutonomyIsAlsoAContractField:
    """The SECOND constant the same merge clobbers.

    MEASURED 2026-09-08 21:34-21:35, agent 89555447799, on the build that
    already carried the persona fix.  Action 2 was finally in the ledger, and
    the turn then burned 99 rounds on it making NO progress:

        rounds (inside reuse while1)      99
        LLM calls in the same window       7
        GOT can_perform_without_user_input 0   <- the steer never fired
        every other branch                 0
        [REUSE-ROUNDS] while1 exhausted 100 rounds at action 2/24

    `_reuse_action_is_autonomous` returned False, so the "complete this task
    independently" steering never reached the group; with no steer, no LLM
    call and nothing appended, each round re-read an identical last_message
    and fell off the end of the loop body.

    WHY IT WAS False: the flow recipe marks all 24 actions 'yes'; the VLM
    override for action 2 says 'no'.  And that 'no' is not a judgement —
    it is a constant:

        this agent's 22 vlm files : {'no': 22}
        ALL vlm files on the box   : {'no': 47}   (47 of 47, zero variance)

    A field with no variance carries no information.  Same shape as persona:
    a VLM file re-authors an action's STEPS, it does not renegotiate the
    action's contract with the user.  So the replaced action's contract
    fields survive; only its content is replaced.
    """

    def test_autonomy_survives_a_vlm_override(self):
        flow = [{'action_id': 1, 'action': 'a', 'persona': 'Executor',
                 'can_perform_without_user_input': 'yes'}]
        out = _vlm_merged_actions(flow, [_vlm(1)])
        assert out[0]['can_perform_without_user_input'] == 'yes', (
            "the flow author said this action is autonomous; a VLM re-authoring "
            "must not silently make it need a human — that is the 99-round spin")
        assert out[0]['recipe'] == [{'steps': 'click'}], (
            "content (steps) must still be replaced")
        assert out[0]['action'] == 'a', (
            "the action text is the GOAL, a contract field (f8bfbcc04)")

    def test_a_genuinely_non_autonomous_flow_action_stays_non_autonomous(self):
        """Preserve the FLOW's value, not a hardcoded 'yes'."""
        flow = [{'action_id': 1, 'action': 'a', 'persona': 'Executor',
                 'can_perform_without_user_input': 'no'}]
        out = _vlm_merged_actions(flow, [_vlm(1)])
        assert out[0]['can_perform_without_user_input'] == 'no'

    def test_absent_on_the_flow_action_leaves_the_vlm_value(self):
        """No flow value to preserve -> do not invent one."""
        flow = [{'action_id': 1, 'action': 'a', 'persona': 'Executor'}]
        out = _vlm_merged_actions(flow, [_vlm(1)])
        assert out[0]['can_perform_without_user_input'] == 'no'

    def test_the_live_shape_keeps_all_24_autonomous(self):
        flow = [{'action_id': i, 'action': 'step %d' % i, 'persona': 'Executor',
                 'can_perform_without_user_input': 'yes'} for i in range(1, 25)]
        out = _vlm_merged_actions(flow, [_vlm(i) for i in range(2, 24)])
        auto = [a for a in out
                if str(a.get('can_perform_without_user_input')).lower() == 'yes']
        assert len(auto) == 24, (
            "22 VLM overrides must not strip autonomy from 22 of 24 actions; "
            "got %d autonomous" % len(auto))


class TestReplacementKeepsTheOwner:

    def test_the_live_shape_keeps_all_24_under_one_persona(self):
        """The exact measured case: 24 flow actions, 22 VLM overrides."""
        flow = _flow(24)
        vlm = [_vlm(i) for i in range(1, 23)]
        out = _vlm_merged_actions(flow, vlm)
        assert len(out) == 24, "merge must not change the action count"
        kept = [a for a in out if a['persona'].lower() == 'executor']
        assert len(kept) == 24, (
            "every action must survive the role filter; got %d of 24 — this is "
            "the 2-of-24 defect" % len(kept))

    def test_the_vlm_body_still_wins(self):
        """Only the contract (owner, goal) is preserved; the re-authored
        STEPS must apply.  Since f8bfbcc04 the action text is the GOAL and
        is kept: a re-learning may change how, never what."""
        out = _vlm_merged_actions(_flow(3), [_vlm(2)])
        a2 = next(a for a in out if a['action_id'] == 2)
        assert a2['action'] == 'step 2'
        assert a2['recipe'] == [{'steps': 'click'}]
        assert a2['persona'] == 'Executor'

    def test_untouched_actions_are_unchanged(self):
        flow = _flow(3)
        out = _vlm_merged_actions(flow, [_vlm(2)])
        for aid in (1, 3):
            assert next(a for a in out if a['action_id'] == aid)['action'] == 'step %d' % aid

    def test_a_multi_persona_flow_keeps_each_actions_own_owner(self):
        """Must not flatten everything onto the session role."""
        flow = [{'action_id': 1, 'action': 'a', 'persona': 'Researcher'},
                {'action_id': 2, 'action': 'b', 'persona': 'Executor'}]
        out = _vlm_merged_actions(flow, [_vlm(1), _vlm(2)])
        assert [a['persona'] for a in out] == ['Researcher', 'Executor'], (
            "replacement inherits the REPLACED action's persona, not the "
            "session role — otherwise a VLM pass would silently reassign work")


class TestAReLearningNeverCreatesAnAction:
    """A VLM file whose id names no flow action is an ORPHAN; it is dropped.

    MEASURED 2026-09-25 21:55:33 (frozen_debug.log.old), agent 18088688973
    ("Auto Research", 6-action flow recipe, ids 1..6):

        [VLM-MERGE] 9 override(s); actions 6 -> 9
        [REUSE-LEDGER] ... tasks=30 actions=9
        Got error as :[Errno 2] ... 18088688973_0_7.json   (and _8, _9)

    On disk, 18088688973_0_{7,8,9}_vlm_agent.json hold "Create a new directory
    called autonomous_research_data in the C drive" and "C:\\temp\\autonomous_
    research", personas userc23d388c / usered8ca05a: other runs' computer
    chores, filed past the end of the flow by a next-free-slot walker.  The
    merge APPENDED them, so the research agent replayed 9 actions, 3 of them
    OS-mutating jobs its owner never authored for it.
    """

    def _live(self):
        flow = [{'action_id': i, 'action': 'research step %d' % i,
                 'persona': 'Executor', 'can_perform_without_user_input': 'yes'}
                for i in range(1, 7)]
        vlm = [_vlm(i) for i in range(1, 10)]
        for v, text in zip(vlm[6:], (
                'Create a new directory called "autonomous_research_data" in the C drive',
                'Create a new directory called "autonomous_research_data" in the C drive with full permissions',
                'Create a new directory at C:\\temp\\autonomous_research')):
            v['action'] = text
        return flow, vlm

    def test_the_live_shape_keeps_its_six_actions(self):
        flow, vlm = self._live()
        out = _vlm_merged_actions(flow, vlm)
        assert [a['action_id'] for a in out] == [1, 2, 3, 4, 5, 6], (
            "a re-learning refines an action, it never creates one: the 6-action "
            "flow grew to %d" % len(out))

    def test_no_foreign_task_text_reaches_the_recipe(self):
        flow, vlm = self._live()
        out = _vlm_merged_actions(flow, vlm)
        assert not [a for a in out if 'autonomous_research' in a['action']], (
            "an orphan file's OS chore must not become one of this agent's actions")

    def test_in_range_relearnings_still_apply(self):
        """Dropping orphans must not drop the real re-learnings with them."""
        flow, vlm = self._live()
        out = _vlm_merged_actions(flow, vlm)
        assert all(a['recipe'] == [{'steps': 'click'}] for a in out)
        assert [a['action'] for a in out] == [
            'research step %d' % i for i in range(1, 7)]

    def test_an_orphan_alone_leaves_the_flow_unchanged(self):
        flow = _flow(2)
        out = _vlm_merged_actions(flow, [_vlm(7)])
        assert out == flow

    def test_the_real_reader_and_merge_on_files_like_the_live_ones(
            self, tmp_path, monkeypatch):
        """End to end through helper.load_vlm_agent_files: the filename id is
        what the reader reports, so files _7.._9 beside a 6-action flow must
        still yield 6 actions.  Banked through the one writer, so each file
        carries the provenance the loader requires (files without it are
        refused outright: test_vlm_learning_must_prove_its_action.py)."""
        import logging
        import types
        import hartos.helper as helper
        monkeypatch.setattr(helper, 'PROMPTS_DIR', str(tmp_path))
        monkeypatch.setattr(helper, 'current_app', types.SimpleNamespace(
            logger=logging.getLogger('test_vlm_orphans')))
        flow, vlm = self._live()
        for v in vlm:
            helper.bank_vlm_learning('18088688973', 0, v['action_id'], 'run', v)
        loaded = helper.load_vlm_agent_files('18088688973', 0)
        assert sorted(v['action_id'] for v in loaded) == list(range(1, 10))
        out = _vlm_merged_actions(flow, loaded)
        assert sorted(a['action_id'] for a in out) == [1, 2, 3, 4, 5, 6]

    def test_an_orphan_is_logged_by_its_id(self, caplog):
        """Countable on the next drive: which file was ignored, and why."""
        import logging
        with caplog.at_level(logging.WARNING):
            _vlm_merged_actions(_flow(2), [_vlm(7)])
        assert any('VLM-ORPHAN' in r.getMessage() and '7' in r.getMessage()
                   for r in caplog.records), [r.getMessage() for r in caplog.records]


class TestNeverRaisesOnTheLoadPath:
    """Runs while building the agent; a bad file must not kill the session."""

    @pytest.mark.parametrize("vlm", [None, [], "nonsense", [None], [{}]])
    def test_junk_vlm_input_returns_the_flow_unchanged(self, vlm):
        flow = _flow(3)
        out = _vlm_merged_actions(flow, vlm)
        assert [a['action_id'] for a in out] == [1, 2, 3]

    @pytest.mark.parametrize("existing", [None, [], "nonsense"])
    def test_junk_existing_input_does_not_raise(self, existing):
        _vlm_merged_actions(existing, [_vlm(1)])

    def test_flow_action_without_a_persona_key(self):
        flow = [{'action_id': 1, 'action': 'a'}]
        out = _vlm_merged_actions(flow, [_vlm(1)])
        assert out[0]['recipe'] == [{'steps': 'click'}]
        assert out[0]['action'] == 'a'

    def test_the_input_list_is_not_mutated(self):
        """Three call sites share `recipes[user_prompt]`; in-place edits there
        were how a reload could compound onto an already-merged list."""
        flow = _flow(2)
        before = [dict(a) for a in flow]
        _vlm_merged_actions(flow, [_vlm(1)])
        assert flow == before
