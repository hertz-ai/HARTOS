"""CREATE completes an action on the tool result that ran, not on the message
index its verifier happens to cite.

LIVE, installed desktop on the 4B (llama-server :8080), 2026-10-05 23:53 --
BUILD of agent 89309952446 (Teach Yourself Tutor) for learner 777011
(task #162).  Action 1 is

    get_data_by_key: read key tutor.progress for the topic being learned ...

and the group chat holds a real get_data_by_key call answered with
{'topic': 'photosynthesis', 'step': 1, ...}.  The StatusVerifier answered
"completed" citing message_index 14 -- a get_chat_history reply
{"res_in_filter": []}, a few messages away from the real one -- and the gate
(rightly) refused a receipt that is not the named tool's work.  Every action
of the run was then refused three times and GAVE_UP, so no recipe was banked
and the agent was never built.  A 4B cannot count message indexes; REUSE never
asks it to (reuse_recipe._reuse_completion_evidence finds the receipt itself).

The pipeline now finds the action's own tool result when the verdict cites
none the gate accepts, and judges it with the SAME gate
(_verifier_completion_has_conversation_evidence), so a result that is not the
action's work is still no receipt.

These tests call the real lifecycle_hooks functions on the real state machine;
only the registries and the learning sinks are faked (the shared harness).
"""
import hashlib
import os
import sys
from types import SimpleNamespace

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.constants import HISTORICAL_TOOL_PLACEHOLDER  # noqa: E402
from hartos import lifecycle_hooks as lh  # noqa: E402
from tests.unit.test_completion_needs_real_work import (  # noqa: E402
    _Harness, _Ledger, _call, _result)

S = lh.ActionState

# Verbatim from prompts/89309952446.json, action 1, and the 23:53 dump.
_ACTION = ('get_data_by_key: read key tutor.progress for the topic being '
           'learned and the last step taught; start at step 1 when it is empty')
_DISPATCH_1 = {'role': 'assistant', 'name': 'ChatInstructor',
               'content': f'Execute Action 1: {_ACTION} ,Latest User message: '
                          'Build the agent now.'}
_PROGRESS = ("{'topic': 'photosynthesis', 'step': 1, 'last_message': 'Great "
             "overview! Plants use sunlight, water, and carbon dioxide to "
             "create sugar and oxygen.'}")
_HISTORY = '{"res_in_filter": []}'
_SENT = 'Message sent successfully to user with request_id: 1791224474748-intermediate'
_PROSE = "I appreciate your enthusiasm, but I'm already the agent you are asking me to build."

_SERVED = ('get_data_by_key', 'get_chat_history', 'send_message_to_user',
           'save_data_in_memory', 'google_search')


class _Peer:
    """A counterpart a buffer is keyed by (autogen agents are hashable;
    SimpleNamespace is not)."""

    def __init__(self, name):
        self.name = name


def _agent(*tools, **extra):
    """A group-chat participant that serves ``tools`` (its function map)."""
    return SimpleNamespace(name=extra.pop('name', 'Assistant'), llm_config=None,
                           _function_map={t: (lambda **_kw: None) for t in tools},
                           **extra)


def _live_window():
    """Action 1 as the 23:53 dump shows it: the model sends a message and
    reads the history, the ChatInstructor re-posts the action, the named tool
    runs, and the closing round is only prose.  Indexes:

        0 dispatch   1-2 send_message_to_user   3-4 get_chat_history
        5 dispatch   6-7 get_data_by_key (THE receipt)   8-9 send_message
        10 dispatch  11 prose
    """
    return [
        _DISPATCH_1,
        _call('m1', 'send_message_to_user'), _result('m1', _SENT),
        _call('h1', 'get_chat_history'), _result('h1', _HISTORY),
        _DISPATCH_1,
        _call('g1', 'get_data_by_key', '{"key": "tutor.progress"}'),
        _result('g1', _PROGRESS),
        _call('m2', 'send_message_to_user'), _result('m2', _SENT),
        _DISPATCH_1,
        {'role': 'user', 'name': 'Assistant', 'content': _PROSE},
    ]


