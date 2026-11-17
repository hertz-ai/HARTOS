"""A send is a step's output only when the person got it and it carried
written work; the step's result is the words they got.

hartos-00's review of 476bbd5d8 (REJECTED), fixed forward:

  * CRITICAL: a send that reached nobody completed the step.  Failure was
    judged by tool_reply_failed, which misses the send tool's own
    non-delivery replies ("Failed to send message to user ...", "Message
    directed to @x agent, not sending to user", "... its delivery was not
    confirmed").  Only the tool's own success reply is a delivery now
    (lifecycle_hooks.send_delivered), in the gate and every reader.
  * Medium: the sent text was never judged.  An empty send, or a send of
    ``{"status": "completed"}``, completed the step while is_written_answer
    refused the same words written as an answer.  One judgment for both
    (written_text_is_answer, read by delivered_text).
  * Medium: the step's result and its world-model record were the tool's
    "Message sent successfully ...", not the words delivered; with the
    critical item, "Failed to send message ..." was promoted as a verified
    success.  The result is the delivered words (receipt_result_text).
  * Medium: an empty send result borrowed the aggregate message's content
    in the receipt finder, where the gate falls back only on None.

    python -m pytest tests/unit/test_only_a_delivered_send_is_output.py -q
"""
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from hartos import lifecycle_hooks as lh  # noqa: E402
from hartos import reuse_recipe as rr  # noqa: E402
from tests.unit.test_completion_needs_real_work import _call, _result  # noqa: E402
from tests.unit.test_prose_step_receipt_is_its_send import (  # noqa: E402
    _ACTION, _BothProjectors, _SENT, _dispatch)

S = lh.ActionState

_TEXT = ("Your topic is basic algebra at a beginner level; the next step "
         "prepares guiding questions on it.")

# The send tool's own replies that reached nobody (core/agent_tools.py and
# both send_message_to_user1 copies).
_NOT_DELIVERED = (
    'Failed to send message to user with request_id: r1',
    'Message directed to @statusverifier agent, not sending to user',
    'Message handed to the sender; its delivery was not confirmed',
)


def _send(reply=_SENT, text=_TEXT, call_id='s1'):
    """A send declared as delivery, and the tool's reply to it."""
    return [_call(call_id, 'send_message_to_user',
                  json.dumps({'text': text, 'expects_reply': False})),
            _result(call_id, reply)]


def _completed_citing(index=2):
    return {'status': 'completed', 'action_id': 1,
            'evidence': {'message_index': index, 'kind': 'tool_receipt'}}


class TestOnlyTheToolsOwnSuccessIsADelivery(_BothProjectors):
    def test_a_send_that_reached_nobody_is_no_receipt(self):
        for reply in _NOT_DELIVERED:
            with self.subTest(reply=reply):
                self._set_log(_dispatch(), *_send(reply=reply))
                self._assert_both(None)

    def test_the_gate_refuses_a_verdict_citing_a_send_that_reached_nobody(self):
        """The reviewer's critical case through the gate: on 476bbd5d8 the
        verdict citing it completed the step."""
        for reply in _NOT_DELIVERED:
            with self.subTest(reply=reply):
                self._set_log(_dispatch(), *_send(reply=reply))
                self.assertFalse(lh._verifier_completion_has_conversation_evidence(
                    self.UP, 1, _completed_citing()))

    def test_a_delivered_send_is_the_receipt(self):
        self._set_log(_dispatch(), *_send())
        self._assert_both({'message_index': 2, 'kind': 'tool_receipt'})
        self.assertTrue(lh._verifier_completion_has_conversation_evidence(
            self.UP, 1, _completed_citing()))

    def test_an_empty_result_never_borrows_the_aggregates_sent(self):
        """Medium: the receipt finders read an empty result as its
        aggregate's content; the gate falls back only on None.  For a step
        that names the send, REUSE's named receipt has no gate behind it,
        so on 476bbd5d8 it took the borrowed "sent" as the receipt."""
        named = "send_message_to_user: tell the learner today's topic"
        self._flow(named)
        call = _call('s1', 'send_message_to_user',
                     json.dumps({'text': _TEXT, 'expects_reply': False}))
        aggregate = {'role': 'tool', 'content': _SENT,
                     'tool_responses': [{'tool_call_id': 's1', 'role': 'tool',
                                         'content': ''}]}
        for words in (_ACTION, named):
            with self.subTest(step=words):
                self._flow(words)
                self._set_log(_dispatch(1, words), call, aggregate)
                self._assert_both(None)


