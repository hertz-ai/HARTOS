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


class TestRequiresBreakdownIsAlsoUnderReporting:
    """'requires_breakdown' strands an action exactly like 'pending' did.

    Measured live 2026-09-06 on agent 89555447799 (24-action recipe, driven as
    its own owner cf125371-...).  The turn ran 107 rounds over 40 minutes and
    never left action 1:

      * action 1 is autonomous (can_perform_without_user_input='yes')
      * its named tool really executed - `INSIDE google search` at 02:2x, so
        _reuse_outstanding_tools is empty
      * the StatusVerifier reported {'status': 'requires_breakdown',
        'action_id': 1} 35 times

    The StatusVerifier emits four statuses.  Speaker routing handles all four
    (reuse_recipe.py:2635 persists requires_breakdown's subtasks, :2646 routes
    error/pending), but the ADVANCE branch recognised only 'pending', so
    requires_breakdown could never advance and had no per-action bound.  The
    only bound was the whole-turn budget - max(4, n_actions*4+4) = 100 rounds
    for this recipe - so ONE wedged action consumed the entire turn and the
    remaining 23 actions never ran.  `Retrieved current_action_id: 1 for
    session: cf125371-..._89555447799` appears 175x; [REUSE]/advance markers
    appear 0x.

    Fix reuses the machinery already here - same counter, same bound, same
    evidence gate, same _advance_or_steer - so nothing advances whose tool did
    not actually execute.  It must NOT become a second notion of the statuses:
    one constant, read at the branch.
    """

    def test_constant_names_both_under_reporting_statuses(self):
        rr = pytest.importorskip('hartos.reuse_recipe')
        statuses = getattr(rr, '_REUSE_UNDERREPORT_STATUSES', None)
        assert statuses is not None, (
            '_REUSE_UNDERREPORT_STATUSES missing — the branch must read ONE '
            'named constant, not inline a second status list')
        assert 'pending' in statuses, 'pending must stay under-reporting'
        assert 'requires_breakdown' in statuses, (
            "requires_breakdown must be treated as under-reporting: live "
            "2026-09-06 it stranded agent 89555447799 at action 1 for 107 "
            "rounds while its google_search had already executed")

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
