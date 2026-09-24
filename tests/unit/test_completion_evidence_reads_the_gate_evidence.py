"""Guard: the receipt the completion commit needs is found wherever the
fabrication gate found the tool run.

Measured on the MSI desktop, nightly 8a11925, 2026-09-24 01:33 -> 07:00:
  [REUSE-VERIFY] ... no canonical receipt in its dispatch window  362
  [FABRICATED-COMPLETE] refusals                                   80
  "[REUSE] Action N TERMINATED, advancing"                          0
and on 01:44:41 the gate itself logged, for session
c23d388c-..._88555124130:
  [FAB-GUARD] action 1 names tool(s) ['execute_windows_or_android_command'];
      executed=['execute_windows_or_android_command', ...]; unrun=[]
immediately followed by REUSE-VERIFY GAVE_UP for the same action.  So the tool
really ran by the gate's rule, and the completion still could not commit.

The two readers disagree about WHERE evidence lives.  _reuse_fabricated_tools
reads _reuse_evidence_msg_lists (the group log plus every agent's pairwise
_oai_messages buffer -- its docstring records a live tool whose result lived
only in a buffer).  _reuse_completion_evidence reads group_chat.messages only.
A tool result that exists only in a buffer therefore passes the gate and has
no receipt, and every daemon action ends GAVE_UP.  With zero committed
completions the world-model bridge receives zero verified samples
(/api/world-model/health: total_unverified_skipped=823, total_recorded=0).

This test pins the divergence on the real functions.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from hartos import reuse_recipe  # noqa: E402


class _FakeTask:
    def __init__(self, action_text):
        self._text = action_text
        self.evidence_seen_call_ids = set()

    def get_action(self, idx):
        return self._text


class _FakeAgent:
    def __init__(self, name, tools, oai_messages=None):
        self.name = name
        self._function_map = {t: (lambda: None) for t in tools}
        self.llm_config = {'tools': [{'function': {'name': t}} for t in tools]}
        self._oai_messages = oai_messages or {}


class _FakeGroupChat:
    def __init__(self, messages, agents):
        self.messages = messages
        self.agents = agents


_UP = 'evidence_user_1'
_TOOL = 'execute_windows_or_android_command'
_ACTION = f'Action #1: Use {_TOOL} to open the settings window.'
_DISPATCH = {'role': 'user', 'name': 'ChatInstructor',
             'content': f'Perform this action -> Action #1: {_ACTION}'}
_PROPOSAL = {'role': 'assistant', 'name': 'Helper', 'content': None,
             'tool_calls': [{'id': 'call_live_1', 'type': 'function',
                             'function': {'name': _TOOL, 'arguments': '{}'}}]}
_RESULT = {'role': 'tool', 'name': 'Assistant', 'content': 'Settings opened.',
           'tool_responses': [{'tool_call_id': 'call_live_1', 'role': 'tool',
                               'content': 'Settings opened.'}]}
_VERDICT = {'role': 'user', 'name': 'StatusVerifier',
            'content': '{"status": "completed", "action_id": 1}'}


class CompletionEvidenceReadsGateEvidence(unittest.TestCase):
    def setUp(self):
        self._saved = reuse_recipe.user_tasks.get(_UP)
        reuse_recipe.user_tasks[_UP] = _FakeTask(_ACTION)

    def tearDown(self):
        if self._saved is None:
            reuse_recipe.user_tasks.pop(_UP, None)
        else:
            reuse_recipe.user_tasks[_UP] = self._saved

    def _chat(self, group_log, buffer):
        helper = _FakeAgent('Helper', [_TOOL], {'Assistant': buffer})
        return _FakeGroupChat(group_log, [helper, _FakeAgent('Assistant', [])])

    def test_result_in_group_log_is_a_receipt(self):
        """Control: the shape the finder already handles must keep working."""
        gc = self._chat([_DISPATCH, _PROPOSAL, _RESULT, _VERDICT], [])
        self.assertEqual(
            reuse_recipe._reuse_fabricated_tools(_UP, 1, gc, gc.agents), [])
        self.assertIsNotNone(
            reuse_recipe._reuse_completion_evidence(_UP, 1, gc))

    def test_result_only_in_agent_buffer_gate_and_receipt_agree(self):
        """The live shape: the gate credits the run from the buffer, so the
        completion must be able to cite it too."""
        gc = self._chat([_DISPATCH, _VERDICT], [_DISPATCH, _PROPOSAL, _RESULT])
        self.assertEqual(
            reuse_recipe._reuse_fabricated_tools(_UP, 1, gc, gc.agents), [],
            "precondition: the gate treats the buffered run as executed")
        self.assertIsNotNone(
            reuse_recipe._reuse_completion_evidence(_UP, 1, gc),
            "the gate passed this action on a real tool result, but the "
            "completion found no receipt -> the action ends GAVE_UP and never "
            "reaches commit_verified_action_completion")

    def test_group_log_receipt_envelope_is_unchanged(self):
        """A group-log receipt keeps the exact envelope CREATE also emits."""
        gc = self._chat([_DISPATCH, _PROPOSAL, _RESULT, _VERDICT], [])
        self.assertEqual(reuse_recipe._reuse_completion_evidence(_UP, 1, gc),
                         {'message_index': 2, 'kind': 'tool_receipt'})

    def test_buffer_receipt_carries_its_address(self):
        gc = self._chat([_DISPATCH, _VERDICT], [_DISPATCH, _PROPOSAL, _RESULT])
        self.assertEqual(
            reuse_recipe._reuse_completion_evidence(_UP, 1, gc),
            {'source': 'buffer', 'agent': 'Helper', 'peer': 'Assistant',
             'message_index': 2, 'kind': 'tool_receipt'})

    def test_stale_buffer_result_from_an_earlier_action_is_not_a_receipt(self):
        """A buffer also carries earlier actions' turns.  Action 1's result,
        sitting before action 2's dispatch, must not complete action 2."""
        reuse_recipe.user_tasks[_UP] = _FakeTask(
            f'Action #2: Use {_TOOL} to close the settings window.')
        dispatch2 = {'role': 'user', 'name': 'ChatInstructor',
                     'content': 'Perform this action -> Action #2: close it'}
        gc = self._chat([_DISPATCH, dispatch2],
                        [_DISPATCH, _PROPOSAL, _RESULT, dispatch2])
        self.assertIsNone(reuse_recipe._reuse_completion_evidence(_UP, 2, gc))