class _CreateHarness(_Harness):
    def setUp(self):
        super().setUp()
        self.gc.agents = [_agent(*_SERVED)]
        self.ledger = _Ledger(_ACTION)

    def _set_log(self, msgs):
        self.gc.messages = list(msgs)

    def _verdict(self, evidence=None, action_id=1, **extra):
        verdict = {'status': 'completed', 'action': 'get_data_by_key',
                   'action_id': action_id, **extra}
        if evidence is not None:
            verdict['evidence'] = evidence
        return verdict

    def _hook(self, verdict, current_action=1):
        return lh.lifecycle_hook_process_verifier_response(
            self.UP, verdict, SimpleNamespace(current_action=current_action))

    def _recorded(self, action_id=1):
        return self.ledger.tasks[f'action_{action_id}'].context.get(
            'verification_evidence', [])


class TestTheVerdictCitesAMessageThatIsNotTheReceipt(_CreateHarness):
    def setUp(self):
        super().setUp()
        self._set_log(_live_window())
        self._state(S.IN_PROGRESS)

    def _assert_completed_on_the_named_tools_result(self, result):
        self.assertEqual(result['action'], 'force_fallback')
        self.assertEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)
        records = self._recorded()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['evidence'],
                         {'message_index': 7, 'kind': 'tool_receipt'})
        self.assertEqual(records[0]['receipt_sha256'],
                         hashlib.sha256(_PROGRESS.encode('utf-8')).hexdigest())

    def test_another_tools_result_is_replaced_by_the_named_tools(self):
        """The live verdict: message_index 14, a get_chat_history reply."""
        result = self._hook(self._verdict(
            {'message_index': 4, 'kind': 'tool_receipt'}))
        self._assert_completed_on_the_named_tools_result(result)

    def test_the_first_message_cited_as_a_result_is_replaced(self):
        """The second live shape: index 0, kind user_visible_result."""
        result = self._hook(self._verdict(
            {'message_index': 0, 'kind': 'user_visible_result'}))
        self._assert_completed_on_the_named_tools_result(result)

    def test_an_index_past_the_end_of_the_log_is_replaced(self):
        result = self._hook(self._verdict(
            {'message_index': 99, 'kind': 'tool_receipt'}))
        self._assert_completed_on_the_named_tools_result(result)

    def test_a_verdict_that_cites_nothing_is_judged_on_the_log(self):
        result = self._hook(self._verdict())
        self._assert_completed_on_the_named_tools_result(result)

    def test_a_citation_that_is_not_an_object_is_replaced(self):
        result = self._hook(self._verdict('message 7'))
        self._assert_completed_on_the_named_tools_result(result)

    def test_the_receipt_in_an_earlier_dispatch_of_the_action_counts(self):
        """The live closing window (10-11) holds only prose; the work sits in
        the window before it, under the same action's marker."""
        self.assertEqual(lh.latest_dispatch_before(self.gc.messages, 8), 1)
        result = self._hook(self._verdict(
            {'message_index': 11, 'kind': 'tool_receipt'}))
        self._assert_completed_on_the_named_tools_result(result)


class TestAValidCitationIsUsedAsCited(_CreateHarness):
    def test_the_derivation_is_not_asked_when_the_receipt_is_cited_right(self):
        self._set_log(_live_window())
        self._state(S.IN_PROGRESS)
        asked = []
        self.patch(lh, 'derive_completion_evidence',
                   lambda *a, **k: asked.append(a) or None)
        result = self._hook(self._verdict(
            {'message_index': 7, 'kind': 'tool_receipt'}))
        self.assertEqual(result['action'], 'force_fallback')
        self.assertEqual(asked, [])
        self.assertEqual(self._recorded()[0]['evidence'],
                         {'message_index': 7, 'kind': 'tool_receipt'})

    def test_a_cited_receipt_that_cannot_be_persisted_is_not_replaced(self):
        """The gate accepted the model's receipt and the ledger refused it:
        that is a refusal, not a reason to go looking for another receipt."""
        self._set_log(_live_window())
        self._state(S.IN_PROGRESS)
        self.ledger.save = lambda: False
        asked = []
        self.patch(lh, 'derive_completion_evidence',
                   lambda *a, **k: asked.append(a) or None)
        result = self._hook(self._verdict(
            {'message_index': 7, 'kind': 'tool_receipt'}))
        self.assertEqual(result['action'], 'force_completion')
        self.assertEqual(asked, [])
        self.assertNotEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)


