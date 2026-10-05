"""A prose action is done by the Assistant's written answer, in a group log as
autogen really writes it.

LIVE, installed desktop on the 4B, 2026-10-06 01:26 -- REUSE of agent 54
("Personalised Learning"), whose one action is "Teach exactly one next step of
the topic the learner chose ... Write the lesson as your reply to the learner."
The Assistant wrote the lesson (an analogy, an overview, a check question), the
StatusVerifier said "completed", and the turn logged

    [REUSE-VERIFY] action 1 passed the fabrication gate but has no canonical
    receipt in its dispatch window; recording GAVE_UP for retry

after which the synthesis round, told that the action "gave up without a
verified result", apologised to the learner instead of giving them the lesson
it had just written.

The cause is a role.  The written-answer receipt was accepted only from a
message with role='assistant', and the group log holds a plain Assistant reply
as role='user': autogen's manager stores what it RECEIVED from a speaker as
'user'.  state_transition's own line "Last message role: ..., name: Assistant"
read user 15 times in that log (plain replies) and assistant 26 times (the
messages that carry tool_calls).  role='assistant' is also what a log rebuilt
from the manager's buffer holds (the #725 sync), and that was the only shape
the tests ever used -- which is how the gate passed them and failed the agent.

Everything below runs on a REAL autogen group chat (scripted seats, no model),
so the shape under test is autogen's, not this file's guess about it.
"""
import os
import sys
import unittest
from types import SimpleNamespace

import autogen

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from hartos import lifecycle_hooks as lh  # noqa: E402
from hartos import reuse_recipe as rr  # noqa: E402
from tests.unit.test_completion_needs_real_work import (  # noqa: E402
    _Harness, _Ledger)

S = lh.ActionState
NL = chr(10)

# Verbatim from agent 54's banked action 1 and the 01:26 dump.
_ACTION = ('Teach exactly one next step of the topic the learner chose, simply '
           '(when the topic is new, open with one everyday analogy and a two '
           'sentence overview), then end with one check question. Write the '
           'lesson as your reply to the learner.')
_WORDS = 'Teach me about photosynthesis.'
_LESSON = (
    "Sawubona! I'm genuinely excited to help you explore photosynthesis with "
    "me." + NL + NL +
    "Imagine photosynthesis like a **solar kitchen** in a plant's leaves! Just "
    "as a kitchen uses sunlight to cook food from raw ingredients, plants use "
    "sunlight to transform carbon dioxide and water into sugar (food) and "
    "oxygen." + NL + NL +
    "**Check question:** Can you think of another everyday example of "
    "something that captures energy from the environment to power a process?")
_VERDICT = ('{"status": "completed","action": "Teach exactly one next step of '
            'the topic the learner chose","action_id": 1,"message": "The '
            'action has been completed successfully."}')
_HANDOFF = '@StatusVerifier Please verify the completion of Action 1.'
_CREATE_DISPATCH = 'Execute Action 1: ' + _ACTION + ' ,Latest User message: ' + _WORDS


def _seat(name, reply):
    """A real autogen agent that answers whatever it is asked with ``reply``."""
    seat = autogen.ConversableAgent(name, llm_config=False,
                                    human_input_mode='NEVER')
    seat.register_reply([autogen.Agent, None],
                        lambda recipient, messages, sender, config: (True, reply),
                        position=0)
    return seat


def real_group_log(dispatch, speakers, initiator='User'):
    """Run a real autogen group chat and return its GroupChat.

    ``initiator`` posts ``dispatch``; each ``(seat name, reply)`` of
    ``speakers`` then speaks once, in order.  What comes back is what autogen
    appended, untouched.
    """
    starter = _seat(initiator, 'TERMINATE')
    seats = [_seat(name, reply) for name, reply in speakers]
    turns = iter(seats)
    group = autogen.GroupChat(
        agents=[starter, *seats], messages=[], max_round=len(seats) + 1,
        speaker_selection_method=lambda last, chat: next(turns))
    manager = autogen.GroupChatManager(groupchat=group, llm_config=False)
    starter.initiate_chat(manager, message=dispatch, clear_history=True,
                          silent=True)
    return group


def _lesson_log(dispatch, **kw):
    return real_group_log(dispatch, [('Assistant', _LESSON),
                                     ('StatusVerifier', _VERDICT)], **kw)


