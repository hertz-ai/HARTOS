"""A tool call's arguments are one JSON object, on the wire and at the tool.

Review of b0fa4989e / 68377afd2, probed:

  * TOOL-ARGS-GUARD (``ensure_tool_call_arguments_json``) promised "a valid
    JSON-object string" but kept any strict JSON: ``[1,2]`` and ``"hello"``
    passed unchanged.  Measured on the live llama-server (b10330, Qwen3.5-4B
    template, /apply-template, 2026-09-26): a prior call whose arguments are
    ``[{"url": "https://x.test"}]`` renders as ``<function=crawl>`` with NO
    parameters -- the template reads arguments only when they are a mapping,
    so the model is shown a call it made with nothing in it.
  * ``tool_argument_error`` checked only a dict.  ``safe_function_call`` runs
    ``[{...}]`` as ``func(**list[0])``, so that call reached the tool with no
    signature check; and the async executor ran the same list as
    ``func({...})``, the dict as the first positional argument.

Behavioural: the real guard, and the real patched ``execute_function`` /
``a_execute_function`` on an autogen ConversableAgent with tools wrapped as
live tools are (core.tool_logging.log_tool_execution); the credential vault
is the only boundary mocked.
"""
import asyncio
import json
import unittest
from unittest import mock

from autogen.agentchat.conversable_agent import ConversableAgent

from core.tool_logging import log_tool_execution
from hartos.helper import (ensure_tool_call_arguments_json,
                           force_apply_autogen_json_fix, safe_function_call)


def _guarded(args):
    msgs = [{'role': 'assistant', 'content': '',
             'tool_calls': [{'id': 'c1', 'type': 'function',
                             'function': {'name': 'crawl',
                                          'arguments': args}}]}]
    ensure_tool_call_arguments_json(msgs)
    return msgs[0]['tool_calls'][0]['function']['arguments']


class GuardSendsOnlyObjects(unittest.TestCase):

    def assert_refused(self, out, original):
        # A strict JSON object that keeps what the model wrote, marked
        # refused with the reason -- not '{}', which erased it (review of
        # 86e580b99): the model's next turn must see its own call.
        from hartos.helper import REFUSED_ARGUMENTS_KEY, REFUSED_BECAUSE_KEY
        parsed = json.loads(out)
        self.assertEqual(parsed[REFUSED_ARGUMENTS_KEY], original)
        self.assertIn('not one JSON object', parsed[REFUSED_BECAUSE_KEY])
        self.assertIn('not run', parsed[REFUSED_BECAUSE_KEY])

    def test_prose_is_kept_visible_not_erased(self):
        text = 'Based on the research focus, list three models.'
        parsed = json.loads(_guarded(text))
        from hartos.helper import REFUSED_ARGUMENTS_KEY, REFUSED_BECAUSE_KEY
        self.assertEqual(parsed[REFUSED_ARGUMENTS_KEY], text)
        self.assertIn('not valid JSON', parsed[REFUSED_BECAUSE_KEY])

    def test_a_raw_lone_surrogate_in_refused_text_is_made_sendable(self):
        # Kept verbatim, the stand-in would carry the lone surrogate llama.cpp
        # 500s on ("invalid string: surrogate", review of bb809af28).
        out = _guarded('open file \ud800 now')
        out.encode('utf-8')  # raises on a lone surrogate
        from hartos.helper import REFUSED_ARGUMENTS_KEY
        self.assertEqual(json.loads(out)[REFUSED_ARGUMENTS_KEY],
                         'open file ' + chr(0xFFFD) + ' now')


    def test_a_valid_array_is_not_sent_as_arguments(self):
        self.assert_refused(_guarded('[1,2]'), '[1,2]')

    def test_a_valid_string_is_not_sent_as_arguments(self):
        self.assert_refused(_guarded('"hello"'), '"hello"')

    def test_an_array_wrapping_an_object_is_not_sent_as_arguments(self):
        self.assert_refused(_guarded('[{"url": "https://x.test"}]'),
                            '[{"url": "https://x.test"}]')

    def test_a_python_list_is_not_sent_as_arguments(self):
        self.assert_refused(_guarded([1, 2]), '[1, 2]')

    def test_an_object_is_left_byte_identical(self):
        text = '{"url": "https://x.test", "n": 2}'
        self.assertEqual(_guarded(text), text)


class _Executor(unittest.TestCase):

    def setUp(self):
        self._orig = (ConversableAgent.execute_function,
                      ConversableAgent.a_execute_function)
        self.addCleanup(self._restore)
        self.assertTrue(force_apply_autogen_json_fix())
        vault = mock.patch('core.tool_logging.credential_vault',
                           return_value=None)
        vault.start()
        self.addCleanup(vault.stop)
        self.calls = []

        @log_tool_execution
        def send_message_to_user(text: str, avatar_id: str = '') -> str:
            self.calls.append(('sync', text, avatar_id))
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

    def assert_refused_by_name(self, ok, reply, *names):
        self.assertFalse(ok)
        self.assertEqual(self.calls, [])
        content = reply['content']
        self.assertTrue(content.startswith('Error:'), content)
        self.assertIn('was not run', content)
        self.assertIn('Expected parameters', content)
        for n in names:
            self.assertIn(n, content)


