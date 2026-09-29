"""The executor runs a tool with the arguments the model wrote, or not at all.

Review of c21b5e6e2 / 7d07c0a2d (rvc21_agent_probe.py): the history guard
(ensure_tool_call_arguments_json) reads arguments with load_wire_json, but
the executor (bind_tool_call_arguments) parsed with plain json.loads and fell
back to retrieve_json -> json.loads(repair_json(...)).  Both read an
overflowing number as inf.  So for ``{"id": 620e51403072992921}`` -- valid
JSON -- the tool ran with id=inf and answered "item inf", tool_reply_failed
called that a success (CREATE would bank it), and the next request showed the
model the id it wrote.

The rules pinned here, through a real autogen generate_tool_calls_reply with
the patched executor and a log_tool_execution-wrapped tool (the credential
vault is the only boundary mocked):

  * the executor reads arguments the way the history guard does
    (hartos.helper.parse_tool_arguments), so the tool gets the token the
    model wrote, as a string, and the next request shows the same thing;
  * a call whose arguments would carry Infinity / NaN the model never wrote
    does not run;
  * no second json.loads of tool arguments exists in the executor path
    (source guard).
"""
import ast
import copy
import inspect
import textwrap
import unittest
from unittest import mock

from autogen.agentchat.conversable_agent import ConversableAgent
from flask import Flask

from core.constants import tool_reply_failed
from core.tool_logging import log_tool_execution
from hartos.helper import ToolMessageHandler, force_apply_autogen_json_fix


class ExecutorReadsWhatTheModelWrote(unittest.TestCase):

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
        def get_item(id: str) -> str:
            self.calls.append(id)
            return 'item ' + str(id)

        self.get_item = get_item

    def _restore(self):
        (ConversableAgent.execute_function,
         ConversableAgent.a_execute_function) = self._orig

    def run_call(self, arguments):
        a = ConversableAgent('a', llm_config=False, human_input_mode='NEVER')
        ex = ConversableAgent('ex', llm_config=False, human_input_mode='NEVER')
        ex.register_function({'get_item': ex._wrap_function(self.get_item)})
        a.send({'role': 'assistant', 'content': None,
                'tool_calls': [{'id': 'c1', 'type': 'function',
                                'function': {'name': 'get_item',
                                             'arguments': arguments}}]},
               ex, request_reply=False, silent=True)
        _, reply = ex.generate_tool_calls_reply(sender=a)
        content = reply['tool_responses'][0]['content']
        history = copy.deepcopy(a._oai_messages[ex])
        with Flask(__name__).app_context():
            out = ToolMessageHandler().validate_messages(history)
        sent = [tc['function']['arguments'] for m in out
                for tc in (m.get('tool_calls') or [])][0]
        return content, sent

    def test_a_valid_overflowing_id_reaches_the_tool_as_written(self):
        content, sent = self.run_call('{"id": 620e51403072992921}')
        self.assertEqual(self.calls, ['620e51403072992921'])
        self.assertEqual(content, 'item 620e51403072992921')
        self.assertIn('620e51403072992921', sent)

    def test_repaired_shapes_never_run_with_inf(self):
        for text in ('{"id": 620e51403072992921"}', '{"id": 1e999)}',
                     '{"id": 1e999e}', '{"id": +1e999}'):
            with self.subTest(text=text):
                self.calls.clear()
                content, _ = self.run_call(text)
                for got in self.calls:
                    self.assertNotIn(str(got),
                                     ('inf', '-inf', 'nan', 'Infinity', 'NaN'))
                self.assertNotIn('item inf', content)
                if not self.calls:
                    self.assertTrue(tool_reply_failed(content), content)

    def test_an_invented_infinity_is_refused(self):
        content, _ = self.run_call('{"id": 1e999e}')
        self.assertEqual(self.calls, [])
        self.assertTrue(tool_reply_failed(content), content)


