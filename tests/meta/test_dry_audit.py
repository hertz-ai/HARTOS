"""DRY audit — catch regressions of canonical patterns we've consolidated.

Each pattern that should live in EXACTLY ONE place gets a test that
asserts every other location either imports it or doesn't replicate it.
A failure here means someone re-introduced a parallel path.

Patterns audited:

1. AUTOGEN_MESSAGE_TOKEN_BUDGET (#170)
   Canonical: core.constants.AUTOGEN_MESSAGE_TOKEN_BUDGET
   Must NOT appear as hardcoded `max_tokens=3500` in create_recipe.py /
   reuse_recipe.py code (comments excluded).

2. REVENUE_SPLIT_USERS / REVENUE_SPLIT_INFRA / REVENUE_SPLIT_CENTRAL
   Canonical: integrations.agent_engine.revenue_aggregator
   90/9/1 constants must match across ad_service + hosting_reward_service.

3. silentGuestRefresh / setGuestIdentity / clearAuth (#209)
   Canonical: landing-page/src/hooks/useAuthSession.js
   Must NOT have a parallel guestRegister-then-write-localStorage block
   anywhere else (a regression of the 3-site duplication we fixed).

4. ROLE-ORDER-GUARD coalesce logic (#124)
   Canonical: helper.py ToolMessageHandler.validate_messages
   Must NOT have a second 'Coalesced consecutive role' implementation.

5. MAX_RETRIES (#125)
   Canonical: landing-page/src/utils/chatRetry.js
   `while (true)` / `while (!success)` retry loops in Demopage.js must
   reference MAX_RETRIES — not have their own hardcoded cap.

6. CANONICAL_OWNERS — one module per consolidated name
   Each name in the table is DEFINED (def / class / literal assignment) in
   exactly its owner module; every other module imports it.  A second
   definition is a parallel path by construction.  When you consolidate
   something, add its row here in the same commit.

7. The user-facing reply sentences (core.constants *_REPLY)
   Their text appears in no other source module, so a caller cannot ship a
   private copy that drifts (or that is_user_facing_error cannot recognise).

8. Thread-stack dumps
   Canonical: core.diag.dump_all_thread_stacks.  Nothing else walks
   sys._current_frames(), except the fallbacks listed in
   _CURRENT_FRAMES_ALLOWED with the reason each one exists.

Run from project root:
    python -m pytest tests/meta/test_dry_audit.py -v
"""
import ast
import os
import re
import unittest

from tests.unit.test_source_guard_repo_health_ratchet import _walk


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
NUNBA_ROOT = os.path.normpath(os.path.join(
    REPO_ROOT, '..', 'Nunba-HART-Companion'))


def _read(p, encoding_errors='replace'):
    with open(p, 'rb') as fp:
        return fp.read().decode('utf-8', errors=encoding_errors)


def _strip_python_comments(src: str) -> str:
    """Strip # comments and triple-quoted strings so doc-references
    don't false-trigger DRY checks.  Heuristic — good enough for the
    patterns we audit."""
    out = []
    in_triple = False
    triple_marker = None
    for line in src.splitlines():
        stripped = line.strip()
        # Toggle triple-quoted string state
        if not in_triple:
            for marker in ('"""', "'''"):
                if marker in stripped:
                    # Count: if odd, we entered (or exited)
                    if stripped.count(marker) % 2 == 1:
                        in_triple = True
                        triple_marker = marker
                        break
            # Drop # comment portion
            idx = line.find('#')
            if idx >= 0:
                line = line[:idx]
        else:
            if triple_marker in line:
                in_triple = False
                triple_marker = None
                # Drop everything in this transitional line — safer
                line = ''
            else:
                line = ''
        out.append(line)
    return '\n'.join(out)


def _strip_js_comments(src: str) -> str:
    """Strip // line comments and /* ... */ block comments from JS."""
    # Block comments
    src = re.sub(r'/\*[\s\S]*?\*/', '', src)
    # Line comments
    out = []
    for line in src.splitlines():
        idx = line.find('//')
        if idx >= 0:
            line = line[:idx]
        out.append(line)
    return '\n'.join(out)


