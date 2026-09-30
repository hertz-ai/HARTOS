"""An action that GAVE UP must not reach the user through the success door.

MEASURED LIVE 2026-09-24 on the installed Nunba (HARTOS 20bfca03d), agent
88094979291 ("summarize into exactly three bullet points"), driven as
livetest_reuse_verify_1790351204 through POST /custom_gpt:

    21:17:52,083  Action 1: in_progress -> gave_up
    (74 ms later)
    [SYNTHESIS] the action already wrote the answer -- recovered 175 chars

The reply was a promise with no bullets, and nothing in it said action 1 had
given up.  Actions 2..4 had never been reached.

WHY.  ``_reuse_synthesis_turn`` picks between the honest-incomplete steer and
the success doors (written-answer recovery, the no-data report,
``_REUSE_SYNTHESIS_STEER``) by ONE question: ``_reuse_outstanding_tools``.
That adapter returns [] for a prose action that names no tool, so for that
action it always answered "nothing outstanding".  The lifecycle had already
recorded the truth -- GAVE_UP from ``force_state_through_valid_path`` in
``_advance_reuse_action``, which also leaves the action pointer ON the
failed action -- and the synthesis turn never read it.

These tests drive the REAL ``_reuse_synthesis_turn`` with the REAL lifecycle
state store (``lifecycle_hooks.action_states``), mocking only the steering
seat (``chat_instructor``) and the tool-evidence adapter, exactly as the
sibling suites do.

    python -m pytest tests/unit/test_reuse_unfinished_turn_not_through_success_door.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

SESSION = 'livetest_reuse_verify_1790351204_88094979291'

# The live shape: the dispatch that opened action 1, the model's promise (a
# user-readable sentence, so the recovery would take it), and the verdict tail.
DISPATCH = {
    'role': 'user', 'name': 'ChatInstructor',
    'content': ("Perform this action -> Action #1:Receive the input text from "
                "the user.\n follow these steps: [{'Read the text': "
                "{'tool_name': '', 'code': ''}}]"),
}
PROMISE = ("Sure! I'll summarize the Quenfar bridge text into exactly three "
           "bullet points for you as soon as it is ready.")
VERDICT = ('{"status": "completed", "action_id": 1, "message": "Input text '
           'received."}')


def _history():
    return [dict(DISPATCH),
            {'role': 'assistant', 'name': 'Assistant', 'content': PROMISE},
            {'role': 'user', 'name': 'StatusVerifier', 'content': VERDICT}]


class _Task:
    """The pipeline's per-session task: an action list and a pointer."""

    def __init__(self, current_action, n_actions=4, autonomous=True):
        # autonomy is DECLARED, as the recipe author writes it: absent reads as
        # "not autonomous" (_reuse_action_is_autonomous), which is a different
        # scenario -- an action allowed to stop and ask the user.
        self.current_action = current_action
        # autonomous=None: the field is ABSENT, as on 52 of 1062 banked actions.
        self.actions = [dict({'action': 'step %d' % i},
                             **({} if autonomous is None else
                                {'can_perform_without_user_input':
                                 'yes' if autonomous else 'no'}))
                        for i in range(1, n_actions + 1)]
        self.evidence_vacuous_action = None

    def get_action(self, i):
        return self.actions[i]


class _Chat:
    def __init__(self, messages):
        self.agents = []
        self.messages = list(messages)


class _Manager:
    def __init__(self):
        self._oai_messages = {}


class _Recorder:
    """Stands in for chat_instructor; captures the steer instead of sending."""

    def __init__(self):
        self.messages = []

    def initiate_chat(self, recipient=None, message=None, **kw):
        self.messages.append(message)


@pytest.fixture
def rr():
    import hartos.reuse_recipe as mod          # a skip here would be vacuous
    return mod


@pytest.fixture
def lh():
    import hartos.lifecycle_hooks as mod
    return mod


def _run(rr, lh, monkeypatch, task, state, outstanding=()):
    monkeypatch.setattr(rr, '_reuse_outstanding_tools',
                        lambda *a, **k: list(outstanding), raising=True)
    monkeypatch.setitem(rr.user_tasks, SESSION, task)
    if state is not None:
        monkeypatch.setitem(lh.action_states, SESSION,
                            {task.current_action: state})
    chat = _Chat(_history())
    rec = _Recorder()
    posted = rr._reuse_synthesis_turn(SESSION, chat, _Manager(), rec)
    return posted, chat, rec


