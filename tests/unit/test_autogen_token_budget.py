"""#170 — autogen token-budget constant lives in core.constants and
the value stays within safe headroom of llama-server's 12288 n_ctx.

477 "Context size has been exceeded" errors in llama_server_8082.log
(2026-05-20) traced back to autogen prompts that exceeded ~7000 tokens
under concurrent slot allocation.  The MessageTokenLimiter caps message
history at AUTOGEN_MESSAGE_TOKEN_BUDGET tokens, but the system prompt +
tool descriptions are appended AFTER the transform — typical overhead
~3000-4000 tokens.  Total prompt = budget + overhead.

Safe headroom calculation:
  - llama-server n_ctx per slot = 12288
  - Concurrent active slots typical = 2-3
  - Per-slot prompt budget (so 2-3 fit) = ~6000-7000 tokens
  - System+tools overhead = ~3500 tokens
  - Therefore message budget MUST be <= 3500 (was 3500, now 2500)
  - 2500 gives 1000-token safety margin

This test pins the contract: the constant must stay under 3500 unless
n_ctx is bumped on the llama-server side first.
"""
import unittest


class AutogenBudgetTests(unittest.TestCase):
    def test_constants_exposed_from_core(self):
        from core.constants import (
            AUTOGEN_MESSAGE_TOKEN_BUDGET,
            AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE,
            AUTOGEN_HISTORY_LIMIT,
        )
        self.assertIsInstance(AUTOGEN_MESSAGE_TOKEN_BUDGET, int)
        self.assertIsInstance(AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE, int)
        self.assertIsInstance(AUTOGEN_HISTORY_LIMIT, int)

    def test_budget_stays_under_safe_ceiling(self):
        """If the budget rises above 3500, the 477-class 400 returns —
        any bump above this requires a matching n_ctx bump on the
        llama-server side AND a concurrency cap reduction."""
        from core.constants import AUTOGEN_MESSAGE_TOKEN_BUDGET
        self.assertLessEqual(
            AUTOGEN_MESSAGE_TOKEN_BUDGET, 3500,
            "Raising AUTOGEN_MESSAGE_TOKEN_BUDGET above 3500 risks "
            "Context-size-exceeded 400s.  See #170."
        )
        self.assertGreaterEqual(
            AUTOGEN_MESSAGE_TOKEN_BUDGET, 1500,
            "Dropping AUTOGEN_MESSAGE_TOKEN_BUDGET below 1500 strips "
            "context that next-step reasoning depends on."
        )

    def test_per_message_cap_does_not_exceed_total_budget(self):
        from core.constants import (
            AUTOGEN_MESSAGE_TOKEN_BUDGET,
            AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE,
        )
        self.assertLessEqual(
            AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE,
            AUTOGEN_MESSAGE_TOKEN_BUDGET,
            "Per-message cap above total budget is non-sensical."
        )

    def test_history_limit_positive(self):
        from core.constants import AUTOGEN_HISTORY_LIMIT
        self.assertGreater(AUTOGEN_HISTORY_LIMIT, 0)

    def _strip_comments(self, source: str) -> str:
        """Strip # comments line-by-line so doc references like
        '# was max_tokens=3500' don't false-trigger the DRY check."""
        out = []
        for line in source.splitlines():
            idx = line.find('#')
            out.append(line[:idx] if idx >= 0 else line)
        return '\n'.join(out)

    def test_create_recipe_uses_the_constant_not_hardcoded(self):
        """DRY check: create_recipe.py must reference the constant,
        not carry its own 3500 literal in the MessageTokenLimiter
        construction.  A regression here means someone re-introduced
        the magic number."""
        with open('create_recipe.py', 'rb') as fp:
            content = fp.read().decode('utf-8', errors='replace')
        code_only = self._strip_comments(content)
        self.assertNotIn(
            'max_tokens=3500',
            code_only,
            "create_recipe.py CODE contains a hardcoded max_tokens=3500. "
            "Use AUTOGEN_MESSAGE_TOKEN_BUDGET from core.constants."
        )
        self.assertIn(
            'AUTOGEN_MESSAGE_TOKEN_BUDGET',
            content,
            "create_recipe.py must import + use AUTOGEN_MESSAGE_TOKEN_BUDGET."
        )

    def test_reuse_recipe_uses_the_constant_not_hardcoded(self):
        with open('reuse_recipe.py', 'rb') as fp:
            content = fp.read().decode('utf-8', errors='replace')
        code_only = self._strip_comments(content)
        # NOTE: reuse_recipe.py:818 intentionally uses max_tokens=3000
        # (NOT 3500) for the select_speaker prompt — that's a tighter,
        # different transform.  We're only asserting no 3500 remains
        # in actual code (not in docstring comments).
        self.assertNotIn(
            'max_tokens=3500',
            code_only,
            "reuse_recipe.py CODE contains a hardcoded max_tokens=3500. "
            "Use AUTOGEN_MESSAGE_TOKEN_BUDGET from core.constants."
        )
        self.assertIn(
            'AUTOGEN_MESSAGE_TOKEN_BUDGET',
            content,
            "reuse_recipe.py must import + use AUTOGEN_MESSAGE_TOKEN_BUDGET."
        )


if __name__ == '__main__':
    unittest.main()