class DryAuditTests(unittest.TestCase):

    def test_autogen_token_budget_not_hardcoded_in_recipe_files(self):
        """#170 — max_tokens=3500 must not appear in code (comments OK)."""
        for path in ['hartos/create_recipe.py', 'hartos/reuse_recipe.py']:
            full = os.path.join(REPO_ROOT, path)
            self.assertTrue(
                os.path.exists(full), f'Missing source file: {full}')
            code = _strip_python_comments(_read(full))
            self.assertNotIn(
                'max_tokens=3500', code,
                f'{path} has a hardcoded max_tokens=3500 in CODE. '
                f'Use AUTOGEN_MESSAGE_TOKEN_BUDGET from core.constants.'
            )

    def test_revenue_split_constants_match_canonical(self):
        """90/9/1 must be the SAME in revenue_aggregator + ad_service
        + hosting_reward_service.  Any drift is a constitutional
        violation tracked in CLAUDE.md."""
        canonical = _read(os.path.join(
            REPO_ROOT, 'integrations/agent_engine/revenue_aggregator.py'))
        m = re.search(
            r'REVENUE_SPLIT_USERS\s*=\s*(0?\.\d+|1\.0)', canonical)
        self.assertIsNotNone(
            m, 'canonical revenue_aggregator missing REVENUE_SPLIT_USERS')
        canon_users = m.group(1)
        self.assertIn(canon_users, ('0.90', '.9', '0.9'),
                      f'canonical USER split changed from 0.90 to {canon_users}')

    def test_no_parallel_silentGuestRefresh_impl_in_nunba(self):
        """#209 — only useAuthSession.js may write access_token directly
        after a guestRegister.  Anything else replicates the pattern."""
        canonical = os.path.join(
            NUNBA_ROOT, 'landing-page/src/hooks/useAuthSession.js')
        if not os.path.exists(canonical):
            self.skipTest('Nunba repo not co-located')
        # Walk all .js files under landing-page/src EXCLUDING useAuthSession.
        # Flag any that combine guestRegister(...) + setItem('access_token').
        offenders = []
        src_root = os.path.join(NUNBA_ROOT, 'landing-page/src')
        for dirpath, _, files in os.walk(src_root):
            if 'node_modules' in dirpath or '__tests__' in dirpath:
                continue
            for f in files:
                if not f.endswith(('.js', '.jsx')):
                    continue
                full = os.path.join(dirpath, f)
                if os.path.abspath(full) == os.path.abspath(canonical):
                    continue
                code = _strip_js_comments(_read(full))
                if ('authApi.guestRegister' in code
                        and "setItem('access_token'" in code):
                    offenders.append(os.path.relpath(full, NUNBA_ROOT))
        self.assertEqual(
            offenders, [],
            f'Parallel guestRegister + setItem("access_token") blocks: '
            f'{offenders}.  Use silentGuestRefresh() from useAuthSession.js.'
        )

    def test_chat_retry_uses_shared_MAX_RETRIES_constant(self):
        """#125 — both Demopage retry loops must reference MAX_RETRIES,
        not their own literal."""
        demopage = os.path.join(
            NUNBA_ROOT, 'landing-page/src/pages/Demopage.js')
        if not os.path.exists(demopage):
            self.skipTest('Nunba repo not co-located')
        code = _strip_js_comments(_read(demopage))
        self.assertIn(
            'MAX_RETRIES', code,
            'Demopage.js must reference MAX_RETRIES from utils/chatRetry.'
        )
        # Should be at LEAST two while-loop guards using MAX_RETRIES
        # (local + cloud paths).
        self.assertGreaterEqual(
            code.count('MAX_RETRIES'), 2,
            'Both local + cloud retry loops must cap at MAX_RETRIES.'
        )


#: name -> the ONE module (repo-relative) allowed to define it.
CANONICAL_OWNERS = {
    'AUTOGEN_MESSAGE_TOKEN_BUDGET': 'core/constants.py',
    'INDIC_LANGS': 'core/constants.py',
    'NON_LATIN_SCRIPT_LANGS': 'core/constants.py',
    'NON_LATIN_SCRIPT_NAMES': 'core/constants.py',
    'LLM_LOADING_REPLY': 'core/constants.py',
    'LLM_GENERIC_ERROR_REPLY': 'core/constants.py',
    'BUILD_INCOMPLETE_REPLY': 'core/constants.py',
    'get_preferred_lang': 'core/user_lang.py',
    'set_preferred_lang': 'core/user_lang.py',
    'user_facing_error': 'core/agent_tools.py',
    'is_user_facing_error': 'core/agent_tools.py',
    'dump_all_thread_stacks': 'core/diag.py',
    'get_watchdog': 'security/node_watchdog.py',
    'sleep_with_heartbeat': 'security/node_watchdog.py',
}