class TestTheShapeUnderTest(unittest.TestCase):
    """What the log really holds, so the tests below mean something.  If a
    new autogen writes it differently these fail first and say so."""

    def test_a_plain_assistant_reply_is_role_user_in_a_native_log(self):
        log = _lesson_log(_CREATE_DISPATCH, initiator='ChatInstructor').messages
        self.assertEqual([(m['name'], m['role']) for m in log[1:]],
                         [('Assistant', 'user'), ('StatusVerifier', 'user')])
        self.assertEqual(log[1]['content'], _LESSON)

    def test_a_message_carrying_tool_calls_is_role_assistant(self):
        call = {'content': 'Let me look that up.', 'tool_calls': [{
            'id': 'c1', 'type': 'function',
            'function': {'name': 'google_search', 'arguments': '{}'}}]}
        log = real_group_log(_CREATE_DISPATCH, [('Assistant', call)],
                             initiator='ChatInstructor').messages
        self.assertEqual((log[1]['name'], log[1]['role']),
                         ('Assistant', 'assistant'))


class TestCreateFindsTheLessonInARealLog(_Harness):
    def setUp(self):
        super().setUp()
        self.ledger = _Ledger(_ACTION)
        self.gc = _lesson_log(_CREATE_DISPATCH, initiator='ChatInstructor')

    def _complete(self, evidence):
        self._state(S.IN_PROGRESS, S.STATUS_VERIFICATION_REQUESTED)
        return lh.commit_verified_action_completion(self.UP, 1, evidence)

    def test_the_pipeline_finds_the_lesson_as_the_receipt(self):
        self.assertEqual(lh.derive_completion_evidence(self.UP, 1),
                         {'message_index': 1, 'kind': 'user_visible_result'})

    def test_a_verdict_that_cites_the_lesson_is_accepted(self):
        self.assertTrue(lh._verifier_completion_has_conversation_evidence(
            self.UP, 1, {'evidence': {'message_index': 1,
                                      'kind': 'user_visible_result'}}))

    def test_the_action_completes_on_the_lesson(self):
        self.assertTrue(self._complete(lh.derive_completion_evidence(self.UP, 1)))
        self.assertEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)

    def test_the_verdict_alone_completes_nothing(self):
        """The control: the verdict is index 2 and is the verifier's own."""
        self.assertFalse(self._complete({'message_index': 2,
                                         'kind': 'user_visible_result'}))
        self.assertNotEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)


class TestWhatIsNotTheAssistantsAnswerInARealLog(_Harness):
    def _derived(self, speakers, dispatch=_CREATE_DISPATCH):
        self.ledger = _Ledger(_ACTION)
        self.gc = real_group_log(dispatch, speakers, initiator='ChatInstructor')
        return lh.derive_completion_evidence(self.UP, 1)

    def test_a_handoff_to_another_agent_is_not_an_answer(self):
        self.assertIsNone(self._derived([('Assistant', _HANDOFF)]))

    def test_a_message_that_tags_an_agent_is_routing_whatever_else_it_says(self):
        """The scripted create loop's shape (test_create_loop_end_to_end,
        'a note to self'): the Assistant reports on a step and tags the next
        agent.  The pipeline appends its memory-skeleton line to such messages
        and REUSE reads any tag as 'addressed to an agent', so a lesson that
        also tags the verifier is routing too: the lesson goes in a message of
        its own, as the live ones do."""
        for note in ('Step 1 done. @StatusVerifier please verify.',
                     'Working on step 1. @Helper please save step 1.',
                     _LESSON + NL + NL + _HANDOFF):
            self.assertIsNone(self._derived([('Assistant', note)]), note)

    def test_the_assistants_own_status_json_is_not_an_answer(self):
        self.assertIsNone(self._derived([('Assistant', _VERDICT)]))

    def test_an_echo_of_the_dispatch_is_not_an_answer(self):
        """A seat name does not always survive a log: the dispatch comes back
        as the Assistant's, role user, and must not be its work."""
        self.assertIsNone(self._derived([('Assistant', _CREATE_DISPATCH)]))
        self.assertIsNone(self._derived(
            [('Assistant', 'Perform this action -> Action #1:' + _ACTION)]))

    def test_another_seats_lesson_is_not_the_assistants(self):
        self.assertIsNone(self._derived([('Helper', _LESSON)]))

    def test_the_verifiers_message_is_not_an_answer(self):
        self.assertIsNone(self._derived([('StatusVerifier', _LESSON)]))

    def test_no_message_after_the_dispatch_is_no_answer(self):
        self.assertIsNone(self._derived([('StatusVerifier', _VERDICT)]))

    def test_a_lesson_is_not_the_work_of_an_action_that_names_a_tool(self):
        """#147's rule on the real shape: the answer is not the tool's work."""
        action = 'get_data_by_key: read key tutor.progress for the topic'
        self.ledger = _Ledger(action)
        self.gc = real_group_log(
            'Execute Action 1: ' + action + ' ,Latest User message: go',
            [('Assistant', _LESSON), ('StatusVerifier', _VERDICT)],
            initiator='ChatInstructor')
        for seat in self.gc.agents:
            seat._function_map['get_data_by_key'] = lambda **_kw: None
        self.assertIsNone(lh.derive_completion_evidence(self.UP, 1))
        self.assertFalse(lh._verifier_completion_has_conversation_evidence(
            self.UP, 1, {'evidence': {'message_index': 1,
                                      'kind': 'user_visible_result'}}))


