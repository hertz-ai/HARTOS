"""Degraded-mode ratchet for the /api/shell surface (task #31).

WHY A SOURCE GUARD, AND WHY THAT IS ALLOWED HERE
────────────────────────────────────────────────
`memory/feedback_no_grep_tests.md` bans grep-shaped tests, with ONE stated
exception: a clearly-labelled `test_source_guard_*` for DRY/aggregate
enforcement across MANY files, where a behavioural test on a single call site
cannot catch the regression. This is that case, and only that case.

There are 246 `/api/shell` routes. The per-route BEHAVIOURAL tests are the
actual work of task #31 and are being added in ranked batches. What no single
behavioural test can do is stop the AGGREGATE from going backwards — someone
deleting the 503 branch from the webcam handler breaks no test that exists,
because the thing lost is a property of the whole surface.

So this file does exactly one job: it counts the honest-degrade signals that
exist today and fails if that count DROPS. It does not claim any route is
correct. It is a ratchet, not a verdict.

THE PATTERN IT PROTECTS — five instances in three days, every one
BROKEN-BUT-QUIET, none with a degraded test before the fix:
  * /api/shell/drivers   silently truncated at 50, no `unclaimed` status
  * antivirus            clamd could run with a 6-month-old DB, looking healthy
  * disk encryption      encrypted /data + plaintext root read as "encrypted"
  * Nunba /status        port bound, executor dead, EVERY route 500
  * hart-shell-memwatch  the leak sampler died under load — i.e. when needed

Each was a surface that could not say it was NOT ok.
"""
import os
import re

import pytest

# This file lives at tests/unit/, so the repo root is THREE levels up.
# Two levels lands on tests/ and every read silently misses, which is exactly
# how the first run of this file failed all 9 assertions at once.
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENGINE = os.path.join(REPO, "integrations", "agent_engine")
assert os.path.isdir(ENGINE), (
    f"agent_engine not found at {ENGINE!r} — this guard reads real source, so "
    f"a bad path must fail LOUDLY here rather than report zero degrades and "
    f"look like a regression in every module at once"
)

#: Modules that serve the shell surface.
SHELL_MODULES = (
    "shell_os_apis.py",
    "shell_system_apis.py",
    "shell_desktop_apis.py",
    "liquid_ui_service.py",
)

#: Ways a handler can honestly say "I cannot do this right now".
#: Counting ONLY `, 503` would undercount: encryption/status answers with
#: available=False, antivirus names the missing tool in an error field, and
#: the drivers route reports `unclaimed` / `truncated`. All are honest
#: degrades; a ratchet that saw only one shape would push authors toward that
#: shape rather than toward honesty.
DEGRADE_SIGNALS = (
    r"\), 503",
    r"'available': False",
    r"'unclaimed'",
    r"'truncated'",
    r"'signatures_stale'",
    r"not available'",
    r"'degraded'",
)

#: Measured 2026-08-03 from source. These are FLOORS: the count may only go
#: up. Raise a floor in the SAME commit that adds the degrade — never lower
#: one to make a suite pass; that is the ratchet slipping, which is the whole
#: failure this file exists to prevent.
DEGRADE_FLOOR = {
    "shell_os_apis.py": 1,
    "shell_system_apis.py": 5,
    "shell_desktop_apis.py": 2,
    "liquid_ui_service.py": 0,
}

#: Total /api/shell routes, measured the same day. Informational: recorded so
#: the ratio (honest degrades vs surface) is visible in one place and the task
#: can be tracked down to zero.
ROUTE_COUNT_AT_BASELINE = {
    "shell_os_apis.py": 67,
    "shell_system_apis.py": 78,
    "shell_desktop_apis.py": 68,
    "liquid_ui_service.py": 33,
}