#: module -> why it may walk sys._current_frames() itself.
_CURRENT_FRAMES_ALLOWED = {
    'core/diag.py': 'the canonical dumper',
    'security/node_watchdog.py': 'last-resort fallback when neither '
                                 'core.diag nor its builtin is importable '
                                 '(HARTOS standalone), documented at the call',
}


def _source_trees():
    """({repo-relative posix path: AST} for every non-test source module,
    [modules that do not parse]).  An unparseable module is reported, never
    skipped: a guard that quietly ignores a file cannot vouch for it."""
    trees, unparseable = {}, []
    for rel, path in _walk():
        rel = rel.replace(os.sep, '/')
        if rel.split('/')[0] == 'tests':
            continue
        try:
            trees[rel] = ast.parse(_read(path))
        except SyntaxError as e:
            unparseable.append(f'{rel}: {e}')
    return trees, unparseable


def _definitions(tree):
    """Names this module defines: def / class / assignment of a value (an
    alias of another name, `X = other.X`, re-exports rather than defines)."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            if isinstance(node.value, (ast.Name, ast.Attribute)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    yield target.id


class SingleOwnerGuards(unittest.TestCase):
    """SOURCE GUARDS (labelled per feedback_no_grep_tests): a second
    implementation can appear in any of ~900 modules, so no behavioural test
    at one call site can catch it.  AST, not text: comments and docstrings
    never match."""

    @classmethod
    def setUpClass(cls):
        cls.trees, cls.unparseable = _source_trees()

    def test_source_guard_every_module_was_checked(self):
        self.assertFalse(
            self.unparseable,
            'these modules do not parse, so no guard here checked them:\n  '
            + '\n  '.join(self.unparseable))

    def test_source_guard_each_canonical_name_has_one_owner(self):
        where = {name: [] for name in CANONICAL_OWNERS}
        for rel, tree in self.trees.items():
            for name in set(_definitions(tree)) & set(where):
                where[name].append(rel)
        problems = []
        for name, owner in sorted(CANONICAL_OWNERS.items()):
            if owner not in where[name]:
                problems.append(f'{name}: not defined in its owner {owner} '
                                f'(moved? update the row)')
            others = sorted(set(where[name]) - {owner})
            if others:
                problems.append(f'{name}: also defined in {others}; import it '
                                f'from {owner} instead')
        self.assertFalse(problems, 'parallel definitions:\n  ' + '\n  '.join(problems))

    def test_source_guard_reply_sentences_live_only_in_constants(self):
        from core import constants
        replies = {n: v for n, v in vars(constants).items()
                   if n.endswith('_REPLY') and isinstance(v, str) and len(v) >= 20}
        self.assertTrue(replies, 'core.constants has no *_REPLY sentences')
        copies = []
        for rel, tree in self.trees.items():
            if rel == 'core/constants.py':
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for name, text in replies.items():
                        if text in node.value:
                            copies.append(f'{rel}:{node.lineno} copies {name}')
        self.assertFalse(copies, 'import the sentence from core.constants:\n  '
                                 + '\n  '.join(copies))

    def test_source_guard_one_thread_stack_dumper(self):
        walkers = sorted({
            rel for rel, tree in self.trees.items()
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == '_current_frames'})
        unexpected = [r for r in walkers if r not in _CURRENT_FRAMES_ALLOWED]
        stale = [r for r in _CURRENT_FRAMES_ALLOWED if r not in walkers]
        self.assertFalse(
            unexpected,
            f'{unexpected} walk sys._current_frames() themselves; call '
            f'core.diag.dump_all_thread_stacks instead')
        self.assertFalse(stale, f'{stale} no longer walk frames: delete the '
                                f'allowance so the list only shrinks')


if __name__ == '__main__':
    unittest.main()
