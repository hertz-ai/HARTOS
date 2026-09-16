"""The reuse pipeline must advance on the completion words the model ACTUALLY emits.

MEASURED LIVE 2026-09-07 on the installed build.  StatusVerifier's contract
(reuse_recipe.py:1427-1430) names four statuses — completed / error / pending /
requires_breakdown — but the model also emits synonyms, and the reuse pipeline
drops them:

    {"status": "done", "action": "Use fetch_news_feeds to pull the latest
     articles from all configured RSS/Atom feeds on an hourly schedule.",
     "action_id": 1}

"done" is a completion claim.  Every one of the five reuse readers tested
`json_obj['status'].lower() == 'completed'` and therefore discarded it, so the
action pointer never advanced and the work was re-run or stalled.

WHY THE SET AND NOT AN INLINE `or 'done'`: core/constants.py:1085 already
declares VERDICT_COMPLETION_STATUSES = {completed, success} for exactly this
question — and it had ZERO consumers.  Meanwhile create_recipe.py:2478 had
hand-rolled `== 'completed' or == 'success'`, rediscovering the dead set's
contents inline.  That is the drift this test exists to stop.

WHAT THIS TEST DELIBERATELY DOES NOT ASSERT: the multi-status GROUPINGS.
core/constants.py:1073-1077 states they differ per call site on purpose
(:2646 groups error+pending; :3438 groups pending+requires_breakdown) and that
collapsing them would change behaviour.  Only the completion TOKENS are shared.
'updated' is excluded for the same reason it must never advance: measured live,
it carries "message": "The fallback strategy for Action 3 requires specific
user input" — a revision that is still PENDING, not a completion.  Treating it
as done would force-advance past work the model explicitly flagged as needing
the user, which is what create_recipe.py:3123's USER-INPUT GATE forbids.

    python -m pytest tests/unit/test_completion_verdict_vocabulary.py --noconftest -q
"""
import re
import pytest


def _constants():
    return pytest.importorskip('core.constants')


class TestCompletionVocabulary:

    def test_done_counts_as_a_completion(self):
        """The synonym the model actually emitted live."""
        c = _constants()
        assert 'done' in c.VERDICT_COMPLETION_STATUSES, (
            "StatusVerifier emits {'status': 'done'} as a completion claim; "
            "if the canonical set does not carry it, every reuse reader drops "
            "the verdict and the action never advances")

    def test_completed_and_success_still_count(self):
        c = _constants()
        assert c.VERDICT_COMPLETED in c.VERDICT_COMPLETION_STATUSES
        assert c.VERDICT_SUCCESS in c.VERDICT_COMPLETION_STATUSES

    def test_updated_is_NOT_a_completion(self):
        """Measured: 'updated' ships a still-pending action.

        {"status": "updated", "updated_action": "... (Pending user-defined
         fallback strategy)", "message": "The fallback strategy for Action 3
         requires specific user input"}

        Advancing on that is the force-completion the anti-overclaim contract
        and create_recipe.py:3123's USER-INPUT GATE both forbid.
        """
        c = _constants()
        assert 'updated' not in c.VERDICT_COMPLETION_STATUSES

    def test_non_completion_verdicts_stay_out(self):
        c = _constants()
        for bad in (c.VERDICT_PENDING, c.VERDICT_ERROR,
                    c.VERDICT_REQUIRES_BREAKDOWN):
            assert bad not in c.VERDICT_COMPLETION_STATUSES, (
                f"{bad!r} has its own execution path — see the note at "
                f"core/constants.py:1091-1110")

    def test_groupings_are_left_alone(self):
        """Guard the design note: only tokens are shared, groupings are not."""
        c = _constants()
        assert c.VERDICT_UNDERREPORT_STATUSES == frozenset({c.VERDICT_PENDING})
        assert c.VERDICT_PENDING not in c.VERDICT_ROUND_TERMINAL_STATUSES


class TestReuseReadersUseTheCanonicalSet:
    """Drift guard: no reuse reader may re-spell the completion test inline.

    Same technique as tests/unit/test_reuse_completion_terminates.py — read the
    source, because the defect IS the inline literal.
    """

    @staticmethod
    def _src():
        import inspect
        rr = pytest.importorskip('hartos.reuse_recipe')
        return inspect.getsource(rr)

    def test_no_inline_status_equals_completed(self):
        src = self._src()
        hits = re.findall(
            r"\[['\"]status['\"]\]\s*\.lower\(\)\s*==\s*['\"]completed['\"]", src)
        assert not hits, (
            f"{len(hits)} reader(s) still hard-code the completion spelling "
            f"instead of consulting VERDICT_COMPLETION_STATUSES — that is how "
            f"'done' got dropped live on 2026-09-07")

    def test_reuse_imports_the_canonical_completion_set(self):
        src = self._src()
        assert 'VERDICT_COMPLETION_STATUSES' in src, (
            'reuse_recipe must consume the canonical set, not a private copy')