class SourceGuardOneArgumentParser(unittest.TestCase):
    """test_source_guard_: tool arguments are parsed in ONE place
    (parse_tool_arguments).  A json.loads of them anywhere in the executor's
    parse-and-bind step is the second path that ran tools with inf."""

    def test_source_guard_bind_does_not_json_loads_arguments(self):
        from hartos import helper
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(helper.bind_tool_call_arguments)))
        called = {(c.func.attr if isinstance(c.func, ast.Attribute)
                   else getattr(c.func, 'id', None))
                  for c in ast.walk(tree) if isinstance(c, ast.Call)}
        self.assertIn('parse_tool_arguments', called)
        for parser in ('loads', 'repair_json', 'literal_eval'):
            self.assertNotIn(parser, called, parser)


if __name__ == '__main__':
    unittest.main()


class ReviewOf3a7abe540(ExecutorReadsWhatTheModelWrote):
    """Review of 3a7abe540 (rv3a7/cases2.py)."""

    def setUp(self):
        super().setUp()

        @log_tool_execution
        def search(q: str, id: str = '') -> str:
            self.calls.append((q, id))
            return 'found ' + q

        @log_tool_execution
        def get_two(id: str, n: int = 0) -> str:
            self.calls.append((id, n))
            return 'item %s %s' % (id, n)

        self.tools = {'search': search, 'get_two': get_two}

    def run_tool(self, name, arguments):
        a = ConversableAgent('a', llm_config=False, human_input_mode='NEVER')
        ex = ConversableAgent('ex', llm_config=False, human_input_mode='NEVER')
        ex.register_function({name: ex._wrap_function(self.tools[name])})
        a.send({'role': 'assistant', 'content': None,
                'tool_calls': [{'id': 'c1', 'type': 'function',
                                'function': {'name': name,
                                             'arguments': arguments}}]},
               ex, request_reply=False, silent=True)
        _, reply = ex.generate_tool_calls_reply(sender=a)
        return reply['tool_responses'][0]['content']

    def test_infinity_inside_a_string_is_not_the_model_writing_infinity(self):
        # "Infinity" appeared in the text (inside "Infinity war"), so the
        # invented Infinity for 1e999e was let through: search('Infinity
        # war', 'Infinity') ran and was banked as a success.
        for text in ('{"q": "Infinity war", "id": 1e999e}',
                     '{"q": "NaN", "id": +1e999}'):
            with self.subTest(text=text):
                self.calls.clear()
                content = self.run_tool('search', text)
                self.assertEqual(self.calls, [], content)
                self.assertTrue(tool_reply_failed(content), content)

    def test_a_value_the_model_wrote_as_infinity_still_runs(self):
        self.calls.clear()
        content = self.run_tool('search', '{"q": "Infinity"}')
        self.assertEqual(self.calls, [('Infinity', '')], content)

    def test_a_line_comment_does_not_swallow_the_rest(self):
        # The repair read format_json_str's output, which has no newlines,
        # so '// the id' swallowed '"n": 2' and the call was refused.  The
        # parent ran it as ('x', 2).
        nl = chr(10)
        for text in ('{' + nl + '  "id": "x", // the id' + nl + '  "n": 2' + nl + '}',
                     '{' + nl + '  "id": "x" // the id' + nl + '  , "n": 2' + nl + '}'):
            with self.subTest(text=text):
                self.calls.clear()
                content = self.run_tool('get_two', text)
                self.assertEqual(self.calls, [('x', 2)], content)


