"""Reading task_time[...]['times'][-1] unguarded loses the action recipe.

PRODUCTION-PROVEN, not hypothetical.  22 occurrences across the installed
build's own logs (~/Documents/Nunba/logs/gui_app.log*, server.log*), e.g.
2026-08-29 12:35:37:

    hart_intelligence_entry - INFO  - Got Individual action recipe save it
    hart_intelligence_entry - ERROR - GOT SOME ERROR WHILE JSON: list index out of range
      File create_recipe.py, line 2549, in state_transition
        json_obj['time_took_to_complete'] = task_time[prompt_id]['times'][-1]
    IndexError: list index out of range

Read the two log lines together: the crash lands between "about to save this
action's recipe" and the save.  The recipe is NOT persisted, the turn logs a
vague "GOT SOME ERROR WHILE JSON" and carries on.  That is a strong candidate
mechanism for #718 ([FLOW-RECIPE-SAVED] never observed) and for the corpus of
saved-but-actionless agent configs.

WHY 'times' CAN BE EMPTY: create_recipe.py:4211 initialises
`task_time[prompt_id] = {'timer': time.time(), 'times': []}` -- empty -- and
the ONLY append is :2438, inside a conditional branch.  Any path that reaches
the RECIPE_RECEIVED save before that branch has ever run sees [].  It is also
a TTLCache (ttl 7200s), so a long-running build can lose the entry entirely.

THE GUARD ALREADY EXISTS IN THIS FUNCTION, 47 lines below, at :2599:

    if prompt_id in task_time and task_time[prompt_id].get('times'):
        json_obj['time_took_to_complete'] = task_time[prompt_id]['times'][-1]

Two sites agree on what the value means; only one checks it is there.  This
test makes the unguarded spelling impossible to reintroduce.

    python -m pytest tests/unit/test_task_time_read_guarded.py --noconftest -q
"""
import io
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SRC = os.path.join(ROOT, 'create_recipe.py')

_READ = re.compile(r"task_time\[[^\]]+\]\['times'\]\[-1\]")
# Either spelling of "I checked it is non-empty first".
_GUARD = re.compile(r"\.get\('times'\)|task_time\[[^\]]+\]\['times'\]\s*(?:\)|and|:)")


def _lines():
    with io.open(SRC, encoding='utf-8') as fh:
        return fh.read().splitlines()


def _unguarded():
    """Line numbers where ['times'][-1] is read with no nearby emptiness check."""
    lines = _lines()
    bad = []
    for i, line in enumerate(lines):
        if not _READ.search(line):
            continue
        # The guard is either on this line (a ternary/and) or on one of the two
        # lines above it (the enclosing `if`).
        window = '\n'.join(lines[max(0, i - 2):i + 1])
        if not _GUARD.search(window):
            bad.append((i + 1, line.strip()[:90]))
    return bad


def test_every_times_read_checks_the_list_is_non_empty():
    bad = _unguarded()
    assert not bad, (
        "these reads index task_time[...]['times'][-1] without first checking "
        "the list is non-empty. 'times' starts as [] (create_recipe.py:4211) "
        "and is appended to only inside a conditional (:2438), so an empty "
        "list is reachable -- 22 real IndexErrors in the installed build's "
        "logs, each one losing the action recipe it was about to save. Use the "
        "guard already present at :2599:\n"
        "    if prompt_id in task_time and task_time[prompt_id].get('times'):\n"
        + "\n".join(f"  create_recipe.py:{ln}  {src}" for ln, src in bad))


def test_the_guarded_exemplar_is_still_there():
    """Anti-vacuous: if the read disappears entirely the test above is empty.

    Pin that at least one guarded read survives, so a regex that stops matching
    real code fails loudly instead of passing on nothing.
    """
    joined = '\n'.join(_lines())
    assert _READ.search(joined), (
        "no task_time[...]['times'][-1] read found at all -- the detection "
        "regex no longer matches the code, so the guard test is vacuous")
    assert "task_time[prompt_id].get('times')" in joined, (
        'the guarded exemplar (create_recipe.py:2599) is gone; nothing left '
        'to copy the idiom from')
