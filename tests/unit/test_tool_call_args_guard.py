"""Guard: a tool_call whose `arguments` field is not valid JSON must be
coerced to a valid-JSON-object string BEFORE the conversation is sent to the
model server.

Live root cause 2026-09-05 02:16 (Auto Research reuse, installed build):
the local model emitted a tool_call with prose arguments
(`"Based on the current research focus, please provide three large language
models..."`).  llama.cpp b10330 is lenient on tool-call OUTPUT but strict on
INPUT — every subsequent request carrying that malformed call in history
returned HTTP 500 "Failed to parse tool call arguments as JSON", so the reuse
turn died before any action advanced.  A plain completion re-probe returned
200, proving the server was healthy and the fault was the malformed args.

`ensure_tool_call_arguments_json` is the tool-call sibling of
validate_messages' ROLE-ORDER-GUARD: purely defensive, a no-op on valid JSON,
enforcing the OpenAI/autogen contract (arguments is a JSON string) rather than
any engine error text (engine-neutral, per feedback_engine_neutral_no_llm_speaker).
"""
import json
import unittest

from hartos.helper import ensure_tool_call_arguments_json


class ToolCallArgsGuard(unittest.TestCase):
    def _args(self, out, i=0):
        return out[0]['tool_calls'][i]['function']['arguments']

    def test_prose_arguments_coerced_to_valid_json_object(self):
        msgs = [{
            'role': 'assistant',
            'tool_calls': [{
                'id': 'x', 'type': 'function',
                'function': {
                    'name': 'google_search',
                    'arguments': ('Based on the current research focus, please '
                                  'provide three large language models.'),
                },
            }],
        }]
        out = ensure_tool_call_arguments_json(msgs)
        parsed = json.loads(self._args(out))  # must NOT raise (was the 500)
        self.assertIsInstance(parsed, dict)

    def test_valid_arguments_left_untouched(self):
        valid = json.dumps({'query': 'small language models 2026'})
        msgs = [{
            'role': 'assistant',
            'tool_calls': [{
                'id': 'x', 'type': 'function',
                'function': {'name': 'google_search', 'arguments': valid},
            }],
        }]
        out = ensure_tool_call_arguments_json(msgs)
        self.assertEqual(self._args(out), valid)

    def test_legacy_function_call_shape_coerced(self):
        msgs = [{
            'role': 'assistant',
            'function_call': {'name': 'x', 'arguments': 'not json at all'},
        }]
        out = ensure_tool_call_arguments_json(msgs)
        json.loads(out[0]['function_call']['arguments'])  # must not raise

    def test_none_arguments_becomes_empty_object(self):
        msgs = [{
            'role': 'assistant',
            'tool_calls': [{
                'id': 'x', 'type': 'function',
                'function': {'name': 'x', 'arguments': None},
            }],
        }]
        out = ensure_tool_call_arguments_json(msgs)
        self.assertEqual(self._args(out), '{}')

    def test_dict_arguments_serialized(self):
        msgs = [{
            'role': 'assistant',
            'tool_calls': [{
                'id': 'x', 'type': 'function',
                'function': {'name': 'x', 'arguments': {'query': 'ok'}},
            }],
        }]
        out = ensure_tool_call_arguments_json(msgs)
        self.assertEqual(json.loads(self._args(out)), {'query': 'ok'})

    def test_plain_messages_pass_through(self):
        msgs = [{'role': 'user', 'content': 'hi'},
                {'role': 'assistant', 'content': 'hello'}]
        out = ensure_tool_call_arguments_json(msgs)
        self.assertEqual(out, msgs)


if __name__ == '__main__':
    unittest.main()