class NumbersATypedParameterCannotHold(ExecutorReadsWhatTheModelWrote):
    """Review of 3a7abe540, item 6: the reader keeps 1e999 or NaN as the
    token the model wrote, a string; a float/int parameter then received
    text ("scaled '1e9991e999'")."""

    def setUp(self):
        super().setUp()

        @log_tool_execution
        def scale(x: float, k: int = 1) -> str:
            self.calls.append((x, k))
            return 'scaled %r' % (x * k,)

        self.scale = scale

    def run_scale(self, arguments):
        a = ConversableAgent('a', llm_config=False, human_input_mode='NEVER')
        ex = ConversableAgent('ex', llm_config=False, human_input_mode='NEVER')
        ex.register_function({'scale': ex._wrap_function(self.scale)})
        a.send({'role': 'assistant', 'content': None,
                'tool_calls': [{'id': 'c1', 'type': 'function',
                                'function': {'name': 'scale',
                                             'arguments': arguments}}]},
               ex, request_reply=False, silent=True)
        _, reply = ex.generate_tool_calls_reply(sender=a)
        return reply['tool_responses'][0]['content']

    def test_an_unholdable_number_for_a_number_parameter_is_refused(self):
        for text in ('{"x": 1e999, "k": 2}', '{"x": NaN}', '{"x": -1e999}'):
            with self.subTest(text=text):
                self.calls.clear()
                content = self.run_scale(text)
                self.assertEqual(self.calls, [], content)
                self.assertTrue(tool_reply_failed(content), content)
                self.assertIn('finite number', content)

    def test_a_number_in_range_still_runs(self):
        self.calls.clear()
        content = self.run_scale('{"x": 1.5, "k": 2}')
        self.assertEqual(self.calls, [(1.5, 2)], content)


class ReviewOfDbfef4360(ExecutorReadsWhatTheModelWrote):
    """Review of dbfef4360 (rv3a7/cases3.py, probe3.py)."""

    def setUp(self):
        super().setUp()
        from typing import Annotated, List, Optional, Union

        @log_tool_execution
        def search(q: str, id: str = '') -> str:
            self.calls.append((q, id))
            return 'found ' + q

        @log_tool_execution
        def lookup(id: Union[int, str]) -> str:
            self.calls.append((id,))
            return 'looked'

        @log_tool_execution
        def bigint(n: int) -> str:
            self.calls.append((n,))
            return 'n'

        @log_tool_execution
        def many(xs: List[float]) -> str:
            self.calls.append((xs,))
            return 'many'

        @log_tool_execution
        def opt(x: Optional[Annotated[float, "a number"]] = None) -> str:
            self.calls.append((x,))
            return 'opt'

        self.tools = {'search': search, 'lookup': lookup, 'bigint': bigint,
                      'many': many, 'opt': opt}

    def run_tool(self, name, arguments):
        a = ConversableAgent('a', llm_config=False, human_input_mode='NEVER')
        ex = ConversableAgent('ex', llm_config=False, human_input_mode='NEVER')
        ex.register_function({name: ex._wrap_function(self.tools[name])})
        a.send({'role': 'assistant', 'content': None,
                'tool_calls': [{'id': 'c1', 'type': 'function',
                                'function': {'name': name,
                                             'arguments': arguments}}]},
               ex, request_reply=False, silent=True)
        _, reply = ex.generate_tool_calls_reply(sender=a)
        return reply['tool_responses'][0]['content']

    def test_infinity_inside_a_longer_string_is_not_written(self):
        for text in ('{"q": "to Infinity, and beyond", "id": 1e999e}',
                     '{"q": "say \'Infinity\' now", "id": 1e999e}',
                     '{"q": Infinity war, "id": 1e999e}'):
            with self.subTest(text=text):
                self.calls.clear()
                content = self.run_tool('search', text)
                self.assertEqual(self.calls, [], content)
                self.assertTrue(tool_reply_failed(content), content)

    def test_a_parameter_that_accepts_text_gets_the_token(self):
        self.calls.clear()
        content = self.run_tool('lookup', '{"id": 620e51403072992921}')
        self.assertEqual(self.calls, [('620e51403072992921',)], content)

    def test_an_exact_big_integer_reaches_an_int_parameter_as_an_int(self):
        big = '9' * 400
        self.calls.clear()
        content = self.run_tool('bigint', '{"n": %s}' % big)
        self.assertEqual(self.calls, [(int(big),)], content)

    def test_an_unholdable_number_inside_a_list_of_floats_is_refused(self):
        self.calls.clear()
        content = self.run_tool('many', '{"xs": [1.5, 1e999]}')
        self.assertEqual(self.calls, [], content)
        self.assertTrue(tool_reply_failed(content), content)

    def test_optional_annotated_float_still_refuses(self):
        self.calls.clear()
        content = self.run_tool('opt', '{"x": 1e999}')
        self.assertEqual(self.calls, [], content)


