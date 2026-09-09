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
SKIP = {'.git', '__pycache__', 'venv', '.venv', 'venv-hart', 'node_modules',
        'claw_native', 'build', 'dist', '.claude', 'landing-page', 'scratchpad'}

# ── BUDGETS — measured 2026-08-17. RATCHET DOWN ONLY. ────────────────────────
# Raising a number here to make CI pass is the failure mode this guard exists to
# prevent: it converts a regression into a silently accepted new normal.
MAX_BARE_SWALLOWS = 1521        # `except ...: pass` (was 1533 -> 1528 -> 1521;
                                # 2026-08-30: world_model_bridge.py's 24 silent
                                # swallows converted to logged debug/warning
                                # excepts — security-control absences now WARN
                                # (task #6, no-silent-gulping), best-effort paths
                                # log debug. Net tree count 1545 -> 1521.)

#: security/ gets its OWN, TIGHTER budget. A swallow here is worst: a control that
#: fails without saying so still reports green, so the system cannot tell a working
#: guardrail from a broken one. Drive this to zero first, ahead of the global count.
#: Note 4 of the remainder are in hive_guardrails.py, which CLAUDE.md forbids
#: modifying (circuit breaker / structural immutability) — they need the steward,
#: not a refactor.
MAX_SECURITY_SWALLOWS = 63      # was 68
MAX_GOD_MODULES = 9             # SOURCE files > 3000 lines (tests excluded —
                                # the first draft said 11 by counting
                                # test_nixos_configs.py and test_agent_engine.py,
                                # and this guard's own staleness check caught it)
MAX_DUP_PUBLIC_NAMES = 122      # one public name defined in >1 SOURCE module

#: The core-pipeline helpers that are verbatim-duplicated across the CREATE/REUSE
#: files. P1 of the graph collapses each to one home in core/. Each name that
#: reaches a single definition should be DELETED from this dict (not set to 1) so
#: the list itself shrinks to nothing.
#: MEASURED CORRECTION 2026-08-18: most of these are NOT copies. `create_recipe`
#: and `reuse_recipe` hold 2-line DELEGATING SHIMS that forward to helper.py — a
#: facade, not duplication. An AST diff (normalised for docstrings/whitespace)
#: showed only ONE entry was real, live, 88-line duplication: `subscribe_and_return`,
#: and it had ALREADY DRIFTED. The audit's "every fix must be made four times" was
#: too strong for the rest; the numbers below are definition COUNTS, and a count
#: above 1 is a question to ask, not automatically a defect.
PIPELINE_DUP_BUDGET = {
    'get_frame': 4,               # hie + helper are 2 REAL impls; create/reuse forward
    'parse_date': 4,              # all four IDENTICAL 2-line forwarders
    'get_llm_config': 3,          # helper implements; create/reuse forward
    'save_conversation_db': 3,    # helper implements; create/reuse forward
    'publish_async': 3,           # hie has a real 52-line one; create/reuse forward
    'get_action_user_details': 3, # 3-line forwarders to core.user_context
    # subscribe_and_return: COLLAPSED 2026-08-18 (was 3). The 88-line copy in
    # create_recipe and reuse_recipe is gone; helper.py owns the one
    # implementation and all four call sites route through it. The floor is 2,
    # not 1, because crossbar_server.py defines a DIFFERENT 9-line function of
    # the same name (global response_message/response_event) — a genuine separate
    # concern, not a copy. Renaming that one would take this to 1.
    'subscribe_and_return': 2,
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
    sec_swallows = 0
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
                    if rel.split(os.sep)[0] == 'security':
                        sec_swallows += 1
    return defs, swallows, sec_swallows, god


class RepoHealthRatchet(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.defs, cls.swallows, cls.sec_swallows, cls.god = _measure()

    def test_security_swallows_do_not_grow(self):
        """Tighter than the global budget on purpose: in security/ a swallowed
        failure means a control that is not working while everything reports
        green. Drive this one to zero first."""
        self.assertLessEqual(
            self.sec_swallows, MAX_SECURITY_SWALLOWS,
            "silent `except: pass` under security/ rose to %d (budget %d). A "
            "security control that fails without saying so is indistinguishable "
            "from one that works." % (self.sec_swallows, MAX_SECURITY_SWALLOWS))

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
        if self.sec_swallows < MAX_SECURITY_SWALLOWS:
            stale.append("MAX_SECURITY_SWALLOWS=%d but actual is %d — lower it"
                         % (MAX_SECURITY_SWALLOWS, self.sec_swallows))
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