class TestAGaveUpActionIsReportedAsUnfinished:

    def test_the_promise_is_not_recovered_as_the_answer(
            self, rr, lh, monkeypatch):
        """THE DEFECT.  RED before the fix: the recovery appended PROMISE as
        the tail and the extractor delivered it."""
        _posted, chat, _rec = _run(rr, lh, monkeypatch, _Task(1),
                                   lh.ActionState.GAVE_UP)
        assert (chat.messages[-1].get('content') or '') != PROMISE, (
            'action 1 GAVE_UP and the synthesis turn still recovered the '
            "model's promise as the finished answer (live 21:17:52)")

    def test_the_honest_incomplete_steer_is_posted(self, rr, lh, monkeypatch):
        posted, _chat, rec = _run(rr, lh, monkeypatch, _Task(1),
                                  lh.ActionState.GAVE_UP)
        assert posted is True and len(rec.messages) == 1
        steer = rec.messages[0]
        assert steer.startswith(rr._REUSE_SYNTHESIS_STEER_INCOMPLETE.split(
            '{unrun}')[0]), 'the steer posted is not the incomplete steer'
        assert 'their tools have already run' not in steer.lower()

    def test_the_steer_names_the_gave_up_action_and_the_unreached_ones(
            self, rr, lh, monkeypatch):
        """The model cannot report what it is not told."""
        _posted, _chat, rec = _run(rr, lh, monkeypatch, _Task(1),
                                   lh.ActionState.GAVE_UP)
        steer = rec.messages[0]
        assert 'action 1 gave up' in steer, steer
        assert 'actions 2 to 4 were not reached' in steer, steer

    def test_the_no_data_report_cannot_speak_for_a_gave_up_action(
            self, rr, lh, monkeypatch):
        """The vacuity stamp names an EARLIER finished action (0 = the one
        before action 1); even when it lines up it must not turn a give-up
        into "there is nothing recorded"."""
        task = _Task(1)
        task.evidence_vacuous_action = 0
        _posted, chat, rec = _run(rr, lh, monkeypatch, task,
                                  lh.ActionState.GAVE_UP)
        assert rec.messages, 'a give-up was answered by the no-data report'
        assert (chat.messages[-1].get('content') or '') != rr._REUSE_NO_DATA_REPORT


class TestAnUnfinishedActionIsNotTheWholeAnswer:

    def test_a_mid_recipe_stop_is_reported_as_unfinished(
            self, rr, lh, monkeypatch):
        """Turn ended at action 2 of 4 while it was still IN_PROGRESS (round
        budget spent): the text written so far is not the finished answer."""
        _posted, chat, rec = _run(rr, lh, monkeypatch, _Task(2),
                                  lh.ActionState.IN_PROGRESS)
        assert rec.messages, 'a half-walked recipe went out as the answer'
        assert 'action 2 did not finish' in rec.messages[0]
        assert 'actions 3 to 4 were not reached' in rec.messages[0]
        assert (chat.messages[-1].get('content') or '') != PROMISE

    def test_the_last_action_gave_up_names_no_unreached_actions(
            self, rr, lh, monkeypatch):
        _posted, _chat, rec = _run(rr, lh, monkeypatch, _Task(4),
                                   lh.ActionState.GAVE_UP)
        assert 'action 4 gave up' in rec.messages[0]
        assert 'not reached' not in rec.messages[0]

    def test_unrun_tools_and_the_give_up_are_both_named(
            self, rr, lh, monkeypatch):
        _posted, _chat, rec = _run(rr, lh, monkeypatch, _Task(1),
                                   lh.ActionState.GAVE_UP,
                                   outstanding=['google_search'])
        assert 'google_search' in rec.messages[0]
        assert 'action 1 gave up' in rec.messages[0]


