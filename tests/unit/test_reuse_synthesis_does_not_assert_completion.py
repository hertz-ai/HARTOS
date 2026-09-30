"""The synthesis steer must not TELL the model the tools ran when they didn't.

MEASURED LIVE 2026-09-09, drive 08:12:20-08:14:20, agent 33323830039,
installed build. The reply delivered to the user was:

    "The directory change to C:\\Users\\sathi\\Documents has been successfully
     completed. The current working directory is now C:\\Users\\sathi\\Documents."

The pipeline's own evidence for that same action, 39 seconds earlier:

    08:13:41 [FAB-GUARD] action 2 names tool(s)
             ['execute_windows_or_android_command']; executed=[];
             unrun=['execute_windows_or_android_command']
    action 2's VLM run: {'status':'incomplete','exit_reason':'max_iterations'}
    "Action 2 TERMINATED, advancing": 0 occurrences

So the fabrication gate REFUSED the action internally and the turn still told
the user it had succeeded.

THE MODEL WAS NOT HALLUCINATING — IT WAS INSTRUCTED. _REUSE_SYNTHESIS_STEER
opens with a flat assertion:

    "The actions are finished and their tools have already run — do NOT run
     any tool again ... Write the ANSWER for the user now"

and the steer is posted whenever the tail is control JSON, with no reference
to whether anything actually executed. Given that premise, "successfully
completed" is the obedient answer, not an invented one.

WHY THIS ONE MATTERS MOST. Every guard built so far — the fabrication gate,
the under-report escape, the FAB-GUARD watermark — constrains the ADVANCE
decision. None of them constrains the sentence the user reads. An agent can
therefore refuse to advance internally and still report success outwardly,
which makes a live walk unfalsifiable from the user's side. That is precisely
the failure the verification contract exists to prevent.

THE EVIDENCE ALREADY EXISTS, one function below the steer:
``_reuse_outstanding_tools`` is the thin adapter over the fabrication gate's
own predicate ("the action's named tools that have NOT executed"). The fix is
to ask it before asserting anything — no new mechanism, no second notion of
"did the tool run", and no change to the gate itself.

    python -m pytest \
        tests/unit/test_reuse_synthesis_does_not_assert_completion.py \
        --noconftest -q
"""
import pytest


RR = 'hartos.reuse_recipe'

# The exact false premise, verbatim from _REUSE_SYNTHESIS_STEER.
FALSE_PREMISE = 'their tools have already run'


class _Chat:
    """Group chat whose tail is control JSON, so synthesis is needed."""

    def __init__(self):
        self.agents = []
        self.messages = [
            {'role': 'user', 'name': 'ChatInstructor', 'content': 'Perform the action'},
            {'role': 'user', 'name': 'StatusVerifier',
             'content': ("{'status': 'pending', 'action': 'Action #2: cd "
                         "C:\\\\Users\\\\sathi\\\\Documents', 'action_id': 2, "
                         "'message': 'The helper agent needs to perform this command.'}")},
        ]


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


def _run(rr, monkeypatch, outstanding):
    """Drive the REAL _reuse_synthesis_turn; return the steer it posted.

    ``_reuse_outstanding_tools`` is patched rather than simulated end to end:
    that is the canonical predicate the fabrication gate uses, and patching it
    is what proves the steer actually CONSULTS it — if the steer ignores it,
    the patch changes nothing and the false premise survives.
    """
    monkeypatch.setattr(rr, '_reuse_outstanding_tools',
                        lambda *a, **k: list(outstanding), raising=True)
    rec = _Recorder()
    posted = rr._reuse_synthesis_turn('sess_1', _Chat(), _Manager(), rec)
    assert posted is True, 'the synthesis steer did not fire at all'
    assert rec.messages, 'no steer was posted'
    return rec.messages[-1]


class TestSteerTellsTheTruthAboutWhatRan:

    def test_does_not_claim_tools_ran_when_they_did_not(self, rr, monkeypatch):
        """THE DEFECT. RED before the fix.

        With a tool outstanding, the steer must not assert that the tools have
        already run — that assertion is what produced the live
        "has been successfully completed" over an unrun tool.
        """
        steer = _run(rr, monkeypatch,
                     outstanding=['execute_windows_or_android_command'])
        assert FALSE_PREMISE not in steer.lower(), (
            "the synthesis steer still tells the model "
            f"{FALSE_PREMISE!r} while the fabrication gate reports the tool as "
            "unrun — the model then reports success it was instructed to "
            "report (measured live 08:14:20)")

    def test_names_what_did_not_run(self, rr, monkeypatch):
        """The model cannot report honestly about something it isn't told."""
        steer = _run(rr, monkeypatch,
                     outstanding=['execute_windows_or_android_command'])
        assert 'execute_windows_or_android_command' in steer, (
            'the steer must name the tool(s) that did NOT execute so the '
            'answer can say so')

    def test_still_asserts_completion_when_everything_ran(self, rr, monkeypatch):
        """ANTI-VACUITY: do not make every reply hedge.

        When the gate reports nothing outstanding, the tools really did run and
        the original steer is correct. A fix that always hedges would degrade
        the honest case and hide real completions behind weasel words.
        """
        steer = _run(rr, monkeypatch, outstanding=[])
        assert FALSE_PREMISE in steer.lower(), (
            'with no outstanding tools the steer should still state plainly '
            'that the tools ran — that is true, and the answer should not '
            'hedge about work that really happened')

    def test_both_variants_still_request_the_same_key(self, rr, monkeypatch):
        """Whatever it says, the extractor must still be able to unwrap it.

        The reply is read back by the existing message2userfinal extractor; a
        steer that asks for a different shape would produce an answer nobody
        reads (#797/D31 is that failure).
        """
        for outstanding in ([], ['execute_windows_or_android_command']):
            steer = _run(rr, monkeypatch, outstanding=outstanding)
            assert 'message2userfinal' in steer, (
                f'steer for outstanding={outstanding!r} does not ask for '
                'message2userfinal, so the extractor cannot unwrap the answer')

    def test_uncertainty_is_treated_as_outstanding(self, rr, monkeypatch):
        """`_reuse_outstanding_tools` returns ['<unknown>'] when it cannot tell.

        That sentinel exists so uncertainty blocks an advance. It must equally
        block a completion CLAIM — asserting success on an unmeasurable state
        is the same defect wearing a different hat.
        """
        steer = _run(rr, monkeypatch, outstanding=['<unknown>'])
        assert FALSE_PREMISE not in steer.lower(), (
            'the unmeasurable case still asserts the tools ran')

    def test_consults_the_canonical_predicate_not_a_new_one(self, rr):
        """One notion of "did the tool run", shared with the gate."""
        import inspect
        src = inspect.getsource(rr._reuse_synthesis_turn)
        assert '_reuse_outstanding_tools' in src, (
            '_reuse_synthesis_turn must ask the fabrication gate\'s own '
            'predicate; a second rule here would drift from the gate')
