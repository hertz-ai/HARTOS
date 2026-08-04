"""The VM verification matrix must name REAL tests (goal: VM-verify each task).

docs/architecture/VM_VERIFICATION_MATRIX.md gives every open task a row saying
which VM test proves it — or saying plainly that none exists yet, and why.

A matrix nobody checks becomes fiction within a week: a test gets renamed, the
row keeps naming the old one, and the table still reads as coverage. So this
verifies the two things that make the table trustworthy:

  1. every `nixos/tests/<file>.nix` it cites EXISTS;
  2. every `hart-*` check name it cites is DEFINED by some test file.

It deliberately does NOT check that a row's status is accurate — EXISTS-RED vs
EXISTS is a judgement about a CI result, and encoding that here would just be a
second place to update. What it prevents is the failure that actually happens:
citing something that is not there.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MATRIX = os.path.join(REPO, "docs", "architecture", "VM_VERIFICATION_MATRIX.md")
TESTS_DIR = os.path.join(REPO, "nixos", "tests")

assert os.path.isfile(MATRIX), (
    f"{MATRIX!r} is missing — the matrix IS the deliverable for 'VM-verify "
    f"each task'; losing it silently would leave the tasks unmapped again")


def _matrix():
    with open(MATRIX, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _defined_check_names():
    """Every `name = "hart-..."` across nixos/tests/*.nix."""
    names = set()
    for entry in os.listdir(TESTS_DIR):
        if not entry.endswith(".nix"):
            continue
        with open(os.path.join(TESTS_DIR, entry), encoding="utf-8",
                  errors="replace") as fh:
            names |= set(re.findall(r'name\s*=\s*"(hart-[a-z0-9-]+)"', fh.read()))
    return names


def test_the_matrix_has_rows():
    """Guard the guard: an empty table would pass everything below."""
    rows = re.findall(r"^\|\s*\d+\s*\|", _matrix(), re.M)
    assert len(rows) >= 20, (
        f"only {len(rows)} task rows in the matrix — it is supposed to cover "
        f"every open task, so a near-empty table means it stopped being "
        f"maintained and every assertion here passes vacuously")


def test_every_cited_test_file_exists():
    cited = set(re.findall(r"`([a-z0-9-]+\.nix)`", _matrix()))
    # lib.nix is a helper, never a check; the matrix should not cite it.
    cited.discard("lib.nix")
    missing = sorted(f for f in cited
                     if not os.path.isfile(os.path.join(TESTS_DIR, f)))
    assert not missing, (
        f"the matrix cites nixos/tests files that do not exist: {missing}.\n"
        f"Either the test was renamed and the row is now fiction, or the row "
        f"was written for a test that was never created.")


def test_every_cited_check_name_is_defined():
    cited = set(re.findall(r"`(hart-[a-z0-9-]+)`", _matrix()))
    defined = _defined_check_names()
    missing = sorted(cited - defined)
    assert not missing, (
        f"the matrix cites check names no nixos/tests/*.nix defines: "
        f"{missing}.\nA row naming a check that cannot be built is a row that "
        f"claims verification which can never run.")


def test_unverifiable_rows_give_a_reason():
    """`NOT VM` must justify itself — 'hard to test' is not a reason.

    Without this the cheapest way to make the matrix look complete is to mark
    everything NOT VM, which is exactly the outcome the goal is trying to
    prevent.
    """
    offenders = []
    for line in _matrix().splitlines():
        if not line.startswith("|") or "NOT VM" not in line:
            continue
        # The status cell is the last column; a bare "NOT VM" with nothing
        # after it explains nothing.
        tail = line.rsplit("NOT VM", 1)[1].strip(" |")
        if len(tail) < 15:
            offenders.append(line.strip()[:100])
    assert not offenders, (
        "NOT VM rows without a stated reason:\n  " + "\n  ".join(offenders)
        + "\nSay WHY a booted machine cannot settle it (needs hardware, needs "
          "a human to look/listen, is a build measurement) — 'hard' is not a "
          "reason.")


def test_blocked_rows_name_their_blocker():
    """`BLOCKED` must name what is missing, or it becomes a dumping ground.

    BLOCKED is the easiest status to abuse: it moves a row out of TO WRITE
    without doing anything, and the count improves. That would be gaming the
    matrix rather than working it. So the same rule NOT VM has applies — the
    row must say WHAT is blocking, specifically enough that a reader can tell
    whose decision it is.
    """
    offenders = []
    for line in _matrix().splitlines():
        if not line.startswith("|") or "BLOCKED" not in line:
            continue
        tail = line.rsplit("BLOCKED", 1)[1].strip(" |")
        # A bare "BLOCKED" or "BLOCKED (decision)" with nothing after it says
        # nothing about WHO decides or WHY writing a test now would be wrong.
        if len(tail.lstrip("(decision)").strip()) < 40:
            offenders.append(line.strip()[:110])
    assert not offenders, (
        "BLOCKED rows that do not name the blocker:\n  " + "\n  ".join(offenders)
        + "\nBLOCKED is not a softer TO WRITE. Say what is actually missing — "
          "a steward decision, an upstream fix, a component that may be "
          "deleted — so the row can be un-blocked by someone reading it.")