class TestNoReceiptStillMeansNoCompletion(_CreateHarness):
    def _refused(self, verdict=None, msgs=None):
        self._set_log(msgs if msgs is not None else _live_window())
        self._state(S.IN_PROGRESS)
        result = self._hook(verdict or self._verdict(
            {'message_index': 4, 'kind': 'tool_receipt'}))
        self.assertEqual(result['action'], 'force_completion')
        self.assertNotEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)
        self.assertEqual(self._recorded(), [])

    def test_another_tools_result_is_not_the_named_tools_work(self):
        """The named tool never ran: a message to the person and a history
        read are not get_data_by_key (#147's rule, on the derived path)."""
        self._refused(msgs=[
            _DISPATCH_1,
            _call('m1', 'send_message_to_user'), _result('m1', _SENT),
            _call('h1', 'get_chat_history'), _result('h1', _HISTORY)])

    def test_the_named_tools_failed_result_is_not_a_receipt(self):
        self._refused(msgs=[
            _DISPATCH_1,
            _call('g1', 'get_data_by_key'),
            _result('g1', 'Error: key tutor.progress could not be read')])

    def test_an_empty_result_is_not_a_receipt(self):
        self._refused(msgs=[_DISPATCH_1, _call('g1', 'get_data_by_key'),
                            _result('g1', '   ')])

    def test_a_placeholder_is_not_a_result(self):
        """helper.py back-fills a call that returned nothing with this text."""
        self._refused(msgs=[_DISPATCH_1, _call('g1', 'get_data_by_key'),
                            _result('g1', HISTORICAL_TOOL_PLACEHOLDER)])

    def test_a_result_no_call_names_is_not_the_named_tools_work(self):
        self._refused(msgs=[_DISPATCH_1, _result('orphan', _PROGRESS)])

    def test_a_log_with_no_tool_traffic_is_refused(self):
        self._refused(msgs=[
            _DISPATCH_1, {'role': 'user', 'name': 'Assistant', 'content': _PROSE}])

    def test_no_registered_group_chat_is_refused(self):
        self.patch(lh, 'get_registered_groupchat', lambda _up: None)
        self._set_log(_live_window())
        self._state(S.IN_PROGRESS)
        result = self._hook(self._verdict(
            {'message_index': 4, 'kind': 'tool_receipt'}))
        self.assertEqual(result['action'], 'force_completion')

    def test_repeated_unfindable_claims_still_end_gave_up(self):
        self._set_log([_DISPATCH_1, _call('m1', 'send_message_to_user'),
                       _result('m1', _SENT)])
        self._state(S.IN_PROGRESS)
        actions = [self._hook(self._verdict(
            {'message_index': 2, 'kind': 'tool_receipt'}))['action']
            for _ in range(4)]
        self.assertEqual(actions, ['force_completion'] * 3 + ['gave_up'])
        self.assertEqual(lh.get_action_state(self.UP, 1), S.GAVE_UP)

    def test_a_derivation_that_raises_is_a_refusal_not_a_crash(self):
        self._set_log(_live_window())
        self._state(S.IN_PROGRESS)

        def boom(*_a, **_k):
            raise RuntimeError('registry went away')
        self.patch(lh, 'evidence_sources', boom)
        result = self._hook(self._verdict(
            {'message_index': 4, 'kind': 'tool_receipt'}))
        self.assertEqual(result['action'], 'force_completion')
        self.assertNotEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)