class _FakeLedger:
    def __init__(self):
        self.tasks = {'action_1': type('T', (), {
            'context': {}, 'description': 'open settings'})()}
        self.saves = 0

    def save(self):
        self.saves += 1
        return True


class ReceiptIsReadFromWhereItWasFound(unittest.TestCase):
    """lifecycle_hooks reads the receipt back through resolve_receipt only."""

    def setUp(self):
        from hartos import lifecycle_hooks as lh
        self.lh = lh
        self.helper = _FakeAgent('Helper', [_TOOL],
                                 {'Assistant': [_DISPATCH, _PROPOSAL, _RESULT]})
        self.gc = _FakeGroupChat([_DISPATCH, _VERDICT],
                                 [self.helper, _FakeAgent('Assistant', [])])
        self.ledger = _FakeLedger()
        self.states = []
        self.credited = []
        self.bridge_calls = []
        self._patches = []

        def patch(obj, name, value):
            self._patches.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        patch(lh, 'get_registered_groupchat', lambda _up: self.gc)
        patch(lh, 'get_registered_ledger', lambda _up: self.ledger)
        patch(lh, 'get_action_state',
              lambda _up, _a: lh.ActionState.STATUS_VERIFICATION_REQUESTED)
        patch(lh, 'validate_state_transition', lambda *_a: True)
        patch(lh, 'safe_set_state',
              lambda _up, a, state, reason, **kw: self.states.append(
                  (a, state, kw.get('result'))) or True)
        import integrations.agent_lightning as al
        patch(al, 'record_verified_outcome_for_agents',
              lambda agents, ok, ctx: self.credited.append(list(agents)) or True)
        import integrations.agent_engine.world_model_bridge as wmb
        bridge = type('B', (), {'record_interaction': lambda _s, **kw:
                                self.bridge_calls.append(kw)})()
        patch(wmb, 'get_world_model_bridge', lambda: bridge)

    def tearDown(self):
        for obj, name, value in reversed(self._patches):
            setattr(obj, name, value)

    def _buffer_evidence(self, **over):
        ev = {'source': 'buffer', 'agent': 'Helper', 'peer': 'Assistant',
              'message_index': 2, 'kind': 'tool_receipt'}
        ev.update(over)
        return ev

    def _accepts(self, evidence, action_id=1):
        return self.lh._verifier_completion_has_conversation_evidence(
            _UP, action_id, {'evidence': evidence})

    def test_buffer_only_receipt_reaches_committed(self):
        ok = self.lh.commit_verified_action_completion(
            _UP, 1, self._buffer_evidence())
        self.assertTrue(ok)
        self.assertEqual(self.states, [
            (1, self.lh.ActionState.COMPLETED, 'Settings opened.')])
        self.assertEqual(self.ledger.saves, 1)
        self.assertEqual(self.bridge_calls[0]['response'], 'Settings opened.')

    def test_agent_lightning_credits_the_buffer_participant_only(self):
        self.lh.commit_verified_action_completion(
            _UP, 1, self._buffer_evidence())
        self.assertEqual(self.credited, [[self.helper]])

    def test_group_log_receipt_still_credits_the_group(self):
        self.gc.messages = [_DISPATCH, _PROPOSAL, _RESULT, _VERDICT]
        ok = self.lh.commit_verified_action_completion(
            _UP, 1, {'message_index': 2, 'kind': 'tool_receipt'})
        self.assertTrue(ok)
        self.assertEqual(self.credited, [self.gc.agents])

    def test_receipt_must_belong_to_a_participant_and_its_peer(self):
        self.assertTrue(self._accepts(self._buffer_evidence()))
        self.assertFalse(self._accepts(self._buffer_evidence(agent='Stranger')))
        self.assertFalse(self._accepts(self._buffer_evidence(peer='Nobody')))
        self.assertFalse(self._accepts(self._buffer_evidence(message_index=9)))

    def test_stale_buffer_receipt_cannot_complete_a_later_action(self):
        dispatch2 = {'role': 'user', 'name': 'ChatInstructor',
                     'content': 'Perform this action -> Action #2: close it'}
        self.helper._oai_messages['Assistant'].append(dispatch2)
        self.assertTrue(self._accepts(self._buffer_evidence(), action_id=1))
        self.assertFalse(self._accepts(self._buffer_evidence(), action_id=2))

    def test_written_answer_is_never_cited_from_a_buffer(self):
        answer = {'role': 'assistant', 'name': 'Assistant', 'content': 'Done.'}
        self.helper._oai_messages['Assistant'].append(answer)
        self.assertFalse(self._accepts(self._buffer_evidence(
            message_index=3, kind='user_visible_result')))


if __name__ == '__main__':
    unittest.main()
