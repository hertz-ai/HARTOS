"""An autonomous action whose tools really ran must not strand on 'pending'.

Mirror of the fabrication gate. That gate refuses to advance an action that
CLAIMS completion while its tool never ran. This covers the opposite failure,
measured live 2026-09-05 on Scout2 (prompt 77712340019):

  * google_search really executed - three HTTP 200s (brave / wikipedia /
    grokipedia) at 19:45:24-25, `INSIDE google search` at 19:45:21
  * the agent wrote the correct deliverable from those results
    ("March 2025, UtmoLight ... 18.1% efficiency")
  * it then reported {'status': 'pending', 'action_id': 1}

Every advance site gates on ``status == 'completed'``
(reuse_recipe.py 3306/3336/3354/4038/4052), so none fired, the turn ended on
'@user', and current_action_id never left 1. Five agents in the live sweep sit
at 1/N for this reason.

The danger in fixing it is advancing work that is genuinely unfinished, which
the verification contract calls "force-completed by a nudge". So the branch
must be evidence-gated: it may only proceed when the action's own tools are
evidenced executed, via the fabrication gate's OWN predicate - never a second
notion of "did the tool run".

    python -m pytest tests/unit/test_reuse_under_reported_completion.py --noconftest -q
"""
import ast
import os
import re

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_REUSE_SRC = os.path.join(_ROOT, 'hartos', 'reuse_recipe.py')


def _source():
    with open(_REUSE_SRC, encoding='utf-8') as fh:
        return fh.read()


