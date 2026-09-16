"""The answer the action already wrote must not be thrown away and rewritten.

MEASURED live 2026-09-10 03:36:19-03:37:39 on the installed build, agent
88094979291, goal "Summarize a given text into exactly three bullet points."
All four actions ran and completed; the group history at synthesis time was:

    Message[10]  user      ChatInstructor   "Perform this action -> Action #4:
                                             Return the summarized text ..."
    Message[11]  assistant Assistant        "Hey there! ... I've successfully
                                             summarized ... into exactly three
                                             bullet points for you: • ... • ...
                                             • ..."
    Message[12]  user      StatusVerifier   {"status": "completed",
                                             "action_id": 4, ...}

`messages[-1]` is the verdict, so `_reuse_needs_synthesis` fired
(`[SYNTHESIS] ... 21 msgs, unrun=none`) and the steer asked the group to write
the answer "in your own words".  The round returned prose:

    "The Apollo program, a massive NASA initiative running from 1961 to 1972,
     successfully landed humans on the Moon in July 1969 ..."

426 characters, ZERO bullets — read back from conversation_entries rowid
206313, which is what the user saw.  The agent's whole goal is the format, and
the synthesis round is what destroyed it: the deliverable was already written
one message back.

This does NOT contradict the 2026-09-09 finding in
test_reuse_answers_not_control_json.py ("walking back cannot fix it").  That
turn's closing history was tool traffic only — nothing to recover — so it
still needs the steer, and `test_asks_when_the_action_wrote_nothing` below
pins that.  Recovery is an EXTRA path that fires only when the answer is
demonstrably there, never a replacement for the steer.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

RR = 'hartos.reuse_recipe'

# Message[11] verbatim in shape: the deliverable, in the agent's own voice,
# carrying the three bullets its recipe was authored to produce.
LIVE_WRITTEN_ANSWER = (
    "Hey there! I'm summarize.local.trio, and I'm genuinely excited to work "
    "with you on this. I've successfully summarized the Apollo program "
    "information into exactly three bullet points for you: "
    "• The Apollo program (1961–1972) was NASA's flagship mission to "
    "land humans on the Moon, culminating in the historic July 1969 landing by "
    "Apollo 11. "
    "• The program cost approximately $25 billion and employed over "
    "400,000 people. "
    "• Six missions landed successfully, returning 382 kilograms of lunar "
    "rock."
)

# Message[12] verbatim in shape: what the extractor would have handed over.
LIVE_ACTION_VERDICT = (
    '{"status": "completed", "action": "Return the summarized text to the '
    'user", "action_id": 4, "message": "Successfully transmitted the '
    'formatted three-bullet summary to the user interface."}'
)

# Message[10] verbatim in shape: the dispatch that opened action 4.  Its
# `name` is what bounds the walk-back to THIS action.
LIVE_ACTION_DISPATCH = {
    'role': 'user', 'name': 'ChatInstructor',
    'content': ("Perform this action -> Action #4:Return the summarized text "
                "to the user.\n follow these steps: [{'Retrieve the final "
                "formatted string containing exactly three bullet points': "
                "{'tool_name': '', 'code': ''}}]"),
}


def _live_history():
    """The 2026-09-10 03:37:33 closing history, in order."""
    return [
        {'role': 'user', 'name': 'StatusVerifier',
         'content': '{"status": "completed", "action_id": 3, "message": "ok"}'},
        dict(LIVE_ACTION_DISPATCH),
        {'role': 'assistant', 'name': 'Assistant',
         'content': LIVE_WRITTEN_ANSWER},
        {'role': 'user', 'name': 'StatusVerifier',
         'content': LIVE_ACTION_VERDICT},
    ]


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
    return pytest.importorskip(RR)


def _run(rr, monkeypatch, messages, outstanding=()):
    """Drive the REAL _reuse_synthesis_turn over `messages`.

    Returns (posted, chat).  `_reuse_outstanding_tools` is patched the same
    way its sibling suite patches it — that is the fabrication gate's own
    predicate and the only thing that decides which steer is legal.
    """
    monkeypatch.setattr(rr, '_reuse_outstanding_tools',
                        lambda *a, **k: list(outstanding), raising=True)
    chat = _Chat(messages)
    rec = _Recorder()
    posted = rr._reuse_synthesis_turn('sess_1', chat, _Manager(), rec)
    return posted, chat, rec


class TestTheWrittenAnswerSurvives:

    def test_no_steer_when_the_action_already_wrote_the_answer(
            self, rr, monkeypatch):
        """THE DEFECT.  RED before the fix — the steer fires and rewrites.

        Nothing is outstanding (live: `unrun=none`), so the answer sitting one
        message back is the finished deliverable.  Asking for it again spends
        a round AND, because the steer says "in your own words", loses whatever
        format the recipe was authored to produce.
        """
        posted, _chat, rec = _run(rr, monkeypatch, _live_history())
        assert rec.messages == [], (
            "a synthesis round was posted even though action 4 had already "
            "written the answer one message back — that round is what "
            "replaced three bullet points with 426 characters of prose "
            "(live 2026-09-10 03:37:33)")
        assert posted is False

    def test_the_recovered_answer_is_what_the_extractor_reads(
            self, rr, monkeypatch):
        """The extractor reads messages[-1]; recovery has to reach it there.

        Recovering into a variable nobody reads would be a fix that changes
        nothing — the same shape as the #797/D31 empty return.
        """
        _posted, chat, _rec = _run(rr, monkeypatch, _live_history())
        tail = chat.messages[-1].get('content') or ''
        assert tail == LIVE_WRITTEN_ANSWER, (
            "the finished answer is not what the extractor will pick up; it "
            f"would still hand over {tail[:80]!r}")
        assert tail.count('•') == 3, (
            'the recovered deliverable must still carry the agent\'s three '
            'bullet points')

    def test_the_history_is_not_mutated_by_the_extractor(
            self, rr, monkeypatch):
        """The extractor edits `last_message['content']` IN PLACE (strips
        '@user ').  Handing it the original dict would rewrite history.

        Asserting only "tail is not messages[-2]" would pass BEFORE the fix
        too — pre-fix the tail is the verdict, trivially a different object.
        So this pins the property on the recovered tail specifically: it
        carries the answer AND is a distinct object from every earlier message
        that carries it.
        """
        _posted, chat, _rec = _run(rr, monkeypatch, _live_history())
        tail = chat.messages[-1]
        assert (tail.get('content') or '') == LIVE_WRITTEN_ANSWER, (
            'nothing was recovered, so this guard would be vacuous')
        originals = [m for m in chat.messages[:-1]
                     if (m.get('content') or '') == LIVE_WRITTEN_ANSWER]
        assert originals, 'the source message vanished from the history'
        assert all(tail is not m for m in originals), (
            'the recovered tail is the same object as the message it came '
            'from; the extractor would edit the historical record')


class TestRecoveryNeverReplacesTheSteer:

    def test_asks_when_the_action_wrote_nothing(self, rr, monkeypatch):
        """The 2026-09-09 33323830039 shape: tool traffic, no prose.

        There is nothing to recover, so the steer must still fire.  This is
        the regression guard for the finding that motivated the steer.
        """
        messages = [
            dict(LIVE_ACTION_DISPATCH),
            {'role': 'assistant', 'name': 'Assistant', 'content': '',
             'tool_calls': [{'id': 'c1'}]},
            {'role': 'tool', 'name': 'Executor', 'content': 'Volume in drive C'},
            {'role': 'user', 'name': 'StatusVerifier',
             'content': LIVE_ACTION_VERDICT},
        ]
        _posted, _chat, rec = _run(rr, monkeypatch, messages)
        assert rec.messages, (
            'no steer was posted for a history that holds no written answer '
            '— the user would get the raw verdict (#799/D33)')

    def test_never_reaches_back_past_this_action(self, rr, monkeypatch):
        """An answer written BEFORE this action's dispatch belongs to another
        action (or another turn — the group log is not cleared between them).
        Delivering it would answer the wrong question."""
        messages = [
            {'role': 'assistant', 'name': 'Assistant',
             'content': 'Here is the answer to your PREVIOUS question.'},
            dict(LIVE_ACTION_DISPATCH),
            {'role': 'user', 'name': 'StatusVerifier',
             'content': LIVE_ACTION_VERDICT},
        ]
        _posted, chat, rec = _run(rr, monkeypatch, messages)
        assert rec.messages, (
            "recovery crossed this action's dispatch and would have "
            "delivered an older action's answer")
        assert 'PREVIOUS' not in (chat.messages[-1].get('content') or '')

    def test_never_recovers_over_an_unrun_tool(self, rr, monkeypatch):
        """D42/#808: when the fabrication gate reports a tool as unrun, the
        user must be TOLD.  A recovered message claiming success would put
        that regression straight back."""
        _posted, _chat, rec = _run(
            rr, monkeypatch, _live_history(),
            outstanding=['execute_windows_or_android_command'])
        assert rec.messages, (
            'recovered an answer that asserts success while the fabrication '
            'gate reports a tool as unrun — the honest-report fix (#808) is '
            'undone')
        assert 'execute_windows_or_android_command' in rec.messages[-1]

    def test_a_control_json_tail_alone_is_still_not_recoverable(
            self, rr, monkeypatch):
        """Anti-vacuity: recovery must judge SHAPE, not position.  A history
        whose only non-dispatch message is control JSON has no answer in it."""
        messages = [
            dict(LIVE_ACTION_DISPATCH),
            {'role': 'user', 'name': 'StatusVerifier',
             'content': '{"status": "pending", "action_id": 4}'},
            {'role': 'user', 'name': 'StatusVerifier',
             'content': LIVE_ACTION_VERDICT},
        ]
        _posted, _chat, rec = _run(rr, monkeypatch, messages)
        assert rec.messages, 'control JSON was treated as a written answer'


class TestThePipelinesOwnTextIsNeverTheAnswer:
    """MEASURED live 2026-09-10 04:42:04 — the first fix's own regression.

    The recovery fired and delivered THIS to the user, verbatim:

        [SYNTHESIS] the action already wrote the answer — recovered 391 chars
        LangChain local response: Perform this action -> Action #4:Return the
        summarized text to the user.

    The walk-back was bounded on `name in _REUSE_STEER_INITIATOR_NAMES`, and
    in `group_chat.messages` after the #725 sync that dispatch does NOT carry
    name='ChatInstructor' — the sync slice-assigns the longest per-agent
    buffer from `manager._oai_messages`, whose `name` fields are that pair's
    view.  The 04:42:02 structure dump shows the pattern the bound assumed
    (`[9] role=user, name=ChatInstructor`), but the list actually walked was
    the 12-entry buffer resynced two seconds later, and there the same text
    passed the shape test as ordinary prose.

    So the bound has to be keyed on the PRODUCER, not on a seat name that the
    buffer may not preserve: `_build_reuse_action_message` always emits
    `_REUSE_ACTION_MESSAGE_PREFIX`.  One constant, used by the producer and
    the recogniser, so rewording the steer moves both at once — which is what
    the seat-name comment asked for and the name field could not deliver.

    This also closes the same hole in the PRE-EXISTING gate: the
    2026-09-09 08:53:22 defect (a dispatch delivered verbatim as the answer)
    was only caught when the name survived.
    """

    LIVE_DISPATCH_UNNAMED = {
        'role': 'user', 'name': 'Assistant',
        'content': ("Perform this action -> Action #4:Return the summarized "
                    "text to the user.\n follow these steps: [{'Retrieve the "
                    "final formatted string': {'tool_name': '', 'code': ''}}]"),
    }

    def test_an_action_dispatch_is_not_an_answer_whatever_its_name(self, rr):
        """THE REGRESSION.  RED before the second fix."""
        assert rr._reuse_message_is_user_answer(
            dict(self.LIVE_DISPATCH_UNNAMED)) is False, (
            "the pipeline's own action dispatch was classified as prose for "
            "the user — this is the 391-char reply the user received at "
            "04:42:04")

    def test_recovery_does_not_deliver_the_dispatch(self, rr, monkeypatch):
        """End to end over the live shape: nothing recoverable, so steer."""
        messages = [
            dict(self.LIVE_DISPATCH_UNNAMED),
            {'role': 'user', 'name': 'StatusVerifier',
             'content': LIVE_ACTION_VERDICT},
        ]
        _posted, chat, rec = _run(rr, monkeypatch, messages)
        tail = chat.messages[-1].get('content') or ''
        assert 'Perform this action ->' not in tail, (
            'the action dispatch reached the extractor as the reply')
        assert rec.messages, (
            'nothing was recoverable, so the synthesis steer had to fire')

    def test_the_prefix_has_one_definition_shared_with_its_producer(self, rr):
        """DRY: the recogniser must read the constant the producer emits."""
        import inspect
        assert rr._REUSE_ACTION_MESSAGE_PREFIX in (
            'Perform this action -> Action #',)
        assert '_REUSE_ACTION_MESSAGE_PREFIX' in inspect.getsource(
            rr._build_reuse_action_message), (
            'the action-message producer must emit the shared constant, not '
            'its own copy of the wording')
        # RE-POINTED 2026-09-10, one function deeper, NOT weakened.  The
        # match moved into `_reuse_is_pipeline_text` when the seed
        # composition ("{user message}\n\n{dispatch}") proved that a
        # `startswith` test cannot see a dispatch the producer put in the
        # middle.  The invariant is the same and is now asserted on BOTH
        # readers rather than one, so a future reader that re-spells the
        # match fails here instead of silently keeping the old hole.
        assert '_REUSE_ACTION_MESSAGE_PREFIX' in inspect.getsource(
            rr._reuse_is_pipeline_text), (
            'the recogniser must read the same constant the producer emits')
        for reader in (rr._reuse_message_is_user_answer,
                       rr._reuse_written_answer):
            assert '_reuse_is_pipeline_text' in inspect.getsource(reader), (
                f'{reader.__name__} must ask the shared recogniser')

    def test_the_producers_real_output_is_rejected(self, rr, monkeypatch):
        """Not a hand-written lookalike — build the string the way the
        pipeline builds it, so a reworded producer fails this test."""
        class _Task:
            current_action = 4

            def get_action(self, i):
                return {'action': 'Return the summarized text to the user.'}

        monkeypatch.setitem(rr.user_tasks, 'sess_x', _Task())
        # Four action slots so the builder takes its normal branch — the
        # short-recipe branch logs through current_app and would fail here
        # for want of a Flask context, not for want of the fix.
        monkeypatch.setitem(rr.recipes, 'sess_x', {'actions': [
            {'recipe': [{'steps': 'step %d' % i, 'tool_name': ''}]}
            for i in range(4)]})
        built = rr._build_reuse_action_message('sess_x', 4)
        assert built.startswith(rr._REUSE_ACTION_MESSAGE_PREFIX)
        assert rr._reuse_message_is_user_answer(
            {'role': 'user', 'name': 'Assistant', 'content': built}) is False


class TestOneNotionOfWhatAnAnswerIs:

    def test_the_gate_and_the_recovery_share_a_predicate(self, rr):
        """DRY.  Two answers to "is this a reply to the user?" would drift the
        moment a new not-an-answer shape is found — and this file's whole
        history is new shapes being found one at a time."""
        import inspect
        src = inspect.getsource(rr._reuse_needs_synthesis)
        assert '_reuse_message_is_user_answer' in src, (
            '_reuse_needs_synthesis must delegate to the shared per-message '
            'predicate instead of carrying its own copy of the shape list')

    def test_the_predicate_accepts_the_live_answer(self, rr):
        assert rr._reuse_message_is_user_answer(
            {'role': 'assistant', 'name': 'Assistant',
             'content': LIVE_WRITTEN_ANSWER}) is True

    def test_the_predicate_rejects_the_live_verdict(self, rr):
        assert rr._reuse_message_is_user_answer(
            {'role': 'user', 'name': 'StatusVerifier',
             'content': LIVE_ACTION_VERDICT}) is False
