"""Shell_Command must never wedge the caller — AST drift-guard (D35).

WHAT WENT WRONG (measured live 2026-09-09 02:17 -> 02:38, agent
33323830039 'skill.local.name').  A reuse turn stopped emitting any log
line for 21 minutes while llama-server sat idle (`/slots`
is_processing=false, a 0.4s completion answered fine), so the wedge was
in Python, not the model.  The on-demand thread dump named it exactly:

    chat (hart_intelligence_entry:10138)
      -> chat_agent (reuse_recipe:5331)
        -> get_agent_response (reuse_recipe:4216)
          -> execute_windows_or_android_command (reuse_recipe:1834)
            -> execute_vlm_instruction -> run_local_agentic_loop
              -> _execute_inprocess (local_computer_tool:733)
                -> _handle_shell_command_tool (hart_intelligence_entry:3182)
                  -> subprocess.run (subprocess.py:559)
                    -> process.communicate()   <-- BLOCKED HERE

subprocess.py:559 is inside `run`'s OWN TimeoutExpired handler:

    except TimeoutExpired as exc:
        process.kill()
        if _mswindows:
            exc.stdout, exc.stderr = process.communicate()   # line 559

That second `communicate()` takes NO timeout.  On Windows `kill()` ends
only the direct child; a surviving grandchild keeps the inherited stdout
/ stderr write handles open, so the reader threads never see EOF and the
join never returns.  The `timeout=30` was therefore UNENFORCEABLE: the
deadline fired, and the handler's own `except subprocess.TimeoutExpired`
branch below could never be reached.  One shell action wedged the whole
chat turn for that user, permanently.

WHY THIS GUARD AND NOT A NEW HELPER.  `core.subprocess_safe` already
exists for exactly this failure (its docstring cites the 27-min wmic
hang, 2026-04-15) and already says: "Do NOT add fresh
`subprocess.run(..., capture_output=True, text=True, timeout=N)` sites
— they reintroduce the reader-thread orphan."  `run_bounded` kills, then
explicitly CLOSES the parent-side pipes so the reader threads unblock,
and its boundedness is already proven by
tests/unit/test_subprocess_safe.py::TestRunProbeBoundedness.  So the fix
is a one-site migration onto machinery that shipped months ago, and this
file's job is to stop the banned shape coming back.

This is a pure source scan on purpose: test_shell_command_tool.py carries
a module-level skipif for when hart_intelligence_entry cannot import, and
a drift-guard that silently skips is not a guard.
"""

import ast
import os

import pytest

_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'hart_intelligence_entry.py',
)
_FUNC = '_handle_shell_command_tool'


def _handler_node():
    """The AST node for the handler, or None when the name is absent."""
    with open(_SRC, encoding='utf-8', errors='replace') as fh:
        tree = ast.parse(fh.read(), filename=_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == _FUNC:
            return node
    return None


def _called_names(node):
    """Every callee spelling inside `node`: 'run_bounded', 'subprocess.run'."""
    names = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        if isinstance(f, ast.Name):
            names.append(f.id)
        elif isinstance(f, ast.Attribute):
            base = f.value
            if isinstance(base, ast.Name):
                names.append(f'{base.id}.{f.attr}')
            else:
                names.append(f.attr)
    return names


# ── anti-vacuity ────────────────────────────────────────────────────────
# Every assertion below is "this name is / is not among the calls".  If the
# scan silently found nothing, all of them would pass while checking
# nothing at all -- the exact vacuous-guard trap this codebase has already
# been bitten by 12 times.  So prove the scan has real material first.

def test_the_handler_is_found_in_the_source():
    assert _handler_node() is not None, (
        f'{_FUNC} not found in {_SRC} — the guard below would pass '
        f'vacuously. If the function was renamed, re-point this file.'
    )


def test_the_handler_actually_contains_calls():
    calls = _called_names(_handler_node())
    assert len(calls) >= 5, (
        f'only {len(calls)} call(s) parsed out of {_FUNC}; the scan is not '
        f'seeing the real body, so its verdicts mean nothing: {calls}'
    )


# ── the guard ───────────────────────────────────────────────────────────

def test_handler_does_not_call_subprocess_run():
    """RED before the fix: this is the call that wedged for 21 minutes."""
    calls = _called_names(_handler_node())
    assert 'subprocess.run' not in calls, (
        'subprocess.run is back in _handle_shell_command_tool. On Windows its '
        'post-kill communicate() has no timeout, so a surviving grandchild '
        'holding the inherited pipes wedges the whole chat turn forever and '
        'the timeout= argument is unenforceable. Use '
        'core.subprocess_safe.run_bounded, which closes the parent-side pipes '
        'after the kill.'
    )


def test_handler_calls_run_bounded():
    calls = _called_names(_handler_node())
    assert 'run_bounded' in calls, (
        f'{_FUNC} must execute through core.subprocess_safe.run_bounded so a '
        f'timed-out child cannot orphan its reader threads. Calls found: '
        f'{sorted(set(calls))}'
    )


def test_run_bounded_is_imported_from_the_canonical_home():
    """One home for bounded execution — not a second local copy."""
    with open(_SRC, encoding='utf-8', errors='replace') as fh:
        tree = ast.parse(fh.read(), filename=_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == 'core.subprocess_safe':
            if any(a.name == 'run_bounded' for a in node.names):
                return
    pytest.fail(
        'run_bounded must be imported from core.subprocess_safe — the module '
        'that owns bounded subprocess execution and already carries the '
        'reader-thread fix and its tests.'
    )


def test_the_timeout_contract_is_still_enforced():
    """The 30s budget must survive the migration, read off `.timed_out`.

    run_bounded never raises TimeoutExpired; it returns a BoundedResult
    with timed_out=True.  A migration that forgot this would silently
    report "Exit code: -1" with empty output instead of the honest
    "timed out after 30s, use Execute_Coding_Task" message.
    """
    node = _handler_node()
    src = ast.dump(node)
    assert 'timed_out' in src, (
        'the handler no longer inspects BoundedResult.timed_out, so a '
        'timed-out command would be reported to the agent as a normal '
        'failed exit instead of a timeout.'
    )
    consts = [n.value for n in ast.walk(node)
              if isinstance(n, ast.Constant) and isinstance(n.value, int)]
    assert 30 in consts, (
        'the 30s shell budget disappeared from the handler; '
        f'int constants present: {sorted(set(consts))}'
    )
