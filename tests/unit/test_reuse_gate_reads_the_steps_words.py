"""A REUSE step requires the tools its WORDS name, not the tools its banked run called.

Live 2026-10-21 on the relaunched desktop, agent 54 (Personalised Learning
Tutor), two new learners (request ids probe237-a1-154851, probe237-b1-154851):

  * step 1 says "Call get_user_id, then get_data_by_key ..." and the gate read
    it as naming ['get_user_id', 'get_data_by_key', 'list_books'];
  * step 2 says "... never through a messaging tool" and the gate demanded
    send_message_to_user and list_book_chapters.

Both turns re-steered until they gave up and never reached step 3, the save of
the learner's progress (134 s and 236.6 s; no {user}_54 store was written).

REUSE keeps the recipe's action dicts, and the gate read ``str(dict)``: the
banked ``recipe`` (the steps the creator's CREATE run took, with the tools that
one run happened to call) counted as the step's words.  On the desktop's 283
recipes, 600 of 1,098 actions carry a banked tool their words do not name.  The
tools a step is OFFERED still come from both its words and its banked steps
(the attach, _reuse_action_tool_names); only what it REQUIRES is its words'.

    python -m pytest tests/unit/test_reuse_gate_reads_the_steps_words.py -q
"""
import os
import sys
import unittest

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from hartos import lifecycle_hooks as lh  # noqa: E402
from hartos import reuse_recipe as rr  # noqa: E402
# DRY (Gate 2): the gate's message and agent shapes, and the real autogen
# group log, live with the gate's own tests.
from tests.unit.test_fab_guard_is_per_action import _agent, _call, _result  # noqa: E402
from tests.unit.test_written_answer_in_a_native_group_log import (  # noqa: E402
    _LESSON, _VERDICT, _WORDS, real_group_log)

SESSION = '555_54'
NOT_FOUND = 'Key not found in stored data.'

#: Agent 54's three steps as its saved recipe holds them (54_0_recipe.json,
#: the words verbatim but shortened, the banked steps with a stand-in learner
#: and book in place of the creator's own).
STEP_1 = {
    'action_id': 1, 'persona': 'Tutor', 'status': 'done',
    'action': ("Call get_user_id, then get_data_by_key with the key "
               "teach.<that user id> to read this learner's saved progress: "
               "the book, chapter or topic, the page range, the pages already "
               "taught. 'Key not found' means a new learner."),
    'recipe': [
        {'steps': 'get_user_id({})', 'tool_name': 'get_user_id'},
        {'steps': 'get_data_by_key({"key":"teach.555"})',
         'tool_name': 'get_data_by_key'},
        {'steps': 'list_books({})', 'tool_name': 'list_books'},
    ],
}
STEP_2 = {
    'action_id': 2, 'persona': 'Tutor', 'status': 'done',
    'action': ("Write this turn's reply to the learner yourself, as your own "
               "chat message to them, never through a messaging tool, from "
               "their latest message and the progress action 1 already read."),
    'recipe': [
        {'steps': 'list_books({})', 'tool_name': 'list_books'},
        {'steps': 'list_book_chapters({"book":"Sample Book"})',
         'tool_name': 'list_book_chapters'},
        {'steps': 'send_message_to_user({"text":"Hello!"})',
         'tool_name': 'send_message_to_user'},
    ],
}
STEP_3 = {
    'action_id': 3, 'persona': 'Tutor', 'status': 'done',
    'action': ("Call save_data_in_memory with the key teach.<that same user "
               "id> to save this learner's updated progress after the reply."),
    'recipe': [
        {'steps': 'save_data_in_memory({"key":"teach.555","value":{}})',
         'tool_name': 'save_data_in_memory'},
        {'steps': 'send_message_to_user({"text":"Saved."})',
         'tool_name': 'send_message_to_user'},
    ],
}
TOOLS = ['get_user_id', 'get_data_by_key', 'list_books', 'list_book_chapters',
         'send_message_to_user', 'save_data_in_memory']


@pytest.fixture
def session(monkeypatch):
    """Agent 54's REUSE session as REUSE builds it: the real Action over the
    recipe's action dicts, and the recipe itself."""
    monkeypatch.setitem(rr.user_tasks, SESSION, rr.Action([STEP_1, STEP_2, STEP_3]))
    monkeypatch.setitem(rr.recipes, SESSION, {'actions': [STEP_1, STEP_2, STEP_3]})
    return [_agent(TOOLS)]


