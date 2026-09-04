"""Guard: the reuse Assistant and StatusVerifier are given today's date as
ground truth.

Live root cause 2026-09-05 (Auto Research reuse 18088688973, installed build):
the local Qwen3.5-4B's training cutoff made it assume it was 2024 — it refused
"2026" research as "the future", injected "2024" into its own google_search
query, and the StatusVerifier errored "the current year is 2024". The agent
context carried no current date. Injecting today's date (from datetime.now())
into both reasoning agents fixes the refusal at the source (one `_date_ctx`
string, given to the Assistant prompt and to the StatusVerifier via
update_system_message).

AST guard (no live llama): the selector-building module must construct a
date-context from datetime.now() and hand it to both agents.
"""
import os
import unittest


SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos', 'reuse_recipe.py')


class ReuseDateContext(unittest.TestCase):
    def setUp(self):
        self.src = open(SRC, encoding='utf-8').read()

    def test_date_context_built_from_now(self):
        self.assertIn('_date_ctx', self.src,
                      "a current-date context string must be built")
        self.assertIn("datetime.now().strftime", self.src,
                      "the date context must come from datetime.now(), not a "
                      "hardcoded year")

    def test_date_context_reaches_status_verifier(self):
        self.assertIn('verify.update_system_message(_date_ctx', self.src,
                      "the StatusVerifier must receive the date context so it "
                      "does not reject current-year work as impossible")

    def test_date_context_in_assistant_prompt(self):
        self.assertIn("f'''{_date_ctx}", self.src,
                      "the Assistant system prompt must lead with the date "
                      "context so the model treats the current year as present")


if __name__ == '__main__':
    unittest.main()
