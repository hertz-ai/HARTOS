"""A tool runs only with arguments that bind to its own signature.

Live, agent_system.log 2026-09-22 08:23:01 and 13:36:40 (log RCA defect 14):
the local 4B emitted tool-call arguments with a string value left unquoted,
e.g. ``{"text": Financial Dashboard ... - Trading: $10,000\\n- Consulting:
$5,000 ...}``.  autogen's json.loads refused it, the patched executor
(hartos/helper.py force_apply_autogen_json_fix) fell back to retrieve_json,
and json_repair cut the value at the first comma and turned every later
"word:" into a new key.  The split dict went into ``func(**arguments)``; the
tool raised "unexpected keyword argument 'Consulting'" and the message never
reached the user.  When nothing could be recovered the executor called the
tool with ``{}`` ("missing 1 required positional argument: 'text'").

These tests drive the real patched ``execute_function`` / ``a_execute_function``
on a real autogen ConversableAgent whose tool is wrapped the way live tools
are (core.tool_logging.log_tool_execution, then autogen's register_function).
Boundaries mocked: the credential vault only.
"""
import asyncio
import unittest
from unittest import mock

from autogen.agentchat.conversable_agent import ConversableAgent

from core.tool_logging import log_tool_execution
from hartos.helper import force_apply_autogen_json_fix

# Unquoted value, the shape of the 2026-09-22 08:23:01 event.
from tests.unit.json_repair_split import UNQUOTED, old_json_repair_split


class _Base(unittest.TestCase):

    # The tests pin the refusal of json_repair's split; replay it unless a
    # class is about the installed library itself.
    replay_old_split = True

    def setUp(self):
        self._orig = (ConversableAgent.execute_function,
                      ConversableAgent.a_execute_function)
        self.addCleanup(self._restore)
        self.assertTrue(force_apply_autogen_json_fix())
        vault = mock.patch('core.tool_logging.credential_vault',
                           return_value=None)
        vault.start()
        self.addCleanup(vault.stop)
        # These tests pin the refusal of json_repair's split, which
        # json-repair >= 0.59.4 no longer makes for UNQUOTED: replay it
        # (tests/unit/json_repair_split.py).
        if self.replay_old_split:
            split = old_json_repair_split()
            split.__enter__()
            self.addCleanup(split.__exit__, None, None, None)

        self.calls = []

        @log_tool_execution
        def send_message_to_user(text: str, avatar_id: str = '',
                                 response_type: str = 'Neutral') -> str:
            self.calls.append(('sync', text, avatar_id, response_type))
            return 'sent'

        @log_tool_execution
        async def text_2_image(text: str) -> str:
            self.calls.append(('async', text))
            return 'drawn'

        self.agent = ConversableAgent('helper', llm_config=False,
                                      human_input_mode='NEVER')
        self.agent.register_function({
            'send_message_to_user': self.agent._wrap_function(send_message_to_user),
            'text_2_image': self.agent._wrap_function(text_2_image),
        })

    def _restore(self):
        (ConversableAgent.execute_function,
         ConversableAgent.a_execute_function) = self._orig

    def run_sync(self, name, arguments):
        return self.agent.execute_function(
            {'name': name, 'arguments': arguments})

    def run_async(self, name, arguments):
        return asyncio.run(self.agent.a_execute_function(
            {'name': name, 'arguments': arguments}))


