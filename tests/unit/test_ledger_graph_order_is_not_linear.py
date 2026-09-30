"""What the ledger graph guarantees — and what it does NOT (it is not a counter).

READ THIS BEFORE RETRYING THE SCHEDULER MIGRATION (ledger #779).  A migration
swapping ``current_action + 1`` for ``get_next_task()`` landed as acab1cc2c
and was REVERTED the same evening, because its central claim was refuted on
the first live run:

    2026-09-09 22:12:05  [REUSE-LEDGER] graph selected action 8 after 1
                         for session <user>_18088688973

The recipe (``18088688973_0_recipe.json``) has SIX actions.  There is no
action 8.  The old counter would have gone 1 -> 2.

WHY the tests below did not catch it, which is the whole lesson: they build a
ledger with ``create_ledger_from_actions`` from trivial one-line actions, and
that yields exactly one task per action, so 1,2,3 comes out.  The REAL call in
reuse_recipe passes ``resume_if_unfinished=True`` (the default), and that
RESUMES a persisted in-flight ledger for (user, prompt) instead of building
one from this recipe.  Live, that ledger reported ``tasks=17 actions=9`` for a
6-action recipe, and its persisted snapshots carry action_2..action_5 already
``completed`` from earlier sessions.  So ``get_next_task()`` legitimately
returns whatever is still PENDING in ACCUMULATED state — which is neither
"the next action of this recipe" nor necessarily an id this recipe HAS.

So the tests in this file are true, and were never the problem: they pin the
package's behaviour for a FRESHLY-BUILT linear ledger.  The false step was
mine — generalising from that fixture to the production call, whose ledger is
resumed rather than built.  Any retry must first decide, with the owner, what
a re-drive of an agent means (fresh ledger per drive?  resume and skip
completed work?) and must pin the answer against a RESUMED ledger, not a
fresh one.  Renamed from test_reuse_advances_via_ledger_graph.py, which named
a migration that no longer exists.

    python -m pytest tests/unit/test_ledger_graph_order_is_not_linear.py --noconftest -q

Original header follows.

The ledger's task graph — not ``current_action + 1`` — decides what runs next.

WHY THIS EXISTS.  reuse_recipe builds the canonical ledger at :1203
(``create_ledger_from_actions``), stores it in ``user_ledgers[user_prompt]``,
and then never asks it anything: :5122 does

    next_id = current_action_id + 1
    user_tasks[user_prompt].current_action = next_id

That is a hand-rolled LINEAR scheduler running beside a task GRAPH that was
already constructed for this session — the parallel system this migration
removes (ledger #779).  Measured 2026-09-09: 118 ``current_action`` references
in reuse_recipe.py against 6 ledger references.

This module pins the CONTRACT the migration depends on, using the real
agent_ledger package (no mocks — a mock would prove only that I can write a
mock).  If any of these change, the swap at :5122 is unsafe and must be
revisited rather than silently drifting.

    python -m pytest tests/unit/test_reuse_advances_via_ledger_graph.py --noconftest -q
"""
import pytest

agent_ledger = pytest.importorskip("agent_ledger")
from agent_ledger import create_ledger_from_actions, get_production_backend  # noqa: E402


def _actions(n):
    """n recipe actions in the shape create_ledger_from_actions consumes."""
    return [{"action_id": i, "action": "step %d" % i} for i in range(1, n + 1)]


def _ledger(n, prompt_id):
    return create_ledger_from_actions(
        user_id=999, prompt_id=prompt_id, actions=_actions(n),
        backend=get_production_backend(), flow_id=0)


def _aid(task):
    """The recipe action id a ledger Task carries."""
    return getattr(task, "recipe_action_id", None)


def _finish(led, task):
    """Take a task PENDING -> IN_PROGRESS -> COMPLETED, the only legal route.

    MEASURED, and it is the whole reason this file exists: calling
    ``complete_task`` on a PENDING task does NOT complete it.  core.py:628
    allows PENDING -> {IN_PROGRESS, PAUSED, CANCELLED, SKIPPED,
    NOT_APPLICABLE, DEFERRED} and COMPLETED only from IN_PROGRESS/DELEGATED,
    so a direct completion logs "Invalid transition from TaskStatus.PENDING to
    TaskStatus.COMPLETED" and returns False -- the task stays PENDING.

    A migration that swapped `current_action + 1` for complete_task() +
    get_next_task() WITHOUT this step would therefore hand back action 1 on
    every advance, forever, for every agent.  The first draft of this file did
    exactly that and these tests failed; that is the failure being pinned.
    """
    led.update_task_status(task.task_id, agent_ledger.TaskStatus.IN_PROGRESS)
    return led.complete_task(task.task_id)


class TestTheGraphReproducesLinearOrder:
    """The migration must not change behaviour for an ordinary linear recipe."""

    def test_first_task_is_action_1(self):
        led = _ledger(3, 900001)
        assert _aid(led.get_next_task()) == 1

    def test_completing_each_task_yields_the_next_in_order(self):
        led = _ledger(3, 900002)
        seen = []
        for _ in range(3):
            nxt = led.get_next_task()
            if nxt is None:
                break
            seen.append(_aid(nxt))
            _finish(led, nxt)
        assert seen == [1, 2, 3], (
            "get_next_task must reproduce 1,2,3 for a linear recipe — this is "
            "what makes replacing `current_action + 1` behaviour-preserving")

    def test_all_complete_yields_none_not_an_out_of_range_id(self):
        """`+1` runs off the end and is caught by `next_id > len(actions)`.

        The graph says None instead, which is the honest terminal signal.
        """
        led = _ledger(2, 900003)
        for _ in range(2):
            nxt = led.get_next_task()
            _finish(led, nxt)
        assert led.get_next_task() is None


class TestTheGraphDoesWhatLinearCannot:
    """The reason to migrate at all — otherwise this is churn."""

    def test_an_incomplete_task_is_not_skipped(self):
        """Without complete_task, the graph does NOT advance.

        This is why reuse's ledger looks inert today: it is built, then never
        told anything finished, so every task stays PENDING and the graph
        keeps offering action 1.
        """
        led = _ledger(3, 900004)
        first = led.get_next_task()
        assert _aid(led.get_next_task()) == _aid(first), (
            "with nothing completed the graph must keep offering the same "
            "task — an advance requires a real completion, not a counter bump")

    def test_task_id_matches_the_documented_action_naming(self):
        """`action_{action_id}` is the id the migration will complete by."""
        led = _ledger(2, 900005)
        ids = {t.task_id for t in led.tasks.values()}
        assert "action_1" in ids and "action_2" in ids, sorted(ids)
