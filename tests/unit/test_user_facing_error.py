"""#716: raw internal errors were returned as the reply and SPOKEN by TTS.

Live observation 2026-08-31 (probe run 4): the user-visible reply was
"Error getting response: Error code: 500 - {'error': {'code': 500,
'message': 'Context size has been exceeded.', ...}}" - read aloud.
user_facing_error is the ONE formatter for the three reply chokepoints
(reuse get_agent_response, create get_response_group,
gather_agentdetails); a guard test pins that no chokepoint regrows an
inline raw-error f-string.

    python -m pytest tests/unit/test_user_facing_error.py --noconftest -q
"""
from pathlib import Path
import unittest

from core.agent_tools import user_facing_error

_ROOT = Path(__file__).resolve().parents[2]


class UserFacingError(unittest.TestCase):

    def test_internal_error_text_never_surfaces(self):
        raw = ("Error code: 500 - {'error': {'code': 500, 'message': "
               "'Context size has been exceeded.', 'type': 'server_error'}}")
        out = user_facing_error(Exception(raw))
        self.assertNotIn('Context size', out)
        self.assertNotIn('500', out)
        self.assertIn('try again', out)

    def test_short_benign_reason_is_kept(self):
        out = user_facing_error(ValueError('recipe file missing'))
        self.assertIn('recipe file missing', out)
        self.assertTrue(out.startswith("I couldn't finish"))

    def test_long_text_is_swallowed(self):
        out = user_facing_error(Exception('x' * 500))
        self.assertNotIn('xxxx', out)

    def test_no_chokepoint_returns_raw_error_fstring(self):
        """Drift-guard: the three reply chokepoints must route through
        user_facing_error, not inline f-strings with str(e)."""
        for rel in ('hartos/reuse_recipe.py', 'hartos/create_recipe.py',
                    'hartos/gather_agentdetails.py'):
            src = (_ROOT / rel).read_text(encoding='utf-8',
                                          errors='replace')
            self.assertNotIn('return f"Error getting response: {str(e)}"',
                             src, rel)
            self.assertNotIn('return f"An error occurred: {str(e)}"',
                             src, rel)


if __name__ == '__main__':
    unittest.main()