def _chat(*messages):
    from types import SimpleNamespace
    return SimpleNamespace(messages=list(messages), agents=[])


# ── the rule ──────────────────────────────────────────────────────────────

def test_a_steps_words_are_its_own_action_text():
    assert lh.action_own_text(STEP_2) == STEP_2['action']
    assert lh.action_own_text('Call get_user_id') == 'Call get_user_id'
    assert lh.action_own_text(None) == ''
    assert lh.action_own_text({'recipe': STEP_1['recipe']}) == ''


def test_the_named_tools_of_a_step_are_the_ones_its_words_name():
    _names, named = lh.action_named_tools([_agent(TOOLS)], STEP_1)
    assert sorted(named) == ['get_data_by_key', 'get_user_id']


# ── the fabrication gate ──────────────────────────────────────────────────

def test_a_new_learners_read_completes_step_one(session):
    """THE LIVE FAILURE: both tools the words name ran; the gate still wanted
    list_books, which only the creator's run called."""
    gc = _chat(_call('u1', 'get_user_id'), _result('u1', '555'),
               _call('d1', 'get_data_by_key'), _result('d1', NOT_FOUND))
    assert rr._reuse_fabricated_tools(SESSION, 1, gc, session) == []


def test_a_tool_the_words_name_is_still_required(session):
    gc = _chat(_call('u1', 'get_user_id'), _result('u1', '555'))
    assert rr._reuse_fabricated_tools(SESSION, 1, gc, session) == [
        'get_data_by_key']


def test_a_written_step_is_not_held_to_its_banked_tools(session):
    """Step 2 forbids a messaging tool; its banked run used one."""
    assert rr._reuse_fabricated_tools(SESSION, 2, _chat(), session) == []


def test_the_save_step_still_needs_its_save(session):
    assert rr._reuse_fabricated_tools(SESSION, 3, _chat(), session) == [
        'save_data_in_memory']


# ── the round allowance ───────────────────────────────────────────────────

def test_a_written_step_names_no_tool_for_the_round_allowance(session):
    assert rr._reuse_own_tool_progress(SESSION, 2, _chat(), session) is None


def test_step_ones_progress_counts_only_its_words_tools(session):
    gc = _chat(_call('u1', 'get_user_id'), _result('u1', '555'),
               _call('d1', 'get_data_by_key'), _result('d1', NOT_FOUND))
    assert rr._reuse_own_tool_progress(SESSION, 1, gc, session) == 2


# ── the receipt ───────────────────────────────────────────────────────────

class TestTheWrittenStepsReceiptIsTheLesson(unittest.TestCase):
    """Step 2's receipt is the lesson the Assistant wrote, found by the same
    receipt search REUSE commits through (lifecycle_hooks)."""

    def setUp(self):
        self._saved = (rr.user_tasks.get(SESSION), rr.recipes.get(SESSION))
        rr.user_tasks[SESSION] = rr.Action([STEP_1, STEP_2, STEP_3])
        rr.recipes[SESSION] = {'actions': [STEP_1, STEP_2, STEP_3]}
        self.addCleanup(self._restore)
        dispatch = rr._build_reuse_action_message(SESSION, 2, user_words=_WORDS)
        self.gc = real_group_log(dispatch, [('Assistant', _LESSON),
                                            ('StatusVerifier', _VERDICT)],
                                 initiator='User')
        assistant = next(a for a in self.gc.agents if a.name == 'Assistant')
        assistant.register_function({n: (lambda **_kw: 'ok') for n in TOOLS})

    def _restore(self):
        for store, value in zip((rr.user_tasks, rr.recipes), self._saved):
            if value is None:
                store.pop(SESSION, None)
            else:
                store[SESSION] = value

    def test_the_lesson_is_the_receipt(self):
        self.assertEqual(rr._reuse_completion_evidence(SESSION, 2, self.gc),
                         {'message_index': 1, 'kind': 'user_visible_result'})


# ── what a step is OFFERED is unchanged ───────────────────────────────────

def test_the_attach_still_offers_the_banked_tools(session):
    offered = rr._reuse_action_tool_names(SESSION, 1)
    assert {'get_user_id', 'get_data_by_key', 'list_books'} <= set(offered)