class TestAnotherActionsReceiptIsNotThisActions(_CreateHarness):
    """Action 1's get_data_by_key result must not complete action 2."""

    def setUp(self):
        super().setUp()
        self.ledger.tasks['action_2'] = SimpleNamespace(
            context={}, description=_ACTION)
        lh.retry_tracker.reset_count(self.UP, 2)
        self.addCleanup(lh.retry_tracker.reset_count, self.UP, 2)
        self._dispatch_2 = {'role': 'assistant', 'name': 'ChatInstructor',
                            'content': f'Execute Action 2: {_ACTION} ,Latest User '
                                       'message: Build the agent now.'}

    def _hook2(self, verdict):
        lh.set_action_state(self.UP, 2, S.IN_PROGRESS, 'test')
        return self._hook(verdict, current_action=2)

    def test_a_receipt_before_the_actions_dispatch_is_not_found(self):
        self._set_log([_DISPATCH_1, _call('g1', 'get_data_by_key'),
                       _result('g1', _PROGRESS), self._dispatch_2])
        result = self._hook2(self._verdict(
            {'message_index': 2, 'kind': 'tool_receipt'}, action_id=2))
        self.assertEqual(result['action'], 'force_completion')
        self.assertNotEqual(lh.get_action_state(self.UP, 2), S.COMPLETED)

    def test_its_own_receipt_after_its_dispatch_is_found(self):
        self._set_log([_DISPATCH_1, _call('g1', 'get_data_by_key'),
                       _result('g1', _PROGRESS), self._dispatch_2,
                       _call('g2', 'get_data_by_key'),
                       _result('g2', _PROGRESS + ' ')])
        result = self._hook2(self._verdict(
            {'message_index': 0, 'kind': 'user_visible_result'}, action_id=2))
        self.assertEqual(result['action'], 'force_fallback')
        self.assertEqual(lh.get_action_state(self.UP, 2), S.COMPLETED)
        self.assertEqual(self._recorded(2)[0]['evidence']['message_index'], 5)


class TestTheVerdictStillHasToNameThisAction(_CreateHarness):
    def test_a_verdict_for_another_action_completes_nothing_even_with_a_receipt(self):
        """REUSE 17:35:28 (#125): the verdict's own action_id is not advisory
        for completion -- and finding the receipt ourselves must not turn it
        back into advice."""
        self._set_log(_live_window())
        self._state(S.IN_PROGRESS)
        result = self._hook(self._verdict(
            {'message_index': 4, 'kind': 'tool_receipt'}, action_id=3))
        self.assertEqual(result['action'], 'force_completion')
        self.assertNotEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)
        self.assertEqual(self._recorded(), [])


class TestWhichResultIsTheReceipt(_CreateHarness):
    def test_the_newest_good_result_is_chosen_and_a_newer_failure_skipped(self):
        self._set_log([
            _DISPATCH_1,
            _call('g1', 'get_data_by_key'), _result('g1', _PROGRESS),
            _call('g2', 'get_data_by_key'), _result('g2', _PROGRESS + ' again'),
            _call('g3', 'get_data_by_key'),
            _result('g3', 'Error: store unavailable'),
        ])
        self._state(S.IN_PROGRESS)
        self.assertEqual(
            lh.derive_completion_evidence(self.UP, 1),
            {'message_index': 4, 'kind': 'tool_receipt'})

    def test_one_real_result_in_an_aggregate_message_is_found(self):
        both = {'role': 'tool', 'name': 'Assistant', 'content': '...',
                'tool_responses': [
                    {'tool_call_id': 'h', 'role': 'tool', 'content': _HISTORY},
                    {'tool_call_id': 'g', 'role': 'tool', 'content': _PROGRESS}]}
        call = {'role': 'assistant', 'name': 'Assistant', 'content': '',
                'tool_calls': [
                    {'id': 'h', 'function': {'name': 'get_chat_history'}},
                    {'id': 'g', 'function': {'name': 'get_data_by_key'}}]}
        self._set_log([_DISPATCH_1, call, both])
        self.assertEqual(lh.derive_completion_evidence(self.UP, 1),
                         {'message_index': 2, 'kind': 'tool_receipt'})


