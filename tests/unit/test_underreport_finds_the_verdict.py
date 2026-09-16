"""The under-report escape must find the verdict, which is never at [-1].

reuse_recipe.py:3991 is the documented MIRROR of the fabrication gate: "the model
says pending but this action's tools already executed -> steer twice, then advance
on the tool evidence".  It has fired ZERO times -- 0 in the 2026-09-08 drive and 0
across every retained log.  A remedy that has never once run.

WHY, measured live 2026-09-08 22:18-22:43 (agent 89555447799, action 4):

  its precondition is
      _pend = group_chat.messages[-1]
      _pend_vj = retrieve_json(_pend['content'])
      if isinstance(_pend_vj, dict) and _pend_vj['status'] in {pending} ...

  and messages[-1] is structurally NOT a verdict.  state_transition logs what it
  sees there: of 128 calls in the wedge, 120 saw "You should" -- the ChatInstructor
  nudge.  The sync explains it:

    [725-SYNC-COMPOSITION] (*=picked)
      User*:n=240,calls=100,answers=0 | Assistant:n=120,answers=0 |
      Helper:n=240,answers=0 | multi_role_agent:n=240,answers=0 |
      Executor:n=240,answers=0 | ChatInstructor:n=240,answers=0 |
      StatusVerifier:n=240,answers=0 | Assistant:n=120,calls=42,answers=30

  The sync picks the LONGEST buffer (User, n=240); the only buffer holding tool
  answers (answers=30) is half its length and is never picked.  The picked buffer
  ends with the steering nudge, so [-1] is the nudge by construction (#789/D22).

  Meanwhile the verdicts DO exist -- 72 parsed in the same window -- just never at
  that one position.  Cost: 40 action-4 verdicts, 0 'completed', a 23-minute wedge,
  the round budget drained (rounds 4 -> 85), the user's /chat timing out at 1800s
  with no reply for work the machine had actually done, and ~8 browser windows
  opened by the retry loop.

TWO DESIGNS WERE REJECTED ON MEASUREMENT, NOT TASTE:

  (a) scope the found verdict by its action_id.  MEASURED over 72 verdicts:
          Action #4  action_id=4   x40
          Action #4  action_id=24  x25   <- 38% mismatch
      25 of 65 carry action_id=24 -- the recipe's TOTAL action count -- while their
      own text correctly reads "Action #4".  Filtering on that integer would drop
      38% of genuine verdicts and could credit them to action 24.  The file already
      treats the model's id as advisory (_advance_or_steer's claimed_action_id and
      its [HALLUCINATION?] log); this follows that, and reads the pipeline's own
      current_action instead.

  (b) stamp a content-hash watermark of verdicts present at dispatch, mirroring
      D28's evidence_seen_call_ids.  MEASURED: the verdicts are textually
      IDENTICAL -- 28 exact copies of one, 25 of another.  A hash set cannot tell
      the second from the first, so the watermark would block every real verdict
      and reproduce the vacuity it was meant to cure.

WHY NO NEW SCOPING IS NEEDED AT ALL.  The feared failure of a wider scan is: action
M is current, a stale 'pending' from an earlier action N still sits in the tail, and
the escape advances M on someone else's verdict.  Condition 5 already forecloses it
-- it asks whether M's OWN tools ran since M was dispatched, and D28
(209478af5) made exactly that question per-action correct.  If M's tools have not
run, cond 5 blocks regardless of whose verdict was found.  If they have run, then
advancing M after two steers on M's own tool evidence is precisely the designed
behaviour.  So the verdict only has to be FINDABLE; the scoping is already there.
That keeps this to one helper and no new state -- the minimal, reuse-existing fix.

    python -m pytest tests/unit/test_underreport_finds_the_verdict.py --noconftest -q
"""
from types import SimpleNamespace

import pytest

from hartos.reuse_recipe import _REUSE_VERDICT_TAIL_SCAN, _reuse_latest_verdict


NUDGE = {'name': 'ChatInstructor', 'content':
         'You should complete this task independently.'}


def _verdict(status='pending', n=4, action_id=None, msg='Action is pending.'):
    """A StatusVerifier message in the exact live shape."""
    aid = n if action_id is None else action_id
    return {'name': 'StatusVerifier', 'content':
            ("{'status': '%s', 'action': 'Action #%d: Open LinkedIn in the "
             "default web browser', 'action_id': %d, 'message': '%s'}"
             % (status, n, aid, msg))}


def _gc(messages):
    return SimpleNamespace(messages=list(messages), agents=[])


class TestItFindsTheVerdictOffTheLastPosition:

    def test_the_live_shape_nudge_at_minus_one_verdict_behind_it(self):
        """THE DEFECT: 120 of 128 reads saw the nudge at [-1] and gave up."""
        gc = _gc([{'name': 'Helper', 'content': 'working'},
                  _verdict('pending'),
                  {'name': 'Assistant', 'content': 'ok'},
                  NUDGE])
        got = _reuse_latest_verdict(gc)
        assert isinstance(got, dict), (
            "the verdict sits behind the nudge; reading only [-1] is why this "
            "escape has fired 0 times in every retained log")
        assert got['status'] == 'pending'

    def test_a_verdict_still_at_minus_one_is_found(self):
        """Non-regression: the old position must keep working."""
        got = _reuse_latest_verdict(_gc([NUDGE, _verdict('pending')]))
        assert got and got['status'] == 'pending'

    def test_the_most_recent_verdict_wins(self):
        """Older verdicts linger under clear_history=False; take the newest."""
        gc = _gc([_verdict('error', msg='old failure'),
                  _verdict('pending', msg='newer'),
                  NUDGE])
        got = _reuse_latest_verdict(gc)
        assert got['message'] == 'newer'

    def test_the_hallucinated_action_id_is_not_filtered_on(self):
        """38% of live verdicts carry action_id=24 for action #4."""
        got = _reuse_latest_verdict(_gc([_verdict('pending', n=4, action_id=24),
                                         NUDGE]))
        assert got is not None, (
            "a verdict whose action_id contradicts its own text is still a real "
            "verdict -- 25 of 65 measured live; discarding them re-breaks this")
        assert got['status'] == 'pending'


class TestItIsBounded:

    def test_it_does_not_scan_the_whole_session(self):
        """clear_history=False makes these lists thousands long."""
        old = [_verdict('pending', msg='ancient')]
        filler = [NUDGE] * (_REUSE_VERDICT_TAIL_SCAN + 5)
        assert _reuse_latest_verdict(_gc(old + filler)) is None, (
            "a verdict older than the scan window must not be resurrected")

    def test_the_bound_is_a_named_constant_not_a_literal(self):
        assert isinstance(_REUSE_VERDICT_TAIL_SCAN, int)
        assert _REUSE_VERDICT_TAIL_SCAN > 1


class TestItNeverRaises:
    """Runs inside the live loop; a bad message must not kill the turn."""

    @pytest.mark.parametrize("messages", [
        [], [None], [{'name': 'x'}], [{'content': None}],
        [{'content': 'not json at all'}], [{'content': '{"no": "status"}'}],
        [{'content': '[1,2,3]'}],
    ])
    def test_junk_returns_none_not_an_exception(self, messages):
        assert _reuse_latest_verdict(_gc(messages)) is None

    def test_a_group_chat_without_messages_is_survivable(self):
        assert _reuse_latest_verdict(SimpleNamespace()) is None
        assert _reuse_latest_verdict(None) is None
