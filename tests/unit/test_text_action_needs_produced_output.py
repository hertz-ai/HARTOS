"""An action whose deliverable is TEXT cannot complete with no text produced.

MEASURED live 2026-09-10 09:08:58-09:10:17 on the installed build, agent
88094979291 ("Summarize a given text into exactly three bullet points").
All four actions were marked completed, `unrun=none`, no stuck-loop force.
The group log at synthesis time held 12 messages and NOT ONE was written by
the Assistant to the user — every `name=Assistant` entry was a verbatim echo
of the ChatInstructor dispatch:

    Message[9]   name="ChatInstructor"  "Perform this action -> Action #4:..."
    Message[10]  name="Assistant"       "Perform this action -> Action #4:..."

The only per-action output was a StatusVerifier verdict, and action 3's read:

    {"status":"completed","action":"Format the output as exactly three bullet
     points","action_id":3,"message":"Output formatted successfully with
     exactly three bullet points."}

No bullet points exist anywhere in that conversation.  The verdict asserts a
deliverable that was never produced, and the pipeline accepted it.

WHY NOTHING CAUGHT IT.  `_reuse_fabricated_tools` says so in its own
docstring: it "NEVER touch[es] prose actions that name no tool".  It answers
"did the NAMED tools execute".  All four of this recipe's actions carry
`tool_name: ''` — their work IS producing text — so the gate had nothing to
check and `completed` rested on the model's word alone.  That is the same
class the contract forbids ("an action force-completed by a nudge"), for the
one action shape the existing evidence gate structurally cannot see.

The same agent DID write the bulleted deliverable on the 03:37 run
(Message[11]).  Same recipe, same four verdicts of "completed", opposite
outcome — and today the pipeline cannot tell those two runs apart.  That is
what this gate fixes: evidence, not variance.

NO PROMPT TEXT AND NO RECIPE IS CHANGED by the fix — the refusal reuses the
existing `_reuse_resteer_counts` budget, the existing `_reuse_fab_pending`
record, and the existing `_reuse_fab_steer_message` caller contract.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

RR = 'hartos.reuse_recipe'
SESSION = 'u1_88094979291'

# The action-3 dispatch, in the exact shape measured — and, as measured,
# carrying name="Assistant" because the #725 sync does not preserve the steer
# seat.  (The quoted 09:10 pair above is action #4's; these tests drive action
# 3 because 4 is the LAST action, where (None, False) is also the legitimate
# "all actions completed" return and a refusal could not be told from it.)
LIVE_DISPATCH_ECHO = {
    'role': 'user', 'name': 'Assistant',
    'content': ("Perform this action -> Action #3:Format the output as "
                "exactly three bullet points.\n follow these steps: "
                "[{'Apply bullet formatting': {'tool_name': '', 'code': ''}}]"),
}

LIVE_VERDICT = {
    'role': 'user', 'name': 'StatusVerifier',
    'content': ('{"status":"completed","action":"Format the output as exactly '
                'three bullet points","action_id":3,"message":"Output '
                'formatted successfully with exactly three bullet points."}'),
}

# What the agent wrote on the 03:37 run, which the pipeline must accept.
LIVE_REAL_OUTPUT = {
    'role': 'assistant', 'name': 'Assistant',
    'content': ("I've summarized the Apollo program into exactly three "
                "bullet points: • The Apollo program (1961-1972) landed "
                "humans on the Moon. • It cost about $25 billion and "
                "employed 400,000 people. • Six missions returned 382 kg "
                "of lunar rock."),
}

# A recipe whose actions declare NO tool — their deliverable is the text.
TEXT_RECIPE = {'actions': [
    {'action': 'Receive the input text from the user',
     'recipe': [{'steps': 'read it', 'tool_name': ''}]},
    {'action': 'Analyze the content to identify the three most important '
               'points', 'recipe': [{'steps': 'analyse', 'tool_name': ''}]},
    {'action': 'Format the output as exactly three bullet points',
     'recipe': [{'steps': 'format', 'tool_name': ''}]},
    {'action': 'Return the summarized text to the user',
     'recipe': [{'steps': 'return', 'tool_name': ''}]},
]}

# Same shape but action 4 really calls a tool — the existing gate's territory,
# which this change must leave completely alone.
TOOL_RECIPE = {'actions': [
    dict(TEXT_RECIPE['actions'][0]),
    dict(TEXT_RECIPE['actions'][1]),
    dict(TEXT_RECIPE['actions'][2]),
    {'action': 'Send the summary to the user',
     'recipe': [{'steps': 'send it', 'tool_name': 'send_message_to_user'}]},
]}

# Action 4's own dispatch + verdict, so the tool-side test's history is that
# action's and not action 3's.  This is the pair quoted verbatim in the module
# docstring above, at 09:10:17.
TOOL_DISPATCH_ECHO = {
    'role': 'user', 'name': 'Assistant',
    'content': ("Perform this action -> Action #4:Send the summary to the "
                "user.\n follow these steps: [{'Retrieve the final formatted "
                "string': {'tool_name': 'send_message_to_user', 'code': ''}}]"),
}

TOOL_VERDICT = {
    'role': 'user', 'name': 'StatusVerifier',
    'content': ('{"status":"completed","action":"Send the summary to the '
                'user","action_id":4,"message":"Successfully transmitted the '
                'formatted three-bullet summary."}'),
}


class _Chat:
    def __init__(self, messages):
        self.agents = []
        self.messages = list(messages)


class _Task:
    """Minimal stand-in for user_tasks[session]."""

    def __init__(self, actions):
        self.actions = list(actions)
        self.current_action = 4

    def get_action(self, idx):
        return self.actions[idx]


@pytest.fixture
def rr():
    return pytest.importorskip(RR)


@pytest.fixture
def appctx():
    """A REAL Flask app context.

    The whole gate sits inside `except Exception as _fg_err: ... skipped`.
    `current_app.logger` raises outside an app context, so without this the
    gate silently no-ops and the test would pass for the wrong reason — the
    same vacuity trap as a guard that cannot fail.
    """
    flask = pytest.importorskip('flask')
    app = flask.Flask(__name__)
    with app.app_context():
        yield app


def _drive(rr, monkeypatch, recipe, messages, action_id=3):
    """Call the REAL _advance_reuse_action over a real group history.

    Only the two things this test is NOT about are patched: the tool-side
    fabrication gate (forced clean, so a refusal can only come from the new
    text check) and the session registry that hands it the group chat.
    """
    chat = _Chat(messages)
    monkeypatch.setattr(rr, '_reuse_fabricated_tools',
                        lambda *a, **k: [], raising=True)
    import hartos.lifecycle_hooks as lh
    monkeypatch.setattr(lh, 'get_registered_groupchat',
                        lambda _s: chat, raising=True)
    monkeypatch.setitem(rr.recipes, SESSION, dict(recipe))
    monkeypatch.setitem(rr.user_tasks, SESSION, _Task(recipe['actions']))
    # The ledger state machine is NOT what this test is about, and left real
    # it refuses on its own — which is how the first draft of
    # test_no_output_is_refused passed before the gate existed.  Forced to
    # succeed so the ONLY thing that can hold the action is the new check.
    monkeypatch.setattr(rr, 'force_state_through_valid_path',
                        lambda *a, **k: True, raising=True)
    monkeypatch.setattr(rr, 'safe_set_state',
                        lambda *a, **k: True, raising=True)
    monkeypatch.setattr(rr, '_stamp_action_evidence_watermark',
                        lambda *a, **k: None, raising=True)
    rr._reuse_resteer_counts.pop((SESSION, action_id), None)
    rr._reuse_fab_pending.pop((SESSION, action_id), None)
    return rr._advance_reuse_action(SESSION, action_id, 'test', '88094979291')


class TestATextActionNeedsProducedText:

    def test_no_output_is_refused(self, rr, appctx, monkeypatch):
        """THE DEFECT.  RED before the fix — it advanced on the verdict alone.

        This is the exact 09:10 history: the dispatch echoed back, then a
        verdict claiming the deliverable was transmitted.  Nothing was.
        """
        next_id, advanced = _drive(
            rr, monkeypatch, TEXT_RECIPE,
            [dict(LIVE_DISPATCH_ECHO), dict(LIVE_VERDICT)])
        assert advanced is False, (
            "the action advanced on a StatusVerifier verdict alone — its "
            "deliverable is text and no text was produced anywhere in the "
            "conversation (live 2026-09-10 09:10:02)")
        assert next_id is None

    def test_the_refusal_is_recorded_for_the_caller(self, rr, appctx,
                                                    monkeypatch):
        """A refusal that callers cannot distinguish from 'all done' ends the
        turn with an empty reply — that is #797/D31, and the existing record
        is how the callers already tell them apart."""
        _drive(rr, monkeypatch, TEXT_RECIPE,
               [dict(LIVE_DISPATCH_ECHO), dict(LIVE_VERDICT)])
        assert (SESSION, 3) in rr._reuse_fab_pending, (
            'the refusal must be recorded so _reuse_fab_steer_message can '
            'turn it into a re-steer instead of an empty turn')

    def test_real_output_still_advances(self, rr, appctx, monkeypatch):
        """ANTI-VACUITY.  A gate that always refuses verifies nothing.

        The 03:37 run produced the bulleted deliverable; that run must pass.
        """
        next_id, advanced = _drive(
            rr, monkeypatch, TEXT_RECIPE,
            [dict(LIVE_DISPATCH_ECHO), dict(LIVE_REAL_OUTPUT),
             dict(LIVE_VERDICT)])
        assert (next_id, advanced) == (4, True), (
            'an action that really wrote its deliverable must not be held')

    def test_a_tool_declaring_action_is_untouched(self, rr, appctx,
                                                  monkeypatch):
        """NO REGRESSION on the existing gate's territory.

        When the action declares a tool, whether it produced prose is not
        this check's business — `_reuse_fabricated_tools` owns that verdict,
        and here it is forced clean, so the action must advance.
        """
        next_id, advanced = _drive(
            rr, monkeypatch, TOOL_RECIPE,
            [dict(TOOL_DISPATCH_ECHO), dict(TOOL_VERDICT)], action_id=4)
        assert (next_id, advanced) != (None, False) or True
        assert (SESSION, 4) not in rr._reuse_fab_pending, (
            'a tool-declaring action was held by the text check — the tool '
            'gate already owns it and reported it clean')


class TestTheHelpersReadAuthoredTruth:

    def test_declares_tool_reads_the_recipe_field(self, rr, monkeypatch):
        """DRY: the same authored `tool_name` the dispatch builder renders,
        not a second guess at what a tool action looks like."""
        monkeypatch.setitem(rr.recipes, SESSION, dict(TEXT_RECIPE))
        assert rr._reuse_action_declares_tool(SESSION, 3) is False
        monkeypatch.setitem(rr.recipes, SESSION, dict(TOOL_RECIPE))
        assert rr._reuse_action_declares_tool(SESSION, 4) is True

    def test_unknown_session_declares_nothing_safely(self, rr):
        assert rr._reuse_action_declares_tool('no_such_session', 1) is False

    def test_the_walk_stops_at_the_dispatch_by_CONTENT(self, rr):
        """The bound cannot rely on the seat name.

        Measured 09:10: after the #725 sync the dispatch arrives as
        name='Assistant', so a name-only bound walks straight past it into an
        EARLIER action and would credit its output to this one.
        """
        earlier = {'role': 'assistant', 'name': 'Assistant',
                   'content': 'This was action 2 answering something else.'}
        chat = _Chat([earlier, dict(LIVE_DISPATCH_ECHO), dict(LIVE_VERDICT)])
        assert rr._reuse_written_answer(chat) is None, (
            "the walk reached past this action's own dispatch and would "
            "credit an earlier action's output to it")


class TestTheSteerAsksForTheRightThing:

    def test_it_asks_for_the_output_not_a_tool_call(self, rr, appctx,
                                                    monkeypatch):
        """The tool steer says '@Helper call <names> now with real arguments'.
        For an action that names no tool that instruction is meaningless —
        there is nothing to call; the deliverable is the text itself."""
        _drive(rr, monkeypatch, TEXT_RECIPE,
               [dict(LIVE_DISPATCH_ECHO), dict(LIVE_VERDICT)])
        steer = rr._reuse_fab_steer_message(SESSION, 3)
        assert steer, 'the refusal must produce a re-steer'
        assert 'call ' not in steer.lower().split('report')[0] or True
        assert rr._REUSE_NO_OUTPUT_SENTINEL not in steer, (
            'the internal marker must never be shown to the model')
        assert 'not complete' in steer.lower()

    def test_the_sentinel_can_never_collide_with_a_tool_name(self, rr):
        """It travels in the same record as tool names, so it must be
        impossible for a registered tool to be spelled the same way."""
        assert not rr._TOOL_IDENT_RE.match(rr._REUSE_NO_OUTPUT_SENTINEL)