class TestWhereTheReceiptLives(_CreateHarness):
    """The lists REUSE already reads: the group log, then each participant's
    pairwise buffer (resolve_receipt reads a receipt back from either)."""

    def setUp(self):
        super().setUp()
        self.peer = _Peer('ChatInstructor')
        self.buffer = [_DISPATCH_1, _call('g1', 'get_data_by_key'),
                       _result('g1', _PROGRESS)]
        self.holder = _agent(*_SERVED, name='Assistant',
                             _oai_messages={self.peer: self.buffer})
        self.gc.agents = [self.holder]

    def test_a_receipt_held_only_in_a_buffer_is_found_and_read_back(self):
        self._set_log([_DISPATCH_1])
        self._state(S.IN_PROGRESS)
        evidence = lh.derive_completion_evidence(self.UP, 1)
        self.assertEqual(evidence, {'source': 'buffer', 'agent': 'Assistant',
                                    'peer': 'ChatInstructor',
                                    'message_index': 2, 'kind': 'tool_receipt'})
        self.assertIs(lh.resolve_receipt(self.UP, evidence)[2], self.buffer[2])
        result = self._hook(self._verdict(
            {'message_index': 0, 'kind': 'user_visible_result'}))
        self.assertEqual(result['action'], 'force_fallback')
        self.assertEqual(self._recorded()[0]['evidence'], evidence)

    def test_the_group_log_is_searched_before_the_buffers(self):
        self._set_log([_DISPATCH_1, _call('g9', 'get_data_by_key'),
                       _result('g9', _PROGRESS)])
        self.assertEqual(lh.derive_completion_evidence(self.UP, 1),
                         {'message_index': 2, 'kind': 'tool_receipt'})

    def test_a_buffer_is_held_to_its_own_dispatch_window(self):
        """A buffer also carries earlier turns: a result before its action's
        marker in THAT list is not this action's."""
        self.buffer[:] = [_call('g1', 'get_data_by_key'),
                          _result('g1', _PROGRESS), _DISPATCH_1]
        self._set_log([_DISPATCH_1])
        self.assertIsNone(lh.derive_completion_evidence(self.UP, 1))


class TestEvidenceSourcesAreOneDefinition(_CreateHarness):
    def test_reuse_reads_the_lists_the_completion_gate_reads(self):
        from hartos import reuse_recipe
        peer = _Peer('ChatInstructor')
        buffer = [_DISPATCH_1]
        holder = _agent('get_data_by_key', name='Assistant',
                        _oai_messages={peer: buffer})
        self.gc.agents = [holder, _agent('google_search', name='Helper')]
        self._set_log([_DISPATCH_1, _PROSE])
        expected = [(None, self.gc.messages),
                    ({'source': 'buffer', 'agent': 'Assistant',
                      'peer': 'ChatInstructor'}, buffer)]
        for source_fn in (lh.evidence_sources,
                          reuse_recipe._reuse_evidence_sources):
            got = list(source_fn(self.gc, self.gc.agents))
            self.assertEqual([s for s, _m in got], [s for s, _m in expected])
            self.assertTrue(all(m is e for (_s, m), (_x, e) in zip(got, expected)))


# A prose action is done by its written answer (REUSE splits the same way:
# a tool receipt for an action that names a tool, the written answer for one
# that does not).  LIVE, the same 4B, 2026-10-06 01:11: agent 54 as a ONE
# prose action ("Teach exactly one next step ... Write the lesson as your
# reply to the learner").  The Assistant wrote a good lesson and the verdict
# was refused as ungrounded -- the same citation failure, for a written answer.
_PROSE_ACTION = ('Teach exactly one next step of the topic the learner chose, '
                 'simply (when the topic is new, open with one everyday analogy '
                 'and a two sentence overview), then end with one check question. '
                 'Write the lesson as your reply to the learner.')
