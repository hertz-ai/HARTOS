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
        out = _vlm_merged_actions(flow, [_vlm(1)], 'Executor')
        assert out[0]['can_perform_without_user_input'] == 'yes', (
            "the flow author said this action is autonomous; a VLM re-authoring "
            "must not silently make it need a human — that is the 99-round spin")
        assert out[0]['action'] == 'vlm step 1', "content must still be replaced"

    def test_a_genuinely_non_autonomous_flow_action_stays_non_autonomous(self):
        """Preserve the FLOW's value, not a hardcoded 'yes'."""
        flow = [{'action_id': 1, 'action': 'a', 'persona': 'Executor',
                 'can_perform_without_user_input': 'no'}]
        out = _vlm_merged_actions(flow, [_vlm(1)], 'Executor')
        assert out[0]['can_perform_without_user_input'] == 'no'

    def test_absent_on_the_flow_action_leaves_the_vlm_value(self):
        """No flow value to preserve -> do not invent one."""
        flow = [{'action_id': 1, 'action': 'a', 'persona': 'Executor'}]
        out = _vlm_merged_actions(flow, [_vlm(1)], 'Executor')
        assert out[0]['can_perform_without_user_input'] == 'no'

    def test_the_live_shape_keeps_all_24_autonomous(self):
        flow = [{'action_id': i, 'action': 'step %d' % i, 'persona': 'Executor',
                 'can_perform_without_user_input': 'yes'} for i in range(1, 25)]
        out = _vlm_merged_actions(flow, [_vlm(i) for i in range(2, 24)], 'Executor')
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
        out = _vlm_merged_actions(flow, vlm, 'Executor')
        assert len(out) == 24, "merge must not change the action count"
        kept = [a for a in out if a['persona'].lower() == 'executor']
        assert len(kept) == 24, (
            "every action must survive the role filter; got %d of 24 — this is "
            "the 2-of-24 defect" % len(kept))

    def test_the_vlm_body_still_wins(self):
        """Only the OWNER is preserved; the re-authored content must apply."""
        out = _vlm_merged_actions(_flow(3), [_vlm(2)], 'Executor')
        a2 = next(a for a in out if a['action_id'] == 2)
        assert a2['action'] == 'vlm step 2'
        assert a2['recipe'] == [{'steps': 'click'}]
        assert a2['persona'] == 'Executor'

    def test_untouched_actions_are_unchanged(self):
        flow = _flow(3)
        out = _vlm_merged_actions(flow, [_vlm(2)], 'Executor')
        for aid in (1, 3):
            assert next(a for a in out if a['action_id'] == aid)['action'] == 'step %d' % aid

    def test_a_new_action_takes_the_flow_persona(self):
        """An APPENDED action has no predecessor to inherit from."""
        out = _vlm_merged_actions(_flow(2), [_vlm(7)], 'Executor')
        assert len(out) == 3
        assert next(a for a in out if a['action_id'] == 7)['persona'] == 'Executor'

    def test_append_without_a_flow_persona_is_left_alone(self):
        """No fallback given -> today's behaviour, no invented owner."""
        out = _vlm_merged_actions(_flow(2), [_vlm(7)], None)
        assert next(a for a in out if a['action_id'] == 7)['persona'].startswith('user')

    def test_a_multi_persona_flow_keeps_each_actions_own_owner(self):
        """Must not flatten everything onto the session role."""
        flow = [{'action_id': 1, 'action': 'a', 'persona': 'Researcher'},
                {'action_id': 2, 'action': 'b', 'persona': 'Executor'}]
        out = _vlm_merged_actions(flow, [_vlm(1), _vlm(2)], 'Executor')
        assert [a['persona'] for a in out] == ['Researcher', 'Executor'], (
            "replacement inherits the REPLACED action's persona, not the "
            "session role — otherwise a VLM pass would silently reassign work")


class TestNeverRaisesOnTheLoadPath:
    """Runs while building the agent; a bad file must not kill the session."""

    @pytest.mark.parametrize("vlm", [None, [], "nonsense", [None], [{}]])
    def test_junk_vlm_input_returns_the_flow_unchanged(self, vlm):
        flow = _flow(3)
        out = _vlm_merged_actions(flow, vlm, 'Executor')
        assert [a['action_id'] for a in out] == [1, 2, 3]

    @pytest.mark.parametrize("existing", [None, [], "nonsense"])
    def test_junk_existing_input_does_not_raise(self, existing):
        _vlm_merged_actions(existing, [_vlm(1)], 'Executor')

    def test_flow_action_without_a_persona_key(self):
        flow = [{'action_id': 1, 'action': 'a'}]
        out = _vlm_merged_actions(flow, [_vlm(1)], 'Executor')
        assert out[0]['action'] == 'vlm step 1'

    def test_the_input_list_is_not_mutated(self):
        """Three call sites share `recipes[user_prompt]`; in-place edits there
        were how a reload could compound onto an already-merged list."""
        flow = _flow(2)
        before = [dict(a) for a in flow]
        _vlm_merged_actions(flow, [_vlm(1)], 'Executor')
        assert flow == before
