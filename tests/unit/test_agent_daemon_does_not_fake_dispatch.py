"""A log line must not assert an action the code does not perform.

THE DEFECT
----------
integrations/agent_engine/agent_daemon.py had, inside its proactive-hive block:

    from integrations.coding_agent.claude_hive_session import get_blueprint
    for task in pending_tasks[:3]:
        logger.info(f"Proactive hive: auto-dispatching task to local "
                    f"hive session: {task_desc[:100]}")

`get_blueprint` is a Flask blueprint factory. It was imported and never called.
No dispatcher was touched, no session received anything, no task status moved
off `pending`. The loop existed only to emit a log line saying the opposite.

That is worse than doing nothing, because it makes a stuck queue look serviced.
Anyone reading the journal saw "auto-dispatching task" scroll past and would
conclude dispatch worked; the tasks were sitting at `pending` in
hive_tasks.json the whole time, invisible to both `hart hive status` (which
reports the SESSION's pending count, a different list) and to the copilot
daemon's poll.

WHY THE FIX IS NOT "MAKE IT DISPATCH"
-------------------------------------
There is exactly one dispatch driver in the repo:
ResourceGovernor._proactive_check_tasks, which calls
HiveTaskDispatcher.dispatch_pending() on its own timer. Calling it from the
daemon as well would make the daemon a SECOND scheduler for the same queue.
Whether hive dispatch should be more responsive than the governor's timer is a
scheduling decision about the hive, and it belongs to whoever owns that lane.
It is not something a log line gets to decide by pretending it already happened.

So the block observes and reports honestly, and this test keeps it that way.
"""

import pathlib
import re

_DAEMON = (pathlib.Path(__file__).resolve().parents[2]
           / "integrations" / "agent_engine" / "agent_daemon.py")


def _proactive_hive_block() -> str:
    """The region of agent_daemon.py that inspects the hive task queue."""
    src = _DAEMON.read_text(encoding="utf-8")
    start = src.index("# Check hive task protocol for unassigned tasks")
    end = src.index("# ── 2. Self-promotion on benchmark results ──", start)
    return src[start:end]


def test_the_block_still_exists():
    """If the anchors move this test silently checks nothing, so assert them."""
    block = _proactive_hive_block()
    assert "get_pending_tasks" in block, (
        "the proactive-hive block no longer reads the dispatcher queue; "
        "re-anchor this test rather than deleting it")


def test_no_log_claims_a_dispatch_that_does_not_happen():
    """The exact regression: a log line asserting dispatch, with no dispatch."""
    block = _proactive_hive_block()

    claims = [m.group(0) for m in
              re.finditer(r'"[^"]*(?:auto-)?dispatch(?:ing|ed)?[^"]*"', block)]
    really_dispatches = "dispatch_pending()" in block

    for claim in claims:
        low = claim.lower()
        # Saying a task IS BEING or WAS dispatched is only allowed if this code
        # actually calls the dispatcher. Naming where dispatch happens is fine.
        asserts_action = ("auto-dispatching" in low
                          or "dispatching task" in low
                          or re.search(r"\bdispatched \d", low) is not None)
        if asserts_action and not really_dispatches:
            raise AssertionError(
                "this log line claims a dispatch that the surrounding code "
                "never performs: %s\n\n"
                "Either call HiveTaskDispatcher.dispatch_pending() here (which "
                "makes this daemon a second scheduler for a queue that already "
                "has one, so decide that deliberately), or describe the backlog "
                "without claiming to have acted on it." % claim)


def test_it_does_not_import_a_blueprint_factory_as_if_it_dispatched():
    """`get_blueprint` is a Flask blueprint factory. Importing it here was the
    tell that nothing in this block ever dispatched anything."""
    block = _proactive_hive_block()
    assert "import get_blueprint" not in block, (
        "get_blueprint is a Flask blueprint factory, not a dispatch entry "
        "point. Importing it inside the proactive-hive block is how the "
        "fabricated 'auto-dispatching' log came to look plausible.")


def test_it_points_at_the_one_real_dispatch_driver():
    """A reader who finds this block must be told where dispatch actually
    happens, or they will re-add the fake one."""
    block = _proactive_hive_block()
    assert "_proactive_check_tasks" in block, (
        "the block must name ResourceGovernor._proactive_check_tasks as the "
        "single real dispatch driver, so the next reader does not conclude "
        "that dispatch is missing and add a second scheduler here")