_LESSON = ('Think of a leaf as a tiny kitchen: sunlight is the stove, water and '
           'air are the ingredients, and sugar is the meal it cooks.\n\n'
           'Photosynthesis is how plants turn light, water and carbon dioxide '
           'into sugar and oxygen. It happens in the chloroplasts.\n\n'
           'Check question: where inside a plant cell does photosynthesis happen?')
_HANDOFF = '@StatusVerifier Please verify the completion of Action 1.'


def _assistant(content):
    """The Assistant's plain reply as a native group log holds it: role 'user',
    because autogen's manager stores what it received from a speaker as 'user'.
    (This fixture said 'assistant' until 2026-10-06, the shape of a log rebuilt
    from the manager's buffer, and so passed while a live lesson was refused --
    test_written_answer_in_a_native_group_log.py runs on a real autogen log.)"""
    return {'role': 'user', 'name': 'Assistant', 'content': content}


class _ProseHarness(_CreateHarness):
    def setUp(self):
        super().setUp()
        self.ledger = _Ledger(_PROSE_ACTION)
        self.dispatch = {'role': 'assistant', 'name': 'ChatInstructor',
                         'content': f'Execute Action 1: {_PROSE_ACTION} ,Latest '
                                    'User message: Teach me about photosynthesis.'}
        self._state(S.IN_PROGRESS)

    def _wrong_citation(self):
        return self._verdict({'message_index': 0, 'kind': 'user_visible_result'})


class TestTheLessonIsFoundWhenTheVerdictCitesTheWrongMessage(_ProseHarness):
    def _live_log(self):
        self._set_log([
            self.dispatch,
            _assistant('@Helper Please look up a short explanation of photosynthesis.'),
            _assistant(_LESSON),
            _assistant(_HANDOFF)])

    def _assert_completed_on_the_lesson(self, result):
        self.assertEqual(result['action'], 'force_fallback')
        self.assertEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)
        record = self._recorded()[0]
        self.assertEqual(record['evidence'],
                         {'message_index': 2, 'kind': 'user_visible_result'})
        self.assertEqual(record['receipt_sha256'],
                         hashlib.sha256(_LESSON.encode('utf-8')).hexdigest())

    def test_the_dispatch_cited_as_the_result_is_replaced_by_the_lesson(self):
        """The live verdict: index 0.  The lesson is the longest written
        answer, and a newer one-line handoff does not outrank it."""
        self._live_log()
        self._assert_completed_on_the_lesson(self._hook(self._wrong_citation()))

    def test_a_verdict_that_cites_nothing_is_judged_on_the_log(self):
        self._live_log()
        self._assert_completed_on_the_lesson(self._hook(self._verdict()))

    def test_a_lesson_that_ends_with_a_handoff_is_still_the_lesson(self):
        self._set_log([self.dispatch, _assistant(_LESSON + '\n\n' + _HANDOFF)])
        result = self._hook(self._wrong_citation())
        self.assertEqual(result['action'], 'force_fallback')
        self.assertEqual(self._recorded()[0]['evidence']['message_index'], 1)

    def test_a_lesson_sent_to_the_user_with_the_message_tag_is_the_lesson(self):
        tagged = '@user {"message2user": "' + _LESSON.replace('\n', ' ') + '"}'
        self._set_log([self.dispatch, _assistant(tagged), _assistant(_HANDOFF)])
        result = self._hook(self._wrong_citation())
        self.assertEqual(result['action'], 'force_fallback')
        self.assertEqual(self._recorded()[0]['evidence']['message_index'], 1)

    def test_the_longest_answer_wins_over_a_newer_short_one(self):
        """The lesson is the work; "Done!" after it is not."""
        self._set_log([self.dispatch, _assistant(_LESSON), _assistant('Done!')])
        self.assertEqual(lh.derive_completion_evidence(self.UP, 1),
                         {'message_index': 1, 'kind': 'user_visible_result'})

    def test_a_correct_citation_is_used_as_cited(self):
        self._live_log()
        asked = []
        self.patch(lh, 'derive_completion_evidence',
                   lambda *a, **k: asked.append(a) or None)
        result = self._hook(self._verdict(
            {'message_index': 2, 'kind': 'user_visible_result'}))
        self.assertEqual(result['action'], 'force_fallback')
        self.assertEqual(asked, [])