class TestTheSameLessonInAResyncedLog(_Harness):
    """The shape every earlier test used: a log rebuilt from the manager's
    buffer reads the Assistant's reply as role='assistant'.  Still the lesson."""

    def test_role_assistant_is_still_the_receipt(self):
        self.ledger = _Ledger(_ACTION)
        self.gc = SimpleNamespace(agents=[], messages=[
            {'role': 'user', 'name': 'User', 'content': _CREATE_DISPATCH},
            {'role': 'assistant', 'name': 'Assistant', 'content': _LESSON},
            {'role': 'assistant', 'name': 'StatusVerifier', 'content': _VERDICT}])
        self.assertEqual(lh.derive_completion_evidence(self.UP, 1),
                         {'message_index': 1, 'kind': 'user_visible_result'})


KEY = 'u9_p54'


class _Task:
    """The slice of user_tasks[session] the REUSE readers touch."""

    def __init__(self):
        self.current_action = 1
        self.actions = [{'action': _ACTION}]
        self.evidence_seen_call_ids = set()

    def get_action(self, _idx):
        return self.actions[0]


class _ReuseOnARealLog:
    """Mixin: the REUSE dispatch as REUSE builds it, then a real group chat."""

    def _install_session(self, key):
        recipe = {'actions': [{'action': _ACTION, 'recipe': [
            {'steps': "Identify the learner's chosen topic.", 'tool_name': '',
             'generalized_functions': ''}]}]}
        self.addCleanup(rr.user_tasks.pop, key, None)
        self.addCleanup(rr.recipes.pop, key, None)
        rr.user_tasks[key] = _Task()
        rr.recipes[key] = recipe

    def _reuse_log(self, key, speakers):
        dispatch = rr._build_reuse_action_message(key, 1, user_words=_WORDS)
        return real_group_log(dispatch, speakers, initiator='User')


class TestReuseFindsTheLessonInARealLog(_ReuseOnARealLog, _Harness):
    def setUp(self):
        super().setUp()
        self.ledger = _Ledger(_ACTION)
        self._install_session(self.UP)
        self.gc = self._reuse_log(self.UP, [('Assistant', _LESSON),
                                            ('StatusVerifier', _VERDICT)])

    def test_the_reuse_receipt_is_the_lesson(self):
        """THE LIVE FAILURE: this returned None and the action was GAVE_UP."""
        self.assertEqual(rr._reuse_completion_evidence(self.UP, 1, self.gc),
                         {'message_index': 1, 'kind': 'user_visible_result'})

    def test_the_shared_gate_completes_the_action_on_it(self):
        """REUSE's receipt goes through the one gate CREATE uses."""
        evidence = rr._reuse_completion_evidence(self.UP, 1, self.gc)
        self._state(S.IN_PROGRESS, S.STATUS_VERIFICATION_REQUESTED)
        self.assertTrue(lh.commit_verified_action_completion(self.UP, 1, evidence))
        self.assertEqual(lh.get_action_state(self.UP, 1), S.COMPLETED)

    def test_no_lesson_is_no_receipt(self):
        self.gc = self._reuse_log(self.UP, [('StatusVerifier', _VERDICT)])
        self.assertIsNone(rr._reuse_completion_evidence(self.UP, 1, self.gc))

    def test_a_dispatch_echo_is_no_receipt(self):
        echo = rr._build_reuse_action_message(self.UP, 1, user_words=_WORDS)
        self.gc = self._reuse_log(self.UP, [('Assistant', echo),
                                            ('StatusVerifier', _VERDICT)])
        self.assertIsNone(rr._reuse_completion_evidence(self.UP, 1, self.gc))

    def test_another_seats_lesson_is_no_receipt(self):
        self.gc = self._reuse_log(self.UP, [('Helper', _LESSON),
                                            ('StatusVerifier', _VERDICT)])
        self.assertIsNone(rr._reuse_completion_evidence(self.UP, 1, self.gc))