class ListWrappedArgumentsAreBound(_Executor):
    """``[{...}]`` is checked against the signature as the call it becomes."""

    def test_sync_unknown_name_in_a_wrapped_object_is_refused(self):
        ok, reply = self.run_sync('send_message_to_user',
                                  '[{"text": "hi", "status": "done"}]')
        self.assert_refused_by_name(ok, reply, 'Unknown argument(s): status')

    def test_sync_missing_required_in_a_wrapped_object_is_refused(self):
        ok, reply = self.run_sync('send_message_to_user',
                                  '[{"avatar_id": "a1"}]')
        self.assert_refused_by_name(ok, reply,
                                    'Missing required argument(s): text')

    def test_async_unknown_name_in_a_wrapped_object_is_refused(self):
        ok, reply = self.run_async('text_2_image',
                                   '[{"text": "a cat", "mood": "calm"}]')
        self.assert_refused_by_name(ok, reply, 'Unknown argument(s): mood')

    def test_sync_a_wrapped_object_that_binds_runs_as_keywords(self):
        ok, reply = self.run_sync('send_message_to_user',
                                  '[{"text": "hi", "avatar_id": "a1"}]')
        self.assertTrue(ok, reply)
        self.assertEqual(self.calls, [('sync', 'hi', 'a1')])

    def test_async_a_wrapped_object_that_binds_runs_as_keywords(self):
        # Was func({"text": "a cat"}): the whole dict as `text`.
        ok, reply = self.run_async('text_2_image', '[{"text": "a cat"}]')
        self.assertTrue(ok, reply)
        self.assertEqual(self.calls, [('async', 'a cat')])


class PositionalListsAreBoundToo(_Executor):
    """A plain list is the positional call safe_function_call makes."""

    def test_a_positional_list_that_binds_runs(self):
        ok, reply = self.run_sync('send_message_to_user', '["hi", "a1"]')
        self.assertTrue(ok, reply)
        self.assertEqual(self.calls, [('sync', 'hi', 'a1')])

    def test_a_positional_list_that_does_not_bind_is_refused(self):
        ok, reply = self.run_sync('send_message_to_user', '["hi", "a1", "x"]')
        self.assert_refused_by_name(ok, reply, 'not one JSON object',
                                    'text (required)')

    def test_a_truncation_sentinel_list_is_refused_not_run_into_an_error(self):
        # safe_function_call's ['truncated'] recovery needs the first call's
        # TypeError, but core.tool_logging's wrapper answers that TypeError
        # itself, so the tool "ran" and replied with a failure envelope
        # (measured: "takes from 1 to 2 positional arguments but 3 were
        # given").  The check refuses it before the call instead.
        ok, reply = self.run_sync('send_message_to_user',
                                  '["hi", "a1", ["truncated"]]')
        self.assert_refused_by_name(ok, reply, 'not one JSON object')


class RefusedStandInNeverRuns(_Executor):

    def test_a_stand_in_repeated_as_a_call_is_refused_by_name(self):
        # If a model copies the stand-in into a new call, the tool is not run
        # with it, and the reply says why.
        stand_in = _guarded('[1,2]')
        ok, reply = self.run_sync('send_message_to_user', stand_in)
        self.assertFalse(ok)
        self.assertEqual(self.calls, [])
        self.assertTrue(reply['content'].startswith('Error:'), reply['content'])
        self.assertIn('refused earlier', reply['content'])


class RefusedStandInAndKwargsTools(_Executor):
    """Review of cd8d9154d, M3: a tool taking **kwargs (clawhub_adapter,
    MCP tool_executor(**kwargs)) binds any names, so the stand-in's keys were
    no protection: it ran with refused_arguments / refused_because."""

    def test_a_kwargs_tool_does_not_run_a_stand_in(self):
        seen = []

        def lenient(**kwargs) -> str:
            seen.append(kwargs)
            return 'ok'

        self.agent.register_function(
            {'lenient': self.agent._wrap_function(lenient)})
        ok, reply = self.run_sync('lenient', _guarded('[1,2]'))
        self.assertFalse(ok)
        self.assertEqual(seen, [])
        self.assertIn('refused', reply['content'])
        self.assertTrue(reply['content'].startswith('Error:'), reply['content'])


class StandInBuiltOnlyWhenNeeded(unittest.TestCase):

    def test_a_repaired_call_never_builds_a_stand_in(self):
        from unittest import mock
        import hartos.helper as h
        with mock.patch.object(h, 'refused_arguments_json',
                               side_effect=AssertionError('built')) as built:
            out = _guarded("{'text': 'a cat'}")  # repaired, not refused
        self.assertEqual(json.loads(out), {'text': 'a cat'})
        built.assert_not_called()

    def test_a_key_collision_is_its_own_error_type(self):
        from hartos.helper import WireKeyCollision, load_wire_json
        with self.assertRaises(WireKeyCollision):
            load_wire_json('{"\\ud800": 1, "\\udc00": 2}')
        self.assertTrue(issubclass(WireKeyCollision, ValueError))


class SafeFunctionCallUsesTheSameShape(unittest.TestCase):
    """The shape the check binds is the shape safe_function_call calls."""

    def test_wrapped_object_and_positional_list_keep_their_calls(self):
        def f(a, b=0):
            return (a, b)
        self.assertEqual(safe_function_call(f, [{'a': 1, 'b': 2}]), (1, 2))
        self.assertEqual(safe_function_call(f, {'a': 3}), (3, 0))
        self.assertEqual(safe_function_call(f, [4, 5]), (4, 5))
        self.assertEqual(safe_function_call(f, 6), (6, 0))


if __name__ == '__main__':
    unittest.main()