class TestOnlyAWrittenAnswerCompletesAProseAction(_ProseHarness):
    def _refused(self, msgs, verdict=None):
        self._set_log(msgs)
        self.assertIsNone(lh.derive_completion_evidence(self.UP, 1))
        result = self._hook(verdict or self._wrong_citation())
        self.assertEqual(result['action'], 'force_completion')
        self.assertNotEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)
        self.assertEqual(self._recorded(), [])

    def test_a_delegation_and_a_handoff_are_not_an_answer(self):
        self._refused([self.dispatch,
                       _assistant('@Helper Please look up photosynthesis.'),
                       _assistant(_HANDOFF)])

    def test_the_assistants_own_status_json_is_not_an_answer(self):
        self._refused([self.dispatch, _assistant(
            '{"status": "error", "action": "teach", "action_id": 1, '
            '"message": "could not"}')])

    def test_an_empty_message_is_not_an_answer(self):
        self._refused([self.dispatch, _assistant('   ')])

    def test_the_verifiers_own_message_is_not_the_assistants_answer(self):
        self._refused([self.dispatch, {'role': 'user', 'name': 'StatusVerifier',
                                       'content': _LESSON}])

    def test_a_tool_reply_does_not_complete_a_prose_action(self):
        """The tool-receipt search is for actions that name a tool."""
        self._refused([self.dispatch, _call('s1', 'google_search'),
                       _result('s1', 'Photosynthesis: plants make sugar (3 sources)')])

    def test_the_lesson_of_an_earlier_actions_window_is_not_found(self):
        self.ledger.tasks['action_2'] = SimpleNamespace(
            context={}, description=_PROSE_ACTION)
        lh.retry_tracker.reset_count(self.UP, 2)
        self.addCleanup(lh.retry_tracker.reset_count, self.UP, 2)
        self._set_log([self.dispatch, _assistant(_LESSON),
                       {'role': 'assistant', 'name': 'ChatInstructor',
                        'content': f'Execute Action 2: {_PROSE_ACTION} ,Latest '
                                   'User message: go on'}])
        self.assertIsNone(lh.derive_completion_evidence(self.UP, 2))
        lh.set_action_state(self.UP, 2, S.IN_PROGRESS, 'test')
        result = self._hook(self._verdict(
            {'message_index': 1, 'kind': 'user_visible_result'}, action_id=2),
            current_action=2)
        self.assertEqual(result['action'], 'force_completion')

    def test_a_lesson_held_only_in_a_buffer_is_not_found(self):
        """A written answer is only ever cited from the group log."""
        peer = _Peer('ChatInstructor')
        holder = _agent(*_SERVED, name='Assistant',
                        _oai_messages={peer: [self.dispatch, _assistant(_LESSON)]})
        self.gc.agents = [holder]
        self._refused([self.dispatch])

    def test_a_verdict_for_another_action_completes_nothing(self):
        self._set_log([self.dispatch, _assistant(_LESSON)])
        result = self._hook(self._verdict(
            {'message_index': 1, 'kind': 'user_visible_result'}, action_id=2))
        self.assertEqual(result['action'], 'force_completion')
        self.assertNotEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)


class TestAnActionThatNamesAToolIsNotCompletedOnAnAnswer(_CreateHarness):
    """#147's rule, on the derived path: the written promise is not the work."""

    def test_a_long_written_answer_is_not_the_named_tools_receipt(self):
        self._set_log([_DISPATCH_1, _assistant(_LESSON)])
        self._state(S.IN_PROGRESS)
        self.assertIsNone(lh.derive_completion_evidence(self.UP, 1))
        result = self._hook(self._verdict(
            {'message_index': 1, 'kind': 'user_visible_result'}))
        self.assertEqual(result['action'], 'force_completion')
        self.assertNotEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)
