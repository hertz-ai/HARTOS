"""Guards the create_recipe.py remedy-replay fix (#485).

Evidence this exists (llm_outbound.jsonl, 1,190 records / 16 MB):
  * 982 of 1088 calls re-sent an already-sent payload
  * worst single payload went out 222 times
  * "create a detailed recipe" 170x, the claim rejection 75x
  * 912 calls / 280 min span / 1156 min cumulative latency

Both replay sites share one shape: a guard condition that the remedy does
not change, so the remedy re-fires with a byte-identical body until
max_iterations (300) or the 30-min pipeline timeout stops it.  That is the
`feedback_vacuous_guards` pattern — the loop's own bounds cap the damage
but never break the cycle.

The AST tests below are the drift guard.  The behavioural tests alone would
still pass if someone deleted the call sites and left the helper orphaned.
"""
import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "hartos/create_recipe.py"


# ── behavioural: the helper itself ────────────────────────────────────

def _helper():
    """Import lazily — create_recipe pulls in autogen/langchain at import."""
    sys.path.insert(0, str(REPO))
    from hartos import create_recipe
    return create_recipe


def test_allows_exactly_max_attempts_then_blocks():
    m = _helper()
    attempts = {}
    key = ("recipe", "p1", 0)
    allowed = [not m._remedy_replay_exceeded(attempts, key) for _ in range(6)]
    # First _REMEDY_MAX_ATTEMPTS calls proceed; every later one is blocked.
    assert allowed[: m._REMEDY_MAX_ATTEMPTS] == [True] * m._REMEDY_MAX_ATTEMPTS
    assert not any(allowed[m._REMEDY_MAX_ATTEMPTS:])


def test_distinct_keys_have_independent_budgets():
    """Action 2's recipe must not be starved by action 1 exhausting its budget."""
    m = _helper()
    attempts = {}
    for _ in range(m._REMEDY_MAX_ATTEMPTS + 2):
        m._remedy_replay_exceeded(attempts, ("recipe", "p1", 1))
    assert m._remedy_replay_exceeded(attempts, ("recipe", "p1", 1)) is True
    assert m._remedy_replay_exceeded(attempts, ("recipe", "p1", 2)) is False
    assert m._remedy_replay_exceeded(attempts, ("claim", "p1", 1, "why")) is False


def test_stays_blocked_once_exhausted():
    m = _helper()
    attempts = {}
    key = ("claim", "p1", 0, "not found in ledger")
    for _ in range(m._REMEDY_MAX_ATTEMPTS):
        m._remedy_replay_exceeded(attempts, key)
    assert all(m._remedy_replay_exceeded(attempts, key) for _ in range(10))


# ── AST drift guard: both replay sites must stay wired ────────────────

def _guard_key_kinds(source: str):
    """Return the set of first-element literals passed as the guard's key.

    Looks for _remedy_replay_exceeded(attempts, ('recipe', ...)) style calls
    and collects 'recipe' / 'claim'.
    """
    kinds = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == "_remedy_replay_exceeded"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Tuple) and arg.elts:
                first = arg.elts[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    kinds.add(first.value)
    return kinds


@pytest.mark.parametrize("kind", ["recipe", "claim"])
def test_replay_site_is_guarded(kind):
    """Each measured replay site keeps its guard.

    'recipe' = the 170x request_recipe_for_action replay.
    'claim'  = the 75x completion-claim rejection replay.
    """
    assert kind in _guard_key_kinds(SRC.read_text(encoding="utf-8")), (
        f"the {kind!r} replay site lost its _remedy_replay_exceeded guard — "
        "the identical-payload loop measured in llm_outbound.jsonl is back"
    )


def test_loop_declares_its_replay_ledger():
    """A guard with no per-run dict would NameError on first use."""
    src = SRC.read_text(encoding="utf-8")
    assert "_remedy_attempts = {}" in src


def test_guard_was_absent_before_the_fix():
    """Proves this suite is RED on the pre-fix file, not vacuously green.

    Reads create_recipe.py as of the last commit.  Skips (rather than fails)
    once the fix is itself committed and HEAD no longer predates it.
    """
    try:
        old = subprocess.run(
            ["git", "show", "HEAD:create_recipe.py"],
            # encoding is explicit: text=True would decode with the Windows
            # locale codec and blow up on the file's UTF-8 em-dashes.
            cwd=REPO, capture_output=True, timeout=60, check=True,
            encoding="utf-8", errors="replace",
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        pytest.skip(f"git unavailable: {exc}")
    if "_remedy_replay_exceeded" in old:
        pytest.skip("fix is committed; HEAD no longer predates it")
    assert _guard_key_kinds(old) == set(), "pre-fix file should have no guards"
