"""Guards for the reuse Assistant text-tool-call remedy.

Live root cause 2026-09-03 22:38:00 (replayed deterministically against
llama-server): service-tool schemas are registered on the Helper only
(register_dual(helper, assistant, ...)), so when the reuse Assistant
decides to call google_search itself it emits Qwen <tool_call> XML as
plain TEXT, which autogen never executes; the turn loops on that text and
Nunba falls back to a knowledge-cutoff answer.  The fix mirrors the
Helper's schema for THAT tool onto the Assistant (execution on the
Executor) via core.agent_tools and routes from the reuse state_transition.

Guards:
  * text_tool_call_name parses the exact live XML (and the JSON form),
    and is None for prose / empty / non-string content;
  * mirror_tool_schema copies the schema (not shares it), registers
    execution on the executor, is idempotent, and reports False when the
    source has no schema or nobody holds the callable;
  * an AST guard that hartos/reuse_recipe.py actually calls both.
"""
import ast
import os
import unittest

from core.agent_tools import mirror_tool_schema, text_tool_call_name

LIVE_XML = ('<tool_call>\n<function=google_search>\n<parameter=text>\n'
            'AI research breakthroughs 2024\n</parameter>\n</function>\n</tool_call>')
SIG = {'type': 'function', 'function': {
    'name': 'google_search', 'description': 'search the web',
    'parameters': {'type': 'object', 'properties': {'text': {'type': 'string'}},
                   'required': ['text']}}}


def _func(**kwargs):
    return 'ok'


class _Agent:
    """Minimal stand-in for autogen.ConversableAgent's tool surface."""

    def __init__(self, name, tools=None, fmap=None):
        self.name = name
        self.llm_config = {'config_list': [{'model': 'local'}]}
        if tools:
            self.llm_config['tools'] = list(tools)
        self._function_map = dict(fmap or {})
        self.executions = []

    def update_tool_signature(self, tool_sig, is_remove=None):
        self.llm_config.setdefault('tools', []).append(tool_sig)

    def register_for_execution(self, name=None, description=None):
        def deco(f):
            self._function_map[name or f.__name__] = f
            self.executions.append(name or f.__name__)
            return f
        return deco


class TextToolCallName(unittest.TestCase):
    def test_live_qwen_xml_form(self):
        self.assertEqual(text_tool_call_name(LIVE_XML), 'google_search')

    def test_json_form(self):
        c = '<tool_call>{"name": "crawl4ai_crawl", "arguments": {"url": "https://x"}}</tool_call>'
        self.assertEqual(text_tool_call_name(c), 'crawl4ai_crawl')

    def test_prose_is_none(self):
        self.assertIsNone(text_tool_call_name('Here are the top 3 AI research advancements of 2024.'))

    def test_empty_and_non_string_are_none(self):
        self.assertIsNone(text_tool_call_name(''))
        self.assertIsNone(text_tool_call_name(None))
        self.assertIsNone(text_tool_call_name([{'type': 'text', 'text': 'hi'}]))


class MirrorToolSchema(unittest.TestCase):
    def test_mirrors_schema_and_execution_once(self):
        helper = _Agent('Helper', tools=[SIG])
        assistant = _Agent('Assistant', fmap={'google_search': _func})
        executor = _Agent('Executor')
        self.assertTrue(mirror_tool_schema(assistant, executor, helper, 'google_search'))
        self.assertEqual([t['function']['name'] for t in assistant.llm_config['tools']],
                         ['google_search'])
        self.assertIs(executor._function_map['google_search'], _func)
        # idempotent across turns
        self.assertTrue(mirror_tool_schema(assistant, executor, helper, 'google_search'))
        self.assertEqual(len(assistant.llm_config['tools']), 1)
        self.assertEqual(executor.executions, ['google_search'])

    def test_schema_is_copied_not_shared(self):
        helper = _Agent('Helper', tools=[SIG])
        assistant = _Agent('Assistant', fmap={'google_search': _func})
        mirror_tool_schema(assistant, _Agent('Executor'), helper, 'google_search')
        self.assertIsNot(assistant.llm_config['tools'][0], helper.llm_config['tools'][0])
        self.assertEqual(assistant.llm_config['tools'][0], helper.llm_config['tools'][0])

    def test_false_when_source_has_no_schema(self):
        helper = _Agent('Helper')
        assistant = _Agent('Assistant', fmap={'x': _func})
        self.assertFalse(mirror_tool_schema(assistant, _Agent('Executor'), helper, 'x'))
        self.assertNotIn('tools', assistant.llm_config)

    def test_false_when_nobody_holds_the_callable(self):
        helper = _Agent('Helper', tools=[SIG])
        self.assertFalse(mirror_tool_schema(_Agent('Assistant'), _Agent('Executor'),
                                            helper, 'google_search'))


class WiredIntoReuseStateTransition(unittest.TestCase):
    def test_reuse_recipe_calls_both_helpers(self):
        src_path = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos', 'reuse_recipe.py')
        with open(src_path, encoding='utf-8') as fh:
            tree = ast.parse(fh.read())
        called = {n.func.id for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertIn('text_tool_call_name', called)
        self.assertIn('mirror_tool_schema', called)


if __name__ == '__main__':
    unittest.main()
