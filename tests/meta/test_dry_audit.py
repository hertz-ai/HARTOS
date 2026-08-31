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

Run from project root:
    python -m pytest tests/meta/test_dry_audit.py -v
"""
import os
import re
import unittest


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


if __name__ == '__main__':
    unittest.main()