class TestBranchIsEvidenceGated:
    """The safety property: no advance without proof the tool executed."""

    def test_branch_requires_both_autonomy_and_no_outstanding_tools(self):
        src = _source()
        assert '_reuse_action_is_autonomous(' in src, 'autonomy predicate missing'
        assert '_reuse_outstanding_tools(' in src, 'tool-evidence predicate missing'
        # The guard must NEGATE outstanding tools: advancing while tools are
        # outstanding is exactly the forbidden force-completion.
        assert 'not _reuse_outstanding_tools(' in src, (
            "the under-reported branch must require NO outstanding tools; "
            "without the negation it would advance an action whose tool never ran")

    def test_evidence_predicate_delegates_to_the_fabrication_gate(self):
        """One notion of 'did the tool run', not two."""
        tree = ast.parse(_source())
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)
                   and n.name == '_reuse_outstanding_tools'), None)
        assert fn is not None, '_reuse_outstanding_tools not found'
        called = {c.func.id for c in ast.walk(fn)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert '_reuse_fabricated_tools' in called, (
            'must delegate to the fabrication gate’s own predicate rather than '
            'growing a second, drifting definition of tool execution')

    def test_steering_is_bounded(self):
        rr = pytest.importorskip('hartos.reuse_recipe')
        assert rr._REUSE_PENDING_STEER_MAX >= 1
        assert rr._REUSE_PENDING_STEER_MAX <= rr._REUSE_FAB_STEER_MAX, (
            'under-reported steering should not outlast the fabrication gate’s '
            'own allowance, or a stuck action burns the whole round budget')


class TestRequiresBreakdownIsNotAnUnderReport:
    """'requires_breakdown' must NOT be force-advanced on tool evidence.

    I got this wrong on 2026-09-06 and this class pins the correction.

    Agent 89555447799 wedged: 35 requires_breakdown verdicts for action 1, 107
    rounds, 0 advances.  My first fix added 'requires_breakdown' to the
    under-report set so the action would advance once its named tool was
    evidenced.  That was wrong twice over:

      * SEMANTICS — the model is not under-reporting a completion.  It is
        correctly saying the action needs decomposing, and it SUPPLIES the
        subtasks in the same verdict.
      * BEHAVIOUR — advancing marks the parent done while its own subtasks sit
        unrun, on evidence from a DIFFERENT tool.  That is exactly the
        "force-completed by a stuck-loop guard or a nudge" the verification
        contract excludes.

    The real defect was a missing execution path.  create_recipe.py:4503-4520
    wires the designed flow —
        add_subtasks() -> get_pending_subtasks() -> "Work on subtask: X"
        -> check_and_unblock_parent() -> parent completes
    — while reuse_recipe did step one only (state_transition:2635 persists,
    then returns a speaker) and imported get_pending_subtasks /
    check_and_unblock_parent at :181-182 without ever calling either.  The
    children went into a ledger nobody read.

    Fix = wire the loop, not widen the status set.
    """

    def test_requires_breakdown_is_excluded_from_the_underreport_set(self):
        rr = pytest.importorskip('hartos.reuse_recipe')
        statuses = getattr(rr, '_REUSE_UNDERREPORT_STATUSES', None)
        assert statuses is not None, '_REUSE_UNDERREPORT_STATUSES missing'
        assert 'pending' in statuses, 'pending stays — it is a real under-report'
        assert 'requires_breakdown' not in statuses, (
            "requires_breakdown must NOT be treated as an under-reported "
            'completion: it has its own execution path (subtasks), and '
            'advancing on tool evidence would complete the parent while its '
            'subtasks are unrun')
        assert 'error' not in statuses, 'error reports a failure; advancing buries it'

    def test_reuse_executes_the_breakdown_instead_of_advancing_past_it(self):
        """The loop create has and reuse was missing."""
        src = _source()
        assert 'get_pending_subtasks(' in src, (
            'reuse imports get_pending_subtasks at :181-182 — it must CALL it. '
            'Persisting subtasks into a ledger nothing reads is why action 1 of '
            'agent 89555447799 could never complete')
        m = re.search(r'BREAKDOWN EXECUTION(.*?)except Exception as _bd_err',
                      src, re.DOTALL)
        assert m, 'breakdown-execution block not found in the reuse loop'
        block = m.group(1)
        assert "== 'requires_breakdown'" in block, 'must key on the verdict'
        assert 'get_pending_subtasks(' in block, 'must read the persisted subtasks back'
        assert 'Work on subtask:' in block, (
            'must post the same work message create_recipe.py:4518 posts — one '
            'shape, not a second protocol')
        assert '_advance_or_steer' not in block, (
            'the breakdown path must EXECUTE the decomposition, never advance '
            'past it')

    def test_advance_branch_reads_the_constant_not_a_bare_pending(self):
        src = _source()
        m = re.search(
            r"_pend_vj = retrieve_json\(.*?\n(?P<block>.*?)\):", src, re.DOTALL)
        assert m, 'under-reported advance branch not found'
        block = m.group('block')
        assert "== 'pending'" not in block, (
            "the advance branch still compares status == 'pending' exactly, so "
            "a 'requires_breakdown' verdict cannot advance — that is the "
            'measured 107-round wedge')
        assert '_REUSE_UNDERREPORT_STATUSES' in block, (
            'the branch must test membership in the shared constant so the '
            'status vocabulary has one home')


class TestAutonomyPredicate:

    def _probe(self, action):
        rr = pytest.importorskip('hartos.reuse_recipe')

        class _T:
            actions = [action]

        rr.user_tasks['probe_auto'] = _T()
        try:
            return rr._reuse_action_is_autonomous('probe_auto', 1)
        finally:
            rr.user_tasks.pop('probe_auto', None)

    def test_yes_is_autonomous(self):
        assert self._probe({'can_perform_without_user_input': 'yes'}) is True

    @pytest.mark.parametrize('value', ['no', '', 'YES-ish', None])
    def test_anything_else_is_not(self, value):
        assert self._probe({'can_perform_without_user_input': value}) is False

    def test_missing_field_is_not_autonomous(self):
        assert self._probe({'action_id': 1}) is False

    def test_absent_session_is_not_autonomous(self):
        rr = pytest.importorskip('hartos.reuse_recipe')
        assert rr._reuse_action_is_autonomous('no_such_prompt', 1) is False


class TestToolEvidenceFailsClosed:

    def test_error_reports_outstanding_tools(self):
        """Uncertainty must BLOCK the advance, never permit it."""
        rr = pytest.importorskip('hartos.reuse_recipe')

        class _Boom:
            @property
            def agents(self):
                raise RuntimeError('group chat unavailable')

        out = rr._reuse_outstanding_tools('any', 1, _Boom())
        assert out, (
            'on error the predicate must return a non-empty (truthy) result so '
            'the caller treats tools as outstanding and refuses to advance')
