"""The refusal-override must stand down once a tool has already run.

Live 2026-08-22 11:34 (weather-in-chennai turn): the model called
google_search, the tool returned nothing, and the model truthfully said
"Since the tool didn't return any weather data, I can't give you the
specific forecast for Chennai right now."  _REFUSAL_PATTERN matched
"I can't give", [REFUSAL-OVERRIDE-LANGCHAIN] re-prompted with "Never
assert data is unavailable without first attempting a tool call", and
the retry FABRICATED a forecast (sunny, 32°C) that reached the user as
fact.  The override's own prompt states its contract — force ONE tool
attempt — so when the outbound prompt already carries the
TEMPLATE_TOOL_RESPONSE scaffold (a tool observation), a refusal-shaped
reply is a report, not a refusal, and must pass through unmodified.

    python -m pytest tests/unit/test_refusal_override_post_tool_guard.py --noconftest -q
"""
import os
import re
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_HIE_PATH = Path(__file__).resolve().parents[2] / 'hart_intelligence_entry.py'

# Truthful post-tool reply in the live turn's shape (fenced agent JSON;
# the live text was log-truncated, so the tail here is reconstructed to
# hit the same "I don't have … data" knowledge-cutoff branch — each
# behavioural test asserts the pattern ACTUALLY matches as a
# precondition, so this can never pass vacuously).
_POST_TOOL_REPORT = (
    '```json\n{\n    "action": "Final Answer",\n    "action_input": '
    '"Since the tool didn\'t return any weather data, I can\'t give you '
    'the specific forecast for Chennai right now. I don\'t have current '
    'weather data without it."\n}\n```'
)

# The IPL-style pre-tool refusal the override exists for (2026-05-13
# request 301eeed0) — this MUST keep triggering the retry.
_PRE_TOOL_REFUSAL = "I don't have the 2026 IPL table yet."

_SCAFFOLD = 'Okay, so what is response for this tool'


def _resp(content):
    r = MagicMock()
    r.json.return_value = {'choices': [{'message': {'content': content}}]}
    return r


class TestPostToolGuard(unittest.TestCase):
    def _run(self, prompt_content, reply):
        """Drive _pooled_post_with_refusal_check with pooled_post mocked.

        Returns the pooled_post mock so callers can count invocations:
        1 call = override skipped, 2 calls = override retried.
        """
        import hart_intelligence_entry as hie
        pooled = MagicMock(return_value=_resp(reply))
        body = {'messages': [{'role': 'user', 'content': prompt_content}]}
        with patch.object(hie, 'pooled_post', pooled), \
                patch.dict(os.environ, {'HEVOLVE_LANGCHAIN_REFUSAL_OVERRIDE': '1'}):
            hie._pooled_post_with_refusal_check(
                'http://127.0.0.1:8080/v1/chat/completions',
                json=body, app_logger=MagicMock())
        return pooled

    def _assert_pattern_matches(self, reply):
        """Precondition: the reply must trip _REFUSAL_PATTERN, otherwise the
        behavioural assertion below would pass vacuously (no retry because
        no refusal was detected, not because the guard worked)."""
        from integrations.agent_engine.speculative_dispatcher import _REFUSAL_PATTERN
        self.assertIsNotNone(
            _REFUSAL_PATTERN.search(reply),
            f"test reply no longer matches _REFUSAL_PATTERN: {reply[:80]!r}")

    def test_post_tool_report_is_not_overridden(self):
        self._assert_pattern_matches(_POST_TOOL_REPORT)
        prompt = (
            "TOOL RESPONSE:\n---------------------\n\n\n"
            "USER'S INPUT\n--------------------\n\n" + _SCAFFOLD +
            ". If using information obtained from the tools you must mention"
        )
        pooled = self._run(prompt, _POST_TOOL_REPORT)
        self.assertEqual(
            pooled.call_count, 1,
            "a refusal-shaped reply AFTER a tool observation is a truthful "
            "report — the override retry fabricates data (live 11:34 turn)")

    def test_pre_tool_refusal_still_overridden(self):
        self._assert_pattern_matches(_PRE_TOOL_REFUSAL)
        pooled = self._run("What is the 2026 IPL table?", _PRE_TOOL_REFUSAL)
        self.assertEqual(
            pooled.call_count, 2,
            "the original IPL case — refusal with NO tool attempt — must "
            "still trigger the forced-tool retry")

    def test_non_refusal_reply_untouched(self):
        pooled = self._run("hello", "Hi! How can I help you today?")
        self.assertEqual(pooled.call_count, 1)

    def test_scaffold_constant_bound_to_template(self):
        """TOOL_RESPONSE_SCAFFOLD must stay byte-identical to the phrase in
        TEMPLATE_TOOL_RESPONSE (hart_intelligence_entry:~7519).  The template
        cannot interpolate the constant (its braces are format-escaped), so
        this test is the binding: reword either copy and it goes red."""
        import hart_intelligence_entry as hie
        self.assertEqual(hie.TOOL_RESPONSE_SCAFFOLD, _SCAFFOLD)
        src = _HIE_PATH.read_text(encoding='utf-8')
        self.assertGreaterEqual(
            src.count(_SCAFFOLD), 2,
            "scaffold phrase must appear in BOTH the constant and the "
            "TEMPLATE_TOOL_RESPONSE template")
        m = re.search(r'TEMPLATE_TOOL_RESPONSE = """.*?"""', src, re.DOTALL)
        self.assertIsNotNone(m, "TEMPLATE_TOOL_RESPONSE template not found")
        self.assertIn(_SCAFFOLD, m.group(0))


if __name__ == '__main__':
    unittest.main()