class TestTheSentWordsAreJudgedLikeAWrittenAnswer(_BothProjectors):
    def test_an_empty_or_control_send_is_no_output(self):
        for text in ('', '   ', 'TERMINATE',
                     json.dumps({'status': 'completed', 'action_id': 1})):
            with self.subTest(text=text):
                self._set_log(_dispatch(), *_send(text=text))
                self._assert_both(None)

    def test_the_same_rule_as_a_written_answer(self):
        for text in ('', 'TERMINATE', '{"status": "completed"}',
                     'Step 1 done. @StatusVerifier please verify.', _TEXT):
            with self.subTest(text=text):
                self.assertEqual(
                    lh.written_text_is_answer(text),
                    lh.is_written_answer({'role': 'assistant', 'name': 'Assistant',
                                          'content': text}))


class TestTheResultIsTheDeliveredWords(_BothProjectors):
    def test_the_steps_result_is_the_words_not_the_receipt(self):
        self._set_log(_dispatch(), *_send())
        evidence = rr._reuse_completion_evidence(self.UP, 1, self.gc)
        self._state(S.IN_PROGRESS, S.STATUS_VERIFICATION_REQUESTED)
        with patch.object(lh, 'safe_set_state', wraps=lh.safe_set_state) as setter:
            self.assertTrue(lh.commit_verified_action_completion(self.UP, 1, evidence))
        results = [c.kwargs.get('result') for c in setter.call_args_list
                   if len(c.args) > 2 and c.args[2] == S.COMPLETED]
        self.assertEqual(results, [_TEXT])

    def test_the_world_model_record_is_the_words_not_the_receipt(self):
        promote = next(value for _obj, name, value in self._patches
                       if name == '_promote_verified_outcome')
        self._set_log(_dispatch(), *_send())
        bridge = MagicMock()
        with patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge',
                   return_value=bridge):
            promote(self.UP, 1, _completed_citing())
        responses = [c.kwargs.get('response')
                     for c in bridge.record_interaction.call_args_list]
        self.assertEqual(responses, [_TEXT])

    def test_any_other_receipt_keeps_its_content(self):
        self._set_log(_dispatch(), _call('r1', 'get_data_by_key', '{"key": "k"}'),
                      _result('r1', 'tutor.progress: lesson 3'))
        resolved = lh.resolve_receipt(self.UP, {'message_index': 2, 'kind': 'tool_receipt'})
        self.assertEqual(lh.receipt_result_text(resolved), 'tutor.progress: lesson 3')


class TestReuseAsksOnlyWhatReachedThePerson(_BothProjectors):
    def test_a_question_that_reached_nobody_is_not_the_steps_ask(self):
        ask = [_call('a1', 'send_message_to_user',
                     json.dumps({'text': 'Which topic today?', 'expects_reply': True})),
               _result('a1', _NOT_DELIVERED[0])]
        log = [_dispatch(), *ask]
        self.assertIsNone(rr._reuse_ask_index(self.gc, log, 0, written_counts=False,
                                              step_waits=True))
        log = [_dispatch(), ask[0], _result('a1', _SENT)]
        self.assertEqual(rr._reuse_ask_index(self.gc, log, 0, written_counts=False,
                                             step_waits=True),
                         (2, 'send', 'Which topic today?'))
