"""A tool result must be matched to its OWN tool_call_id.

Autogen's executor returns (proven from the shipped bytecode's dict-key
tuples in lib/autogen/agentchat/conversable_agent.pyc,
generate_tool_calls_reply):

    ('tool_call_id', 'role', 'content')      <- each per-call return
    ('role', 'tool_responses', 'content')    <- the OUTER tool message

so the outer message carries NO top-level 'tool_call_id'; the authoritative
id sits in tool_responses[i]['tool_call_id'].

ToolMessageHandler.apply_transform matched only on a TOP-LEVEL tool_call_id
(helper.py:1670 `if 'tool_call_id' in msg`). Finding nothing, it fell into a
repair branch that GUESSES from the pending calls of the most recent
assistant. With one pending call the guess is accidentally right; with
several it binds the result to the wrong call and leaves the others
unanswered, which is what mints HISTORICAL_TOOL_PLACEHOLDER over real output.
For parallel returns the joined blob was copied to every pending id, so one
tool's output was served as several tools' answers.

Measured live 2026-09-07 (agent 19794274829, 358s drive): 60x "Adding
missing tool_call_id", 83x "Added placeholder for historical tool_call_id",
against only 5 real tool executions. The message list at a mint showed three
assistants each carrying one tool_call and exactly ONE role='tool' message.

These tests drive the real apply_transform. They are behavioural: they
assert on the returned message list, never on source text.
"""
import unittest
from unittest.mock import MagicMock, patch


_fake_current_app = MagicMock()
_fake_current_app.logger = MagicMock()


def _assistant(call_id, fn="f"):
    return {
        'role': 'assistant', 'content': '',
        'tool_calls': [{'id': call_id, 'type': 'function',
                        'function': {'name': fn, 'arguments': '{}'}}],
    }


def _autogen_tool_message(*pairs):
    """The shape autogen actually emits: id lives under tool_responses."""
    return {
        'role': 'tool',
        'tool_responses': [
            {'tool_call_id': cid, 'role': 'tool', 'content': body}
            for cid, body in pairs
        ],
        'content': '\n\n'.join(body for _, body in pairs),
    }


class ToolResponseIdResolutionTests(unittest.TestCase):
    def setUp(self):
        self._patcher = patch('hartos.helper.current_app', _fake_current_app)
        self._patcher.start()
        from hartos.helper import ToolMessageHandler
        self.handler = ToolMessageHandler(user_tasks=None, user_prompt=None)

    def tearDown(self):
        self._patcher.stop()

    @staticmethod
    def _answers(out):
        """tool_call_id -> content, for every tool message in the output."""
        return {m.get('tool_call_id'): str(m.get('content', ''))
                for m in out if m.get('role') == 'tool'}

    def _run(self, messages):
        """Drive the REAL entry point.

        apply_transform (helper.py:1546) is autogen's transform hook and is
        where tool messages are matched to their calls; it ends by calling
        validate_messages (the ROLE-ORDER-GUARD, :1038), which never assigns
        ids and must not. An earlier draft of this file drove the guard and
        proved nothing — the same wrong-boundary mistake that let a Nunba
        adapter test pass while the HTTP contract was false.
        """
        return self.handler.apply_transform(messages)

    def test_result_attaches_to_its_own_call_not_the_newest(self):
        """The live shape. Three assistants, one tool_call each; the single
        tool message answers the FIRST call. It must not be bound to the
        third just because that assistant spoke most recently."""
        messages = [
            {'role': 'user', 'content': 'go'},
            _assistant('call_A', 'alpha'),
            {'role': 'user', 'name': 'StatusVerifier', 'content': 'checking'},
            _assistant('call_B', 'beta'),
            {'role': 'user', 'name': 'StatusVerifier', 'content': 'checking'},
            _assistant('call_C', 'gamma'),
            _autogen_tool_message(('call_A', 'REAL OUTPUT FOR A')),
        ]
        out = self._run(messages)
        answers = self._answers(out)

        self.assertIn(
            'call_A', answers,
            f"the result never reached its own call; answers={answers}")
        self.assertIn(
            'REAL OUTPUT FOR A', answers['call_A'],
            "call_A's slot does not carry the real output")
        self.assertNotIn(
            'call_C', answers,
            "the result was mis-bound to the most recent call — call_C never "
            "produced anything, so this answer is fabricated")

    def test_parallel_results_each_keep_their_own_output(self):
        """One assistant, two parallel calls, one autogen message carrying
        both returns. Each id must get ITS OWN content, not a copy of the
        joined blob."""
        messages = [
            {'role': 'user', 'content': 'do both'},
            {'role': 'assistant', 'content': '',
             'tool_calls': [
                 {'id': 'p1', 'type': 'function',
                  'function': {'name': 'one', 'arguments': '{}'}},
                 {'id': 'p2', 'type': 'function',
                  'function': {'name': 'two', 'arguments': '{}'}},
             ]},
            _autogen_tool_message(('p1', 'OUTPUT ONE'), ('p2', 'OUTPUT TWO')),
        ]
        out = self._run(messages)
        answers = self._answers(out)

        self.assertEqual(set(answers), {'p1', 'p2'}, f"answers={answers}")
        self.assertIn('OUTPUT ONE', answers['p1'])
        self.assertIn('OUTPUT TWO', answers['p2'])
        self.assertNotIn(
            'OUTPUT TWO', answers['p1'],
            "p1 was given p2's output as well — the joined blob was copied "
            "instead of the per-call return being used")

    def test_single_call_single_result_still_pairs(self):
        """The easy case must keep working — it is the one the guess branch
        got right by luck, so a fix must not regress it."""
        messages = [
            {'role': 'user', 'content': 'go'},
            _assistant('solo'),
            _autogen_tool_message(('solo', 'SOLO OUTPUT')),
        ]
        answers = self._answers(self._run(messages))
        self.assertIn('solo', answers)
        self.assertIn('SOLO OUTPUT', answers['solo'])

    def test_top_level_tool_call_id_still_honoured(self):
        """Messages that DO carry a top-level id (non-autogen producers)
        must be untouched by any tool_responses handling."""
        messages = [
            {'role': 'user', 'content': 'go'},
            _assistant('plain'),
            {'role': 'tool', 'tool_call_id': 'plain', 'content': 'PLAIN OUT'},
        ]
        answers = self._answers(self._run(messages))
        self.assertEqual(answers, {'plain': 'PLAIN OUT'})


if __name__ == '__main__':
    unittest.main()