class ReviewOf1bf298f5b(ReviewOfDbfef4360):
    """Review of 1bf298f5b (rv3a7/cases4.py, cases5.py)."""

    def setUp(self):
        super().setUp()
        from typing import List

        @log_tool_execution
        def many_ints(xs: List[int]) -> str:
            self.calls.append((xs,))
            return 'ints'

        @log_tool_execution
        def ping() -> str:
            self.calls.append(('ping',))
            return 'pong'

        self.tools.update({'many_ints': many_ints, 'ping': ping})

    def test_a_whole_number_past_the_int_digit_limit_is_refused(self):
        # int() refuses more than 4300 digits (ValueError); uncaught, the
        # turn died.  Refused instead.
        self.calls.clear()
        content = self.run_tool('bigint', '{"n": %s}' % ('9' * 5000))
        self.assertEqual(self.calls, [], content)
        self.assertTrue(tool_reply_failed(content), content)

    def test_an_unquoted_phrase_ending_in_infinity_is_not_written(self):
        self.calls.clear()
        content = self.run_tool('search', '{"q": to Infinity, "id": 1e999e}')
        self.assertEqual(self.calls, [], content)

    def test_exact_big_ints_in_a_list_of_ints_arrive_as_ints(self):
        big = '9' * 400
        self.calls.clear()
        content = self.run_tool('many_ints', '{"xs": [1, %s]}' % big)
        self.assertEqual(self.calls, [([1, int(big)],)], content)

    def test_empty_arguments_run_a_zero_parameter_tool_and_the_history_agrees(self):
        from flask import Flask
        self.calls.clear()
        a = ConversableAgent('a', llm_config=False, human_input_mode='NEVER')
        ex = ConversableAgent('ex', llm_config=False, human_input_mode='NEVER')
        ex.register_function({'ping': ex._wrap_function(self.tools['ping'])})
        a.send({'role': 'assistant', 'content': None,
                'tool_calls': [{'id': 'c1', 'type': 'function',
                                'function': {'name': 'ping', 'arguments': ''}}]},
               ex, request_reply=False, silent=True)
        _, reply = ex.generate_tool_calls_reply(sender=a)
        self.assertEqual(self.calls, [('ping',)], reply)
        with Flask(__name__).app_context():
            out = ToolMessageHandler().validate_messages(
                copy.deepcopy(a._oai_messages[ex]))
        sent = [tc['function']['arguments'] for m in out
                for tc in (m.get('tool_calls') or [])][0]
        self.assertEqual(sent, '{}')

    def test_the_non_object_refusal_reads_the_same_in_reply_and_history(self):
        from flask import Flask
        from hartos.helper import REFUSED_BECAUSE_KEY
        import json as _json
        a = ConversableAgent('a', llm_config=False, human_input_mode='NEVER')
        ex = ConversableAgent('ex', llm_config=False, human_input_mode='NEVER')
        ex.register_function({'ping': ex._wrap_function(self.tools['ping'])})
        a.send({'role': 'assistant', 'content': None,
                'tool_calls': [{'id': 'c1', 'type': 'function',
                                'function': {'name': 'ping', 'arguments': '[1, 2]'}}]},
               ex, request_reply=False, silent=True)
        _, reply = ex.generate_tool_calls_reply(sender=a)
        content = reply['tool_responses'][0]['content']
        with Flask(__name__).app_context():
            out = ToolMessageHandler().validate_messages(
                copy.deepcopy(a._oai_messages[ex]))
        sent = [tc['function']['arguments'] for m in out
                for tc in (m.get('tool_calls') or [])][0]
        because = _json.loads(sent)[REFUSED_BECAUSE_KEY]
        tail = because.split('not run: ', 1)[1]
        self.assertIn(tail, content)