class SplitArgumentsAreNotCalled(_Base):

    def _assert_json_refusal(self, ok, reply, params):
        content = reply['content']
        self.assertFalse(ok)
        self.assertTrue(content.startswith('Error:'), content)
        self.assertIn('not valid JSON', content)
        self.assertIn('double quotes', content)
        for p in params:
            self.assertIn(p, content)
        # The split keys are named as what was received, never as the fix.
        self.assertNotIn('unexpected keyword argument', content)

    def test_sync_unquoted_value_does_not_run_the_tool(self):
        ok, reply = self.run_sync('send_message_to_user', UNQUOTED)
        self.assertEqual(self.calls, [])
        self._assert_json_refusal(ok, reply,
                                  ('text', 'avatar_id', 'response_type'))
        self.assertIn('Consulting', reply['content'])

    def test_async_unquoted_value_does_not_run_the_tool(self):
        ok, reply = self.run_async('text_2_image', UNQUOTED)
        self.assertEqual(self.calls, [])
        self._assert_json_refusal(ok, reply, ('text',))

    def test_async_sync_tool_unquoted_value_does_not_run(self):
        ok, reply = self.run_async('send_message_to_user', UNQUOTED)
        self.assertEqual(self.calls, [])
        self._assert_json_refusal(ok, reply, ('text',))

    def test_nothing_recovered_is_not_called_with_empty_args(self):
        ok, reply = self.run_sync('send_message_to_user', '   ')
        self.assertEqual(self.calls, [])
        self._assert_json_refusal(ok, reply, ('text',))
        self.assertIn('Missing required', reply['content'])

    def test_nothing_recovered_async(self):
        ok, reply = self.run_async('text_2_image', '   ')
        self.assertEqual(self.calls, [])
        self._assert_json_refusal(ok, reply, ('text',))

    def test_repair_that_empties_a_required_value_does_not_run(self):
        # json_repair fills a value the model never wrote with "": a cut-off
        # call '{"text":' became {"text": ""}, which binds, and the tool ran
        # with nothing to say.  A required value left empty by a repair is
        # refused like a missing one.
        for text in ('{"text":', '{"text": }', '{"text": , "avatar_id": "a"}'):
            with self.subTest(text=text):
                ok, reply = self.run_sync('send_message_to_user', text)
                self.assertEqual(self.calls, [])
                self._assert_json_refusal(ok, reply, ('text',))
                self.assertIn('Required argument(s) left empty: text',
                              reply['content'])

    def test_repair_that_empties_a_required_value_async(self):
        ok, reply = self.run_async('text_2_image', '{"text": }')
        self.assertEqual(self.calls, [])
        self._assert_json_refusal(ok, reply, ('text',))
        self.assertIn('Required argument(s) left empty: text', reply['content'])

    def test_repaired_positional_list_is_not_run(self):
        # A list is never bound positionally any more: arguments that are
        # not one JSON object are refused by the executor as by the history
        # guard (test_tool_args_must_be_an_object.py,
        # NonObjectArgumentsAreNeverRun).  This list, which used to bind with
        # an emptied first value, is refused as not an object.
        ok, reply = self.run_sync('send_message_to_user', '["  ", "a",]')
        self.assertFalse(ok)
        self.assertEqual(self.calls, [])
        self.assertIn('not one JSON object', reply['content'])

    def test_repair_may_leave_an_optional_value_empty(self):
        # Only a REQUIRED value must be there; an optional one the repair left
        # empty is the model's to leave out.
        ok, reply = self.run_sync('send_message_to_user',
                                  '{"text": "hi", "avatar_id": ,}')
        self.assertTrue(ok, reply)
        self.assertEqual(self.calls, [('sync', 'hi', '', 'Neutral')])


