"""Guard: a reuse action's group chat TERMINATES on the StatusVerifier
'completed' verdict, and the w1 loop advances off that terminal verdict.

Live root cause 2026-09-05 (Auto Research reuse 18088688973, installed build):
the w1 GroupChatManager carried NO is_termination_msg, so it fell back to the
default (content == 'TERMINATE') and ran to max_round=10.  A completed action is
signalled by the StatusVerifier verdict {'status':'completed'} — which does not
contain 'TERMINATE'.  state_transition treats that verdict as action-terminal
(returns chat_instructor expecting a 'TERMINATE' that a human_input_mode='NEVER'
UserProxy does not reliably emit), so the group spun to the cap and
get_agent_response never regained control: 'GOT COMPLETED FOR ACTION' fired at
03:29:12 yet current_action_id stayed 1 and actions 2-6 never ran.

Fix (both guarded here):
  A. the three reuse managers pass is_termination_msg=_reuse_group_terminate,
     which returns True on _is_terminate_msg OR a 'completed' verdict, so
     initiate_chat returns the moment an action completes;
  B. the w1 robust-advance reads the TERMINAL message (messages[-1]) for a
     'completed' verdict and advances the KNOWN pipeline action — it must NOT
     re-introduce the action_id==current_action gate (the verdict shape is
     inconsistent: some verdicts omit action_id, and history accumulates under
     clear_history=False so a [-4:] tail scan matched a stale prior verdict).

AST/text guard (no live llama needed).
"""
import ast
import os
import unittest


SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos', 'reuse_recipe.py')


class ReuseCompletionTerminates(unittest.TestCase):
    def setUp(self):
        self.src = open(SRC, encoding='utf-8').read()
        self.tree = ast.parse(self.src)  # also proves the module still parses

    # --- Fix A: manager terminates on the completed verdict ---
    def test_terminator_defined_once(self):
        self.assertEqual(
            self.src.count('def _reuse_group_terminate(msg):'), 1,
            "the shared reuse group terminator must be defined exactly once "
            "(one closure, not a per-manager parallel copy)")

    def test_all_three_managers_wired(self):
        self.assertEqual(
            self.src.count('is_termination_msg=_reuse_group_terminate,'), 3,
            "all three reuse managers (main/timer/visual) must terminate on the "
            "completion verdict — else the group runs to max_round and the loop "
            "never regains control to advance")

    def test_terminator_recognises_completed_verdict(self):
        # The terminator must fall through _is_terminate_msg to a status check
        # parsed with the canonical retrieve_json.
        #
        # 2026-09-06: this assertion used to pin the literal
        # `== 'completed'`.  That exact-equality form was itself the next bug —
        # 'requires_breakdown' is equally action-terminal (the outer loop must
        # regain control to EXECUTE the decomposition) and could never match,
        # so 17 breakdown verdicts on agent 89555447799 produced 0 executions.
        # The contract is now membership in the canonical round-terminal set;
        # the behavioural coverage lives in
        # tests/unit/test_reuse_group_terminates_on_breakdown.py, which calls
        # the predicate for real rather than matching its source text.
        self.assertIn('_is_terminate_msg(msg)', self.src)
        self.assertIn('in VERDICT_ROUND_TERMINAL_STATUSES', self.src,
                      "the terminator must end the round on any canonical "
                      "round-terminal verdict, not just an equality test "
                      "against one hardcoded status")
        self.assertIn('retrieve_json((msg or {}).get', self.src,
                      "the verdict must be parsed with the canonical retrieve_json")

    # --- Fix B: advance off the terminal verdict, known action, no action_id gate ---
    def test_robust_advance_reads_terminal_message(self):
        self.assertIn('group_chat.messages[-1] if group_chat.messages else None', self.src,
                      "the robust-advance must key on the TERMINAL message (the "
                      "just-terminated action's verdict), not a [-4:] tail that "
                      "matches a stale prior-action verdict under clear_history=False")

    def test_robust_advance_does_not_gate_on_llm_action_id(self):
        self.assertNotIn("str(_rc_vj.get('action_id', '')) == str(_reuse_current_action)", self.src,
                         "the robust-advance must NOT require the LLM to echo "
                         "action_id — some 'completed' verdicts omit it; advance "
                         "the KNOWN pipeline action instead")

    def test_robust_advance_still_uses_canonical_advance_fn(self):
        # No parallel advance path.  The five advance-and-steer sites now go
        # through ONE helper, _advance_or_steer, which is the only caller of
        # _advance_reuse_action — so this site must reach the canonical
        # advance through that helper, not by re-inlining the block.
        self.assertIn('"reuse-w1-completed"', self.src,
                      "the robust completion-advance site must still exist")
        self.assertIn('_advance_or_steer(', self.src,
                      "advancement must go through the canonical helper")
        self.assertNotIn(
            '_advance_reuse_action(\n                        user_prompt, _reuse_current_action, "reuse-w1-completed"',
            self.src,
            "the inline copy at this site was collapsed into _advance_or_steer; "
            "re-inlining it would restore the five-way drift")


if __name__ == '__main__':
    unittest.main()