def _read(mod):
    with open(os.path.join(ENGINE, mod), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _degrade_count(src):
    return sum(len(re.findall(p, src)) for p in DEGRADE_SIGNALS)


def _route_count(src):
    return len(re.findall(r"@app\.route\('/api/shell", src))


@pytest.mark.parametrize("mod", SHELL_MODULES)
def test_source_guard_degraded_paths_never_decrease(mod):
    """RATCHET: the honest-degrade count may rise, never fall.

    Fails if someone removes an unavailable-path. It does NOT assert any
    route is correct — the per-route behavioural tests do that, and they are
    the actual work of #31.
    """
    count = _degrade_count(_read(mod))
    floor = DEGRADE_FLOOR[mod]
    assert count >= floor, (
        f"{mod} lost an honest-degrade path: {count} < floor {floor}.\n"
        f"A route that cannot say it is unavailable reports success while "
        f"serving nothing — the exact shape of the five defects in this "
        f"file's docstring. Restore it, or if the route genuinely no longer "
        f"needs one, lower the floor IN THE SAME COMMIT with the reason."
    )


@pytest.mark.parametrize("mod", SHELL_MODULES)
def test_source_guard_shell_surface_is_still_inventoried(mod):
    """The surface may GROW — but a grown surface means new unaudited routes.

    This does not fail on growth; it fails when growth outruns the recorded
    inventory badly enough that the task's own numbers are stale. Keeping the
    inventory honest is what lets #31 be tracked down to zero instead of
    being an open-ended chore.
    """
    routes = _route_count(_read(mod))
    baseline = ROUTE_COUNT_AT_BASELINE[mod]
    # A little drift is normal between audits; a large jump means the
    # inventory in task #31 no longer describes reality.
    assert routes <= baseline + 15, (
        f"{mod} now serves {routes} /api/shell routes vs the recorded "
        f"baseline {baseline}. Re-measure the inventory in task #31 and "
        f"update ROUTE_COUNT_AT_BASELINE, so 'routes still needing a "
        f"degraded test' stays a real number rather than a stale one."
    )


def test_the_reference_degrades_are_still_present():
    """The four worked examples must not silently regress.

    These are the shapes every other route should copy, so they are asserted
    by NAME rather than by count — a count would let one be deleted and
    another added, losing the exemplar while the ratchet stayed green.
    """
    sysapis = _read("shell_system_apis.py")
    assert "'signatures_stale'" in sysapis, (
        "antivirus lost `signatures_stale` — a live clamd with an ancient "
        "database looks healthy and catches nothing")
    assert "'root_encrypted'" in sysapis, (
        "encryption lost `root_encrypted` — an encrypted /data with a "
        "plaintext root reads as 'encrypted' and protects far less")
    assert "runtime_toggle_supported" in sysapis, (
        "encryption lost the explicit 'you cannot toggle this at runtime' "
        "field, so callers will hunt for a toggle that cannot exist")
    liquid = _read("liquid_ui_service.py")
    assert "'unclaimed'" in liquid and "'truncated'" in liquid, (
        "the device tree lost `unclaimed`/`truncated` — the yellow-bang and "
        "the honest-truncation signals that replaced a silent 50-device cap")


# ═══════════════════════════════════════════════════════════════
# Silent `except: pass` — the shape the tasks/kill defect lived in
# ═══════════════════════════════════════════════════════════════
# A handler whose ENTIRE body is `pass` discards the one signal that says
# something went wrong. That is banned outright by
# memory/feedback_no_silent_exception_gulping_2026-07-15.md, and it is
# exactly where the /api/shell/tasks/kill guard failed OPEN: the
# protected-process check sat inside `except Exception: pass`, so whenever
# psutil was absent or Process(pid) raised, the check was skipped and the
# kill proceeded (fixed 0b05949b).
#
# MEASURED 2026-08-03 by AST walk (not grep — a comment or a string
# containing "except: pass" would fool a text search):
#     shell_os_apis      28
#     shell_system_apis  18
#     shell_desktop_apis 10
#     liquid_ui_service   0
#
# These are CEILINGS: the count may only go DOWN. Not every one is a defect —
# best-effort cleanup on a teardown path is legitimate — so this does not
# demand zero. It demands that the number never grows while #31 walks the
# 246-route surface, and it makes the remaining count VISIBLE instead of
# invisible.
SILENT_GULP_CEILING = {
    # 28 -> 26 and 10 -> 9 on 2026-08-03: the THREE control-flow-changing
    # gulps (a try containing a return, guarded by except: pass) now LOG.
    # Lowered in the SAME commit that removed them, per this file own rule.
    "shell_os_apis.py": 26,
    "shell_system_apis.py": 18,
    "shell_desktop_apis.py": 9,
    "liquid_ui_service.py": 0,
}


def _silent_gulp_count(src):
    import ast
    tree = ast.parse(src)
    return sum(
        1 for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Pass)
    )


@pytest.mark.parametrize("mod", SHELL_MODULES)
def test_source_guard_silent_exception_gulps_never_increase(mod):
    """RATCHET DOWN: no new `except: pass` on the shell surface.

    AST-based, so a comment or a docstring mentioning the pattern cannot
    move the number either way. Lower the ceiling in the SAME commit that
    removes one — that is how #31 gets tracked to zero rather than drifting.
    """
    count = _silent_gulp_count(_read(mod))
    ceiling = SILENT_GULP_CEILING[mod]
    assert count <= ceiling, (
        f"{mod} grew a silent `except: pass`: {count} > ceiling {ceiling}.\n"
        f"A handler whose whole body is `pass` throws away the signal that "
        f"something failed. That is how /api/shell/tasks/kill's "
        f"protected-process guard failed OPEN — psutil absent meant the "
        f"check was skipped and the kill went ahead (0b05949b).\n"
        f"Log it, degrade honestly, or fail closed — but do not swallow it."
    )