class TestAPauseForTheUserIsNotAFailure:
    """Review of 784a8143c, measured with a probe on both commits: a REUSE turn
    may END mid-recipe on purpose -- the REUSE-NODRIVER break stops the turn at
    an action with can_perform_without_user_input='no', and the pointer carries
    over to the user's next /chat turn.  That turn's tail is the agent's
    question to the user; reporting it as "action 1 did not finish" replaces
    the question with a failure report.  Same for the states that mean
    "waiting for the user" (lifecycle_hooks.ACTION_STATES_AWAITING_USER)."""

    def test_a_non_autonomous_action_asking_the_user_keeps_its_question(
            self, rr, lh, monkeypatch):
        posted, chat, rec = _run(rr, lh, monkeypatch,
                                 _Task(1, autonomous=False),
                                 lh.ActionState.IN_PROGRESS)
        assert rec.messages == [] and posted is False, (
            'a turn paused for the user was steered into a failure report: '
            f'{rec.messages!r}')
        assert chat.messages[-1].get('content') == PROMISE

    @pytest.mark.parametrize('state', ['PENDING', 'FALLBACK_REQUESTED',
                                       'PREVIEW_PENDING'])
    def test_a_state_waiting_for_the_user_is_not_unfinished(
            self, rr, lh, monkeypatch, state):
        _posted, chat, rec = _run(rr, lh, monkeypatch, _Task(2),
                                  getattr(lh.ActionState, state))
        assert rec.messages == [], f'{state} was reported as a failure'
        assert chat.messages[-1].get('content') == PROMISE

    def test_a_non_autonomous_action_that_gave_up_is_still_unfinished(
            self, rr, lh, monkeypatch):
        """A give-up is a failure whoever was meant to drive the action."""
        _posted, _chat, rec = _run(rr, lh, monkeypatch,
                                   _Task(1, autonomous=False),
                                   lh.ActionState.GAVE_UP)
        assert rec.messages and 'action 1 gave up' in rec.messages[0]

    def test_an_action_with_no_autonomy_field_that_stopped_is_unfinished(
            self, rr, lh, monkeypatch):
        """Review of b6ac59c89, measured: a missing field is not a declared
        pause.  Reading it as one silenced the incomplete report for 25 whole
        recipes, among them agent 88094979291 (all four actions lack it)."""
        _posted, _chat, rec = _run(rr, lh, monkeypatch,
                                   _Task(2, autonomous=None),
                                   lh.ActionState.IN_PROGRESS)
        assert rec.messages, 'a stop with no declared pause was not reported'
        assert 'action 2 did not finish' in rec.messages[0]
        assert 'actions 3 to 4 were not reached' in rec.messages[0]

    def test_a_no_with_its_reason_is_still_a_declared_pause(
            self, rr, lh, monkeypatch):
        """Review of 8439cd049, measured on a banked recipe: the CREATE prompt
        asks for 'no' WITH a reason, and one of the 15 explicit values is
        'no - requires specific dish constraints, ...'.  Equality with 'no'
        reported that pause as unfinished; the lifecycle hook reads a
        leading 'no'.  One rule, lifecycle_hooks.autonomy_needs_user."""
        task = _Task(1)
        for a in task.actions:
            a['can_perform_without_user_input'] = (
                'no - requires specific dish constraints, dietary restrictions')
        posted, chat, rec = _run(rr, lh, monkeypatch, task,
                                 lh.ActionState.IN_PROGRESS)
        assert rec.messages == [] and posted is False, rec.messages
        assert chat.messages[-1].get('content') == PROMISE

    @pytest.mark.parametrize('value,needs', [
        ('no', True), ('No - I need the likes of the user', True),
        (' no', True), ('yes', False), ('', False), (None, False)])
    def test_the_shared_rule(self, lh, value, needs):
        assert lh.autonomy_needs_user(value) is needs

    def test_the_waiting_states_are_exactly_the_three_the_enum_names(self, lh):
        assert lh.ACTION_STATES_AWAITING_USER == frozenset({
            lh.ActionState.PENDING, lh.ActionState.FALLBACK_REQUESTED,
            lh.ActionState.PREVIEW_PENDING})


class TestAFinishedTurnStillUsesTheSuccessDoor:
    """ANTI-VACUITY: a fix that always hedges would destroy the honest case
    (test_reuse_recovers_the_written_answer's three bullet points)."""

    def test_every_action_finished_recovers_the_written_answer(
            self, rr, lh, monkeypatch):
        # Pointer past the end: _advance_reuse_action moved it there only
        # after the last action's evidence-backed TERMINATED.
        posted, chat, rec = _run(rr, lh, monkeypatch, _Task(5),
                                 lh.ActionState.TERMINATED)
        assert rec.messages == [] and posted is False
        assert chat.messages[-1].get('content') == PROMISE

    def test_every_action_finished_with_nothing_written_asks_for_the_answer(
            self, rr, lh, monkeypatch):
        monkeypatch.setattr(rr, '_reuse_outstanding_tools',
                            lambda *a, **k: [], raising=True)
        monkeypatch.setitem(rr.user_tasks, SESSION, _Task(5))
        chat = _Chat([dict(DISPATCH),
                      {'role': 'user', 'name': 'StatusVerifier',
                       'content': VERDICT}])
        rec = _Recorder()
        rr._reuse_synthesis_turn(SESSION, chat, _Manager(), rec)
        assert rec.messages == [rr._REUSE_SYNTHESIS_STEER]