class ValidArgumentsStillRun(_Base):

    def test_strict_json_runs(self):
        ok, reply = self.run_sync('send_message_to_user',
                                  '{"text": "hi", "response_type": "Happy"}')
        self.assertTrue(ok)
        self.assertEqual(self.calls, [('sync', 'hi', '', 'Happy')])
        self.assertEqual(reply['content'], 'sent')

    def test_benign_repair_that_binds_still_runs(self):
        # Trailing comma: not JSON, json_repair fixes it without splitting.
        ok, reply = self.run_sync('send_message_to_user', '{"text": "hi",}')
        self.assertTrue(ok)
        self.assertEqual(self.calls, [('sync', 'hi', '', 'Neutral')])

    def test_benign_repair_async(self):
        ok, reply = self.run_async('text_2_image', "{'text': 'a cat'}")
        self.assertTrue(ok)
        self.assertEqual(self.calls, [('async', 'a cat')])
        self.assertEqual(reply['content'], 'drawn')

    def test_strict_json_with_an_unknown_name_is_refused_by_name(self):
        # Valid JSON, wrong name: same one rule, but the message is about the
        # names, not about JSON syntax.
        ok, reply = self.run_sync('send_message_to_user',
                                  '{"text": "hi", "status": "done"}')
        self.assertFalse(ok)
        self.assertEqual(self.calls, [])
        content = reply['content']
        self.assertTrue(content.startswith('Error:'), content)
        self.assertIn('status', content)
        self.assertIn('text', content)
        self.assertNotIn('not valid JSON', content)

    def test_strict_json_empty_required_value_still_runs(self):
        # The model wrote "" itself, in valid JSON: its choice, not a repair.
        ok, reply = self.run_sync('send_message_to_user', '{"text": ""}')
        self.assertTrue(ok, reply)
        self.assertEqual(self.calls, [('sync', '', '', 'Neutral')])

    def test_strict_json_missing_required_async(self):
        ok, reply = self.run_async('text_2_image', '{}')
        self.assertFalse(ok)
        self.assertEqual(self.calls, [])
        self.assertIn('Missing required', reply['content'])
        self.assertIn('text', reply['content'])

    def test_a_tool_taking_any_keyword_keeps_extra_names(self):
        seen = []

        def lenient(text: str, **extra) -> str:
            seen.append((text, extra))
            return 'ok'

        self.agent.register_function(
            {'lenient': self.agent._wrap_function(lenient)})
        ok, reply = self.run_sync('lenient', '{"text": "a", "mood": "b"}')
        self.assertTrue(ok)
        self.assertEqual(seen, [('a', {'mood': 'b'})])
        # Missing its required name, it is refused, but the extra name is
        # not reported as unknown: the tool takes any keyword.
        ok, reply = self.run_sync('lenient', '{"mood": "b"}')
        self.assertFalse(ok)
        self.assertIn('Missing required argument(s): text', reply['content'])
        self.assertNotIn('Unknown', reply['content'])
        self.assertEqual(len(seen), 1)


class TheInstalledJsonRepairKeepsTheValueWhole(_Base):
    """The same logged call through the real json-repair (requirements pin
    >= 0.59.4): the unquoted value comes back whole, so the message the model
    wrote is delivered in full instead of refused.  If a json-repair release
    splits it again, this fails, and the refusal tests above say what the
    executor then does."""

    replay_old_split = False

    def test_the_repair_keeps_the_whole_message(self):
        import json
        from hartos.helper import repair_json
        self.assertEqual(json.loads(repair_json(UNQUOTED)),
                         {'text': UNQUOTED[len('{"text": '):-1]})

    def test_the_logged_call_delivers_the_whole_message(self):
        ok, reply = self.run_sync('send_message_to_user', UNQUOTED)
        self.assertTrue(ok, reply)
        self.assertEqual(self.calls, [('sync', UNQUOTED[len('{"text": '):-1],
                                       '', 'Neutral')])

    def test_the_logged_call_delivers_the_whole_message_async(self):
        ok, reply = self.run_async('text_2_image', UNQUOTED)
        self.assertTrue(ok, reply)
        self.assertEqual(self.calls, [('async', UNQUOTED[len('{"text": '):-1])])


class SourceGuardOneParseAndBind(unittest.TestCase):
    """test_source_guard_: the sync and async executors must not parse
    arguments themselves; bind_tool_call_arguments is the one place, so a
    third inline parse (which would skip the signature check) fails here."""

    def test_source_guard_executors_parse_only_through_the_helper(self):
        import ast
        import inspect
        import textwrap
        from hartos import helper

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(helper.force_apply_autogen_json_fix)))
        executors = {n.name: n for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name in ('enhanced_execute_function',
                                    'enhanced_a_execute_function')}
        self.assertEqual(len(executors), 2)
        for name, node in executors.items():
            called = {(c.func.attr if isinstance(c.func, ast.Attribute)
                       else getattr(c.func, 'id', None))
                      for c in ast.walk(node) if isinstance(c, ast.Call)}
            self.assertIn('bind_tool_call_arguments', called, name)
            for parser in ('retrieve_json', 'repair_json', 'loads',
                           'literal_eval'):
                self.assertNotIn(parser, called, f'{name} calls {parser}')


if __name__ == '__main__':
    unittest.main()