class TestReuseAdvancesOnTheLesson(_ReuseOnARealLog, unittest.TestCase):
    """The user-visible step, through the real _advance_reuse_action: a lesson
    written in a native log advances the action instead of recording GAVE_UP."""

    def setUp(self):
        import flask
        self._patches = []

        def patch(obj, name, value):
            self._patches.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        self._install_session(KEY)
        self.committed = []
        self.gave_up = []
        self.chat = self._reuse_log(KEY, [('Assistant', _LESSON),
                                          ('StatusVerifier', _VERDICT)])
        patch(rr, '_reuse_fabricated_tools', lambda *a, **k: [])
        patch(lh, 'get_registered_groupchat', lambda _s: self.chat)
        patch(rr, 'force_state_through_valid_path',
              lambda *a, **k: self.gave_up.append(a[2:]) or True)
        patch(rr, 'safe_set_state', lambda *a, **k: True)
        patch(rr, 'commit_verified_action_completion',
              lambda *a, **k: self.committed.append(a[2]) or True)
        patch(rr, '_stamp_action_evidence_watermark', lambda *a, **k: None)
        rr._reuse_resteer_counts.pop((KEY, 1), None)
        rr._reuse_fab_pending.pop((KEY, 1), None)
        ctx = flask.Flask(__name__).app_context()
        ctx.push()
        self.addCleanup(ctx.pop)

    def tearDown(self):
        for obj, name, value in reversed(self._patches):
            setattr(obj, name, value)

    def test_the_lesson_advances_the_action(self):
        """Agent 54 has one action, so the return value of its last advance
        is (None, False) -- the same pair a GAVE_UP returns.  What tells them
        apart is what was done: a receipt committed, no GAVE_UP recorded, the
        pointer moved on."""
        rr._advance_reuse_action(KEY, 1, 'test', 'rid')
        self.assertEqual(self.gave_up, [])
        self.assertEqual(self.committed,
                         [{'message_index': 1, 'kind': 'user_visible_result'}])
        self.assertEqual(rr.user_tasks[KEY].current_action, 2)


class TestTheMentionListIsOneList(unittest.TestCase):
    """"Is this message addressed to an agent" has one answer.  REUSE's loop
    and send_message_to_user keep the same five literally (the REUSE module's
    extract-and-exec tests need its constants literal), so they are pinned to
    the one in core.constants that the gate reads."""

    def test_reuse_reads_the_agents_the_gate_reads(self):
        from core.constants import AGENT_MENTIONS
        self.assertEqual(tuple(rr._REUSE_AGENT_MENTIONS), AGENT_MENTIONS)

    def test_source_guard_send_message_to_user_reads_the_same_five(self):
        import ast
        from core.constants import AGENT_MENTIONS
        path = os.path.join(_ROOT, 'core', 'agent_tools.py')
        tree = ast.parse(open(path, encoding='utf-8').read())
        found = [ast.literal_eval(node.value) for node in ast.walk(tree)
                 if isinstance(node, ast.Assign)
                 and any(getattr(t, 'id', None) == '_AGENT_MENTIONS'
                         for t in node.targets)]
        self.assertEqual([tuple(v) for v in found], [AGENT_MENTIONS])


if __name__ == '__main__':
    unittest.main()
