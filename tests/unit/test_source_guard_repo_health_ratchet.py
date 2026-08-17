"""SOURCE GUARD (labelled per feedback_no_grep_tests) — the repo-health RATCHET.

P0 of the refactor graph in docs/architecture/HARTOS_PARALLEL_PATH_AUDIT.md.

WHY A SOURCE GUARD IS LEGITIMATE HERE
─────────────────────────────────────
The findings are properties of the TREE, not of any one call site: "the same
public name is defined in four modules", "1,533 bare excepts". No behavioural test
at any single location can catch the 123rd duplicate or the 1,534th swallow. This
is exactly the DRY-across-many-files carve-out, and it ACCOMPANIES behavioural
tests elsewhere rather than replacing them.

WHAT IT DOES
────────────
Measures the tree with an AST walk (exhaustive — reading misses things at 837
files) and asserts the numbers never grow. It is a RATCHET, not a target: it does
not demand the debt be fixed today, it demands it stop growing while it is fixed.

When you improve something, LOWER the budget in the same commit. A budget that
drifts above reality stops being a gate.
"""
import ast
import collections
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKIP = {'.git', '__pycache__', 'venv', '.venv', 'node_modules', 'claw_native',
        'build', 'dist', '.claude', 'landing-page'}

# ── BUDGETS — measured 2026-08-17. RATCHET DOWN ONLY. ────────────────────────
# Raising a number here to make CI pass is the failure mode this guard exists to
# prevent: it converts a regression into a silently accepted new normal.
MAX_BARE_SWALLOWS = 1533        # `except ...: pass`
MAX_GOD_MODULES = 9             # SOURCE files > 3000 lines (tests excluded —
                                # the first draft said 11 by counting
                                # test_nixos_configs.py and test_agent_engine.py,
                                # and this guard's own staleness check caught it)
MAX_DUP_PUBLIC_NAMES = 122      # one public name defined in >1 SOURCE module

#: The core-pipeline helpers that are verbatim-duplicated across the CREATE/REUSE
#: files. P1 of the graph collapses each to one home in core/. Each name that
#: reaches a single definition should be DELETED from this dict (not set to 1) so
#: the list itself shrinks to nothing.
PIPELINE_DUP_BUDGET = {
    'get_frame': 4,
    'parse_date': 4,
    'get_llm_config': 3,
    'save_conversation_db': 3,
    'publish_async': 3,
    'get_action_user_details': 3,
    'subscribe_and_return': 3,
}


def _walk():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith('.')]
        for fn in files:
            if fn.endswith('.py'):
                p = os.path.join(root, fn)
                yield os.path.relpath(p, REPO), p


def _measure():
    defs = collections.defaultdict(set)
    swallows = 0
    god = []
    for rel, p in _walk():
        try:
            txt = open(p, encoding='utf-8', errors='replace').read()
        except OSError:
            continue
        is_test = rel.split(os.sep)[0] == 'tests'
        if not is_test and txt.count('\n') + 1 > 3000:
            god.append(rel)
        try:
            tree = ast.parse(txt)
        except SyntaxError:
            continue
        if not is_test:
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                    defs[node.name].add(rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                body = [n for n in node.body if not isinstance(n, ast.Expr)]
                if len(body) == 1 and isinstance(body[0], ast.Pass):
                    swallows += 1
    return defs, swallows, god


class RepoHealthRatchet(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.defs, cls.swallows, cls.god = _measure()

    def test_silent_swallows_do_not_grow(self):
        """`except: pass` makes failure invisible. 1,533 of them is why the
        flywheel could be broken for weeks with every dashboard green."""
        self.assertLessEqual(
            self.swallows, MAX_BARE_SWALLOWS,
            "bare `except: pass` count rose to %d (budget %d). Silent failure is "
            "banned by house rule — log it, narrow the except, or delete the try."
            % (self.swallows, MAX_BARE_SWALLOWS))

    def test_god_modules_do_not_grow(self):
        self.assertLessEqual(
            len(self.god), MAX_GOD_MODULES,
            "source files over 3000 lines rose to %d (budget %d):\n  %s"
            % (len(self.god), MAX_GOD_MODULES, "\n  ".join(sorted(self.god))))

    def test_duplicate_public_names_do_not_grow(self):
        dupes = [n for n, f in self.defs.items()
                 if len(f) > 1 and not n.startswith('_')
                 and n not in ('main', 'setup', 'Config', 'Meta')]
        self.assertLessEqual(
            len(dupes), MAX_DUP_PUBLIC_NAMES,
            "public names defined in >1 module rose to %d (budget %d). Note some "
            "are legitimate INTERFACES (channel adapters all define search/post/"
            "timeline); a NEW one is usually not." % (len(dupes), MAX_DUP_PUBLIC_NAMES))

    def test_the_core_pipeline_helpers_do_not_spread_further(self):
        """THE headline finding: the Recipe Pattern — the product's central claim —
        is implemented across four files that share copy-pasted helpers, so every
        fix must be made four times or the paths drift."""
        worse = []
        for name, budget in sorted(PIPELINE_DUP_BUDGET.items()):
            n = len(self.defs.get(name, ()))
            if n > budget:
                worse.append("%s: %d definitions (budget %d)" % (name, n, budget))
        self.assertFalse(
            worse,
            "a core-pipeline helper spread to MORE files:\n  " + "\n  ".join(worse) +
            "\nCollapse it to one home in core/ (P1 of the refactor graph), do not "
            "add another copy.")

    def test_the_budgets_are_not_stale(self):
        """A budget far above reality is not a gate — it is a comment. If the tree
        improved, lower the number in the same commit that improved it."""
        stale = []
        if self.swallows + 50 < MAX_BARE_SWALLOWS:
            stale.append("MAX_BARE_SWALLOWS=%d but actual is %d — lower it"
                         % (MAX_BARE_SWALLOWS, self.swallows))
        if len(self.god) < MAX_GOD_MODULES:
            stale.append("MAX_GOD_MODULES=%d but actual is %d — lower it"
                         % (MAX_GOD_MODULES, len(self.god)))
        for name, budget in PIPELINE_DUP_BUDGET.items():
            n = len(self.defs.get(name, ()))
            if n < budget:
                stale.append("%s budget %d but actual %d — lower it (or delete the "
                             "entry if it reached 1)" % (name, budget, n))
        self.assertFalse(stale, "RATCHET IS STALE — tighten it:\n  " +
                                "\n  ".join(stale))


if __name__ == '__main__':
    unittest.main()
