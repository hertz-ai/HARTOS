"""Regression guard for the boot-time lazy-import of autogen.

WHY THIS EXISTS
───────────────
`import autogen` drags google.api_core (7.6s) + flaml + the contrib
capabilities chain -> llmlingua -> torch (4.2s).  create_recipe.py and
reuse_recipe.py used autogen ONLY inside functions (verified by AST:
zero module-level / class-base uses), yet imported it eagerly at module
top, adding ~11-24s to every `import create_recipe` (and therefore to
the backend boot, which imports it transitively).

These two behavioural tests lock the optimization in place:

  1. `core.optional_import.lazy_module` returns a proxy that does NOT
     import the target until the FIRST attribute access, then forwards
     to the real module (so call sites keep `autogen.AssistantAgent`).
  2. Importing create_recipe / reuse_recipe must NOT pull autogen (or
     its heavy transitive deps google.api_core / torch) into
     sys.modules.  If someone re-adds a top-level `import autogen`, this
     fails loudly.

These are behavioural — they import the real modules, inspect the real
sys.modules state, and exercise the real proxy.  Not grep tests.
"""
import importlib
import subprocess
import sys
import textwrap

import pytest


# ── 1. The lazy_module proxy itself ──────────────────────────────────
def test_lazy_module_defers_until_first_attribute_access():
    from core.optional_import import lazy_module

    # Use a stdlib module that is NOT normally imported at test start.
    # `wave` is tiny, pure-python, and rarely pre-imported.
    target = "wave"
    sys.modules.pop(target, None)

    proxy = lazy_module(target)
    # Creating the proxy must NOT import the module.
    assert target not in sys.modules, (
        "lazy_module imported %s eagerly — defeats the purpose" % target
    )

    # First attribute access triggers the real import and forwards.
    real_attr = proxy.Wave_read  # noqa: F841 — access is the trigger
    assert target in sys.modules, "attribute access did not import the module"

    # The forwarded attribute is identical to the real module's.
    real_mod = importlib.import_module(target)
    assert proxy.Wave_read is real_mod.Wave_read


def test_lazy_module_forwards_repeatedly_without_reimport():
    from core.optional_import import lazy_module

    proxy = lazy_module("base64")
    importlib.import_module("base64")  # ensure loaded
    # Two accesses return the same object (proxy caches the resolved module).
    assert proxy.b64encode is proxy.b64encode


# ── 2. create_recipe / reuse_recipe must not eagerly pull autogen ────
# Run in a FRESH subprocess so the test-session's own sys.modules (which
# may already contain autogen from another test) cannot mask a regression.
# The probe imports the module then prints a uniquely-marked line listing
# any heavy modules that leaked into sys.modules.  A unique marker is used
# because create_recipe emits its own INFO log lines to stdout at import,
# so we cannot rely on "last line" — we grep for the marker instead.
_MARK = "@@HEAVY@@"
_PROBE = textwrap.dedent(
    """
    import sys
    import {mod}            # noqa
    heavy = [m for m in ("autogen", "google.api_core", "torch", "flaml")
             if m in sys.modules]
    print("{mark}" + ",".join(heavy))
    """
)


@pytest.mark.parametrize("mod", ["hartos.create_recipe", "hartos.reuse_recipe"])
def test_module_import_does_not_pull_autogen(mod):
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE.format(mod=mod, mark=_MARK)],
        capture_output=True,
        text=True,
        cwd=str(_repo_root()),
        timeout=180,
    )
    assert proc.returncode == 0, (
        "importing %s failed:\nSTDOUT:%s\nSTDERR:%s"
        % (mod, proc.stdout, proc.stderr[-2000:])
    )
    marked = [ln for ln in proc.stdout.splitlines() if ln.startswith(_MARK)]
    assert marked, (
        "probe produced no marker line — STDOUT:%s STDERR:%s"
        % (proc.stdout[-500:], proc.stderr[-1000:])
    )
    offenders = marked[-1][len(_MARK):].strip()
    assert offenders == "", (
        "%s eagerly imported heavy module(s): %s — autogen must stay lazy"
        % (mod, offenders)
    )


_NO_AUTOGEN_PROBE = textwrap.dedent(
    """
    import sys

    class _Block:
        def find_module(self, name, path=None):
            return self if name == "autogen" or name.startswith("autogen.") else None
        def find_spec(self, name, path=None, target=None):
            if name == "autogen" or name.startswith("autogen."):
                raise ImportError("blocked for test: autogen is not installed")
            return None
    sys.meta_path.insert(0, _Block())

    import gather_agentdetails          # must NOT raise
    print("IMPORT_OK")

    # the module's own guard must still be reachable and still explain itself
    try:
        gather_agentdetails.create_agents_for_user("u1")
    except ImportError as exc:
        print("GUARD_OK", "pyautogen" in str(exc))
    except Exception as exc:                       # noqa: BLE001
        print("GUARD_WRONG", type(exc).__name__, exc)
    """
)


def test_module_imports_on_a_node_without_autogen():
    """gather_agentdetails must import where autogen is absent.

    Regression (2026-08-08 nightly, every nixosTests shard, 300 occurrences):

        AttributeError: 'NoneType' object has no attribute 'AssistantAgent'

    The module bound `autogen = None` on ImportError, then annotated two
    functions with `autogen.AssistantAgent`. Annotations are evaluated when the
    `def` executes — at module import — so the module died before reaching any
    function body, and its own `if autogen is None: raise ImportError(...)`
    guard sat BELOW the statement that killed it and could never run.

    Fixed by canonicalising onto the same mechanism create_recipe.py uses
    (`lazy_module`, no None sentinel to miss) plus PEP 563 annotations.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _NO_AUTOGEN_PROBE],
        capture_output=True, text=True, cwd=str(_repo_root()), timeout=180,
    )
    assert "IMPORT_OK" in proc.stdout, (
        "gather_agentdetails failed to import without autogen:\n"
        "STDOUT:%s\nSTDERR:%s" % (proc.stdout, proc.stderr[-2500:])
    )
    assert "GUARD_WRONG" not in proc.stdout, (
        "the missing-autogen path raised the wrong exception type: %s"
        % proc.stdout
    )


def _repo_root():
    import pathlib
    return pathlib.Path(__file__).resolve().parents[2]
